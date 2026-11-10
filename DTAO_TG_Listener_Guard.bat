@echo off
REM Legacy guard launcher kept for old shortcuts.
REM Its old 'start ... tg_guard.py' line could hand a .py file to Windows
REM ShellExecute and trigger the 'choose an app' dialog. Now forwards only.
call "%~dp0DTAO_TG_Listener.bat"
