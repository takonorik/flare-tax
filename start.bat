@echo off
rem UTF-8 (must come before any non-ASCII line)
chcp 65001 >nul
rem Flare Tax for Cryptact 手元起動スクリプト (ダブルクリックで起動)
rem ブラウザだけで動くので、インストールもAPIサーバーも不要です。
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python が見つかりません。python.org から Python 3 を入れてから、もう一度開いてください。
  echo ^(Python が無くても、公開されている URL を開けば使えます^)
  pause
  exit /b 1
)

rem 少し待ってからブラウザで開く (file:// では動かないので http で開く)
start "" cmd /c "timeout /t 1 >nul & start http://localhost:5500"

echo.
echo ==========================================
echo  Flare Tax for Cryptact 起動中
echo  http://localhost:5500
echo  終了するには このウィンドウで Ctrl+C
echo ==========================================
echo.
py -m http.server 5500 --bind 127.0.0.1
