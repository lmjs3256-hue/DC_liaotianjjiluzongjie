@echo off
chcp 65001 >nul
REM ===========================================================================
REM DTAO TG 监听控制台（双击本文件即可）
REM
REM 以前双击 bat 窗口一闪就没：有的是 `start "" pythonw`（本来就没窗口），
REM 有的是「已有守护在跑」后直接 exit（窗口连同提示一起消失），
REM 于是看起来像「打不开、一直自动关闭」。
REM
REM 现在统一走 tgctl.py：先打印真实状态，再按选择执行，结尾一定 pause。
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PYEXE=python"
if exist "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
  set "PYEXE=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo ============================================================
echo   DTAO TG 监听控制台
echo ============================================================
"%PYEXE%" "%~dp0tgctl.py" status
echo.
echo   1) 启动监听      2) 停止监听
echo   3) 重启监听      4) 看最近日志
echo   0) 退出
echo.
set "CH="
set /p CH=请输入序号后回车（直接回车=退出）:
echo.
if "%CH%"=="1" "%PYEXE%" "%~dp0tgctl.py" start
if "%CH%"=="2" "%PYEXE%" "%~dp0tgctl.py" stop
if "%CH%"=="3" "%PYEXE%" "%~dp0tgctl.py" restart
if "%CH%"=="4" "%PYEXE%" "%~dp0tgctl.py" logs
echo.
echo ------------------------------------------------------------
pause
