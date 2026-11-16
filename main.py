"""
Flare Tax for Cryptact — Backend
Flare Networkの取引履歴を取得し、クリプタクトCSV形式で出力するためのAPIサーバー。

構成:
  flare_tax/blockscout.py  — Blockscout APIクライアント
  flare_tax/classify.py    — 1件単位の基本分類
  flare_tax/protocols.py   — プロトコル定義とハンドラのレジストリ (サービス追加はここ)
  flare_tax/postprocess.py — tx_hash単位のグループ後処理
  flare_tax/pipeline.py    — 取得済みデータ→行 の純粋パイプライン
  flare_tax/service.py     — 取得 → 分類 → 円価格 の段取り (FastAPI に依存しない。ブラウザの Pyodide でも使う)
  main.py                  — FastAPIエンドポイント (開発・テスト用の薄い包み。このファイル)
"""

import asyncio
from typing import Optional

import httpx  # noqa: F401  (テストが main.httpx を差し替える)
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from flare_tax import history as hist
from flare_tax import service
from flare_tax.service import ADDR_RE

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(title="Flare Tax for Cryptact", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# ---------------------------------------------------------------------------
# 取得進捗 (ローカル単一ユーザー前提のグローバル状態)
# ---------------------------------------------------------------------------
PROGRESS = dict(service._Progress().d)  # 中身は service が on_progress で更新する


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/progress")
async def progress():
    """取得中のページ/件数カウント。フロントがポーリングして表示する。"""
    return PROGRESS


def _parse_extras(wallet: str, extra_wallets) -> list:
    if not isinstance(extra_wallets, str):  # 関数として直接呼ばれた場合 (テスト)
        extra_wallets = None
    extras = []
    for a in (extra_wallets or "").replace("\n", ",").replace(" ", ",").split(","):
        a = a.strip()
        if not a:
            continue
        if not ADDR_RE.match(a):
            raise HTTPException(status_code=400, detail=f"追加アドレスの形式が正しくありません: {a}")
        if a.lower() != wallet.lower() and a.lower() not in [e.lower() for e in extras]:
            extras.append(a)
    return extras


async def _run(wallet: str, start_date, end_date, extras: list,
               saved_wallets: Optional[dict] = None, price_cache: Optional[dict] = None,
               make_history: bool = False) -> dict:
    """取得 → 分類 → 円価格 (本体は flare_tax/service.py)。進捗はこのファイルの PROGRESS に入れる。"""
    result, data = await service._run_core(
        [wallet, *extras], start_date, end_date, saved_wallets, price_cache, make_history,
        progress=service._Progress(PROGRESS.update))
    if data is not None:
        result["history"]["token"] = hist.store(result["history"]["filename"], data)
    return result


@app.get("/transactions")
async def get_transactions(
    wallet: str = Query(..., min_length=42, max_length=42, pattern="^0x[a-fA-F0-9]{40}$"),
    start_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    extra_wallets: Optional[str] = Query(None, max_length=2000),
):
    """
    Fetch and classify Flare transactions for the given wallet.

    extra_wallets: 同じ所有者としてまとめて処理する追加アドレス (Smart Account など)。カンマ区切り。
    注意: start_dateはあくまで出力の絞り込み。取得は常に全履歴。
    途中で打ち切るとLP差分計上の元本 (過去の預入) が欠けて不正確になるため。
    履歴ファイルを使わない (全履歴を取得する) 版。履歴ファイルを使うのは POST /transactions。
    """
    return await _run(wallet, start_date, end_date, _parse_extras(wallet, extra_wallets))


async def _read_body(request: Request) -> bytes:
    """本文を上限つきで読む (超えたら途中で打ち切って 413)。"""
    too_big = HTTPException(status_code=413, detail="履歴ファイルが大きすぎます。")
    try:
        if int(request.headers.get("content-length") or 0) > hist.MAX_BODY:
            raise too_big
    except ValueError:
        pass
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > hist.MAX_BODY:
            raise too_big
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/transactions")
async def post_transactions(
    request: Request,
    wallet: str = Query(..., min_length=42, max_length=42, pattern="^0x[a-fA-F0-9]{40}$"),
    start_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    extra_wallets: Optional[str] = Query(None, max_length=2000),
    refresh: Optional[int] = Query(None),
):
    """
    GET /transactions と同じ処理。本文に履歴ファイル (gzip のまま) を載せると、その続きだけを取得する。
    refresh=1 のときは本文があっても使わず、全履歴を取り直す。応答の history から最新の履歴ファイルを
    GET /history/{token} で受け取れる (ファイルは利用者の PC に保存し、サーバーには溜めない)。
    """
    extras = _parse_extras(wallet, extra_wallets)
    body = await _read_body(request)
    saved_wallets, price_cache = None, {}
    if body and not refresh:
        try:
            saved_wallets, price_cache = await asyncio.to_thread(hist.unpack, body)
        except hist.HistoryError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return await _run(wallet, start_date, end_date, extras, saved_wallets, price_cache, make_history=True)


@app.get("/history/{token}")
async def get_history(token: str):
    """直近に作った履歴ファイルを返す (メモリに1時間だけ置く)。"""
    got = hist.load(token)
    if not got:
        raise HTTPException(status_code=404, detail="履歴ファイルの保存期限が切れました。もう一度取得してください。")
    filename, data = got
    return Response(content=data, media_type="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
