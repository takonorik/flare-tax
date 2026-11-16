"""Blockscout APIクライアント。全フェッチャは (items, truncated) を返す。"""

import asyncio
from datetime import datetime, timedelta
from typing import Optional

import httpx

from .helpers import format_units

# Blockscout (Cloudflare) は短時間にリクエストが集中すると 429 を返す。
# 同時リクエスト数を抑え、429・5xx・通信エラーは待って再試行する。
_SEMS: dict = {}  # イベントループごと (Python 3.9 の Semaphore は作成時のループに紐づく)
RETRY_STATUS = {429, 500, 502, 503, 504}


def _sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _SEMS:
        _SEMS.clear()
        _SEMS[loop] = asyncio.Semaphore(4)
    return _SEMS[loop]


async def get_json(client: httpx.AsyncClient, url: str, params: Optional[dict] = None,
                   timeout: float = 30, tries: int = 6):
    """GET して JSON を返す。最終的に取れなければ None。"""
    delay = 2.0
    for attempt in range(tries):
        try:
            async with _sem():
                resp = await client.get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code not in RETRY_STATUS:
                return None
            wait = float(resp.headers.get("Retry-After") or delay)
        except (httpx.TransportError, ValueError):
            wait = delay
        if attempt < tries - 1:
            await asyncio.sleep(min(wait, 30))
            delay *= 2
    return None

BLOCKSCOUT_BASE = "https://flare-explorer.flare.network/api/v2"
BLOCKSCOUT_V1 = "https://flare-explorer.flare.network/api"
MAX_PAGES = 400  # 50件/ページ × 400 = 最大20,000件
INTERNAL_TX_PAGE_SIZE = 10000
INTERNAL_TX_MAX_PAGES = 20  # 最大200,000件


# ---- 保存済みの履歴 (履歴ファイル) + 新しい分だけの取得 ----
# 直近この期間 (14日) は保存済みでも必ず取り直して重ねる (エクスプローラーの取り込みが遅れて、
# 後から同じ時期の item が増えることがあるため)
OVERLAP = timedelta(days=14)


def flr_key(item: dict):
    """通常 tx の重複判定キー。"""
    return item.get("hash")


def transfer_key(item: dict):
    """
    トークン転送の重複判定キー。同じログから複数行が出る場合に備えて、トークン・量・送受信者も含める。
    """
    tok = item.get("token") or {}
    return (
        item.get("transaction_hash") or item.get("tx_hash"),
        item.get("log_index"),
        (tok.get("address_hash") or tok.get("address") or "").lower(),
        str((item.get("total") or {}).get("value")),
        ((item.get("from") or {}).get("hash") or "").lower(),
        ((item.get("to") or {}).get("hash") or "").lower(),
    )


def _item_dt(item: dict) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((item.get("timestamp") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _saved_state(saved, key):
    """保存済みの (キー集合, 取り直しの境目の時刻)。時刻が分からなければ境目は None (= 途中で止めない)。"""
    keys = {key(i) for i in saved}
    times = [d for d in (_item_dt(i) for i in saved) if d is not None]
    return keys, (max(times) - OVERLAP if times else None)


def _stop_here(items, saved_keys, cutoff, key) -> bool:
    """このページで止めてよいか: 保存済みに当たり、かつ最後の item が境目より古い。"""
    if cutoff is None or not items or not any(key(i) in saved_keys for i in items):
        return False
    last = _item_dt(items[-1])
    return last is not None and last < cutoff


def _merge_saved(fetched: list, saved: list, key) -> list:
    """新しく取った分を先に、保存済みの残りを後ろに (新しい順)。同じキーは新しく取った方を正とする。"""
    got = {key(i) for i in fetched}
    return fetched + [i for i in saved if key(i) not in got]


async def fetch_flr_transactions(
    client: httpx.AsyncClient,
    wallet: str,
    start_dt: Optional[datetime] = None,
    on_page=None,
    saved: Optional[list] = None,
) -> tuple:
    """Fetch FLR (native) transactions for wallet from Blockscout.

    Returns (items, truncated). truncated=True means the result is incomplete
    (page limit reached or an HTTP error mid-fetch) — NOT the intentional
    early stop when start_dt is passed.
    on_page(pages, items) is called after each page for progress reporting.
    saved: 履歴ファイルの保存済み (新しい順)。渡すと、保存済みに当たって直近14日の重なりを抜けた所で止め、
    新しく取った分 + 保存済みの残りを返す (全履歴を取り直した結果と同じ並び・同じ中身)。
    """
    wallet_lower = wallet.lower()
    url = f"{BLOCKSCOUT_BASE}/addresses/{wallet_lower}/transactions"
    all_txs = []
    params: Optional[dict] = None
    truncated = True  # only cleared by a clean exit below
    saved_keys, cutoff = _saved_state(saved, flr_key) if saved else (None, None)

    for page in range(MAX_PAGES):
        data = await get_json(client, url, params)
        if data is None:
            break
        items = data.get("items", [])
        all_txs.extend(items)
        if on_page:
            on_page(page + 1, len(all_txs))

        if start_dt and items:
            last_item_ts = datetime.fromisoformat(items[-1].get("timestamp", "").replace("Z", "+00:00"))
            if last_item_ts < start_dt:
                truncated = False
                break

        if saved_keys is not None and _stop_here(items, saved_keys, cutoff, flr_key):
            truncated = False
            break

        next_params = data.get("next_page_params")
        if not next_params:
            truncated = False
            break
        params = next_params

    if saved:
        all_txs = _merge_saved(all_txs, saved, flr_key)
    return all_txs, truncated


async def fetch_token_transfers(
    client: httpx.AsyncClient,
    wallet: str,
    start_dt: Optional[datetime] = None,
    on_page=None,
    saved: Optional[list] = None,
) -> tuple:
    """Fetch ERC-20 token transfers for wallet from Blockscout.

    Returns (items, truncated) — see fetch_flr_transactions (saved の扱いも同じ)。
    """
    wallet_lower = wallet.lower()
    url = f"{BLOCKSCOUT_BASE}/addresses/{wallet_lower}/token-transfers"
    all_transfers = []
    params: dict = {"type": "ERC-20"}
    truncated = True  # only cleared by a clean exit below
    saved_keys, cutoff = _saved_state(saved, transfer_key) if saved else (None, None)

    for page in range(MAX_PAGES):
        data = await get_json(client, url, params)
        if data is None:
            break
        items = data.get("items", [])
        all_transfers.extend(items)
        if on_page:
            on_page(page + 1, len(all_transfers))

        if start_dt and items:
            last_item_ts = datetime.fromisoformat(items[-1].get("timestamp", "").replace("Z", "+00:00"))
            if last_item_ts < start_dt:
                truncated = False
                break

        if saved_keys is not None and _stop_here(items, saved_keys, cutoff, transfer_key):
            truncated = False
            break

        next_params = data.get("next_page_params")
        if not next_params:
            truncated = False
            break
        # Merge type filter with pagination params
        params = {**next_params, "type": "ERC-20"}

    if saved:
        all_transfers = _merge_saved(all_transfers, saved, transfer_key)
    return all_transfers, truncated


async def fetch_internal_transactions(client: httpx.AsyncClient, wallet: str, on_page=None) -> tuple:
    """Fetch internal transactions using Blockscout v1 API to avoid v2 timeout issues.

    Returns (items, truncated). ページネーション対応 (10,000件/ページ)。
    """
    wallet_lower = wallet.lower()
    all_transfers = []
    truncated = False

    for page in range(1, INTERNAL_TX_MAX_PAGES + 1):
        url = (
            f"{BLOCKSCOUT_V1}?module=account&action=txlistinternal"
            f"&address={wallet_lower}&sort=desc&page={page}&offset={INTERNAL_TX_PAGE_SIZE}"
        )
        data = await get_json(client, url)
        if data is None:
            truncated = True
            break
        if data.get("status") != "1":
            break  # "0" = これ以上履歴なし
        items = data.get("result", [])
        all_transfers.extend(items)
        if on_page:
            on_page(page, len(all_transfers))
        if len(items) < INTERNAL_TX_PAGE_SIZE:
            break
    else:
        truncated = True

    return all_transfers, truncated


async def fetch_native_balance(client: httpx.AsyncClient, wallet: str) -> Optional[int]:
    """現在のネイティブ FLR 残高 (wei)。取れなければ None。"""
    j = await get_json(client, f"{BLOCKSCOUT_BASE}/addresses/{wallet.lower()}", tries=3)
    try:
        return int((j or {}).get("coin_balance"))
    except (TypeError, ValueError):
        return None


# Enosys Loans の Stability Pool が操作のたびに出すイベント。
# DepositOperation(address indexed depositor, uint8 operation, uint256 depositLossSinceLastOperation,
#   int256 topUpOrWithdrawal, uint256 yieldGainSinceLastOperation, uint256 yieldGainClaimed,
#   uint256 ethGainSinceLastOperation, uint256 ethGainClaimed)
SP_DEPOSIT_OPERATION = "0xdf459587a9bfd896271616423088d4842cfad6948a5a975c7d82b52d951805e4"


async def fetch_sp_operations(client: httpx.AsyncClient, wallet: str, sp_addresses) -> dict:
    """
    Stability Pool の操作ごとの数字を {tx_hash: {"loss", "yield_claimed"}} (CDP, 文字列) で返す。
    loss = 前回の操作から清算で使われた (減った) CDP、yield_claimed = この操作で受け取った利回り。
    取れなければ空 (その場合は従来どおり元本の差分だけで計算する)。
    """
    ops: dict = {}
    topic1 = "0x" + "0" * 24 + wallet[2:].lower()
    for sp in sp_addresses:
        j = await get_json(client, BLOCKSCOUT_V1, {
            "module": "logs", "action": "getLogs", "address": sp, "topic0": SP_DEPOSIT_OPERATION,
            "topic1": topic1, "topic0_1_opr": "and", "fromBlock": "0", "toBlock": "latest"})
        for log in (j or {}).get("result") or []:
            data = log.get("data", "")[2:]
            words = [data[i:i + 64] for i in range(0, len(data), 64)]
            if len(words) < 7:
                continue
            loss, yld = int(words[1], 16), int(words[4], 16)
            ops[log["transactionHash"].lower()] = {
                "loss": format_units(loss, 18), "yield_claimed": format_units(yld, 18)}
    return ops


# V3 ポジションマネージャー (NFTポジション) のイベント。値は data 内の流動性の位置 (32バイト単位)
V3_INCREASE = {
    "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f": 0,  # Uniswap V3: liquidity
    "0x8a82de7fe9b33e0e6bca0e26f5bd14a74f1164ffe236d50e0a36c3ea70f2b814": 1,  # Algebra: actualLiquidity
}
V3_DECREASE = "0x26f6a048ee9138f2c0ce266f322cb99228e8d619ae2bff30c67f8dcf9d2377b4"


async def fetch_v3_positions(client: httpx.AsyncClient, txs: list, liquidity: Optional[dict] = None,
                             failed: Optional[set] = None) -> dict:
    """
    V3 ポジションマネージャー宛ての tx ごとに、動かしたポジション (NFT番号) と
    その tx で流動性が0になった (全部解除した) かを {tx_hash: {"id", "closed"}} で返す。
    流動性は自分の tx の増減を時系列に足し合わせて追う。1つの tx で複数ポジションを動かした場合や、
    途中の増減が取れずに合計が合わない場合は付けない (その tx は従来どおりプール単位で計算する)。
    liquidity: 前回までの流動性 ({(npm, id): 量}、履歴ファイルから)。渡すと txs の分をその続きとして足す (渡した辞書を更新する)。
    failed: 取得に失敗した tx のハッシュを入れる (呼び出し側が次回やり直す)。どちらも省略すれば従来どおり。
    """
    async def logs(t):
        h = t.get("hash") or ""
        j = await get_json(client, f"{BLOCKSCOUT_BASE}/transactions/{h}/logs")
        moves = []
        for log in (j or {}).get("items") or []:
            topics = log.get("topics") or []
            if len(topics) < 2 or not topics[0]:
                continue
            t0 = topics[0].lower()
            if t0 not in V3_INCREASE and t0 != V3_DECREASE:
                continue
            data = (log.get("data") or "")[2:]
            word = V3_INCREASE.get(t0, 0)
            liq = int(data[word * 64:(word + 1) * 64] or "0", 16)
            npm = ((log.get("address") or {}).get("hash") or "").lower()
            moves.append(((npm, int(topics[1], 16)), liq if t0 in V3_INCREASE else -liq))
        return h.lower(), t.get("timestamp") or "", j is not None, moves

    results = await asyncio.gather(*[logs(t) for t in txs])
    out: dict = {}
    if liquidity is None:
        liquidity = {}
    for h, _, ok, moves in sorted(results, key=lambda x: x[1]):
        if not ok and failed is not None:
            failed.add(h)
        ids = {k for k, _ in moves}
        for k, d in moves:
            liquidity[k] = liquidity.get(k, 0) + d
        if not ok or len(ids) != 1:
            continue
        (npm, token_id), = ids
        left = liquidity[(npm, token_id)]
        if left < 0:
            continue
        out[h] = {"id": f"{token_id} ({npm[:6]})",
                  "closed": left == 0 and any(d < 0 for _, d in moves)}
    return out


# 遅延アンステーク (SparkDEX stFLR) の申請イベント: WithdrawalRequested(requestId, owner, amountOfFlr, amountOfShares)
WITHDRAWAL_REQUESTED = "0xe6d14ce42ad5a4efe0f111f81c1a1123db4ee41f561c3161e0960d54a9221ebe"


async def fetch_withdrawal_requests(client: httpx.AsyncClient, txs: list) -> dict:
    """
    requestWithdrawal の tx ごとに、申請で確定した受取量を {tx_hash: {"flr", "shares"}} (文字列) で返す。
    stFLR は申請時に受け取る FLR の量が決まり、後の claim ではその量の WFLR が届くだけ。
    """
    async def one(t):
        h = (t.get("hash") or "").lower()
        j = await get_json(client, f"{BLOCKSCOUT_BASE}/transactions/{h}/logs")
        for log in (j or {}).get("items") or []:
            topics = log.get("topics") or []
            if topics and (topics[0] or "").lower() == WITHDRAWAL_REQUESTED:
                data = (log.get("data") or "")[2:]
                if len(data) >= 128:
                    return h, {"flr": format_units(int(data[:64], 16), 18),
                               "shares": format_units(int(data[64:128], 16), 18)}
        return h, None

    return {h: v for h, v in await asyncio.gather(*[one(t) for t in txs]) if v}


# LayerZero OFT (Stargate・USDT0 など) の送金/着金イベント
OFT_SENT = "0x85496b760a4b7f8d66384b9df21b381f5d1b1e79f229a47aaf4c232edc2fe59a"      # OFTSent(guid, dstEid, from, amountSentLD, amountReceivedLD)
OFT_RECEIVED = "0xefed6d3500546b29533b128a29e3a94d70788727f0507505ac12eaf2e578fd9c"  # OFTReceived(guid, srcEid, to, amountReceivedLD)


async def fetch_oft_events(client: httpx.AsyncClient, hashes, failed: Optional[set] = None) -> dict:
    """
    ブリッジの tx ごとに {tx_hash: {"dir": "sent"|"received", "eid": 相手チェーン, "amount": 届く量 (最小単位)}} を返す。
    送った tx の「相手チェーンに届く量」と、戻ってきた tx の「届いた量」が一致すれば、同じお金の往復と分かる。
    failed: 取れなかった (イベントが無いのではない) tx のハッシュを入れる。
    """
    async def one(h):
        j = await get_json(client, f"{BLOCKSCOUT_BASE}/transactions/{h}/logs")
        if j is None and failed is not None:
            failed.add(h)
        for log in (j or {}).get("items") or []:
            topics = log.get("topics") or []
            t0 = (topics[0] or "").lower() if topics else ""
            data = (log.get("data") or "")[2:]
            if t0 == OFT_SENT and len(data) >= 192:
                return h, {"dir": "sent", "eid": int(data[:64], 16), "amount": int(data[128:192], 16)}
            if t0 == OFT_RECEIVED and len(data) >= 128:
                return h, {"dir": "received", "eid": int(data[:64], 16), "amount": int(data[64:128], 16)}
        return h, None

    return {h: v for h, v in await asyncio.gather(*[one(h.lower()) for h in hashes]) if v}


# sFLR (Sceptre) の Redeem(user, 申請時刻, 返した sFLR, 受け取った FLR)
SFLR_REDEEM = "0xbd5034ffbd47e4e72a94baa2cdb74c6fad73cb3bcdc13036b72ec8306f5a7646"


async def fetch_sflr_redeems(client: httpx.AsyncClient, txs: list) -> dict:
    """
    sFLR の redeem tx ごとに、返した sFLR と受け取った FLR を {tx_hash: {"shares", "flr"}} (文字列) で返す。
    Unlock を申請した sFLR は Transfer イベントなしで減るので、トークン転送からは数量が分からない。
    1つの tx で複数の申請分をまとめて受け取ることがあるので、Redeem イベントを合計する。
    """
    async def one(t):
        h = (t.get("hash") or "").lower()
        j = await get_json(client, f"{BLOCKSCOUT_BASE}/transactions/{h}/logs")
        shares = flr = 0
        for log in (j or {}).get("items") or []:
            topics = log.get("topics") or []
            data = (log.get("data") or "")[2:]
            if topics and (topics[0] or "").lower() == SFLR_REDEEM and len(data) >= 192:
                shares += int(data[64:128], 16)
                flr += int(data[128:192], 16)
        return h, ({"shares": format_units(shares, 18), "flr": format_units(flr, 18)} if shares else None)

    return {h: v for h, v in await asyncio.gather(*[one(t) for t in txs]) if v}
