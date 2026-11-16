"""
httpx の代用 (ブラウザの Pyodide 専用)。

flare_tax は httpx.AsyncClient の get / post と TransportError、応答の status_code / headers / json() しか使わない。
この範囲だけを、ブラウザの fetch で実装する。Pyodide のファイルシステムに「httpx.py」という名前で置いて使う。
(PC の Python では本物の httpx を使うので、このファイルは使われない)
"""

import json as _json
from urllib.parse import urlencode

import js
from pyodide.http import pyfetch


class TransportError(Exception):
    """通信そのものの失敗 (つながらない・タイムアウト・CORS など)。"""


class _Headers(dict):
    """ヘッダー名の大文字小文字を区別しない辞書 (本物の httpx と同じ。"Retry-After" で引ける)。"""

    def __init__(self, items):
        super().__init__((str(k).lower(), v) for k, v in items.items())

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def __getitem__(self, key):
        return super().__getitem__(str(key).lower())

    def __contains__(self, key):
        return super().__contains__(str(key).lower())


class Response:
    def __init__(self, status, headers, text):
        self.status_code = status
        self.headers = _Headers(headers)
        self.text = text

    def json(self):
        return _json.loads(self.text)


def _q(v):
    # httpx と同じ: None は空文字、真偽値は小文字の true / false (Blockscout の next_page_params に合わせる)
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


class AsyncClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, timeout=None, headers=None):
        if params:
            url += ("&" if "?" in url else "?") + urlencode({k: _q(v) for k, v in params.items()})
        kw = {"method": "GET"}
        if headers:
            kw["headers"] = dict(headers)
        return await self._req(url, timeout, **kw)

    async def post(self, url, json=None, timeout=None, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        return await self._req(url, timeout, method="POST", body=_json.dumps(json), headers=h)

    async def _req(self, url, timeout, **kw):
        try:
            if timeout:
                kw["signal"] = js.AbortSignal.timeout(int(float(timeout) * 1000))
            r = await pyfetch(url, **kw)
            return Response(r.status, dict(r.headers), await r.string())
        except Exception as e:
            raise TransportError(str(e))
