"""
履歴ファイル (続きだけ取得) のテスト。通信はしない (get_json を偽物に差し替える)。

確かめること: 保存済み + 新しい分 の結果が、全履歴を取り直した結果と同じ並び・同じ中身になること。
"""

import asyncio
import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from flare_tax import blockscout as bs
from flare_tax import history as hist

PAGE = 50
NEWEST = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def make_txs(n=400):
    """30分おきの通常 tx (新しい順)。400件 = 約8日分。"""
    return [{"hash": f"0x{i:04x}", "timestamp": (NEWEST - timedelta(minutes=30 * i)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")}
            for i in range(n)]


def make_transfers(n=400):
    return [{"transaction_hash": f"0x{i // 2:04x}", "log_index": i % 2, "token": {"address_hash": "0xAA"},
             "total": {"value": str(i)}, "from": {"hash": "0xF"}, "to": {"hash": "0xT"},
             "timestamp": (NEWEST - timedelta(minutes=30 * i)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")}
            for i in range(n)]


class FakeApi:
    """items を新しい順に 50件ずつ返す偽物。next_page_params は開始位置。"""

    def __init__(self, items):
        self.items = items
        self.calls = 0

    async def get_json(self, client, url, params=None, **kw):
        self.calls += 1
        start = int((params or {}).get("start", 0))
        chunk = self.items[start:start + PAGE]
        nxt = {"start": start + PAGE} if start + PAGE < len(self.items) else None
        return {"items": chunk, "next_page_params": nxt}


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def fetch(api, monkeypatch, fn, saved=None):
    monkeypatch.setattr(bs, "get_json", api.get_json)
    return run(fn(None, "0xabc", None, saved=saved))


@pytest.mark.parametrize("make,fn", [(make_txs, bs.fetch_flr_transactions),
                                     (make_transfers, bs.fetch_token_transfers)])
class TestIncremental:
    def test_same_as_full(self, make, fn, monkeypatch):
        a = make()
        for cut in (0, 1, 120, 399):  # 新しい側 cut 件がまだ保存されていない
            api = FakeApi(a)
            got, trunc = fetch(api, monkeypatch, fn, saved=a[cut:])
            assert got == a and trunc is False

    def test_saved_empty(self, make, fn, monkeypatch):
        a = make()
        got, trunc = fetch(FakeApi(a), monkeypatch, fn, saved=[])
        assert got == a and trunc is False

    def test_nothing_new_stops_early(self, make, fn, monkeypatch):
        a = make(1000)  # 30分おき = 約21日分 (21ページ)
        api = FakeApi(a)
        got, _ = fetch(api, monkeypatch, fn, saved=a)
        assert got == a
        # 直近14日 (30分おき = 672件 = 14ページ弱) を重ねたら止まる。全21ページは取らない
        assert api.calls <= 15

    def test_keeps_going_until_overlap_passed(self, make, fn, monkeypatch):
        a = make(1000)
        api = FakeApi(a)
        fetch(api, monkeypatch, fn, saved=a)
        # 保存済みに当たっても、14日を抜けるまでは止まらない (14ページ目で初めて境目より古くなる)
        assert api.calls >= 14

    def test_late_item_in_overlap_is_picked_up(self, make, fn, monkeypatch):
        a = make()
        saved = a[:60] + a[61:]          # 直近3日のうち1件だけ保存済みに無い (取り込みが遅れていた)
        got, _ = fetch(FakeApi(a), monkeypatch, fn, saved=saved)
        assert got == a

    def test_new_item_wins_over_saved(self, make, fn, monkeypatch):
        a = make()
        saved = json.loads(json.dumps(a))
        saved[10]["stale"] = True        # 保存済みの同じキーは捨てて、新しく取った方を使う
        got, _ = fetch(FakeApi(a), monkeypatch, fn, saved=saved)
        assert got == a

    def test_error_midway_is_truncated(self, make, fn, monkeypatch):
        a = make()
        api = FakeApi(a)
        real = api.get_json

        async def flaky(client, url, params=None, **kw):
            if int((params or {}).get("start", 0)) >= PAGE:
                return None
            return await real(client, url, params)

        monkeypatch.setattr(bs, "get_json", flaky)
        got, trunc = run(fn(None, "0xabc", None, saved=a[300:]))
        assert trunc is True                   # 呼び出し側は、このウォレットを履歴ファイルに保存しない
        assert got[-len(a[300:]):] == a[300:]  # 計算用には保存済みが残る


def test_transfer_key_separates_same_log_rows():
    a = make_transfers(4)
    b = dict(a[0], total={"value": "999"})  # 同じ (tx, log_index) でも量が違えば別の行
    assert bs.transfer_key(a[0]) != bs.transfer_key(b)


def test_internal_not_in_file_format():
    data = hist.pack({"0xabc": {"flr_txs": [], "token_transfers": [], "extras": {}}})
    doc = json.loads(gzip.decompress(data))
    assert "internal_txs" not in doc["wallets"]["0xabc"]


# ---- ファイルの読み書き ----

def sample():
    return {"0xabc": {"flr_txs": make_txs(5), "token_transfers": make_transfers(5),
                      "extras": {"oft_events": {"0x1": {"dir": "sent", "eid": 30101, "amount": 5}}}}}


def test_roundtrip():
    w = sample()
    got, pc = hist.unpack(hist.pack(w, {"rows": {"k": {"base": "1"}}}))
    assert got == w and pc == {"rows": {"k": {"base": "1"}}}


def test_checksum_mismatch():
    doc = json.loads(gzip.decompress(hist.pack(sample())))
    doc["wallets"]["0xabc"]["flr_txs"][0]["hash"] = "0xevil"
    with pytest.raises(hist.HistoryError, match="壊れている"):
        hist.unpack(gzip.compress(json.dumps(doc).encode()))


def test_other_app_and_version():
    doc = json.loads(gzip.decompress(hist.pack(sample())))
    for patch in ({"app": "other"}, {"version": 99}):
        with pytest.raises(hist.HistoryError):
            hist.unpack(gzip.compress(json.dumps({**doc, **patch}).encode()))


def test_broken_gzip_and_json():
    with pytest.raises(hist.HistoryError):
        hist.unpack(b"not gzip at all")
    with pytest.raises(hist.HistoryError):
        hist.unpack(gzip.compress(b"{broken"))


def test_bad_price_cache_is_dropped_not_fatal():
    doc = json.loads(gzip.decompress(hist.pack(sample(), {"rows": {}})))
    doc["price_cache"] = {"rows": {"tampered": {}}}
    got, pc = hist.unpack(gzip.compress(json.dumps(doc).encode()))
    assert got == sample() and pc == {}


def test_store_expires(monkeypatch):
    t = hist.store("f.json.gz", b"x")
    assert hist.load(t) == ("f.json.gz", b"x")
    monkeypatch.setattr(hist.time, "time", lambda: 10 ** 11)
    assert hist.load(t) is None


# ---- 録画データで: 履歴ファイルから続けた結果 = 全履歴の結果 ----

def test_resume_from_file_equals_full(recorded_wallet, patched_client):
    import main
    from test_snapshot import canonicalize

    full = run(main._run(recorded_wallet, None, None, [], make_history=True))
    token = full["history"]["token"]
    assert full["history"]["used_file"] is False
    _, data = hist.load(token)
    saved, pc = hist.unpack(data)
    again = run(main._run(recorded_wallet, None, None, [], saved, pc, make_history=True))
    assert again["history"]["used_file"] is True
    assert again["history"]["added"] == 0
    assert canonicalize(again) == canonicalize(full)
    # 保存済みの分だけで分類しても、同じ行数になる
    assert again["history"]["wallets"][0]["txs"] == full["history"]["wallets"][0]["txs"]


def test_endpoints(recorded_wallet, patched_client, monkeypatch):
    from fastapi.testclient import TestClient
    import main

    c = TestClient(main.app)
    q = {"wallet": recorded_wallet}
    first = c.post("/transactions", params=q)
    assert first.status_code == 200 and first.json()["history"]["used_file"] is False
    h = first.json()["history"]
    got = c.get(f"/history/{h['token']}")
    assert got.status_code == 200 and got.headers["content-type"] == "application/gzip"
    assert h["filename"] in got.headers["content-disposition"] and len(got.content) == h["bytes"]

    # 履歴ファイルを載せる / 全履歴を取り直す (refresh=1 なら本文は使わない)
    second = c.post("/transactions", params=q, content=got.content, headers={"content-type": "application/octet-stream"})
    assert second.json()["history"]["used_file"] is True
    refreshed = c.post("/transactions", params={**q, "refresh": 1}, content=got.content)
    assert refreshed.json()["history"]["used_file"] is False
    assert second.json()["rows"] == first.json()["rows"] == refreshed.json()["rows"]

    # 壊れたファイルは 400 (画面にそのまま出せる日本語)、無いトークンは 404
    bad = c.post("/transactions", params=q, content=b"garbage")
    assert bad.status_code == 400 and "履歴ファイル" in bad.json()["detail"]
    assert c.get("/history/nothing").status_code == 404

    # 本文の上限
    monkeypatch.setattr(main.hist, "MAX_BODY", 10)
    assert c.post("/transactions", params=q, content=b"x" * 100).status_code == 413


# ---- 重なりの期間 ----

def test_overlap_is_14_days():
    assert bs.OVERLAP == timedelta(days=14)


# ---- service: 履歴ファイルの検査・使えないファイル・取り直しの比較 (通信なし) ----

from flare_tax import service  # noqa: E402

A1 = "0x" + "a" * 40
A2 = "0x" + "b" * 40
A3 = "0x" + "c" * 40


def two_wallet_file():
    w = {"flr_txs": make_txs(5), "token_transfers": make_transfers(5), "extras": {}}
    return hist.pack({A1: w, A3: {"flr_txs": [], "token_transfers": [], "extras": {}}})


def test_inspect_history():
    got = service.inspect_history(two_wallet_file(), [A1, A2.upper().replace("0X", "0x")])
    assert got["ok"] is True and got["saved_at"]
    one, two = got["wallets"]
    assert one["in_file"] is True and one["txs"] == 5 and one["transfers"] == 5
    assert one["latest"] == make_txs(1)[0]["timestamp"]
    assert two == {"address": A2.upper().replace("0X", "0x"), "in_file": False}
    assert got["others"] == 1  # A3 は今回の登録に無い


def test_inspect_history_broken_is_not_exception():
    for bad in (b"garbage", gzip.compress(b"{broken"), b""):
        got = service.inspect_history(bad, [A1])
        assert got["ok"] is False and got["message"]


def test_service_has_no_fastapi():
    import pathlib
    src = pathlib.Path(service.__file__).read_text(encoding="utf-8")
    assert "fastapi" not in src and "starlette" not in src


class FakeWallet:
    """_fetch_wallet の偽物。どのウォレットも同じ内容を返し、渡された saved を記録する。"""

    def __init__(self, txs, transfers):
        self.txs, self.transfers, self.saved_args = txs, transfers, []

    async def __call__(self, client, wallet, track, saved=None, progress=None):
        self.saved_args.append(saved)
        txs, transfers = json.loads(json.dumps(self.txs)), json.loads(json.dumps(self.transfers))
        return {"wallet": wallet, "flr_txs": txs, "token_transfers": transfers, "internal_txs": [],
                "fxrp_mint_txs": set(), "oft_events": {}, "warnings": [], "notices": [],
                "to_save": {"flr_txs": txs, "token_transfers": transfers, "extras": {}},
                "from_file": saved is not None, "added_txs": 0, "added_transfers": 0}


@pytest.fixture
def fake_run(monkeypatch):
    """取得・分類・円価格を偽物にして、service.run の段取りだけを動かす。"""
    fake = FakeWallet(make_txs(5), make_transfers(5))

    async def no_price(*a, **k):
        return None

    monkeypatch.setattr(service, "_fetch_wallet", fake)
    monkeypatch.setattr(service, "price_rows", no_price)
    monkeypatch.setattr(service, "build_result", lambda *a, warnings=None, **k: {"rows": [], "warnings": warnings or []})
    return fake


def file_of(txs, transfers, wallet=A1):
    return hist.pack({wallet: {"flr_txs": txs, "token_transfers": transfers, "extras": {}}})


def test_broken_file_falls_back_to_full(fake_run):
    got = run(service.run([A1], history_bytes=b"garbage", strict=False))
    assert fake_run.saved_args == [None]  # 履歴ファイルを使わず全履歴の経路
    assert got["result"]["history"]["used_file"] is False
    assert any("全履歴を取得しました" in w for w in got["result"]["warnings"])
    assert got["history_bytes"]
    # 既定 (strict) では今までどおりエラー
    with pytest.raises(hist.HistoryError):
        run(service.run([A1], history_bytes=b"garbage"))


def test_good_file_is_used(fake_run):
    got = run(service.run([A1], history_bytes=file_of(make_txs(5), make_transfers(5)), strict=False))
    assert fake_run.saved_args[0] is not None and got["result"]["history"]["used_file"] is True
    assert got["result"]["warnings"] == []


def test_compare_same(fake_run):
    data = file_of(make_txs(5), make_transfers(5))
    h = run(service.run([A1], history_bytes=data, refresh=True))["result"]["history"]
    assert h["used_file"] is False
    assert h["compare"] == {"checked": True, "wallets": [{"address": A1, "only_new": 0, "only_file": 0}], "same": True}


def test_compare_only_new_and_only_file(fake_run):
    txs, transfers = make_txs(5), make_transfers(5)
    # 取り直しにだけある: ファイルから古い tx を1件抜く (最新ではない)。
    # ファイルにだけある: ファイルに、取り直しには無い古い tx を足す
    ghost = {"hash": "0xghost", "timestamp": "2020-01-01T00:00:00.000000Z"}
    data = file_of(txs[:3] + txs[4:] + [ghost], transfers)
    c = run(service.run([A1], history_bytes=data, refresh=True))["result"]["history"]["compare"]
    assert c["same"] is False
    assert c["wallets"] == [{"address": A1, "only_new": 1, "only_file": 1}]


def test_compare_ignores_wallet_not_in_file_and_newer_than_saved(fake_run):
    # ファイルの最新より新しい item は比べない (保存後に増えた分)
    data = file_of(make_txs(5)[1:], make_transfers(5))
    c = run(service.run([A1, A2], history_bytes=data, refresh=True))["result"]["history"]["compare"]
    assert [w["address"] for w in c["wallets"]] == [A1] and c["wallets"][0]["only_new"] == 0
