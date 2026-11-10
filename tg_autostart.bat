@echo off
REM DTAO TG listener autostart (GUI subsystem, no console window).
REM Hard-coded absolute pythonw.exe: never fall back to a bare name.
set "DIR=D:/UGit/DC_price_talkzongjie/DC_liaotianjjiluzongjie"
set "PYW=C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/pythonw.exe"
if not exist "%PYW%" exit /b 1
cd /d "%DIR%" 2>nul
start "" "%PYW%" "%DIR%\tgctl.py" start
exit /b 0
