"""
円価格の自動計算 (価格データの無いトークン用)。XRPL Tax for Cryptact と共通のルール:

  0. クリプタクトに価格があるトークン → 価格は空欄 (クリプタクトの時価を使う)
  1. 取引の片方が価格のある資産 → その資産を決済通貨にする (円換算不要。画面側で行の向きを決める)
  2. 価格の無いトークン同士のスワップ → 同じtx内で経由した WFLR の量 × FLR/JPY
  3. (2回目で追加予定) プールの価格
  4. LP引き出しの払い戻し比率 (V2 のみ)
  5. どれも取れなければ要確認 (手入力)

基軸資産から円へは bitbank の公開API。Flare 固有の調整:
  - FXRP は XRP と 1:1 のペッグ → bitbank XRP/JPY
  - sFLR はコントラクトの交換比率 (Submitted イベントの flrAmount/shareAmount) × FLR/JPY
  - FLR/JPY の1分足は出来高ゼロが多いので、同じ日の「直前の出来高あり」の足、無ければ1時間足

各行に {"jpy": {"base": 円/枚, "counter": 円/枚, "basis": 根拠}} を付ける (取れたものだけ)。
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx

from .blockscout import get_json
from .helpers import JST

BITBANK = "https://public.bitbank.cc"
BLOCKSCOUT_V1 = "https://flare-explorer.flare.network/api"
BLOCKSCOUT_V2 = "https://flare-explorer.flare.network/api/v2"

NATIVE = "native"
WFLR = "0x1d80c49bbbcd1c0911346656b529df9e5c2f783d"
FXRP = "0xad552a648c74d49e10027ab8a618a3ad4901c5be"
SFLR = "0x12e605bc104e93b45e1ad99f9e555f659051c2bb"
SFLR_SUBMITTED = "0xbb0070894135d02edfa550b04d7e5e141aa8090b46e57597ad45bfedd6554498"

RPC = "https://flare-api.flare.network/ext/C/rpc"
# プールを探す工場コントラクト (ルール3: プールの価格)
V2_FACTORIES = {
    "Enosys": "0x28b70f6ed97429e40fe9a9cd3eb8e86bcba11dd4",
    "BlazeSwap": "0x440602f459d7dd500a74528003e6a20a46d6e2a6",
}
V3_FACTORIES = {"SparkDEX V3": "0x8a2578d23d4c532cc9a98fad91c0523f5efde652"}
V3_FEES = (100, 500, 3000, 10000)
SYNC_TOPIC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"      # V2 Sync(reserve0, reserve1)
SWAP_V3_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"   # V3 Swap(..., sqrtPriceX96, ...)
MIN_POOL_WFLR = Decimal(1000)   # これより薄いプールは価格が歪みやすいので使わない
USDT0 = "0xe7cd86e13ac4309349f30b3435a9d337750fc82d"
USD_PEGGED = {"0x6cd3a5ba46fa254d4d2e3c2b37350ae337e94a0f"}  # CDP Dollar: 1ドル × USDT0/WFLR のレート

# クリプタクトに価格があるトークン (画面のトークン設定の組み込みと同じ)。これらは円換算しない
PRICED = {
    NATIVE, WFLR,
    "0xe7cd86e13ac4309349f30b3435a9d337750fc82d",  # USDT0
    "0xfbda5f676cb37624f28265a144a48b0d6e87d3b6",  # USDC.e
    "0x0b38e83b86d491735feaa0a791f65c2b99535396",  # USDT
    "0xe6505f92583103af7ed9974dec451a7af4e3a3be",  # JOULE
    "0x1502fa4be69d526124d453619276faccab275d3d",  # WETH
    "0xd38220cff996a73e9110aaca64e02d581b83a0cd",  # KII (JOULE と同一扱い → JOULE の時価)
}
FLR_LIKE = {NATIVE, WFLR}


def _ts(row) -> int:
    dt = datetime.strptime(row["timestamp"], "%Y/%m/%d %H:%M:%S").replace(tzinfo=JST)
    return int(dt.timestamp())


def _fmt(d: Decimal) -> str:
    """有効数字10桁で、指数表記にせず出す (極小価格のトークンも0に丸めない)。"""
    s = format(Decimal(f"{d:.10g}"), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


class Pricer:
    def __init__(self, client: httpx.AsyncClient):
        self.c = client
        self.sem = asyncio.Semaphore(6)
        self.days: dict = {}       # (pair, gran, yyyymmdd) -> ohlcv list
        self.sflr: dict = {}       # block -> rate
        self.hops: dict = {}       # tx -> WFLR量
        self.decs: dict = {}       # token -> decimals
        self.pool_cache: dict = {} # token -> [(kind, pool, name)]
        self.log_cache: dict = {}  # (pool, topic, bucket) -> log

    # ---- bitbank ----
    async def _candles(self, pair: str, gran: str, day: str) -> list:
        key = (pair, gran, day)
        if key not in self.days:
            async def load():
                async with self.sem:
                    try:
                        r = await self.c.get(f"{BITBANK}/{pair}/candlestick/{gran}/{day}", timeout=20)
                        j = r.json()
                        if j.get("success") == 1:
                            return j["data"]["candlestick"][0]["ohlcv"]
                    except Exception:
                        pass
                    return []
            self.days[key] = asyncio.ensure_future(load())
        return await self.days[key]

    async def jpy(self, pair: str, ts: int) -> Optional[tuple]:
        """bitbank の終値 (直前の出来高ありの1分足 → 無ければ1時間足)。(価格, 根拠)"""
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
        ms = ts * 1000
        mins = await self._candles(pair, "1min", day)
        best = None
        for c in mins:
            if c[5] <= ms and float(c[4]) > 0:
                best = c
            elif c[5] > ms:
                break
        if best:
            return Decimal(best[3]), f"bitbank {pair.upper().replace('_', '/')} {best[3]}"
        hours = await self._candles(pair, "1hour", day)
        best = None
        for c in hours:
            if c[5] <= ms:
                best = c
        if not best and hours:
            best = hours[0]
        if best:
            return Decimal(best[3]), f"bitbank {pair.upper().replace('_', '/')}(1h) {best[3]}"
        return None

    # ---- sFLR の交換比率 ----
    async def sflr_rate(self, block: int) -> Optional[Decimal]:
        if block in self.sflr:
            return self.sflr[block]
        rate = None
        step = 20000
        hi = block
        for _ in range(15):  # 最大30万ブロック (約1週間) 遡る
            lo = max(hi - step, 0)
            j = await get_json(self.c, BLOCKSCOUT_V1, {
                "module": "logs", "action": "getLogs", "address": SFLR, "topic0": SFLR_SUBMITTED,
                "fromBlock": str(lo), "toBlock": str(hi)})
            res = (j or {}).get("result") or []
            if res:
                data = res[-1]["data"][2:]
                flr, shares = int(data[:64], 16), int(data[64:128], 16)
                if shares:
                    rate = Decimal(flr) / Decimal(shares)
                break
            if lo == 0:
                break
            hi = lo
        self.sflr[block] = rate
        return rate

    # ---- 同じtx内で経由した WFLR の量 ----
    async def wflr_hop(self, tx: str, wallets: set) -> Optional[Decimal]:
        if tx in self.hops:
            return self.hops[tx]
        amt = None
        j = await get_json(self.c, f"{BLOCKSCOUT_V2}/transactions/{tx}/token-transfers")
        vals = []
        for it in (j or {}).get("items", []):
            tok = ((it.get("token") or {}).get("address_hash") or "").lower()
            frm = ((it.get("from") or {}).get("hash") or "").lower()
            to = ((it.get("to") or {}).get("hash") or "").lower()
            value = (it.get("total") or {}).get("value")
            if tok == WFLR and value and frm not in wallets and to not in wallets:
                vals.append(Decimal(int(value)) / Decimal(10 ** 18))
        if vals:
            amt = max(vals)  # プールからプールへ渡った WFLR (中継額)
        self.hops[tx] = amt
        return amt

    # ---- RPC (現在の状態の読み取り: プールの発見・トークンの桁数) ----
    async def call(self, to: str, data: str) -> Optional[str]:
        delay = 1.0
        for _ in range(5):
            try:
                async with self.sem:
                    r = await self.c.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                                     "params": [{"to": to, "data": data}, "latest"]}, timeout=20)
                if r.status_code == 200:
                    res = r.json().get("result")
                    return res if res and res != "0x" else None
                if r.status_code not in (429, 500, 502, 503, 504):
                    return None
            except (httpx.TransportError, ValueError):
                pass
            await asyncio.sleep(delay)
            delay *= 2
        return None

    async def decimals(self, token: str) -> int:
        if token not in self.decs:
            res = await self.call(token, "0x313ce567")
            self.decs[token] = int(res, 16) if res else 18
        return self.decs[token]

    async def pools_for(self, token: str) -> list:
        """token/WFLR のプールを、流動性 (プール内の WFLR) の厚い順に返す。"""
        if token in self.pool_cache:
            return await self.pool_cache[token]

        async def find():
            a, b = sorted([token, WFLR])
            cands = []
            for name, fac in V2_FACTORIES.items():
                res = await self.call(fac, "0xe6a43905" + token[2:].rjust(64, "0") + WFLR[2:].rjust(64, "0"))
                if res and int(res, 16):
                    cands.append(("v2", "0x" + res[-40:], name))
            for name, fac in V3_FACTORIES.items():
                for fee in V3_FEES:
                    res = await self.call(fac, "0x1698ee82" + a[2:].rjust(64, "0") + b[2:].rjust(64, "0") + hex(fee)[2:].rjust(64, "0"))
                    if res and int(res, 16):
                        cands.append(("v3", "0x" + res[-40:], f"{name} {fee / 10000:g}%"))
            out = []
            for kind, pool, name in cands:
                bal = await self.call(WFLR, "0x70a08231" + pool[2:].rjust(64, "0"))
                liq = Decimal(int(bal, 16)) / Decimal(10 ** 18) if bal else Decimal(0)
                if liq >= MIN_POOL_WFLR:
                    out.append((liq, kind, pool, name))
            out.sort(reverse=True)
            return [(k, p, n) for _, k, p, n in out]

        self.pool_cache[token] = asyncio.ensure_future(find())
        return await self.pool_cache[token]

    async def _last_log(self, pool: str, topic: str, block: int) -> Optional[dict]:
        """block 以前で最後のイベント (1時間程度の単位でキャッシュ)。最大約1週間遡る。"""
        bucket = block // 1800
        key = (pool, topic, bucket)
        if key in self.log_cache:
            return await self.log_cache[key]

        async def find():
            hi = (bucket + 1) * 1800
            for _ in range(15):
                lo = max(hi - 20000, 0)
                j = await get_json(self.c, BLOCKSCOUT_V1, {"module": "logs", "action": "getLogs", "address": pool,
                                                           "topic0": topic, "fromBlock": str(lo), "toBlock": str(hi)})
                res = (j or {}).get("result") or []
                if res:
                    return res[-1]
                if lo == 0:
                    break
                hi = lo
            return None

        self.log_cache[key] = asyncio.ensure_future(find())
        return await self.log_cache[key]

    async def pool_price_wflr(self, token: str, block: int) -> Optional[tuple]:
        """プールの直前の価格で、token 1枚が何 WFLR か。(価格, 根拠)"""
        dt, dw = await self.decimals(token), 18
        token_is_0 = token < WFLR
        for kind, pool, name in await self.pools_for(token):
            log = await self._last_log(pool, SYNC_TOPIC if kind == "v2" else SWAP_V3_TOPIC, block)
            if not log:
                continue
            data = log["data"][2:]
            at = int(log["blockNumber"], 16)
            if kind == "v2":
                r0, r1 = int(data[:64], 16), int(data[64:128], 16)
                rt, rw = (r0, r1) if token_is_0 else (r1, r0)
                if not rt or not rw:
                    continue
                price = (Decimal(rw) / Decimal(10 ** dw)) / (Decimal(rt) / Decimal(10 ** dt))
            else:
                sqrt_p = Decimal(int(data[128:192], 16)) / Decimal(2 ** 96)
                p1_per_0 = sqrt_p * sqrt_p  # 生の単位
                if not p1_per_0:
                    continue
                if token_is_0:   # token0 = token, token1 = WFLR
                    price = p1_per_0 * Decimal(10 ** dt) / Decimal(10 ** dw)
                else:            # token0 = WFLR, token1 = token
                    price = (Decimal(1) / p1_per_0) * Decimal(10 ** dt) / Decimal(10 ** dw)
            return price, f"{name} pool {pool[:8]}…@{at} {_fmt(price)} WFLR"
        return None

    async def pool_jpy(self, token: str, row) -> Optional[tuple]:
        """ルール3: プールの価格 × FLR/JPY (ドル連動は 1ドル × USDT0 のレート)。"""
        block = row.get("block")
        if not block:
            return None
        block = int(block)
        if token in USD_PEGGED:
            p = await self.pool_price_wflr(USDT0, block)
            label = "USDペッグ × "
        else:
            p = await self.pool_price_wflr(token, block)
            label = ""
        if not p:
            return None
        f = await self.jpy("flr_jpy", _ts(row))
        if not f:
            return None
        return p[0] * f[0], f"{label}{p[1]} × {f[1]}"

    # ---- 1トークンの円価格 (ペッグ・交換比率で直接決まるもの) ----
    async def direct(self, token: str, row) -> Optional[tuple]:
        ts = _ts(row)
        if token in FLR_LIKE:
            return await self.jpy("flr_jpy", ts)
        if token == FXRP:
            x = await self.jpy("xrp_jpy", ts)
            return (x[0], f"FXRP=XRP 1:1 × {x[1]}") if x else None
        if token == SFLR and row.get("block"):
            rate = await self.sflr_rate(int(row["block"]))
            f = await self.jpy("flr_jpy", ts)
            if rate and f:
                return rate * f[0], f"sFLR rate {_fmt(rate)} FLR @block {row['block']} × {f[1]}"
        return None


def _is_swap(row) -> bool:
    return row.get("action") == "SELL" and row.get("counter") not in ("JPY", "", None) and row.get("price")


# 履歴ファイルに保存する円価格の使い回し。判定のコードを変えたら上げる (古い結果を使わないため)
CACHE_VERSION = 1
# この日数より新しい行は保存しない (当日のローソク足など、後から値が確定するものがあるため)
CACHE_MIN_AGE = 2 * 86400


def _cache_key(r: dict, wallets: set) -> str:
    """行の円価格は、これらだけで決まる (価格は過去のデータなので、同じ入力なら同じ答えになる)。"""
    fields = [r.get(k) for k in ("tx_hash", "timestamp", "block", "action", "base_token", "counter_token",
                                 "counter", "volume", "counter_volume", "price")]
    return "|".join(str(x) for x in fields) + "|" + ",".join(sorted(wallets))


async def price_rows(client: httpx.AsyncClient, rows: list, wallets: set, on_progress=None,
                     cache: Optional[dict] = None) -> None:
    """
    出力対象・要確認の行のうち、円価格が必要になりうるものに jpy を付ける。
    cache: 履歴ファイルの円価格 ({キー: jpy})。あればそれを先に引き、取れた結果をここに足す。
    取れなかった行は保存しない (一時的な通信エラーを覚えてしまわないため)。
    """
    p = Pricer(client)
    if cache is not None and cache.get("_version") != CACHE_VERSION:
        cache.clear()
        cache["_version"] = CACHE_VERSION
    store = cache.setdefault("rows", {}) if cache is not None else None
    now = datetime.now(timezone.utc).timestamp()
    targets = []
    for r in rows:
        # 要確認の行には付けない: 多くは勝手に届いたミームコインで、取引の無いプールを遡って
        # 探すのに時間がかかる。出力に切り替えた行は、画面が「円価格なし」と知らせる
        if r.get("status") != "export":
            continue
        b, c = r.get("base_token"), r.get("counter_token")
        if _is_swap(r):
            if b not in PRICED and c not in PRICED:
                targets.append(r)  # 価格の無いトークン同士 → 円を挟んだ2行になる
        elif b and b not in PRICED:
            targets.append(r)      # 報酬・差益・差損など、主軸の円価格が要る行

    done = 0

    async def one(r):
        nonlocal done
        try:
            key = _cache_key(r, wallets) if store is not None else None
            if key in (store or {}):
                r["jpy"] = dict(store[key])
                return
            b, c = r.get("base_token"), r.get("counter_token")
            if _is_swap(r):
                sv = Decimal(r["volume"])
                rv = Decimal(r["counter_volume"]) if r.get("counter_volume") else sv * Decimal(r["price"])
                total, basis = None, ""
                # 片方がペッグ・交換比率で決まるなら、そちらの価値で両方を決める
                for tok, amt in ((b, sv), (c, rv)):
                    d = await p.direct(tok, r)
                    if d:
                        total, basis = d[0] * amt, d[1]
                        break
                if total is None:
                    hop = await p.wflr_hop(r["tx_hash"], wallets)
                    if hop:
                        f = await p.jpy("flr_jpy", _ts(r))
                        if f:
                            total, basis = hop * f[0], f"WFLR経由 {_fmt(hop)} WFLR × {f[1]}"
                if total is None:
                    for tok, amt in ((b, sv), (c, rv)):
                        d = await p.pool_jpy(tok, r)
                        if d:
                            total, basis = d[0] * amt, d[1]
                            break
                if total is not None and sv > 0 and rv > 0:
                    r["jpy"] = {"base": _fmt(total / sv), "counter": _fmt(total / rv), "basis": basis}
            else:
                d = await p.direct(b, r) or await p.pool_jpy(b, r)
                if d:
                    r["jpy"] = {"base": _fmt(d[0]), "basis": d[1]}
            if store is not None and r.get("jpy") and now - _ts(r) > CACHE_MIN_AGE:
                store[key] = dict(r["jpy"])
        finally:
            done += 1
            if on_progress:
                on_progress(done, len(targets))

    await asyncio.gather(*(one(r) for r in targets))
