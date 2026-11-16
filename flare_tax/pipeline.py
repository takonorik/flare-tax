"""
取得済みデータ → クリプタクト行 の純粋パイプライン (ネットワークI/Oなし)。
"""

from typing import Optional

from .classify import (
    aggregate_token_transfers,
    classify_flr_transaction,
    classify_token_transfer,
)
from .helpers import WFLR_ADDRESS, ZERO_ADDRESS, filter_by_date
from .protocols import normalize_method
from .postprocess import (
    link_bridge_round_trips,
    merge_internal_transactions,
    post_process_defi_groups,
    post_process_lp_differential,
    post_process_wflr_mints,
)


def classify_wallet(
    flr_txs: list,
    token_transfers: list,
    internal_txs: list,
    wallet: str,
    fxrp_mint_txs: Optional[set] = None,
) -> list:
    """1つのアドレスの生データを、差分計上の手前 (ステップ1〜5) まで分類する。"""
    rows = []
    seen_hashes = set()
    # エクスプローラーが記号で返したメソッド名を、分かる範囲で名前に直しておく (判定の揺れを防ぐ)
    for tx in flr_txs:
        normalize_method(tx)
    flr_txs_by_hash = {tx["hash"]: tx for tx in flr_txs if "hash" in tx}

    # 1. Native FLR transactions
    for tx in flr_txs:
        row = classify_flr_transaction(tx, wallet)
        if row is None:
            continue
        if row["tx_hash"] not in seen_hashes:
            rows.append(row)
            seen_hashes.add(row["tx_hash"])

    # 2. Token transfers (dedup + same-token aggregation, then classify)
    for transfer in aggregate_token_transfers(token_transfers, wallet):
        tx = transfer.get("tx_hash") or transfer.get("transaction_hash", "")
        if fxrp_mint_txs and tx in fxrp_mint_txs:
            # Smart Account のミント: FXRP はコントローラー経由で届くので 0x0 からではない
            transfer["_fxrp_mint"] = True
        rows.append(classify_token_transfer(transfer, wallet))

    # 3. Post-process: rFLR Withdraw / Delegate rewards
    rows = post_process_wflr_mints(rows, flr_txs_by_hash)

    # 4. Post-process: DeFi transaction groups (registry + generic patterns)
    rows = post_process_defi_groups(rows, flr_txs_by_hash)

    # 5. Internal transactions (FLR staking rewards / refunds)
    #    ※LP差分計上より先に実行する: addLiquidityFLRの余りFLR返金を
    #      LEND元本から差し引いてから集計するため
    rows = merge_internal_transactions(rows, internal_txs, wallet, flr_txs_by_hash)

    for r in rows:
        r["wallet"] = wallet.lower()
    return rows


def build_result(
    flr_txs: list,
    token_transfers: list,
    internal_txs: list,
    wallet: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    warnings: Optional[list] = None,
    extra_wallets: Optional[list] = None,
    oft_events: Optional[dict] = None,
    notices: Optional[list] = None,
) -> dict:
    """
    Classify raw Blockscout data into Cryptact rows and stats.

    extra_wallets: 同じ所有者としてまとめて処理する追加アドレス (Smart Account など) の生データ。
      [{"wallet", "flr_txs", "token_transfers", "internal_txs", "fxrp_mint_txs"}, ...]
    oft_events: ブリッジ tx の OFT イベント (blockscout.fetch_oft_events)。往復の対応付けに使う。
    アドレスごとに分類してから合算し、自分のアドレス同士のやり取りはスキップにする。
    差分計上 (LP・レンディング・vault) は全アドレスを通して行う。
    """
    datasets = [{"wallet": wallet, "flr_txs": flr_txs, "token_transfers": token_transfers,
                 "internal_txs": internal_txs, "fxrp_mint_txs": set()}] + list(extra_wallets or [])
    owned = {d["wallet"].lower() for d in datasets}

    rows = []
    all_transfers = []
    for i, d in enumerate(datasets):
        w_rows = classify_wallet(d["flr_txs"], d["token_transfers"], d["internal_txs"],
                                 d["wallet"], d.get("fxrp_mint_txs"))
        if i > 0:
            short = f"{d['wallet'][:6]}…{d['wallet'][-4:]}"
            for r in w_rows:
                r["_owner"] = short
        rows += w_rows
        all_transfers += d["token_transfers"]

    # 自分のアドレス同士のやり取り → 送金 (スキップ)
    if len(owned) > 1:
        for r in rows:
            if r.get("counterparty") in owned:
                r["status"] = "skip"
                r["comment"] = f"{r['comment']}（自分のアドレス間）"

    # ブリッジで送った分が、そのまま (別トークンになって) 戻ってきた往復を対応付ける。
    # 月をまたぐこともあるので、日付で絞る前に全履歴で行う
    oft = dict(oft_events or {})
    for d in datasets[1:]:
        oft.update(d.get("oft_events") or {})
    rows = link_bridge_round_trips(rows, oft)

    # 6. LP差分計上 (元本超過分をBONUS化 / 完全撤退時の差損をLOSS化)
    #    lp_positions: プール別台帳。日付フィルタ前に全履歴から構築するので
    #    常に「現時点のポジション状態」を表す
    lp_positions: list = []
    rows = post_process_lp_differential(rows, lp_positions)

    # 7. Date filter
    rows = filter_by_date(rows, start_date, end_date)

    # 8. Sort by timestamp descending
    rows.sort(key=lambda r: r["timestamp"], reverse=True)

    # 9. Final cleanup of internal bookkeeping keys
    rows = [r for r in rows if not r.get("_delete", False)]
    for r in rows:
        r.pop("_delete", None)
        for k in ("_lp", "_pool", "_async", "_locked", "_owner", "_v3_closed"):
            r.pop(k, None)
        # counterparty(相手先)はアドレス帳での判断が必要な「素の送受金」だけに残す。
        # Swap/LP/報酬等に再分類された行の相手はコントラクトなので登録対象外
        if not r.get("comment", "").startswith("[Transfer]"):
            r.pop("counterparty", None)
        if len(owned) == 1:
            r.pop("wallet", None)

    tokens = resolve_token_addresses(rows, all_transfers)
    for r in rows:
        r.pop("_tok", None)
        r.pop("_ctok", None)

    export_count = sum(1 for r in rows if r["status"] == "export")
    skip_count = sum(1 for r in rows if r["status"] == "skip")
    review_count = sum(1 for r in rows if r["status"] == "review")

    return {
        "total": len(rows),
        "export_count": export_count,
        "skip_count": skip_count,
        "review_count": review_count,
        "warnings": warnings or [],
        "notices": notices or [],
        "lp_positions": lp_positions,
        "tokens": tokens,
        "rows": rows,
    }


def native_balance_from_history(flr_txs: list, token_transfers: list, internal_txs: list, wallet: str) -> int:
    """
    取得した履歴だけから計算したネイティブ FLR の残高 (wei)。実際の残高と比べて、取得漏れに気づくために使う。
    Unwrap (WFLR の焼却) で届く FLR は内部トランザクションで来るが、エクスプローラー側で欠けていることがある。
    その分は焼却量から補う (届いたことが確実なので)。
    """
    w = wallet.lower()
    bal = 0
    for t in flr_txs:
        frm = ((t.get("from") or {}).get("hash") or "").lower()
        to = ((t.get("to") or {}).get("hash") or "").lower()
        if frm == w:
            bal -= int(t.get("gas_used") or 0) * int(t.get("gas_price") or 0)
        if t.get("status") != "ok":
            continue
        value = int(t.get("value") or 0)
        if frm == w:
            bal -= value
        if to == w:
            bal += value
    internal_in = set()
    for t in internal_txs:
        if t.get("isError") != "0" or t.get("callType") == "delegatecall":
            continue
        value = int(t.get("value") or 0)
        if (t.get("from") or "").lower() == w:
            bal -= value
        if (t.get("to") or "").lower() == w:
            bal += value
            internal_in.add(t.get("hash") or t.get("transactionHash"))
    seen = set()
    for t in token_transfers:
        tx = t.get("transaction_hash") or t.get("tx_hash")
        key = (tx, t.get("log_index"))
        if key in seen:
            continue
        seen.add(key)
        if (((t.get("token") or {}).get("address_hash") or "").lower() == WFLR_ADDRESS
                and ((t.get("from") or {}).get("hash") or "").lower() == w
                and ((t.get("to") or {}).get("hash") or "").lower() == ZERO_ADDRESS
                and tx not in internal_in):
            bal += int((t.get("total") or {}).get("value") or 0)
    return bal


NATIVE = "native"


def resolve_token_addresses(rows: list, token_transfers: list) -> dict:
    """
    各行の base / counter にトークンのコントラクトアドレス (base_token / counter_token) を付け、
    画面のトークン設定用に {アドレス: {symbol, name}} を返す。

    同じシンボルを名乗る偽トークンと区別するため、まず同じtx内の転送から
    アドレスを引き、なければそのシンボルで最も多く使われたアドレスにする。
    FLR (ネイティブ) は "native"。
    """
    by_tx: dict = {}
    counts: dict = {}
    tokens: dict = {NATIVE: {"symbol": "FLR", "name": "Flare (native)"}}
    for t in token_transfers:
        info = t.get("token") or {}
        sym = info.get("symbol")
        addr = (info.get("address_hash") or info.get("address") or "").lower()
        if not sym or not addr:
            continue
        tx = t.get("tx_hash") or t.get("transaction_hash", "")
        by_tx.setdefault((tx, sym), addr)
        counts.setdefault(sym, {}).setdefault(addr, 0)
        counts[sym][addr] += 1
        tokens.setdefault(addr, {"symbol": sym, "name": info.get("name") or ""})
    most = {s: max(c, key=c.get) for s, c in counts.items()}

    def lookup(sym: str, tx: str) -> str:
        if not sym or sym == "JPY":
            return ""
        if sym == "FLR":
            # 「FLR」を名乗る偽トークン (アドレスポイズニング) は、同じtxのトークン転送に出てくる。
            # 本物のFLR (ネイティブ) はトークン転送には現れない
            return by_tx.get((tx, sym)) or NATIVE
        return by_tx.get((tx, sym)) or most.get(sym, "")

    def resolve(r, sym_key: str, tok_key: str) -> str:
        # 分類の時点で分かっているアドレスを優先する (同じ tx に同じシンボルの別トークンがあっても取り違えない)。
        # 後から別のトークンに書き換えた行はシンボルが合わなくなるので、その場合はシンボルから引く
        sym = r.get(sym_key)
        known = r.get(tok_key)
        if known and known[0] == sym and known[1]:
            return known[1]
        return lookup(sym, r.get("tx_hash", ""))

    for r in rows:
        r["base_token"] = resolve(r, "base", "_tok")
        r["counter_token"] = resolve(r, "counter", "_ctok")
    return tokens
