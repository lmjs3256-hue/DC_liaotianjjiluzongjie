@echo off
REM ===========================================================================
REM DTAO TG 监听守护脚本
REM
REM 历史事故（v1.5.4 及以前）：
REM   这个脚本被「启动文件夹」和多次手动双击叠加运行，最多同时存在 4 个
REM   :loop 循环，每个循环各自 start 一个 tg_listener.py —— 于是同一台机器上
REM   跑了 3~4 个监听进程，同一条 /digest 被各执行一遍，一天推了 3 条日报，
REM   Telegram 那边还不停报 HTTP 409 Conflict。
REM
REM 现在三层防护：
REM   1) tg_listener.py 自带单实例互斥体，多开会被挡住并直接退出；
REM   2) 本脚本用独占锁文件，保证「守护循环」本身也只能有一个；
REM   3) tg_listener 启动时会主动杀掉旧的监听进程（改完代码重启即可，无需手工清理）。
REM   所以本脚本无论被双击多少次，最终只会有一个监听在跑。
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PYEXE=python"
if exist "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
  set "PATH=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12;%PATH%"
  set "PYEXE=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)

REM 守护循环独占锁：独占方式打开被占用的文件会失败
set "GUARD_LOCK=%~dp0.guard.lock"
2>nul ( >>"%GUARD_LOCK%" (call ) ) || (
  echo [guard] 另一个守护循环已在运行，本实例退出。
  timeout /t 3 >nul
  exit /b 0
)

:loop
"%PYEXE%" "%~dp0tg_listener.py"
REM 监听退出（多开被挡 / 用户 Ctrl+C）时不要疯狂重启
timeout /t 20 >nul
goto loop
