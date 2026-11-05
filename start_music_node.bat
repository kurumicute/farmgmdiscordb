@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem 在這台電腦執行音樂節點（只有 Lavalink＋yt-cipher）：正式環境的 Bot 在 VPS，經 Cloudflare Tunnel 連到這裡的 Lavalink，
rem 聲音由這台電腦直接送到 Discord（VPS 會不定時停頓，在 VPS 播放會卡）。
rem 使用同一份 docker-compose.yml 與 .env，但專案名稱是 farmgm-music，Docker Desktop 會顯示成獨立的一組，可以直接按啟動／停止。
rem 不要在這台電腦啟動 farmgm 專案的 bot／web：正式環境在 VPS，同一個 Bot Token 只能在一處執行。
cd /d "%~dp0"
title farmgm 音樂節點

if not exist ".env" (
    echo [錯誤] 找不到 .env
    goto :fail
)

docker info >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 無法連線 Docker，請先開啟 Docker Desktop
    goto :fail
)

rem farmgm 專案的 lavalink 也綁 127.0.0.1:2333，兩組不能同時執行
docker compose -p farmgm stop lavalink yt-cipher >nul 2>&1

echo 啟動音樂節點（Lavalink、yt-cipher）...
docker compose -p farmgm-music up -d --wait lavalink yt-cipher
if errorlevel 1 (
    echo.
    echo [錯誤] 音樂節點沒有正常啟動，請查看：docker compose -p farmgm-music logs lavalink
    goto :fail
)

echo.
docker compose -p farmgm-music ps --format "table {{.Service}}\t{{.Status}}"
echo.
echo 音樂節點已啟動。之後可以直接在 Docker Desktop 的 farmgm-music 按啟動／停止；
echo 開著 Docker Desktop 時會自動啟動（restart: unless-stopped），手動停止後才不會自動啟動。
echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1
