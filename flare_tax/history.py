"""
履歴ファイル (取得済みの生の履歴を1つにまとめた gzip JSON) の読み書き。

チェーンの履歴は後から変わらないので、取得済みの分を利用者の PC に保存して使い回す。
分類 (build_result) は毎回全履歴に対して行うので、ここは「生データの置き場」だけを扱う。
ファイルの形と増分の止め方は README の「履歴ファイル」を参照。
"""

import gzip
import hashlib
import io
import json
import secrets
import time
from datetime import datetime, timezone

APP = "flare-tax-history"
VERSION = 1
MAX_BODY = 200 * 1024 * 1024        # 受け取る本文 (gzip) の上限
MAX_UNPACKED = 1024 * 1024 * 1024   # gzip を開いた後の上限 (圧縮爆弾よけ)

BROKEN = "履歴ファイルが壊れているか、書き換えられています。全履歴を取り直してください。"


class HistoryError(Exception):
    """画面にそのまま出せる日本語のメッセージを持つ。"""


def _checksum(obj) -> str:
    # キー順ソート・区切り固定の JSON にして SHA-256 (書き出しと読み込みで同じ文字列になるように)
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def history_filename(now=None) -> str:
    return "flare_history_" + (now or datetime.now()).strftime("%y%m%d") + ".json.gz"


def pack(wallets: dict, price_cache: dict = None, now=None) -> bytes:
    """wallets = {小文字アドレス: {"flr_txs", "token_transfers", "extras"}} を gzip JSON にする。"""
    price_cache = price_cache or {}
    doc = {
        "app": APP,
        "version": VERSION,
        "saved_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wallets": wallets,
        "price_cache": price_cache,
        "checksum": _checksum(wallets),
        # 円価格のキャッシュは壊れても捨てて取り直せばよいので、別の checksum にする
        "price_checksum": _checksum(price_cache),
    }
    return gzip.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)


def unpack(body: bytes) -> tuple:
    """gzip JSON を (wallets, price_cache) にする。壊れている・別のファイルならエラー (黙って使わない)。"""
    return unpack_full(body)[:2]


def unpack_full(body: bytes) -> tuple:
    """unpack と同じ検査をして、(wallets, price_cache, saved_at) を返す。"""
    if len(body) > MAX_BODY:
        raise HistoryError("履歴ファイルが大きすぎます。")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as f:
            raw = f.read(MAX_UNPACKED + 1)
    except (OSError, EOFError, ValueError):
        raise HistoryError(BROKEN)
    if len(raw) > MAX_UNPACKED:
        raise HistoryError("履歴ファイルが大きすぎます。")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HistoryError(BROKEN)
    if not isinstance(doc, dict) or doc.get("app") != APP:
        raise HistoryError("Flare Tax の履歴ファイルではありません。別のファイルを選んでいないか確認してください。")
    if doc.get("version") != VERSION:
        raise HistoryError("この履歴ファイルは形式が違います (新しい版か古い版で作られたものです)。全履歴を取り直してください。")
    wallets = doc.get("wallets")
    if not isinstance(wallets, dict) or doc.get("checksum") != _checksum(wallets):
        raise HistoryError(BROKEN)
    for w in wallets.values():
        if not isinstance(w, dict) or not isinstance(w.get("flr_txs"), list) \
                or not isinstance(w.get("token_transfers"), list) or not isinstance(w.get("extras", {}), dict):
            raise HistoryError(BROKEN)
    pc = doc.get("price_cache")
    if not isinstance(pc, dict) or doc.get("price_checksum") != _checksum(pc):
        pc = {}  # 円価格は取り直せるので、合わなければ使わないだけにする
    return {a.lower(): w for a, w in wallets.items()}, pc, doc.get("saved_at")


# ---- 作ったファイルを一時的にメモリに置く (ダウンロード用。ディスクには書かない) ----
KEEP_SECONDS = 3600
KEEP_COUNT = 3
_STORE: dict = {}  # token -> (期限, ファイル名, bytes)


def store(filename: str, data: bytes) -> str:
    now = time.time()
    for t in [t for t, v in _STORE.items() if v[0] < now]:
        del _STORE[t]
    while len(_STORE) >= KEEP_COUNT:
        del _STORE[min(_STORE, key=lambda t: _STORE[t][0])]
    token = secrets.token_urlsafe(16)
    _STORE[token] = (now + KEEP_SECONDS, filename, data)
    return token


def load(token: str):
    """(ファイル名, bytes)。無い・期限切れなら None。"""
    v = _STORE.get(token)
    if not v or v[0] < time.time():
        _STORE.pop(token, None)
        return None
    return v[1], v[2]
