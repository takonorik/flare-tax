"""
取得 → 分類 → 円価格 の段取り (FastAPI に依存しない)。

ブラウザ (Pyodide) でも、開発用の main.py (FastAPI) でも、この run() を入口にして同じ処理を動かす。
通信は httpx 経由 (ブラウザでは web/httpx.py の代用モジュールが入る)。
"""

import asyncio
import copy
import json
import re
import sys
from typing import Optional

import httpx

from .blockscout import (
    BLOCKSCOUT_BASE,
    _item_dt,
    flr_key,
    get_json,
    fetch_flr_transactions,
    fetch_internal_transactions,
    fetch_native_balance,
    fetch_oft_events,
    fetch_sflr_redeems,
    fetch_sp_operations,
    fetch_token_transfers,
    fetch_v3_positions,
    fetch_withdrawal_requests,
    transfer_key,
)
from .protocols import ENOSYS_SP_MAP, NPM_ADDRESSES, SCEPTRE_SFLR
from .pipeline import build_result, native_balance_from_history
from .pricing import price_rows
from . import history as hist


async def _in_thread(fn, *args):
    """重い処理を別スレッドで。Pyodide (ブラウザ) はスレッドが使えないので、その場で動かす。"""
    if sys.platform == "emscripten":
        return fn(*args)
    return await asyncio.to_thread(fn, *args)


class _Progress:
    """取得の進捗。変わるたびに on_progress(dict) へ知らせる (None なら何もしない)。"""

    def __init__(self, on_progress=None):
        self.d: dict = {}
        self.cb = on_progress
        self.reset(False)

    def update(self, *a, **kw):
        self.d.update(*a, **kw)
        if self.cb:
            self.cb(dict(self.d))

    def reset(self, active: bool):
        self.update(
            active=active,
            flr_pages=0, flr_items=0,
            transfer_pages=0, transfer_items=0,
            internal_items=0,
            pricing_done=0, pricing_total=0,
            from_file=False, saved_items=0,
            phase="fetch",  # fetch → extras → classify → pricing (画面の表示に使う)
        )


ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
FXRP_ADDRESS = "0xad552a648c74d49e10027ab8a618a3ad4901c5be"
ZERO = "0x0000000000000000000000000000000000000000"


async def _fxrp_mint_txs(client: httpx.AsyncClient, wallet: str, transfers: list,
                         checked: Optional[set] = None) -> tuple:
    """
    Smart Account では、ミントされた FXRP がコントローラー経由で届く (0x0 からではない)。
    FXRP を受け取った tx の中に FXRP の発行 (0x0 から) が含まれていれば、ミントとみなす。
    checked: 履歴ファイルに残っている調べ済みの tx (ミントかどうかは過去の tx では変わらない)。
    返り値は (ミントの tx の集合, 調べ済みの tx の集合)。取れなかった tx は調べ済みにしない (次回またやり直す)。
    """
    cand = {t.get("transaction_hash") for t in transfers
            if ((t.get("token") or {}).get("address_hash") or "").lower() == FXRP_ADDRESS
            and (t.get("to") or {}).get("hash", "").lower() == wallet.lower()
            and (t.get("from") or {}).get("hash", "").lower() != ZERO}
    mints = set()
    checked = set(checked or ())
    for h in cand:
        if h in checked:
            continue
        try:
            j = await get_json(client, f"{BLOCKSCOUT_BASE}/transactions/{h}/token-transfers")
            if j is not None:
                checked.add(h)
            for it in (j or {}).get("items", []):
                if ((it.get("token") or {}).get("address_hash") or "").lower() == FXRP_ADDRESS \
                        and (it.get("from") or {}).get("hash", "").lower() == ZERO:
                    mints.add(h)
                    break
        except Exception:
            pass
    return mints, checked


MARKS = ("_sp_op", "_v3", "_withdraw_req", "_sflr_redeem")  # 取引ごとの追加取得で tx に付ける印


def _h(t: dict) -> str:
    return (t.get("hash") or "").lower()


async def _fetch_wallet(client: httpx.AsyncClient, wallet: str, track: bool, saved: Optional[dict] = None,
                        progress: Optional[_Progress] = None) -> dict:
    """
    1アドレス分の全履歴を取得する。track=True のアドレスだけ進捗表示に反映する。
    saved: 履歴ファイルにあるこのアドレスの分 ({"flr_txs", "token_transfers", "extras"})。
    あれば通常 tx・トークン転送は続きだけを取り、取引ごとの追加取得も新しい tx の分だけ行う。
    結果 (分類に渡すデータ) は全履歴を取り直した場合と同じになる。
    """
    track = track and progress is not None
    on = (lambda key_p, key_n: (lambda p, n: progress.update({key_p: p, key_n: n}))) if track else (lambda *_: None)
    # 途中で止まったとき、履歴ファイルには「保存済みのまま」を残す (取れた分を混ぜると、次回「保存済みに当たった」
    # で止まって穴が残る)。追加取得の印は保存済みの item にも付くので、先に丸ごと控えておく
    kept = copy.deepcopy(saved) if saved else None
    saved_flr = saved["flr_txs"] if saved else None
    saved_transfers = saved["token_transfers"] if saved else None
    ex = (saved or {}).get("extras") or {}
    (
        (flr_txs, flr_truncated),
        (token_transfers, transfers_truncated),
        (internal_txs, internal_truncated),
    ) = await asyncio.gather(
        fetch_flr_transactions(client, wallet, None, on_page=on("flr_pages", "flr_items"), saved=saved_flr),
        fetch_token_transfers(client, wallet, None, on_page=on("transfer_pages", "transfer_items"), saved=saved_transfers),
        fetch_internal_transactions(client, wallet, on_page=(lambda p, n: progress.update(internal_items=n)) if track else None),
    )
    # ここからは取引ごとの追加情報 (LP のポジション・ブリッジのイベントなど) を取る。件数は動かないので、段階を知らせる
    if track:
        progress.update(phase="extras")
    # 直近14日の重なりで取り直した tx は新しい item が正だが、追加取得の印 (過去の tx では変わらない) は保存済みから引き継ぐ
    if saved_flr:
        old_by_hash = {t.get("hash"): t for t in saved_flr}
        for t in flr_txs:
            old = old_by_hash.get(t.get("hash"))
            if old is not None and old is not t:
                for k in MARKS:
                    if k in old and k not in t:
                        t[k] = old[k]
    new_extras: dict = {}
    # 通常txが0件 = Smart Account (オペレーター経由で動く)。ミントの判定に tx 全体を見る
    if not flr_txs:
        new_mints, fxrp_checked = await _fxrp_mint_txs(client, wallet, token_transfers, set(ex.get("fxrp_checked") or ()))
        fxrp_mints = new_mints | set(ex.get("fxrp_mint_txs") or ())
        new_extras["fxrp_mint_txs"] = sorted(fxrp_mints)
        new_extras["fxrp_checked"] = sorted(fxrp_checked)
    else:
        fxrp_mints = set()
    # Stability Pool を使った tx には、清算で減った CDP と受け取った利回りを付けておく (分類で使う)
    # 保存済みで印が付いている tx、前回調べ済みの tx は調べ直さない
    sp_checked = set(ex.get("sp_checked") or ())
    sp_todo = [t for t in flr_txs if ((t.get("to") or {}).get("hash") or "").lower() in ENOSYS_SP_MAP
               and "_sp_op" not in t and _h(t) not in sp_checked]
    if sp_todo:
        sp_ops = await fetch_sp_operations(client, wallet, ENOSYS_SP_MAP)
        for t in sp_todo:
            op = sp_ops.get(_h(t))
            if op:
                t["_sp_op"] = op
        if sp_ops:  # 1件も返らなかったときは通信失敗かもしれないので、調べ済みにしない
            sp_checked |= {_h(t) for t in sp_todo}
    new_extras["sp_checked"] = sorted(sp_checked)
    # V3 の LP はポジション (NFT) ごとに元本を管理し、全部解除した時に差損を出す (分類で使う)
    # 流動性は時系列の積み上げなので、保存済みの tx の分は履歴ファイルの途中経過から続ける
    npm_txs = [t for t in flr_txs
               if "NonfungiblePositionManager" in ((t.get("to") or {}).get("name") or "")
               or ((t.get("to") or {}).get("hash") or "").lower() in NPM_ADDRESSES]
    v3_state = ex.get("v3")
    if npm_txs:
        if v3_state:
            processed = set(v3_state.get("processed") or ())
            liquidity = {(k.rsplit("|", 1)[0], int(k.rsplit("|", 1)[1])): int(v)
                         for k, v in (v3_state.get("liquidity") or {}).items()}
            v3_todo = [t for t in npm_txs if _h(t) not in processed]
        else:  # 途中経過が無ければ、全部やり直す (古い印は付け直す)
            processed, liquidity, v3_todo = set(), {}, npm_txs
            for t in npm_txs:
                t.pop("_v3", None)
        v3_failed: set = set()
        v3 = await fetch_v3_positions(client, v3_todo, liquidity, v3_failed) if v3_todo else {}
        for t in v3_todo:
            info = v3.get(_h(t))
            if info:
                t["_v3"] = info
        if v3_failed:  # 取れなかった tx があると積み上げがずれるので、途中経過は残さず次回やり直す
            v3_state = None
        else:
            processed |= {_h(t) for t in v3_todo}
            v3_state = {"processed": sorted(processed),
                        "liquidity": {f"{npm}|{tid}": str(v) for (npm, tid), v in liquidity.items()}}
    if v3_state:
        new_extras["v3"] = v3_state
    # 遅延アンステークの申請には、申請で確定した受取量を付けておく (申請の時点で Swap にする)
    req_txs = [t for t in flr_txs if ("requestWithdrawal" in (t.get("method") or "")
               or (t.get("raw_input") or "").startswith("0x9ee679e8")) and "_withdraw_req" not in t]
    if req_txs:
        reqs = await fetch_withdrawal_requests(client, req_txs)
        for t in req_txs:
            info = reqs.get(_h(t))
            if info:
                t["_withdraw_req"] = info
    # sFLR の redeem には、返した sFLR と受け取った FLR を付けておく (Unlock 後の受け取りを Swap にする)
    redeem_txs = [t for t in flr_txs if ((t.get("to") or {}).get("hash") or "").lower() == SCEPTRE_SFLR
                  and ("redeem" in (t.get("method") or "").lower()
                       or (t.get("raw_input") or "")[:10] in ("0xdb006a75", "0xbe040fb0"))
                  and "_sflr_redeem" not in t]
    if redeem_txs:
        redeems = await fetch_sflr_redeems(client, redeem_txs)
        for t in redeem_txs:
            info = redeems.get(_h(t))
            if info:
                t["_sflr_redeem"] = info
    # ブリッジの送金と着金 (0x0 から発行されて届いたもの) には、OFT のイベントを付けておく。
    # 送った分がそのまま戻ってきた往復を見つけるため (pipeline で対応付ける)
    own = {(t.get("hash") or "").lower() for t in flr_txs}
    bridge_hashes = {(t.get("hash") or "").lower() for t in flr_txs
                     if any(k in ((t.get("to") or {}).get("name") or "") for k in ("Stargate", "OFT"))
                     or (t.get("method") or "") == "send"
                     or (t.get("raw_input") or "").startswith("0xc7c7f5b3")}
    bridge_hashes |= {(t.get("transaction_hash") or "").lower() for t in token_transfers
                      if (t.get("from") or {}).get("hash", "").lower() == ZERO
                      and (t.get("to") or {}).get("hash", "").lower() == wallet.lower()
                      and (t.get("transaction_hash") or "").lower() not in own}
    # 調べ済み (イベントがあった / 無かった) の tx は調べ直さない。取れなかったものだけ次回またやり直す
    oft_known = {h: v for h, v in (ex.get("oft_events") or {}).items() if h in bridge_hashes}
    oft_checked = set(ex.get("oft_checked") or ())
    oft_todo = bridge_hashes - set(oft_known) - oft_checked
    oft_failed: set = set()
    oft_new = await fetch_oft_events(client, oft_todo, oft_failed) if oft_todo else {}
    oft_events = {**oft_known, **oft_new}
    oft_checked |= oft_todo - oft_failed - set(oft_new)
    new_extras["oft_events"] = oft_events
    new_extras["oft_checked"] = sorted(oft_checked & bridge_hashes)
    label = f"{wallet[:6]}…{wallet[-4:]}"
    warnings = []
    stopped = flr_truncated or transfers_truncated
    if stopped and saved:
        warnings.append(f"{label}: 新しい取引の取得が途中で止まりました。今回は履歴ファイルの分と取れた分で計算しています。"
                        "履歴ファイルは更新していません (取れた分は保存しません)。もう一度取得してください。")
    else:
        keep_note = " この分は履歴ファイルに保存しません。"
        if flr_truncated:
            warnings.append(f"{label}: 通常トランザクションの取得が上限に達しました。古い取引が欠落している可能性があります。{keep_note}")
        if transfers_truncated:
            warnings.append(f"{label}: トークン転送の取得が上限に達しました。古い取引が欠落している可能性があります。{keep_note}")
    if internal_truncated:
        warnings.append(f"{label}: 内部トランザクションの取得が上限に達しました。古いFLR受取(ステーキング報酬等)が欠落している可能性があります。")
    # 取得漏れに気づくための照合: 履歴から計算した FLR 残高と、実際の残高を比べる
    notices = []
    actual = None if (flr_truncated or internal_truncated) else await fetch_native_balance(client, wallet)
    if actual is not None:
        diff = (actual - native_balance_from_history(flr_txs, token_transfers, internal_txs, wallet)) / 10 ** 18
        if diff >= 1:
            notices.append(
                f"{label}: 取得した履歴から計算した FLR 残高が、実際の残高より {diff:,.2f} FLR 少なくなっています。"
                "エクスプローラー側の内部トランザクションに欠けがあり、FLR の受け取りが一部出ていない可能性があります。"
                + (" 履歴ファイルを使っている場合は、『全履歴を取り直す』を試してください。" if saved is not None else ""))
        elif diff <= -1:
            notices.append(
                f"{label}: 取得した履歴から計算した FLR 残高が、実際の残高より {-diff:,.2f} FLR 多くなっています。"
                "P チェーンへの移動（ステーキング）など、エクスプローラーに出ない出金があるとこうなります（損益には影響しません）。")
    # 新しい履歴ファイルに入れる分 (止まったときは保存済みのまま。保存済みも無ければ入れない)
    if not stopped:
        to_save = {"flr_txs": flr_txs, "token_transfers": token_transfers, "extras": new_extras}
    else:
        to_save = kept
    old_flr = {t.get("hash") for t in saved_flr or ()}
    old_tr = {transfer_key(t) for t in saved_transfers or ()}
    return {"wallet": wallet, "flr_txs": flr_txs, "token_transfers": token_transfers,
            "internal_txs": internal_txs, "fxrp_mint_txs": fxrp_mints, "oft_events": oft_events,
            "warnings": warnings, "notices": notices,
            "to_save": to_save, "from_file": saved is not None,
            "added_txs": 0 if stopped else sum(1 for t in flr_txs if t.get("hash") not in old_flr),
            "added_transfers": 0 if stopped else sum(1 for t in token_transfers if transfer_key(t) not in old_tr)}




def _latest_item(w: dict) -> Optional[tuple]:
    """ウォレット1件分 (flr_txs / token_transfers) で一番新しい item の (時刻, 元の文字列)。無ければ None。"""
    best = None
    for item in [*w["flr_txs"], *w["token_transfers"]]:
        dt = _item_dt(item)
        if dt is not None and (best is None or dt > best[0]):
            best = (dt, item.get("timestamp"))
    return best


def inspect_history(history_bytes: bytes, wallets: list) -> dict:
    """
    履歴ファイルの中身を、通信なしでウォレットごとに調べる (画面で「どこまで保存済みか」を見せる用)。
    壊れている・形式が違うときは例外にせず {"ok": False, "message"} を返す。
    """
    try:
        saved, _, saved_at = hist.unpack_full(history_bytes)
    except hist.HistoryError as e:
        return {"ok": False, "message": str(e)}
    except Exception:
        return {"ok": False, "message": hist.BROKEN}
    given = []
    for a in wallets:
        if a.lower() not in [g.lower() for g in given]:
            given.append(a)
    out = []
    for a in given:
        w = saved.get(a.lower())
        if w is None:
            out.append({"address": a, "in_file": False})
            continue
        latest = _latest_item(w)
        out.append({"address": a, "in_file": True, "txs": len(w["flr_txs"]),
                    "transfers": len(w["token_transfers"]), "latest": latest[1] if latest else None})
    others = len([k for k in saved if k not in {g.lower() for g in given}])
    return {"ok": True, "saved_at": saved_at, "wallets": out, "others": others}


def _compare_with_file(addrs: list, results: list, file_wallets: dict) -> dict:
    """
    「全履歴を取り直し」の結果と履歴ファイルを比べる。比べるのは、ファイルの保存時点
    (そのウォレットの最新の時刻) より前の item だけ。取り直しにだけある / ファイルにだけある件数を数える。
    取得が途中で止まったウォレット (to_save が None) は、足りないのが当然なので比べない。
    """
    rows = []
    for a, d in zip(addrs, results):
        w = file_wallets.get(a.lower())
        if w is None or d["to_save"] is None:
            continue
        latest = _latest_item(w)
        if latest is None:
            continue

        def keys(txs, transfers):
            out = set()
            for kind, items, key in (("tx", txs, flr_key), ("tr", transfers, transfer_key)):
                for i in items:
                    dt = _item_dt(i)
                    if dt is not None and dt < latest[0]:
                        out.add((kind, key(i)))
            return out

        fresh = keys(d["flr_txs"], d["token_transfers"])
        old = keys(w["flr_txs"], w["token_transfers"])
        rows.append({"address": a, "only_new": len(fresh - old), "only_file": len(old - fresh)})
    return {"checked": bool(rows), "wallets": rows,
            "same": bool(rows) and all(r["only_new"] == 0 and r["only_file"] == 0 for r in rows)}


async def _run_core(addrs: list, start_date, end_date, saved_wallets: Optional[dict] = None,
                    price_cache: Optional[dict] = None, make_history: bool = False,
                    progress: Optional[_Progress] = None, compare_with: Optional[dict] = None,
                    extra_warnings: Optional[list] = None) -> tuple:
    """
    取得 → 分類 → 円価格。saved_wallets (履歴ファイルの中身) があれば、その続きから取得する。
    addrs の先頭がメイン。返り値は (画面に返す結果, 履歴ファイルのバイト列 or None)。
    compare_with: 履歴ファイルの中身。渡すと、取り直した結果との違いを history.compare に入れる。
    """
    saved_wallets = saved_wallets or {}
    progress = progress or _Progress()
    wallet, extras = addrs[0], addrs[1:]
    main_saved = saved_wallets.get(wallet.lower())
    progress.reset(True)
    progress.update(from_file=main_saved is not None,
                    saved_items=len((main_saved or {}).get("flr_txs", [])) + len((main_saved or {}).get("token_transfers", [])))
    try:
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *[_fetch_wallet(client, a, track=(i == 0), saved=saved_wallets.get(a.lower()), progress=progress)
                  for i, a in enumerate(addrs)],
            )
    finally:
        progress.reset(False)

    # 履歴ファイルの中身は、分類が item に印を付ける前の形で控える
    new_wallets = None
    if make_history:
        new_wallets = {a: w for a, w in saved_wallets.items()}  # 今回指定されていないウォレットはそのまま残す
        for a, d in zip(addrs, results):
            if d["to_save"] is not None:
                new_wallets[a.lower()] = d["to_save"]
            else:
                new_wallets.pop(a.lower(), None)
        new_wallets = await _in_thread(lambda: json.loads(json.dumps(new_wallets)))
    # 違いの比較も、印が付く前の item で行う
    compare = _compare_with_file(addrs, results, compare_with) if compare_with is not None else None

    main_data, extra_data = results[0], list(results[1:])
    warnings = list(extra_warnings or []) + [w for d in results for w in d["warnings"]]
    notices = [n for d in results for n in d["notices"]]

    progress.update(phase="classify")
    result = build_result(
        main_data["flr_txs"],
        main_data["token_transfers"],
        main_data["internal_txs"],
        wallet,
        start_date=start_date,
        end_date=end_date,
        warnings=warnings,
        extra_wallets=extra_data,
        oft_events=main_data["oft_events"],
        notices=notices,
    )

    # 価格データの無いトークンの円価格 (bitbank・sFLRの交換比率・経由したWFLR)
    progress.update(active=True, pricing_done=0, pricing_total=0, phase="pricing")
    try:
        async with httpx.AsyncClient() as client:
            await price_rows(
                client, result["rows"], {wallet.lower(), *[a.lower() for a in extras]},
                on_progress=lambda d, t: progress.update(pricing_done=d, pricing_total=t),
                cache=price_cache,
            )
    finally:
        progress.reset(False)

    data = None
    if make_history:
        data = await _in_thread(hist.pack, new_wallets, price_cache)
        result["history"] = {
            "filename": hist.history_filename(),
            "wallets": [{"address": a, "txs": len(d["flr_txs"]), "transfers": len(d["token_transfers"]),
                         "added_txs": d["added_txs"], "added_transfers": d["added_transfers"],
                         "from_file": d["from_file"]} for a, d in zip(addrs, results)],
            "added": sum(d["added_txs"] + d["added_transfers"] for d in results),
            "used_file": any(d["from_file"] for d in results),
            "bytes": len(data),
        }
        if compare is not None:
            result["history"]["compare"] = compare
    return result, data


def _unique_wallets(wallets: list) -> list:
    out, seen = [], set()
    for a in wallets:
        if a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


async def run(wallets: list, start_date=None, end_date=None,
              history_bytes: bytes = None, refresh: bool = False,
              on_progress=None, strict: bool = True) -> dict:
    """
    wallets: アドレスのリスト (先頭がメイン)。
    history_bytes: 履歴ファイル (gzip のバイト列)。None なら全履歴を取得。
    refresh: True なら履歴ファイルは取得には使わない (全履歴を取り直し、ファイルとの違いだけ比べる)。
    on_progress(dict): 進捗。None なら何もしない。
    strict: True (既定) だと、使えない履歴ファイルは HistoryError で止める。False だと止めずに全履歴を取得し、
      warnings にその旨を入れる (ブラウザはこちら)。
    戻り値: {"result": {...画面に返す JSON ("history" を含む)}, "history_bytes": 最新の履歴ファイル}
    """
    addrs = _unique_wallets(wallets)
    saved_wallets, price_cache, compare_with, warnings = None, {}, None, []
    if history_bytes:
        try:
            file_wallets, file_prices, _ = hist.unpack_full(history_bytes)
            if refresh:
                compare_with = file_wallets  # 取得には使わず、比べるだけ (円価格のキャッシュも使わない)
            else:
                saved_wallets, price_cache = file_wallets, file_prices
        except hist.HistoryError as e:
            if strict and not refresh:
                raise
            if not refresh:
                warnings.append(f"履歴ファイルが使えなかったので、全履歴を取得しました（{str(e).rstrip('。')}）。")
    result, data = await _run_core(addrs, start_date, end_date, saved_wallets, price_cache, True,
                                   _Progress(on_progress), compare_with, warnings)
    if refresh and history_bytes and compare_with is None:
        result["history"]["compare"] = {"checked": False, "wallets": [], "same": False}
    return {"result": result, "history_bytes": data}
