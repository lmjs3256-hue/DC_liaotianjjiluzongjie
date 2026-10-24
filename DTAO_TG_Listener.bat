@echo off
REM 开机自启版：无窗口后台运行监听（把本文件放进「启动」文件夹即可开机自启）
REM 启动文件夹路径：C:/Users/你的用户名/AppData/Roaming/Microsoft/Windows/Start Menu/Startup/
start "" pythonw "%~dp0tg_listener.py"
