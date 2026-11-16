"""
tx_hash単位のグループ後処理。

流れ (post_process_defi_groups):
  1. tx_hashで行をグループ化
  2. プロトコルレジストリ (protocols.PROTOCOL_HANDLERS) を上から評価
  3. どれにもマッチしなければ汎用パターン (報酬 / スワップ / LST / LP) で推定
  4. 最後にガス代を代表行へ集約
"""

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from .helpers import JST, ZERO_ADDRESS, format_decimal, ratio_str, wei_to_flr
from .protocols import (
    DAOFLARE_CLAIM, KII_CONTRACT, KNOWN_SELECTORS, LST_MAP, PROTOCOL_HANDLERS, REBRANDS, ROUTER_TO_DEX, SAME_TOKEN,
)


def post_process_wflr_mints(rows: list, flr_txs_by_hash: dict) -> list:
    """
    Handle WFLR mints (WFLRラップ / from ZERO_ADDRESS):
    1. If paired with rFLR burn -> FTSO報酬 Withdraw (BONUS)
    2. If FLR transaction value == 0 -> Delegate報酬 Claim (BONUS)
    3. Else (FLR value > 0) -> WFLR Lap (SENDFEE / skip)
    """
    # Find tx_hashes that contain rFLR burns
    rflr_burn_txs = set()
    for row in rows:
        if row["base"] == "rFLR" and "rFLR Withdraw" in row.get("comment", ""):
            rflr_burn_txs.add(row["tx_hash"])
            row["_delete"] = True

    # 自分が何かを送っている tx (Swap など) の中で WFLR が発行されて届いた場合は、報酬ではなく取引の受け取り側
    sent_in_tx = {r["tx_hash"] for r in rows
                  if r["action"] == "SELL" and r["base"] not in ("FLR", "WFLR", "rFLR") and float(r.get("volume") or 0) > 0}

    wrap_txs = set()
    for row in rows:
        # We only care about WFLR that was originally classified as a receive/wrap
        if row["base"] == "WFLR" and row["action"] in ("BUY", "SENDFEE"):
            tx_hash = row["tx_hash"]

            # Rule 1: Paired with rFLR burn -> FTSO reward
            if tx_hash in rflr_burn_txs:
                row["action"] = "BONUS"
                row["status"] = "export"
                row["comment"] = "[rFLR] Convert to WFLR"
                continue

            # Rule 2: If it's a mint (WFLRラップ) but user sent 0 FLR -> Delegate reward
            if "[Wrap] Wrap FLR" in row.get("comment", ""):
                flr_tx = flr_txs_by_hash.get(tx_hash, {})
                sent_value = int(flr_tx.get("value", "0"))
                if sent_value == 0 and tx_hash in sent_in_tx:
                    row["action"] = "BUY"
                    row["status"] = "review"
                    row["comment"] = "[Transfer] Receive WFLR"
                elif sent_value == 0:
                    row["action"] = "BONUS"
                    row["status"] = "export"
                    row["comment"] = "[Delegate] Claim Rewards"
                else:
                    # It's an actual wrap (FLR sent > 0). Track to silence the native FLR send.
                    wrap_txs.add(tx_hash)

    # Merge the native FLR send row into the WFLR row for genuine Wrap transactions
    for tx_hash in wrap_txs:
        flr_row = next((r for r in rows if r["tx_hash"] == tx_hash and r["base"] == "FLR" and r["action"] == "SELL"), None)
        wflr_row = next((r for r in rows if r["tx_hash"] == tx_hash and r["base"] == "WFLR" and r.get("comment", "") == "[Wrap] Wrap FLR"), None)

        if flr_row and wflr_row:
            wflr_row["fee"] = flr_row["fee"]
            flr_row["_delete"] = True

    return rows


# ---------------------------------------------------------------------------
# 汎用パターン (プロトコルレジストリにマッチしなかったグループの推定)
# ---------------------------------------------------------------------------

def _is_lp_token(symbol: str) -> bool:
    return "_LP" in symbol or symbol.endswith("-LP") or symbol.startswith("IV-")


def _handle_reward_pattern(group_rows, sends, receives, ctx) -> None:
    """受取のみのグループ → 報酬とみなす。"""
    method = ctx["method"]
    dex_name = ctx["dex_name"]
    m_name = method.split("(")[0]
    has_lp = any(_is_lp_token(r["base"]) for r in receives)
    for r in receives:
        if (r["comment"].startswith("[Transfer]") and not _is_lp_token(r["base"]) and r["base"] != "xSPRK"
                and (m_name in SALE_METHODS or (m_name in PRINCIPAL_RETURN_METHODS and not has_lp))):
            # 報酬とは限らない受け取り → 要確認 (既定の区分は BUY のまま。報酬なら画面で BONUS に変える)
            r["status"] = "review"
            if m_name in SALE_METHODS:
                r["comment"] = f"[NFT] {m_name} {r['base']}（NFTなどの売却代金。取得費との差が損益）"
            else:
                r["comment"] = f"[Farm] {m_name} {r['base']}（預けていた分の戻りなら出力不要。報酬なら BONUS）"
            continue
        if _is_lp_token(r["base"]):
            # LPトークンが戻ってきただけ (ファームからのUnstake等) — 収入ではない
            r["action"] = "SENDFEE"
            r["status"] = "skip"
            r["comment"] = f"[Farm] Receive LP Token {r['base']}"
        elif r["base"] == "xSPRK":
            r["status"] = "skip"
            r["comment"] = "[SPRK Staking] xSPRK Return"
        else:
            if r["status"] == "skip" and r["base"] in ("WFLR", "rFLR"):
                pass
            else:
                r["action"] = "BONUS"
                r["status"] = "export"

                if dex_name and ("claim" in method or "multicall" in method):
                    r["comment"] = f"[{dex_name}] Claim Delegate Rewards"
                elif "harvestAllDividends" in method and r["base"] == "WFLR":
                    r["comment"] = "[SPRK Staking] Claim WFLR"
                elif r["base"] == "stFLR":
                    r["comment"] = "[SPRK Staking] Claim stFLR"
                elif r["base"] == "SPRK":
                    r["comment"] = "[SPRK Staking] Claim SPRK"
                elif r["base"] == "KII":
                    r["comment"] = "[Kii] Staking Rewards（JOULEとして出力）"
                elif (r["comment"].startswith("[Transfer]") and ctx.get("tx_info")
                        and any(m in method for m in CLAIM_METHODS)):
                    _label_claim([r], method, ctx["tx_info"])


CLAIM_METHODS = ("claim", "Claim", "withdrawFees", "harvest", "getReward")
# 預けていたトークンがそのまま戻る呼び出し (単体ステーキングの解除など)。受け取りを報酬と決めつけない
PRINCIPAL_RETURN_METHODS = {"withdraw", "withdrawAll", "unstake", "exit", "leave", "emergencyWithdraw"}
# NFT マーケットなどで売った代金の受け取り
SALE_METHODS = {"acceptOffer", "acceptBid"}
# コントラクト名 → 表示名 (サービス名が分かるもの)
CLAIM_NAMES = {"Unitroller": "Kinetic", "CTokenRewards": "Kinetic", "Distributor": "Merkl"}


def _label_claim(receives, method, tx_info) -> None:
    """自分の claim 系 tx での受け取り (「[Transfer]」のままの行だけ)。コントラクト名で表示する。"""
    to = tx_info.get("to") or {}
    impl = ((to.get("implementations") or [{}])[0] or {}).get("name") or ""
    name = to.get("name") or ""
    if not name or name.endswith("Proxy"):
        name = impl or name or "Contract"
    name = CLAIM_NAMES.get(name, name)
    m = method.split("(")[0]
    for r in receives:
        if r["status"] == "skip" or not r["comment"].startswith("[Transfer]"):
            continue
        r["action"] = "BONUS"
        r["status"] = "export"
        if name == "CollateralPool":
            # FAssets の担保プール: 参加者に分配されるミント手数料 (FXRP) の受け取り
            r["comment"] = f"[FAssets Pool] Withdraw Fees {r['base']}（担保プールの手数料分配）"
        else:
            r["comment"] = f"[{name}] {m} {r['base']}"


def _handle_solitary_send(group_rows, sends, receives, ctx) -> None:
    """送付のみ1件のグループ。"""
    s_row = sends[0]
    if _is_lp_token(s_row["base"]):
        # LPトークンをファーム等へ預けただけ — 譲渡ではない
        s_row["action"] = "SENDFEE"
        s_row["status"] = "skip"
        s_row["comment"] = f"[Farm] Send LP Token {s_row['base']}"
    elif s_row["base"] == "xSPRK":
        s_row["action"] = "DEFIFEE"
        s_row["status"] = "skip"
        s_row["comment"] = "[SPRK Staking] Staking"


def _set_swap(s_row, r_row) -> None:
    """送った行を Swap (SELL 送った側 / Counter 受け取った側) にする。受取量は丸めずに持つ。"""
    s_row["action"] = "SELL"
    s_row["status"] = "export"
    s_row["counter"] = r_row["base"]
    s_row["counter_volume"] = r_row["volume"]
    s_row["price"] = ratio_str(Decimal(r_row["volume"]), Decimal(s_row["volume"]))
    if r_row.get("_tok"):
        s_row["_ctok"] = r_row["_tok"]


def _handle_one_to_one(group_rows, sends, receives, ctx) -> None:
    """送付1・受取1のグループ → Swap / LSTステーキング / SPRKステーキング。"""
    s_row = sends[0]
    r_row = receives[0]
    dex_name = ctx["dex_name"]
    new_rows = ctx["new_rows"]

    # DaoFlare の毎月の claim: DFLR (取得の記録なし) を焼却して HLN を受け取る → HLN の報酬
    if (s_row["base"], r_row["base"]) == DAOFLARE_CLAIM:
        s_row["action"] = "SENDFEE"
        s_row["status"] = "skip"
        s_row["comment"] = "[DaoFlare] DFLR を焼却（報酬の受け取りに使用）"
        r_row["action"] = "BONUS"
        r_row["status"] = "export"
        r_row["comment"] = "[DaoFlare] Claim HLN（毎月の報酬）"
        return

    # 同一トークンとして扱う変換 (JOULE→KII) → スキップ
    same = SAME_TOKEN.get((s_row["base"], r_row["base"]))
    if same:
        s_row["action"] = "SENDFEE"
        s_row["status"] = "skip"
        s_row["comment"] = same
        r_row["_delete"] = True
        return

    # 1:1 のトークン変換 → REBRAND (取得原価をそのまま引き継ぐ。損益なし)
    rebrand = REBRANDS.get((s_row["base"], r_row["base"]))
    if rebrand:
        try:
            s_vol, r_vol = Decimal(s_row["volume"]), Decimal(r_row["volume"])
        except Exception:
            s_vol = r_vol = Decimal(0)
        if s_vol > 0 and r_vol > 0:
            s_row["action"] = "REBRAND"
            s_row["status"] = "export"
            s_row["counter"] = r_row["base"]
            s_row["price"] = format_decimal(r_vol / s_vol)
            s_row["comment"] = f"{rebrand}（REBRAND）"
            r_row["_delete"] = True
            return

    is_s_lending = s_row["base"] == "xSPRK"
    is_r_lending = r_row["base"] == "xSPRK"

    # Detect LST pairs (sFLR, stFLR, stXRP, flrETH)
    # DEX ルーター経由 (dex_name あり) は LST の組み合わせでも市場での Swap なので対象外
    lst_tag = None
    lst_token = None
    is_stake = False
    is_unstake = False
    if dex_name is None:
        for lst, (underlying, tag) in LST_MAP.items():
            if s_row["base"] in underlying and r_row["base"] == lst:
                lst_tag, lst_token, is_stake = tag, lst, True
                dex_name = None  # Neutralize DEX block logic so LST logic handles it
                break
            elif s_row["base"] == lst and r_row["base"] in underlying:
                lst_tag, lst_token, is_unstake = tag, lst, True
                dex_name = None
                break

    if lst_tag:
        # LST Staking / Unstaking (export as Swap)
        try:
            s_vol = Decimal(s_row["volume"])
            r_vol = Decimal(r_row["volume"])
            if s_vol > 0:
                _set_swap(s_row, r_row)

                if is_stake:
                    action_word = "Mint" if lst_token == "stFLR" else "Stake"
                    s_row["comment"] = f"{lst_tag} {action_word} {lst_token}"
                else:
                    action_word = "Burn" if lst_token == "stFLR" else "Unstake"
                    s_row["comment"] = f"{lst_tag} {action_word} {lst_token}"
                # 即時アンステークの手数料は、受け取った量 (交換比率) に含まれている。別の LOSS 行は出さない
                # (出すと LST を二重に減らしてしまう)
                r_row["_delete"] = True
        except Exception:
            pass

    elif is_s_lending or is_r_lending:
        # SPRK Staking
        if s_row["base"] == "SPRK":
            s_row["action"] = "SENDFEE"
            s_row["status"] = "skip"
            s_row["comment"] = "[SPRK Staking] Convert to xSPRK"
            r_row["_delete"] = True
        else:
            s_row["action"] = "SENDFEE"
            s_row["status"] = "skip"
            s_row["comment"] = "[SPRK Staking] Convert to SPRK"
            r_row["_delete"] = True

            try:
                s_vol = Decimal(s_row["volume"])
                r_vol = Decimal(r_row["volume"])
                penalty = s_vol - r_vol
                if penalty > Decimal("0.000001"):
                    penalty_pct = float(penalty / s_vol) * 100
                    loss_row = dict(s_row)
                    loss_row["action"] = "LOSS"
                    loss_row["base"] = "SPRK"
                    loss_row["volume"] = format_decimal(penalty)
                    loss_row["status"] = "export"
                    loss_row["fee"] = "0"
                    loss_row["comment"] = f"[SPRK Staking] Instant Fee({penalty_pct:.0f}%)"
                    new_rows.append(loss_row)
            except Exception:
                pass
    else:
        # Swap
        if s_row["base"] in ("rFLR", "WFLR") and r_row["base"] in ("WFLR", "rFLR"):
            pass
        else:
            try:
                s_vol = Decimal(s_row["volume"])
                r_vol = Decimal(r_row["volume"])
                if s_vol > 0:
                    _set_swap(s_row, r_row)
                    dex_prefix = f"[{dex_name}]" if dex_name else "[DEX]"
                    s_row["comment"] = f"{dex_prefix} Swap {s_row['base']} to {r_row['base']}"
                    r_row["_delete"] = True
            except Exception:
                pass


def _handle_lp_complex(group_rows, sends, receives, ctx) -> None:
    """送付または受取が複数で、どのプロトコルにも当てはまらなかったグループ → 要確認。"""
    dex_prefix = f"[{ctx['dex_name']}]" if ctx["dex_name"] else "[DEX]"

    if len(sends) >= 2 and len(receives) <= 1:
        tokens = " / ".join(r["base"] for r in sends)
        tag = f"{dex_prefix} Provide LP {tokens}"
    elif len(receives) >= 2 and len(sends) <= 1:
        tokens = " / ".join(r["base"] for r in receives)
        tag = f"{dex_prefix} Withdraw LP {tokens}"
    else:
        tokens_s = " / ".join(r["base"] for r in sends)
        tokens_r = " / ".join(r["base"] for r in receives)
        tag = f"{dex_prefix} LP {tokens_s} / {tokens_r}"

    for r in group_rows:
        if r["action"] == "DEFIFEE" and float(r.get("volume", "0")) == 0:
            continue
        if r["comment"].startswith(("[Transfer]", "[Bridge/Mint]")) and r["status"] == "review":
            # どの操作か判定できなかった行。黙って外さず、要確認に残す
            r["comment"] = f"{r['comment']}（複数トークンが動いた取引・自動判定できません）"
            continue
        r["status"] = "skip"
        if not r["comment"].startswith("["):
            r["comment"] = tag


def _consolidate_gas_fee(group_rows, fallback=()) -> None:
    """
    数量0でガス代だけの行を、同グループの代表行にマージする。
    fallback: グループに残る行が無いときの移し先 (ハンドラが新しく作った同じ tx の行)。
    """
    gas_row = None
    target_row = None
    for r in group_rows:
        try:
            if float(r.get("volume", "0")) == 0 and float(r.get("fee", "0")) > 0:
                gas_row = r
                break
        except Exception:
            pass

    if not gas_row:
        return

    # 出力対象 → 要確認 → それ以外 の順。削除される行 (Swap の受け取り側など) に寄せるとガス代が消える
    live = [r for r in group_rows if r is not gas_row and not r.get("_delete")]
    for status in ("export", "review"):
        target_row = next((r for r in live if r["status"] == status), None)
        if target_row:
            break
    if not target_row:
        target_row = live[0] if live else next(iter(fallback), None)

    if target_row:
        target_row["fee"] = gas_row["fee"]
        gas_row["fee"] = "0"
        gas_row["_delete"] = True


def _plain_moves(group_rows, action):
    """まだ何の操作とも判定されていない、素の送付 (SELL) / 受取 (BUY) の行をトークン別に集める。"""
    out = defaultdict(list)
    for r in group_rows:
        tok = (r.get("_tok") or ("", ""))[1]
        if (tok and r["action"] == action and not r.get("_locked") and not r.get("_delete")
                and r["comment"].startswith(("[Transfer]", "[Bridge/Mint]")) and float(r.get("volume") or 0) > 0):
            out[tok].append(r)
    return out


def _simplify_plain_moves(group_rows) -> None:
    """
    どのプロトコルにも当てはまらなかった tx の下ごしらえ:
      1. 同じトークンが同じ量だけ入って、そのまま出ていった分は手元に残らない通過なので判定から外す
         (報酬の引換券トークンが届いて焼却される / 発行された LP トークンがそのままファームへ行く、など)
      2. 同じトークンを複数の宛先に送った分は1行にまとめる (送付時に一部が焼却されるトークンなど)
    """
    ins, outs = _plain_moves(group_rows, "BUY"), _plain_moves(group_rows, "SELL")
    for tok in set(ins) & set(outs):
        if sum(Decimal(r["volume"]) for r in ins[tok]) != sum(Decimal(r["volume"]) for r in outs[tok]):
            continue
        for r in ins[tok] + outs[tok]:
            r["action"] = "SENDFEE"
            r["status"] = "skip"
            r["comment"] = f"[Pass-through] {r['base']}（同じ取引の中で受け取り、そのまま出ていった分）"
            r["_locked"] = True
    for tok, sends in _plain_moves(group_rows, "SELL").items():
        if len(sends) < 2:
            continue
        # 0x0 宛て (焼却) ではない行を残す
        keep = next((r for r in sends if r.get("counterparty") != ZERO_ADDRESS), sends[0])
        keep["volume"] = format_decimal(sum(Decimal(r["volume"]) for r in sends))
        for r in sends:
            if r is not keep:
                if r.get("fee", "0") not in ("0", "") and keep.get("fee", "0") in ("0", ""):
                    keep["fee"] = r["fee"]
                r["_delete"] = True


# ---------------------------------------------------------------------------
# グループ後処理の本体
# ---------------------------------------------------------------------------

def post_process_defi_groups(rows: list, flr_txs_by_hash: dict = None) -> list:
    """Group transactions by tx_hash and classify DeFi patterns."""
    # KII の解除申請 (KII をコントラクトに預ける) とその取り消し (戻ってくる) は損益なし
    for r in rows:
        if r.get("base") == "KII" and r.get("counterparty") == KII_CONTRACT:
            r["status"] = "skip"
            r["action"] = "SENDFEE" if r["action"] in ("SELL", "SENDFEE") else r["action"]
            r["comment"] = "[Kii] Redeem request (KIIの解除申請)" if r["action"] == "SENDFEE" else "[Kii] Cancel redeem (解除申請の取り消し)"
            r["_locked"] = True
    groups = defaultdict(list)
    new_rows = []

    for row in rows:
        groups[row["tx_hash"]].append(row)

    for tx_hash, all_group_rows in groups.items():
        # FXRPのミント・償還など、出力しないことが確定している行 (_locked) は判定に含めない
        group_rows = [r for r in all_group_rows if not r.get("_locked")]
        sends = [r for r in group_rows if r["action"] in ("SELL", "SENDFEE") and float(r.get("volume", "0")) > 0]
        receives = [r for r in group_rows if r["action"] in ("BUY", "BONUS") and float(r.get("volume", "0")) > 0]

        tx_info = flr_txs_by_hash.get(tx_hash) if flr_txs_by_hash else None
        tx_info = tx_info or {}
        decoded_input = tx_info.get("decoded_input") or {}
        # 未検証コントラクトはdecoded_inputが無いので、Blockscoutの
        # method名フィールド (4バイトセレクタ由来) にフォールバックする
        method = decoded_input.get("method_call", "") or tx_info.get("method") or ""
        # Blockscout は同じtxでも名前を返したりセレクタのまま返したりする。
        # 名前もセレクタも無ければ入力データの先頭4バイトを使い、既知のセレクタは名前に読み替える
        if not method:
            method = (tx_info.get("raw_input") or "")[:10]
        method = KNOWN_SELECTORS.get(method.lower(), method)

        to_addr = (tx_info.get("to") or {}).get("hash", "").lower()
        to_name = (tx_info.get("to") or {}).get("name", "") or ""
        dex_name = ROUTER_TO_DEX.get(to_addr)

        ctx = {
            "method": method,
            "to_name": to_name,
            "to_addr": to_addr,
            "dex_name": dex_name,
            "new_rows": new_rows,
            "tx_info": tx_info,
        }

        # 1. プロトコルレジストリ (最初にマッチしたものだけ実行)
        #    ※1行グループにも適用する (reserveCollateral等の単独手数料tx)
        matched = False
        for matcher, handler in PROTOCOL_HANDLERS:
            if matcher(group_rows, sends, receives, ctx):
                handler(group_rows, sends, receives, ctx)
                matched = True
                break

        if not matched:
            _simplify_plain_moves(group_rows)
            group_rows = [r for r in group_rows if not r.get("_locked") and not r.get("_delete")]
            sends = [r for r in group_rows if r["action"] in ("SELL", "SENDFEE") and float(r.get("volume", "0")) > 0]
            receives = [r for r in group_rows if r["action"] in ("BUY", "BONUS") and float(r.get("volume", "0")) > 0]

        if len(group_rows) <= 1:
            # DEXルーターの claim/multicall で報酬を1件だけ受け取った (同じトークンの転送は合算済み)。
            # 例: BlazeSwap の multicall で LP の委任報酬 (WFLR) を受け取る
            if (not matched and receives and not sends and ctx["dex_name"]
                    and ("claim" in method or "multicall" in method)):
                _handle_reward_pattern(group_rows, sends, receives, ctx)
            # 自分が実行した claim 系の呼び出しで受け取っただけ → 報酬。相手はコントラクトなので
            # 「[Transfer]」(アドレス帳の登録対象) にしない
            elif (not matched and receives and not sends and tx_info
                    and any(m in method for m in CLAIM_METHODS)):
                _label_claim(receives, method, tx_info)
            if len(all_group_rows) > 1:
                _consolidate_gas_fee(all_group_rows, [n for n in new_rows if n["tx_hash"] == tx_hash])
            continue

        # 2. 汎用パターン
        if not matched:
            if len(sends) == 0 and len(receives) >= 1:
                _handle_reward_pattern(group_rows, sends, receives, ctx)
            elif len(sends) == 1 and len(receives) == 0:
                _handle_solitary_send(group_rows, sends, receives, ctx)
            elif len(sends) == 1 and len(receives) == 1:
                _handle_one_to_one(group_rows, sends, receives, ctx)
            elif len(sends) >= 2 or len(receives) >= 2:
                _handle_lp_complex(group_rows, sends, receives, ctx)

        # 3. Gas Fee Consolidation
        _consolidate_gas_fee(group_rows, [n for n in new_rows if n["tx_hash"] == tx_hash])

    # Add any newly generated rows (e.g. penalty LOSS)
    rows.extend(new_rows)

    # Remove rows marked for deletion
    rows = [r for r in rows if not r.get("_delete", False)]

    # Clean up the _delete key
    for r in rows:
        r.pop("_delete", None)

    return rows


# ---------------------------------------------------------------------------
# LP差分計上 (「入れた時と出した時の差分だけ課税」方針の実装)
#
# プロトコルハンドラが行に付けた "_lp" タグ (pool, role) を時系列で集計する:
#   lend    → プールの元本に加算
#   recover → 元本から控除。元本を超えた分だけ BONUS 行に分離 (=差益)
#   lp_in / lp_out → LPトークン残高を追跡し、残高が0に戻った時 (完全撤退) に
#                    元本が残っていれば LOSS 行を生成 (=差損)
# V3 (NFTポジション) は LPトークンが無いので、ポジションの流動性が0になった tx
# (_v3_closed、取得時にイベントから判定) を完全撤退とみなす。判定できない tx は LOSS を出さない。
# ---------------------------------------------------------------------------

_DUST = Decimal("0.000000001")


def _lp_dust(st) -> Decimal:
    """LPトークン残高を「0に戻った」とみなす閾値。発行量そのものが極小のシェア (ICHI Vault など) では、
    固定の閾値だと一部を返しただけで全部返したことになってしまうので、最大残高に対する割合でも見る。"""
    return min(_DUST, st["lp_peak"] * Decimal("0.000001"))


def _norm_token(base: str) -> str:
    """元本管理上はFLRとWFLRを同一資産として扱う (ラップは交換ではないため)。"""
    return "FLR" if base in ("FLR", "WFLR") else base


def _pool_key(group: list) -> str:
    """
    プールの識別子: DEX名 + 構成トークン(正規化)の組。
    LPトークンのシンボルは使わない — BlazeSwapは全ペアが同じ
    シンボル(BLAZE-LP)を共有していて衝突するため。
    """
    explicit = next((r["_pool"] for r in group if r.get("_pool")), None)
    if explicit:
        key = explicit
    else:
        dex = group[0]["_lp"][0]
        bases = sorted({_norm_token(r["base"]) for r in group if r["_lp"][1] in ("lend", "recover", "borrow", "return")})
        key = f"{dex}:{'-'.join(bases)}"
    # 追加アドレス (Smart Account など) のポジションは、メインとは別のポジションとして扱う
    owner = group[0].get("_owner")
    return f"{key} @{owner}" if owner else key


def post_process_lp_differential(rows: list, positions_out: list = None) -> list:
    """差分計上の状態機械。positions_out にプール別台帳を追記する (省略可)。"""
    tagged = defaultdict(list)
    for r in rows:
        if r.get("_lp") and not r.get("_delete"):
            tagged[r["tx_hash"]].append(r)
    if not tagged:
        return rows

    pools: dict = {}
    new_rows = []

    # tx単位で時系列に処理する
    for tx_hash in sorted(tagged, key=lambda h: min(r["timestamp"] for r in tagged[h])):
        group = tagged[tx_hash]
        pool = _pool_key(group)
        st = pools.setdefault(pool, {
            "principal": defaultdict(Decimal), "lp": Decimal(0), "lp_peak": Decimal(0),
            "borrowed": defaultdict(Decimal),
            "deposited": defaultdict(Decimal), "recovered": defaultdict(Decimal),
            "bonus": defaultdict(Decimal), "loss": defaultdict(Decimal),
            "dex": group[0]["_lp"][0], "first": group[0]["timestamp"], "last": group[0]["timestamp"],
            "events": 0,
        })
        st["last"] = max(st["last"], max(r["timestamp"] for r in group))
        st["first"] = min(st["first"], min(r["timestamp"] for r in group))
        st["events"] += 1
        had_lp_out = False

        # writeoff (Stability Pool の清算で使われた CDP) は、同じ tx の引き出しより先に元本から引く。
        # 先に引かないと、引き出しが元本に収まって利回りが差益にならない
        for r in sorted(group, key=lambda x: x["_lp"][1] != "writeoff"):
            _, role = r["_lp"]
            vol = Decimal(r.get("volume") or "0")

            if role == "writeoff":
                # 行そのものは LOSS として出力済み。台帳の元本だけ減らす
                key = _norm_token(r["base"])
                st["principal"][key] = max(st["principal"][key] - vol, Decimal(0))
                st["loss"][key] += vol
            elif role == "lend":
                st["principal"][_norm_token(r["base"])] += vol
                st["deposited"][_norm_token(r["base"])] += vol
            elif role == "lp_in":
                st["lp"] += vol
                st["lp_peak"] = max(st["lp_peak"], st["lp"])
            elif role == "lp_out":
                st["lp"] -= vol
                had_lp_out = True
            elif role == "recover":
                key = _norm_token(r["base"])
                principal = st["principal"][key]
                part = min(vol, principal)
                excess = vol - part
                st["principal"][key] = principal - part
                st["recovered"][key] += part
                if excess > _DUST:
                    st["bonus"][key] += excess
                    if part > _DUST:
                        # 元本分はRECOVERのまま、超過分だけBONUSに分離
                        r["volume"] = format_decimal(part)
                        bonus = dict(r)
                        bonus.pop("_lp", None)
                        bonus["action"] = "BONUS"
                        bonus["status"] = "export"
                        bonus["volume"] = format_decimal(excess)
                        bonus["fee"] = "0"
                        bonus["comment"] = f"{r['comment']} 差益"
                        new_rows.append(bonus)
                    else:
                        # 元本ゼロ (全額が差益)
                        r["action"] = "BONUS"
                        r["status"] = "export"
                        r["comment"] = f"{r['comment']} 差益"
            elif role == "borrow":
                st["borrowed"][_norm_token(r["base"])] += vol
            elif role == "return":
                key = _norm_token(r["base"])
                borrowed = st["borrowed"][key]
                part = min(vol, borrowed)
                excess = vol - part
                st["borrowed"][key] = borrowed - part
                if excess > _DUST and part > _DUST:
                    # 借入額を超えた返済分 = 利息/手数料 → LOSSに分離
                    r["volume"] = format_decimal(part)
                    interest = dict(r)
                    interest.pop("_lp", None)
                    interest["action"] = "LOSS"
                    interest["status"] = "export"
                    interest["volume"] = format_decimal(excess)
                    interest["fee"] = "0"
                    interest["comment"] = f"{r['comment']} 利息"
                    new_rows.append(interest)
                elif excess > _DUST:
                    # 借入残ゼロ (完済後に残っていた利息だけの返済)
                    r["action"] = "LOSS"
                    r["comment"] = f"{r['comment']} 利息"

        # 完全撤退の判定 (V2: lp_outを含み、LPトークン残高が0に戻った / V3: ポジションの流動性が0になった)。
        # キュー方式のvault (_async) はシェア返却の時点では原資産がまだ届いていないので対象外
        is_async = any(r.get("_async") for r in group)
        v3_closed = any(r.get("_v3_closed") for r in group)
        if ((had_lp_out and st["lp"] <= _lp_dust(st)) or v3_closed) and not is_async:
            dex = group[0]["_lp"][0]
            template = group[0]
            for token, principal in list(st["principal"].items()):
                if principal > _DUST:
                    loss = dict(template)
                    loss.pop("_lp", None)
                    loss["action"] = "LOSS"
                    loss["base"] = token
                    loss["volume"] = format_decimal(principal)
                    loss["price"] = ""
                    loss["counter"] = "JPY"
                    loss["fee"] = "0"
                    loss["status"] = "export"
                    loss["comment"] = f"[{dex}] LP差損 {token}"
                    new_rows.append(loss)
                    st["loss"][token] += principal
                st["principal"][token] = Decimal(0)

    rows.extend(new_rows)

    # プール別台帳 (LPポジションレポート)
    if positions_out is not None:
        for pool, st in pools.items():
            if not (st["deposited"] or st["recovered"] or st["bonus"] or st["loss"]):
                continue  # 借入のみのプールは台帳対象外
            remaining = {t: v for t, v in st["principal"].items() if v > _DUST}
            is_open = bool(remaining) or st["lp"] > _lp_dust(st)
            positions_out.append({
                "pool": pool,
                "dex": st["dex"],
                "tokens": sorted(set(st["deposited"]) | set(st["recovered"])),
                "deposited": {t: format_decimal(v) for t, v in st["deposited"].items() if v > 0},
                "recovered": {t: format_decimal(v) for t, v in st["recovered"].items() if v > 0},
                "remaining_principal": {t: format_decimal(v) for t, v in remaining.items()},
                "lp_balance": format_decimal(st["lp"]) if st["lp"] > _lp_dust(st) else "0",
                "realized_bonus": {t: format_decimal(v) for t, v in st["bonus"].items() if v > _DUST},
                "realized_loss": {t: format_decimal(v) for t, v in st["loss"].items() if v > _DUST},
                "status": "open" if is_open else "closed",
                "events": st["events"],
                "first_tx": st["first"],
                "last_tx": st["last"],
            })
        positions_out.sort(key=lambda p: (p["status"] != "open", p["last_tx"]), reverse=False)

    return rows


# ---------------------------------------------------------------------------
# Internal Transactions (FLRステーキング報酬 / DeFiからのFLR受取 / 返金)
# ---------------------------------------------------------------------------

# 報酬コントラクト (tx の to の名前) → ラベル
_REWARD_LABELS = {
    "ValidatorRewardManager": "[FLR Staking] Claim Rewards",   # P-chain ステーキング報酬
    "FtsoRewardManager": "[Delegate] Claim Rewards",          # FTSO デリゲート報酬 (旧)
    "RewardManager": "[Delegate] Claim Rewards",              # FTSO デリゲート報酬
    "DistributionToDelegators": "[FlareDrop] Claim",
    "TokenDistributor": "[FlareDrop] Claim",
}


# 報酬を払い出すコントラクト (内部txの送り元)。エグゼキューターによる自動クレームなど、
# 他者が実行した tx で届いた FLR でも、ここからなら報酬と分かる
_REWARD_SENDERS = {
    "0x85627d71921ae25769f5370e482ada5e1e418d37": "[Delegate] Claim Rewards",    # FtsoRewardManager
    "0x9edca806834e89cc928ef4951ce0506be8416309": "[Delegate] Claim Rewards",    # FtsoRewardManager (旧)
    "0xc8f55c5aa2c752ee285bd872855c749f4ee6239b": "[Delegate] Claim Rewards",    # RewardManager
    "0xc0cf3aaf93bd978c5bc662564aa73e331f2ec0b5": "[FLR Staking] Claim Rewards", # ValidatorRewardManager
    "0x9c7a4c83842b29bb4a082b0e689cb9474bd938d0": "[FlareDrop] Claim",           # DistributionToDelegators
}


def _internal_flr_row(dt_jst, value, fee, comment, tx_hash, status="export", action="BONUS", block=None):
    return {
        "block": block,
        "timestamp": dt_jst.strftime("%Y/%m/%d %H:%M:%S"),
        "action": action,
        "source": "Flare_Wallet",
        "base": "FLR",
        "volume": wei_to_flr(str(value)),
        "price": "",
        "counter": "JPY",
        "counter_volume": "",
        "fee": fee,
        "fee_ccy": "FLR",
        "comment": comment,
        "tx_hash": tx_hash,
        "status": status,
    }


def merge_internal_transactions(rows: list, internal_txs: list, wallet: str, flr_txs_by_hash: dict = None) -> list:
    """
    internal tx (コントラクトから届くネイティブFLR) を行にマージする。判定の順番:
      1. Unwrap の払い戻し・同じtx内で出ていくだけの通過 → 取り込まない
      2. LP引き出しで返るFLR → RECOVER (差分計上の元本に充当)
      3. トークン → ネイティブFLR の Swap → 送ったトークンの行を Swap にする
      4. プロトコル操作の余り返金 → 送った量から差し引く
      5. 自分でFLRを払ったtx (ゲーム・NFTのミントなど) → 払った額以下なら返金、超えたら当選金としてBONUS
      6. それ以外 → 自分の tx・報酬コントラクトからなら報酬 (出どころに合わせたラベル)。
         他者の tx で届いた分は、報酬・売却代金・送金のどれか決められないので要確認
    delegatecall は残高の移動を伴わない (呼び出し元の値を引き継いで見えるだけ) ので数えない。
    """
    flr_txs_by_hash = flr_txs_by_hash or {}
    wallet_lower = wallet.lower()
    unwrap_txs = set(r["tx_hash"] for r in rows if r["base"] == "WFLR" and "[Wrap] Unwrap WFLR" in r.get("comment", ""))

    # FLRが「入ってきて同じtx内で出ていく」通過だけのtx (Smart Account でオペレーターが
    # 立て替えた手数料など) は収入ではないので取り込まない
    passthrough_txs = set()
    internal_txs = [tx for tx in internal_txs if tx.get("callType") != "delegatecall"]
    for tx in internal_txs:
        if tx.get("isError") == "0" and (tx.get("from", "")).lower() == wallet_lower and int(tx.get("value", "0")) > 0:
            passthrough_txs.add(tx.get("hash", tx.get("transactionHash", "")))

    by_tx = defaultdict(list)
    for r in rows:
        by_tx[r["tx_hash"]].append(r)

    for tx in internal_txs:
        tx_hash = tx.get("hash", tx.get("transactionHash", ""))
        if tx.get("isError") != "0":
            continue
        if (tx.get("to", "")).lower() != wallet_lower:
            continue
        value = int(tx.get("value", "0"))
        if value == 0:
            continue
        # 1. Unwrap の払い戻し / 通過
        if tx_hash in unwrap_txs or tx_hash in passthrough_txs:
            continue

        unix_ts = int(tx.get("timeStamp", "0"))
        dt_jst = datetime.fromtimestamp(unix_ts, tz=timezone.utc).astimezone(JST)
        tx_rows = [r for r in by_tx.get(tx_hash, []) if not r.get("_delete")]
        flr_vol = Decimal(wei_to_flr(str(value)))
        t = flr_txs_by_hash.get(tx_hash) or {}
        own_tx = bool(t) and ((t.get("from") or {}).get("hash", "") or "").lower() == wallet_lower
        to_info = t.get("to") or {}
        to_name = to_info.get("name") or ""
        to_addr = (to_info.get("hash") or "").lower()
        method = ((t.get("decoded_input") or {}).get("method_call") or t.get("method") or "").split("(")[0]
        method = KNOWN_SELECTORS.get(method.lower(), method)

        # ガス代だけの行があれば、このFLR受取の行に寄せる
        inherit_fee = "0"
        for r in tx_rows:
            if r.get("action") == "DEFIFEE" and not r.get("_delete", False):
                inherit_fee = r.get("fee", "0")
                r["_delete"] = True
                break

        def emit(comment, status="export", action="BONUS", **extra):
            block = int(tx["blockNumber"]) if str(tx.get("blockNumber", "")).isdigit() else None
            row = _internal_flr_row(dt_jst, value, inherit_fee, comment, tx_hash, status, action, block)
            row.update(extra)
            rows.append(row)
            by_tx[tx_hash].append(row)

        # 2. LP引き出しで返るFLR → RECOVER として LP グループに注入
        lp_tags = [r["_lp"] for r in tx_rows if r.get("_lp")]
        if any(role in ("recover", "lp_out") for _, role in lp_tags):
            dex = lp_tags[0][0]
            src = next(r for r in tx_rows if r.get("_lp"))
            emit(f"[{dex}] Withdraw LP FLR", status="skip", action="RECOVER", _lp=(dex, "recover"),
                 **{k: src[k] for k in ("_pool", "_v3_closed") if k in src})
            continue

        # 3. トークン → ネイティブFLR の Swap (例: BlazeSwap swapExactTokensForNAT)
        token_sends = [r for r in tx_rows
                       if r["action"] == "SELL" and r["base"] not in ("FLR", "WFLR")
                       and r.get("comment", "").startswith("[Transfer] Send") and float(r.get("volume") or 0) > 0]
        receives = [r for r in tx_rows if r["action"] in ("BUY", "BONUS") and float(r.get("volume") or 0) > 0]
        if own_tx and len(token_sends) == 1 and not receives:
            s = token_sends[0]
            s_vol = Decimal(s["volume"])
            dex = ROUTER_TO_DEX.get(to_addr) or (to_name.replace("DexRouter", "").replace("Router", "") if "Router" in to_name else "DEX")
            s["action"] = "SELL"
            s["status"] = "export"
            s["counter"] = "FLR"
            s["counter_volume"] = format_decimal(flr_vol)
            s["price"] = ratio_str(flr_vol, s_vol)
            s["comment"] = f"[{dex}] Swap {s['base']} to FLR"
            if inherit_fee != "0" and s.get("fee", "0") in ("0", ""):
                s["fee"] = inherit_fee
            continue

        # 4. プロトコル操作の余り返金 (送った FLR/WFLR から差し引く)
        proto_row = next((r for r in tx_rows
                          if r.get("comment", "").startswith("[")
                          and r.get("comment", "").split("]")[0] + "]" not in ("[Transfer]", "[Wrap]", "[FLR Staking]", "[Delegate]", "[rFLR]")
                          and r.get("action") in ("SELL", "SENDFEE", "LEND") and r.get("base") in ("FLR", "WFLR")), None)
        if proto_row:
            new_vol = Decimal(proto_row["volume"]) - flr_vol
            if new_vol > 0:
                proto_row["volume"] = format_decimal(new_vol)
                if proto_row.get("counter_volume") and proto_row.get("counter") not in ("JPY", "", None):
                    # Swap の送付量が減ったので、交換比率も実際に払った量で出し直す
                    proto_row["price"] = ratio_str(Decimal(proto_row["counter_volume"]), new_vol)
                continue

        # 5. 自分でFLRを払ったtx (ゲーム・NFTのミントなど)
        paid = next((r for r in tx_rows if r["action"] == "SELL" and r["base"] == "FLR"
                     and r.get("comment", "").startswith("[Transfer] Send")), None)
        if paid:
            paid_vol = Decimal(paid["volume"])
            label = to_name or "Contract"
            if flr_vol <= paid_vol:
                # 払った額の一部が返ってきた = 返金
                paid["volume"] = format_decimal(paid_vol - flr_vol)
                paid["comment"] = f"[{label}] {method} 支払い（返金 {format_decimal(flr_vol)} FLR を差し引き済み）"
                continue
            emit(f"[{label}] {method} でFLR受取（ゲーム・NFTの当選金など）")
            continue

        # 6. 報酬 (出どころに合わせたラベル)
        sender = (tx.get("from") or "").lower()
        if not own_tx and sender in _REWARD_SENDERS:
            comment = f"{_REWARD_SENDERS[sender]}（自動クレーム）"
        elif not own_tx:
            emit("[Transfer] Receive FLR（他者の取引の中で届いた分。報酬なら BONUS に変更）",
                 status="review", action="BUY", counterparty=sender)
            continue
        elif to_name in _REWARD_LABELS:
            comment = _REWARD_LABELS[to_name]
        elif to_name:
            comment = f"[{to_name}] Claim FLR"
        else:
            comment = f"[Rewards] Claim FLR（{method or 'claim'}）"
        emit(comment)

    return rows


# LayerZero のチェーン番号 (eid) → 表示名
LZ_CHAINS = {
    30101: "Ethereum", 30102: "BNB Chain", 30106: "Avalanche", 30109: "Polygon",
    30110: "Arbitrum", 30111: "Optimism", 30184: "Base", 30295: "Flare",
}
_BRIDGE_WINDOW_SEC = 3 * 24 * 3600


def link_bridge_round_trips(rows: list, oft: dict) -> list:
    """
    ブリッジで別チェーンに送った分が、そのまま自分のアドレスに戻ってきた往復を対応付ける。
    送った tx の「相手チェーンに届く量」と、そのチェーンから戻ってきた着金の量が一致し、
    3日以内に戻っていれば同じお金とみなす (例: USDT を Arbitrum 経由で USDT0 にして戻す)。
      - 同じトークンで戻った → 送金・着金ともスキップ。減った分はブリッジ手数料 (SENDFEE)
      - 別のトークンで戻った → 送ったトークンを売って戻ったトークンを買った Swap (SELL)。着金はスキップ
    量が一致しない往復 (戻す前に向こうで別の操作をした等) は対応付けず、従来どおり要確認のまま。
    """
    def ts(r):
        return datetime.strptime(r["timestamp"], "%Y/%m/%d %H:%M:%S").timestamp()

    sends = [r for r in rows if r["comment"].startswith("[Bridge] Send ") and r["action"] == "SELL"
             and (oft.get(r["tx_hash"].lower()) or {}).get("dir") == "sent"]
    recvs = [r for r in rows if r["comment"].startswith("[Bridge/Mint] Receive ") and r["action"] == "BUY"
             and (oft.get(r["tx_hash"].lower()) or {}).get("dir") == "received"]
    used = set()
    new_rows = []
    for s in sorted(sends, key=ts):
        ev = oft[s["tx_hash"].lower()]
        match = next((r for r in sorted(recvs, key=ts)
                      if id(r) not in used
                      and 0 < ts(r) - ts(s) <= _BRIDGE_WINDOW_SEC
                      and oft[r["tx_hash"].lower()]["eid"] == ev["eid"]
                      and oft[r["tx_hash"].lower()]["amount"] == ev["amount"]), None)
        if not match:
            continue
        used.add(id(match))
        chain = LZ_CHAINS.get(ev["eid"], f"eid {ev['eid']}")
        sent, back = Decimal(s["volume"]), Decimal(match["volume"])
        match["status"] = "skip"
        match["comment"] = f"[Bridge] Receive {match['base']} from {chain}（{s['timestamp']} に送った分が戻ったもの）"
        if _norm_token(s["base"]) == _norm_token(match["base"]):
            s["status"] = "skip"
            s["comment"] = f"[Bridge] Send {s['base']} to {chain}（{match['timestamp']} に戻った往復）"
            if sent - back > _DUST:
                fee = dict(s)
                fee.update(action="SENDFEE", status="export", volume=format_decimal(sent - back),
                           price="", counter="JPY", counter_volume="", fee="0", fee_ccy=s["base"],
                           comment=f"[Bridge] Bridge Fee {s['base']}（往復で減った分）")
                new_rows.append(fee)
        else:
            s["status"] = "export"
            s["counter"] = match["base"]
            s["counter_volume"] = match["volume"]
            s["price"] = format_decimal(back / sent)
            s["comment"] = (f"[Bridge] Swap {s['base']} to {match['base']} via {chain}"
                            f"（送った {s['base']} が {match['timestamp']} に {match['base']} で戻った）")
    return rows + new_rows
