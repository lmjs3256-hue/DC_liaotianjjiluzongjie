@echo off
chcp 65001 >nul
REM DTAO TG listener control console
setlocal
cd /d "%~dp0"
set "PYEXE=C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
if not exist "%PYEXE%" set "PYEXE=python.exe"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
echo ============================================================
echo   DTAO TG listener control
echo ============================================================
"%PYEXE%" "%~dp0tgctl.py" status
echo.
echo   1) start     2) stop
echo   3) restart   4) logs
echo   0) exit
echo.
set "CH="
echo Enter number and press Return (Return=exit):
set /p "CH="
echo.
if "%CH%"=="1" "%PYEXE%" "%~dp0tgctl.py" start
if "%CH%"=="2" "%PYEXE%" "%~dp0tgctl.py" stop
if "%CH%"=="3" "%PYEXE%" "%~dp0tgctl.py" restart
if "%CH%"=="4" "%PYEXE%" "%~dp0tgctl.py" logs
echo.
pause
