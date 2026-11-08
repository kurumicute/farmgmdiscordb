@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem 第一次在這台電腦原生執行音樂節點前執行一次（start.bat 發現沒安裝也會自動呼叫）：
rem   1. 下載 Lavalink 4.2.2（Lavalink.jar，需要 Java 17 以上）
rem   2. 下載 yt-cipher 原始碼（與 docker-compose.yml 使用的映像同一個版本），依官方 Dockerfile 的步驟編譯成 server.exe（需要 Deno）
rem 全部放在 music_node\（已 gitignore）；要重新安裝時刪除 music_node 資料夾再執行。
cd /d "%~dp0"
title farmgm 音樂節點安裝

set "LAVALINK_VERSION=4.2.2"
set "YTC_COMMIT=1e1fd8e2f34ca90cf23545be72e46307bd3d3d2a"
set "EJS_COMMIT=cd4e87f52e87ab6d8b318fd3a817adda6fafa8dc"
set "NODE_DIR=%~dp0music_node"

where java >nul 2>&1 || (echo [錯誤] 找不到 Java，請安裝 Java 17 以上（例如 Eclipse Temurin 21） & goto :fail)
where deno >nul 2>&1 || (echo [錯誤] 找不到 Deno，請安裝：https://deno.com & goto :fail)
where git >nul 2>&1 || (echo [錯誤] 找不到 Git & goto :fail)
where curl >nul 2>&1 || (echo [錯誤] 找不到 curl & goto :fail)

if not exist "%NODE_DIR%" mkdir "%NODE_DIR%"
if not exist "%NODE_DIR%\logs" mkdir "%NODE_DIR%\logs"

if exist "%NODE_DIR%\Lavalink.jar" (
    echo Lavalink.jar 已存在，略過下載
) else (
    echo 下載 Lavalink %LAVALINK_VERSION% ...
    curl -fL --retry 3 -o "%NODE_DIR%\Lavalink.jar.part" "https://github.com/lavalink-devs/Lavalink/releases/download/%LAVALINK_VERSION%/Lavalink.jar" || goto :fail
    move /y "%NODE_DIR%\Lavalink.jar.part" "%NODE_DIR%\Lavalink.jar" >nul
)

if exist "%NODE_DIR%\yt-cipher\server.exe" (
    echo yt-cipher 已編譯，略過
    goto :done
)
if exist "%NODE_DIR%\yt-cipher" rmdir /s /q "%NODE_DIR%\yt-cipher"
echo 下載 yt-cipher（%YTC_COMMIT:~0,7%）...
git clone -q https://github.com/kikkia/yt-cipher.git "%NODE_DIR%\yt-cipher" || goto :fail
pushd "%NODE_DIR%\yt-cipher"
git checkout -q %YTC_COMMIT% || goto :failpop
echo 下載 yt-dlp ejs（%EJS_COMMIT:~0,7%）並套用 yt-cipher 的修補...
git init -q ejs || goto :failpop
git -C ejs remote add origin https://github.com/yt-dlp/ejs.git || goto :failpop
git -C ejs fetch -q --depth 1 origin %EJS_COMMIT% || goto :failpop
git -C ejs checkout -q --detach FETCH_HEAD || goto :failpop
deno run --allow-read --allow-write ./scripts/patch-ejs.ts || goto :failpop
rmdir /s /q ejs\.git 2>nul
rmdir /s /q ejs\node_modules 2>nul
echo 編譯 yt-cipher（約 1 分鐘）...
deno compile --no-check --output server.exe --allow-net --allow-read --allow-write --allow-env --include worker.ts server.ts || goto :failpop
if not exist player_cache mkdir player_cache
popd

:done
echo.
echo 安裝完成：%NODE_DIR%
echo 之後執行 start.bat 啟動音樂節點（Lavalink 第一次啟動會下載 youtube-plugin）。
if /i "%~1"=="--from-start" exit /b 0
pause
exit /b 0

:failpop
popd
:fail
echo.
echo [錯誤] 安裝失敗，請看上面的訊息；要重新安裝時刪除 music_node 資料夾再執行。
if /i "%~1"=="--from-start" exit /b 1
pause
exit /b 1
