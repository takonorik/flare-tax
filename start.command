#!/bin/bash
# Flare Tax for Cryptact 手元起動スクリプト (Finderでダブルクリックで起動)
# ブラウザだけで動くので、インストールもAPIサーバーも不要です。
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 が見つかりません。python.org などから Python 3 を入れてから、もう一度開いてください。"
  echo "(Python が無くても、公開されている URL を開けば使えます)"
  exit 1
fi

# 少し待ってからブラウザで開く (file:// では動かないので http で開く)
(sleep 1 && open "http://localhost:5500") &

echo ""
echo "=========================================="
echo " Flare Tax for Cryptact 起動中"
echo " http://localhost:5500"
echo " 終了するには このウィンドウで Ctrl+C"
echo "=========================================="
echo ""
python3 -m http.server 5500 --bind 127.0.0.1
