@echo off
chcp 65001 >nul
REM DTAO TG listener - the ONLY supported launcher.
REM Runs python.exe with tgctl.py as an argument (no ShellExecute on .py).
setlocal
cd /d "%~dp0"
set "PYEXE=C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
if not exist "%PYEXE%" set "PYEXE=python.exe"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
echo --- DTAO TG listener status ---
echo.
"%PYEXE%" "%~dp0tgctl.py" status
echo.
echo --- starting / confirming listener ---
echo.
"%PYEXE%" "%~dp0tgctl.py" start
echo.
pause
