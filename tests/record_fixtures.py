"""
Blockscout APIのレスポンスをフィクスチャとして録画するスクリプト。

テストはネットワークに出ず、ここで保存したJSONをモックとして使う。
使い方: ./venv/bin/python tests/record_fixtures.py <ウォレットアドレス>
保存先 tests/fixtures/ には個人のウォレットの全履歴が入るので、公開しない (.gitignore 済み)。
"""

import json
import sys
from pathlib import Path

import httpx

BLOCKSCOUT_BASE = "https://flare-explorer.flare.network/api/v2"
BLOCKSCOUT_V1 = "https://flare-explorer.flare.network/api"
MAX_PAGES = 100

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def record_paginated(client: httpx.Client, url: str, base_params: dict, name: str) -> None:
    """ページ送りしながら全ページを {name}_page{N}.json に保存する。"""
    params = dict(base_params)
    for page in range(MAX_PAGES):
        resp = client.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        out = FIXTURE_DIR / f"{name}_page{page}.json"
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print(f"  {out.name}: {len(data.get('items', []))} items")
        next_params = data.get("next_page_params")
        if not next_params:
            break
        params = {**base_params, **next_params}


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("使い方: python tests/record_fixtures.py <ウォレットアドレス (0x...)>")
        print("自分のウォレットの履歴を tests/fixtures/ に録ります (ネットワークを使います)。")
        sys.exit(1)
    wallet = sys.argv[1].strip().lower()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / "wallet.txt").write_text(wallet)

    with httpx.Client() as client:
        print("native transactions:")
        record_paginated(client, f"{BLOCKSCOUT_BASE}/addresses/{wallet}/transactions", {}, "transactions")

        print("token transfers:")
        record_paginated(
            client,
            f"{BLOCKSCOUT_BASE}/addresses/{wallet}/token-transfers",
            {"type": "ERC-20"},
            "token_transfers",
        )

        print("internal transactions (v1):")
        resp = client.get(
            BLOCKSCOUT_V1,
            params={
                "module": "account",
                "action": "txlistinternal",
                "address": wallet,
                "sort": "desc",
                "page": "1",
                "offset": "10000",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        (FIXTURE_DIR / "internal_txs.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print(f"  internal_txs.json: {len(data.get('result', []))} items")


if __name__ == "__main__":
    main()
