@echo off
chcp 65001 >nul
REM Foreground listener (debug only). Ctrl+C to stop.
setlocal
cd /d "%~dp0"
set "PYEXE=C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
if not exist "%PYEXE%" set "PYEXE=python.exe"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%PYEXE%" "%~dp0tg_listener.py"
echo.
pause
