@echo off
cd /d "%~dp0"
echo 启动 DTAO TG 指令监听（Ctrl+C 退出）...
python tg_listener.py
pause
