@echo off
chcp 65001 >nul
title Turb GPT Free Register - WebUI 控制台

:: 强制 UTF-8 编码，解决控制台中文乱码
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

:: 自动定位目录
cd /d "%~dp0"
if exist "turb-gpt-free-register-main\web.py" (
    cd /d "%~dp0turb-gpt-free-register-main"
)

echo =======================================================
echo          Turb GPT Free Register 一键启动
echo =======================================================
echo.

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 goto :no_python

:: 检查 5000 端口是否已在运行
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5000 " ^| findstr "LISTENING"') do (
    set RUNNING_PID=%%a
    goto :already_running
)

echo [1/2] 正在启动 WebUI 服务...
echo [2/2] 服务启动后将自动在浏览器打开: http://127.0.0.1:5000
echo.
echo 提示: 请保持此控制台窗口开启。关闭窗口或运行 [停止项目.bat] 即可停止服务。
echo.

python web.py --open-browser
goto :exit_point

:already_running
echo [提示] 检测到 WebUI 已经在运行中 (PID: %RUNNING_PID%)
echo 正在为您打开浏览器: http://127.0.0.1:5000
start http://127.0.0.1:5000
echo.
echo 如需重启服务，请先运行 [停止项目.bat]，再重新运行本脚本。
echo.
pause
exit /b 0

:no_python
echo [错误] 未检测到 Python 环境，请检查是否已安装 Python 并添加到系统 PATH
echo.
pause
exit /b 1

:exit_point
if errorlevel 1 (
    echo.
    echo [异常退出] 程序已退出
    pause
)
