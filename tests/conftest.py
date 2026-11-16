"""
テスト共通設定。

録画済みフィクスチャ (tests/fixtures/) を httpx.MockTransport で返すことで、
ネットワークに出ずに main.get_transactions() の全パイプラインを実行する。
"""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

FIXTURE_DIR = Path(__file__).parent / "fixtures"


SKIP_REASON = "録ったデータがありません。tests/record_fixtures.py で自分のウォレットを録ってください"


def _require_fixtures() -> None:
    """録ったデータ (tests/fixtures/) が無い環境では、頼るテストを失敗ではなくスキップにする。"""
    if not (FIXTURE_DIR / "wallet.txt").exists() or not (FIXTURE_DIR / "internal_txs.json").exists():
        pytest.skip(SKIP_REASON)


def _load_pages(prefix: str, base_params: dict) -> dict:
    """
    ページ送りフィクスチャを {正規化パラメータ: レスポンスJSON} の辞書に組み立てる。
    ページNのリクエストパラメータは「ページN-1の next_page_params + 固定パラメータ」。
    """
    pages = {}
    params = dict(base_params)
    for page_no in range(200):
        path = FIXTURE_DIR / f"{prefix}_page{page_no}.json"
        if not path.exists():
            break
        data = json.loads(path.read_text())
        key = tuple(sorted((k, str(v)) for k, v in params.items()))
        pages[key] = data
        next_params = data.get("next_page_params")
        if not next_params:
            break
        params = {**base_params, **next_params}
    return pages


@pytest.fixture(scope="session")
def recorded_wallet() -> str:
    _require_fixtures()
    return (FIXTURE_DIR / "wallet.txt").read_text().strip()


@pytest.fixture(scope="session")
def mock_transport() -> httpx.MockTransport:
    _require_fixtures()
    tx_pages = _load_pages("transactions", {})
    transfer_pages = _load_pages("token_transfers", {"type": "ERC-20"})
    internal_data = json.loads((FIXTURE_DIR / "internal_txs.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        params = tuple(sorted((k, str(v)) for k, v in request.url.params.items()))
        path = request.url.path

        if path.endswith("/transactions"):
            page = tx_pages.get(params)
        elif path.endswith("/token-transfers"):
            page = transfer_pages.get(params)
        elif dict(params).get("action") == "txlistinternal":
            return httpx.Response(200, json=internal_data)
        else:
            return httpx.Response(404, json={"message": f"unmocked path: {path}"})

        if page is None:
            return httpx.Response(404, json={"message": f"unmocked params: {params}"})
        return httpx.Response(200, json=page)

    return httpx.MockTransport(handler)


@pytest.fixture
def patched_client(monkeypatch, mock_transport):
    """main.py 内の httpx.AsyncClient がモックトランスポートを使うようにする。"""
    import main

    real_client = httpx.AsyncClient

    def make_client(**kwargs):
        kwargs["transport"] = mock_transport
        return real_client(**kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", make_client)
    return make_client
