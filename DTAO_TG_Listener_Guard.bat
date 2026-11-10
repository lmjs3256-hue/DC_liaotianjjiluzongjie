@echo off
chcp 65001 >nul
REM ===========================================================================
REM DTAO TG 监听守护（入口）
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
REM
REM 2026-11-10 修复「打不开、一直自动关闭」：
REM   以前守护发现已有实例在跑就 `exit /b`，窗口连同提示一起关掉，
REM   用户完全看不出发生了什么。现在先打印状态，结尾一定 pause。
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PYEXE=python"
if exist "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
  set "PATH=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12;%PATH%"
  set "PYEXE=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo ============================================================
echo   DTAO TG 监听守护
echo ============================================================
"%PYEXE%" "%~dp0tgctl.py" status
echo.
echo 正在启动/确认守护（本窗口可以最小化，关掉也不影响后台监听）...
echo.
REM 2026-11-10 二次修复：守护以前用带控制台的 python.exe 前台跑。
REM 用户只要用鼠标点一下这个窗口，Windows 就进入「快速编辑/标记」状态，
REM 守护下一次写控制台时会被永久冻结 —— 当天监听以 75 退出请求热重载，
REM 守护正好卡死，13 分钟没拉起新版本。现在一律用 pythonw 无窗口后台启动，
REM 守护日志只写 tg_guard.log，彻底没有可被冻结的控制台。
set "PYW=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
start "" "%PYW%" "%~dp0tg_guard.py"
echo 守护已在无窗口后台启动。若日志提示「已有守护循环在运行」，
echo 说明后台已经有一个守护在跑，属正常，不必重复启动。
echo 查看状态：python tgctl.py status ；查看守护日志：tg_guard.log
echo.
pause
exit /b 0
