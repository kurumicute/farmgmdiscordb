@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem 停止 start.bat 啟動的原生音樂節點：結束在 127.0.0.1:2333（Lavalink）與 127.0.0.1:8001（yt-cipher）監聽的程式。
title farmgm 停止音樂節點
for %%p in (2333 8001) do (
    for /f "tokens=5" %%i in ('netstat -ano -p TCP ^| findstr /r /c:"127\.0\.0\.1:%%p .*LISTENING"') do (
        echo 停止 Port %%p 的程式（PID %%i）
        taskkill /pid %%i /t /f >nul 2>&1
    )
)
echo 音樂節點已停止。
pause
