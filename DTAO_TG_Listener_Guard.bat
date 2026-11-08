@echo off
REM ===========================================================================
REM DTAO TG 监听守护脚本（入口）
REM
REM 真正的守护循环在 tg_guard.py 里，用 Windows 命名互斥体做单例。
REM
REM 为什么不再用批处理自己循环（2026-11-09 事故）：
REM   老版本用 `>>.guard.lock (call)` 想「独占打开文件」来做守护单例，
REM   但 cmd 的重定向是共享追加，多个实例都能打开成功 —— 这把锁从来没生效。
REM   于是「启动文件夹 + 手动双击」叠出多个 :loop，每个循环各拉一个
REM   tg_listener.py，而 tg_listener 启动时又会主动杀掉其它实例，
REM   两个循环便每 ~20 秒互相残杀。症状：TG 发指令日志里有「收到指令」，
REM   同一秒就被「已终止旧监听进程」打断，永远等不到回复。
REM
REM 现在：本 bat 只负责转发到 tg_guard.py；守卫单例由互斥体保证，
REM       无论双击多少次，最终只有一个守护循环、一个监听进程。
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PYEXE=python"
if exist "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
  set "PATH=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12;%PATH%"
  set "PYEXE=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)

"%PYEXE%" "%~dp0tg_guard.py"
if errorlevel 1 (
  timeout /t 3 >nul
)
exit /b 0
