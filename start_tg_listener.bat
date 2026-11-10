@echo off
chcp 65001 >nul
REM 前台运行监听（能看到实时日志，Ctrl+C 退出）。仅用于排查问题；
REM 日常请用 TG_Listener_Control.bat 或 DTAO_TG_Listener.bat（后台无窗口）。
REM
REM 注意：前台运行时，若要同时用 tgctl stop 停止后台监听，两者会抢同一个互斥体，
REM 这里会提示「已有同版本监听在运行」并退出 —— 属正常，先停后台那个即可。
setlocal
cd /d "%~dp0"
set "PYEXE=python"
if exist "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
  set "PYEXE=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
echo 启动 DTAO TG 指令监听（前台，Ctrl+C 退出）...
echo.
"%PYEXE%" "%~dp0tg_listener.py"
echo.
echo 监听已退出。
pause
