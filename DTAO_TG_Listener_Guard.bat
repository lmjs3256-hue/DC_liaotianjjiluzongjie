@echo off
REM Guard version: auto-restart tg_listener if it exits or crashes.
REM Uses python (console, minimized) instead of pythonw so crash traceback is visible.
REM Put a SHORTCUT of this file into the Startup folder for boot auto-start with self-healing.
REM Startup folder: C:/Users/<you>/AppData/Roaming/Microsoft/Windows/Start Menu/Startup/
cd /d "%~dp0"
:loop
start "" /min /wait python "%~dp0tg_listener.py"
timeout /t 15 >nul
goto loop
