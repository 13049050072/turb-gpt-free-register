@echo off
chcp 65001 >nul
title Turb GPT Free Register - 停止服务

echo =======================================================
echo          Turb GPT Free Register 一键停止
echo =======================================================
echo.

set FOUND=0

:: 1. 查找并结束监听 5000 端口的进程
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5000 " ^| findstr "LISTENING"') do (
    echo [停止] 正在结束占用 5000 端口的进程 PID: %%a
    taskkill /F /PID %%a >nul 2>&1
    set FOUND=1
)

:: 2. 终止运行 web.py 的残留 python 进程
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*web.py*' } | Stop-Process -Force" >nul 2>&1

:: 3. 清理单实例锁文件
del /f /q "%TEMP%\turb-gpt-free-register-web-*.lock" >nul 2>&1

if "%FOUND%"=="1" (
    echo.
    echo [成功] WebUI 服务已成功停止
) else (
    echo.
    echo [提示] 未发现运行中的 WebUI 服务（或已停止）
)

echo.
ping 127.0.0.1 -n 3 >nul
