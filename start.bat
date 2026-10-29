@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem 一鍵啟動（全部在 Docker 的 Linux 容器執行）：PostgreSQL、Lavalink、yt-cipher、監控網站、介紹網站、Discord Bot
rem 設定 MUSIC_BOT_TOKEN 時另外啟動獨立音樂 Bot（compose 的 music profile）
rem 程式碼有修改時會自動重建映像；停止全部：docker compose stop（不要用 down -v，會刪除資料庫）
cd /d "%~dp0"
title farmgm 啟動器

if not exist ".env" (
    echo [錯誤] 找不到 .env，請先複製 .env.example 並填入設定
    goto :fail
)

set "APP_PORT=8000"
set "SITE_PORT=24444"
for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"APP_PORT=" /c:"SITE_PORT=" .env') do set "%%a=%%b"

docker info >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 無法連線 Docker，請先開啟 Docker Desktop
    goto :fail
)

set "PROFILE="
findstr /r /b /c:"MUSIC_BOT_TOKEN=..*" .env >nul 2>&1
if not errorlevel 1 set "PROFILE=--profile music"

echo 建置並啟動容器（第一次需要下載映像，約數分鐘）...
docker compose %PROFILE% up -d --build --wait
if errorlevel 1 (
    echo.
    echo [錯誤] 有容器沒有正常啟動，請查看：docker compose ps 與 docker compose logs 服務名稱
    echo        若日誌顯示 Migration 不是最新：先備份資料庫，再執行 docker compose run --rm web python -m alembic upgrade head
    goto :fail
)

echo.
docker compose %PROFILE% ps --format "table {{.Service}}\t{{.Status}}"
echo.
echo 監控網站：http://127.0.0.1:%APP_PORT%
echo 介紹網站：http://127.0.0.1:%SITE_PORT%
echo 查看 Bot 日誌：docker compose logs -f bot
echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1
