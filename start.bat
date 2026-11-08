@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem 在這台電腦「原生」執行音樂節點（Lavalink＋yt-cipher），不需要 Docker Desktop：
rem 正式環境的 Bot 在 VPS，經 Cloudflare Tunnel（lavalink.kurumicute.com → 127.0.0.1:2333）連到這裡，聲音由這台電腦送到 Discord。
rem 兩個程式在背景執行（最小化視窗「farmgm Lavalink」「farmgm yt-cipher」），日誌在 music_node\logs；停止請執行 stop_music.bat。
rem 設定沿用 .env（LAVALINK_PASSWORD、YT_CIPHER_TOKEN、YOUTUBE_OAUTH_REFRESH_TOKEN、LAVALINK_MAX_HEAP）
rem 與 docker\lavalink\application.yml（每次啟動複製一份，只把位址改成本機）。
rem 舊的 Docker 版本：start_docker.bat（全部服務）／start_music_node.bat（只有音樂節點）。
cd /d "%~dp0"
title farmgm 音樂節點（原生）

set "NODE_DIR=%~dp0music_node"
if not exist ".env" (
    echo [錯誤] 找不到 .env
    goto :fail
)

rem ---- 讀取 .env 需要的設定（不顯示內容） ----
set "LAVALINK_PASSWORD="
set "YT_CIPHER_TOKEN="
set "YOUTUBE_OAUTH_REFRESH_TOKEN="
set "LAVALINK_MAX_HEAP=256M"
for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="LAVALINK_PASSWORD" set "LAVALINK_PASSWORD=%%b"
    if /i "%%a"=="YT_CIPHER_TOKEN" set "YT_CIPHER_TOKEN=%%b"
    if /i "%%a"=="YOUTUBE_OAUTH_REFRESH_TOKEN" set "YOUTUBE_OAUTH_REFRESH_TOKEN=%%b"
    if /i "%%a"=="LAVALINK_MAX_HEAP" if not "%%b"=="" set "LAVALINK_MAX_HEAP=%%b"
)
if not defined LAVALINK_PASSWORD (
    echo [錯誤] .env 沒有設定 LAVALINK_PASSWORD
    goto :fail
)
if not defined YT_CIPHER_TOKEN (
    echo [錯誤] .env 沒有設定 YT_CIPHER_TOKEN
    goto :fail
)

rem ---- 第一次使用：下載並編譯 ----
if not exist "%NODE_DIR%\Lavalink.jar" goto :setup
if not exist "%NODE_DIR%\yt-cipher\server.exe" goto :setup
goto :installed
:setup
echo 第一次使用，先安裝音樂節點...
call "%~dp0setup_music_node.bat" --from-start || goto :fail
:installed
if not exist "%NODE_DIR%\logs" mkdir "%NODE_DIR%\logs"

rem ---- Docker 版的音樂節點也綁 127.0.0.1:2333，先停掉（Docker Desktop 沒開時略過） ----
docker info >nul 2>&1
if not errorlevel 1 (
    docker compose -p farmgm-music stop lavalink yt-cipher >nul 2>&1
    docker compose -p farmgm stop lavalink yt-cipher >nul 2>&1
)

rem ---- 已經在執行就不重複啟動 ----
call :listening 2333
if not errorlevel 1 (
    echo 127.0.0.1:2333 已經有程式在執行（音樂節點可能已啟動）。要重新啟動請先執行 stop_music.bat。
    goto :end
)

rem ---- yt-cipher（只在本機 8001，Lavalink 以 YT_CIPHER_TOKEN 呼叫） ----
call :listening 8001
if errorlevel 1 (
    echo 啟動 yt-cipher...
    set "API_TOKEN=%YT_CIPHER_TOKEN%"
    set "HOST=127.0.0.1"
    set "PORT=8001"
    set "OVERRIDE_PLAYER_VARIANT=IAS"
    set "MAX_THREADS=1"
    pushd "%NODE_DIR%\yt-cipher"
    start "farmgm yt-cipher" /min cmd /c "server.exe >> "%NODE_DIR%\logs\yt-cipher.log" 2>&1"
    popd
)

rem ---- Lavalink ----
echo 啟動 Lavalink（第一次會下載 youtube-plugin，約 1 分鐘）...
copy /y "%~dp0docker\lavalink\application.yml" "%NODE_DIR%\application.yml" >nul
set "LAVALINK_SERVER_PASSWORD=%LAVALINK_PASSWORD%"
set "PLUGINS_YOUTUBE_REMOTECIPHER_PASSWORD=%YT_CIPHER_TOKEN%"
set "PLUGINS_YOUTUBE_REMOTECIPHER_URL=http://127.0.0.1:8001"
set "PLUGINS_YOUTUBE_OAUTH_REFRESHTOKEN=%YOUTUBE_OAUTH_REFRESH_TOKEN%"
set "SERVER_ADDRESS=127.0.0.1"
rem 省記憶體（與 docker-compose.yml 的 Lavalink 相同）：SerialGC、閒置時把 heap 還給系統、只用 C1 編譯器、執行緒堆疊減半
set "JAVA_OPTS=-Xms32M -Xmx%LAVALINK_MAX_HEAP% -XX:+UseSerialGC -XX:MinHeapFreeRatio=10 -XX:MaxHeapFreeRatio=30 -XX:TieredStopAtLevel=1 -XX:ReservedCodeCacheSize=48M -Xss512k"
pushd "%NODE_DIR%"
start "farmgm Lavalink" /min cmd /c "java %JAVA_OPTS% -jar Lavalink.jar >> "%NODE_DIR%\logs\lavalink.log" 2>&1"
popd

rem ---- 等 Lavalink 能接受連線（最多 3 分鐘） ----
set /a "WAITED=0"
:wait
timeout /t 3 /nobreak >nul
set /a "WAITED+=3"
curl -sf -o nul -H "Authorization: %LAVALINK_PASSWORD%" http://127.0.0.1:2333/version
if not errorlevel 1 goto :ready
if %WAITED% geq 180 (
    echo [錯誤] Lavalink 3 分鐘內沒有啟動，請查看 music_node\logs\lavalink.log
    goto :fail
)
goto :wait

:ready
echo.
echo 音樂節點已啟動：Lavalink 127.0.0.1:2333、yt-cipher 127.0.0.1:8001
echo 日誌：music_node\logs\lavalink.log、yt-cipher.log；停止：stop_music.bat
echo 確認沒有其他容器要用時，可以關閉 Docker Desktop（並在它的設定取消開機自動啟動），省下整個 Linux 虛擬機的記憶體。
:end
echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1

rem 指定的本機 Port 有程式在監聽時 errorlevel = 0
:listening
netstat -ano -p TCP | findstr /r /c:"127\.0\.0\.1:%1 .*LISTENING" /c:"0\.0\.0\.0:%1 .*LISTENING" >nul
exit /b %errorlevel%
