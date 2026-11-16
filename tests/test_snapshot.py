"""
スナップショットテスト。

「税務的に正しい」ことの保証ではなく、リファクタや修正で
出力が意図せず変わっていないかを検出するための網。

録ったデータ (tests/fixtures/) と分類結果 (tests/snapshots/) は個人のウォレットの
履歴なので公開しない。無い環境ではスキップになる。自分のウォレットで試すとき:
    ./venv/bin/python tests/record_fixtures.py <ウォレットアドレス>
    ./venv/bin/python -m pytest tests/ -q     (初回は控えを作ってスキップ)

意図的に出力を変えたときの更新:
    UPDATE_SNAPSHOTS=1 ./venv/bin/python -m pytest tests/ -q
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"

CASES = {
    "full_history": {"start_date": None, "end_date": None},
    "april_2026": {"start_date": "2026-04-01", "end_date": "2026-04-30"},
}


def canonicalize(result: dict) -> dict:
    """行順に依存しない比較のため、決定的な順序でソートして返す。"""
    rows = sorted(
        result["rows"],
        key=lambda r: (
            r["timestamp"],
            r["tx_hash"],
            r["base"],
            r["action"],
            str(r["volume"]),
            r["comment"],
        ),
    )
    return {
        "total": result["total"],
        "export_count": result["export_count"],
        "skip_count": result["skip_count"],
        "review_count": result["review_count"],
        "rows": rows,
    }


def run_pipeline(wallet: str, start_date, end_date) -> dict:
    import main

    return asyncio.get_event_loop().run_until_complete(
        main.get_transactions(wallet=wallet, start_date=start_date, end_date=end_date)
    )


@pytest.mark.parametrize("case_name", CASES.keys())
def test_snapshot(case_name, recorded_wallet, patched_client):
    # recorded_wallet / patched_client は、録ったデータが無ければここに来る前にスキップされる
    dates = CASES[case_name]
    actual = canonicalize(run_pipeline(recorded_wallet, **dates))

    snapshot_path = SNAPSHOT_DIR / f"{case_name}.json"
    # 録ったデータはあるが分類結果の控えが無いときは、控えを作ってスキップ (自分のウォレット用に作り直す)
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not snapshot_path.exists():
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(actual, ensure_ascii=False, indent=1))
        pytest.skip(f"snapshot written: {snapshot_path.name}")

    expected = json.loads(snapshot_path.read_text())

    assert actual["total"] == expected["total"]
    assert actual["export_count"] == expected["export_count"]
    assert actual["skip_count"] == expected["skip_count"]
    assert actual["review_count"] == expected["review_count"]

    for i, (a, e) in enumerate(zip(actual["rows"], expected["rows"])):
        assert a == e, f"row {i} differs:\nactual:   {a}\nexpected: {e}"
