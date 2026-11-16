#!/usr/bin/env python3
"""GitHub Pages に置くファイルを out ディレクトリにまとめる。

限定公開（アクセスコード）は GitHub の設定だけで切り替える:
  Settings → Secrets   ACCESS_CODE = アクセスコード
  Settings → Variables ACCESS_GATE = on / off
on のときだけ gate.json を置き、検索エンジンに載らないよう noindex を付ける。
コード自体はどこにも書き出さず、ハッシュだけを置く。
"""

import glob
import hashlib
import json
import os
import shutil
import sys
import unicodedata

# 画面と、ブラウザの中で動かす Python (web/worker.js が読みに来るもの)
FILES = ["index.html", ".nojekyll", "web/worker.js", "web/httpx.py"] + sorted(glob.glob("flare_tax/*.py"))
SALT = "flare-tax:"


def normalize(code: str) -> str:
    """index.html の initGate と同じ（全角入力・大文字・前後の空白を吸収）"""
    return unicodedata.normalize("NFKC", code).strip().lower()


def gate_hash(code: str) -> str:
    return hashlib.sha256((SALT + normalize(code)).encode("utf-8")).hexdigest()


def main(out: str) -> int:
    for f in FILES:
        dst = os.path.join(out, f)
        os.makedirs(os.path.dirname(dst) or out, exist_ok=True)
        shutil.copy(f, dst)

    gate_on = os.environ.get("ACCESS_GATE", "").strip().lower() == "on"
    code = os.environ.get("ACCESS_CODE", "")
    if gate_on and not normalize(code):
        print("ACCESS_GATE=on ですが ACCESS_CODE が空です。鍵なしで公開しないよう中止します。", file=sys.stderr)
        return 1

    if gate_on:
        with open(os.path.join(out, "gate.json"), "w") as f:
            json.dump({"enabled": True, "salt": SALT, "hash": gate_hash(code)}, f)
        index = os.path.join(out, "index.html")
        html = open(index, encoding="utf-8").read()
        html = html.replace("<head>", '<head>\n  <meta name="robots" content="noindex, nofollow">', 1)
        open(index, "w", encoding="utf-8").write(html)
        print("限定公開: オン（アクセスコードあり）")
    else:
        print("限定公開: オフ（誰でも閲覧可）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "_site"))
