@echo off
chcp 65001 >nul
REM 启动监听（无窗口后台）。想看菜单/停止/重启，用 TG_Listener_Control.bat
REM
REM 旧版这里是 `start "" pythonw tg_listener.py`：窗口一闪就没、成功失败都看不出来。
REM 现在改走 tgctl.py start —— 它会先报状态，启动后等进程真的活着才返回，结尾 pause。
setlocal
cd /d "%~dp0"
set "PYEXE=python"
if exist "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
  set "PYEXE=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%PYEXE%" "%~dp0tgctl.py" start
echo.
pause
