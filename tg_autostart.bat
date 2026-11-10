@echo off
REM ===========================================================================
REM DTAO TG 监听 —— 开机自启入口（供「计划任务 / 启动文件夹」调用）
REM
REM 用 pythonw.exe 启动 tgctl.py start：GUI 子系统、没有窗口，
REM 所以不存在「窗口被误关导致监听一起退出」的问题。
REM ===========================================================================
set "DIR=D:\UGit\DC_price_talkzongjie\DC_liaotianjjiluzongjie"
set "PYW=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
cd /d "%DIR%" 2>nul
start "" "%PYW%" "%DIR%\tgctl.py" start
exit /b 0
