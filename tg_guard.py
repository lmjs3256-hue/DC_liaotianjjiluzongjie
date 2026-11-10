#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TG 监听守护循环（单实例，Windows 命名互斥体）

为什么不再用批处理做锁：
    老版 DTAO_TG_Listener_Guard.bat 用 `>>lock (call)` 这种「独占打开文件」
    的土办法做守护单例。cmd 的重定向是**共享追加**打开，多个实例都能成功，
    所以那把锁从来没有挡住过任何东西。结果是「启动文件夹 + 手动双击」叠出
    两个以上守护循环，各自 start 一个 tg_listener.py，而 tg_listener 启动
    时又会主动杀掉其它实例 —— 两个循环于是每 ~20 秒互相残杀一次。
    表现：发了指令日志里能看到「收到指令」，但同一秒就被「已终止旧监听进程」
    打断，永远等不到回复（2026-11-09 00:22 现场）。

现在的做法：
    本进程用 Windows 命名互斥体（Local\\DTAO_TG_Guard）保证守护循环全局唯一，
    抢不到锁直接退出，绝不去干扰已经在跑的那个循环。
    唯一的一个守护循环负责拉起 tg_listener.py，退出后等 20 秒重启。

连续秒退保护：
    若拉起后 10 秒内就退出，视为「有人手动接管 / 环境异常」，最多连试 3 次，
    仍不行就自己退出并打印提示，避免和手工启动的进程互相打断。
"""

import atexit
import ctypes
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LISTENER = os.path.join(HERE, "tg_listener.py")
GUARD_LOG = os.path.join(HERE, "tg_guard.log")
GUARD_MUTEX = "Local\\DTAO_TG_Guard_SingleInstance"
PID_FILE = os.path.join(HERE, "tg_listener.pid")
GUARD_PID_FILE = os.path.join(HERE, "tg_guard.pid")

RESTART_DELAY = 20      # listener 正常退出后，等多久再拉起
RELOAD_DELAY = 2        # 退出码 75 = 「我要热重载」：立刻拉起，不等 20 秒
FAST_EXIT_SEC = 10      # 小于这个存活时间算「秒退」
FAST_EXIT_MAX = 3       # 连续秒退多少次后守护自己退出
EXIT_RELOAD = 75        # 与 tg_listener 约定：代码有新版本，请立即重启我

# CREATE_NO_WINDOW + DETACHED_PROCESS：拉起的监听不占窗口，
# 也不随「守护所在的控制台被关掉」一起被杀（这是以前「自动关闭」的一个来源）。
_DETACHED = 0x00000008
_NO_WINDOW = 0x08000000


def _pythonw():
    """优先用 pythonw.exe 拉起监听：无黑窗，用户看不到也关不掉。"""
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def _pid_alive(pid):
    """进程是否真的还活着（拿到句柄不代表活着，要看退出码）。"""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def _running_listener_pid():
    """当前活着的监听 pid；没有则返回 None。

    这是「不打架」的关键：守护循环只负责**保证有一个监听在跑**，
    而不是每次循环都重新拉一个。若已有监听（例如用户手工
    start_tg_listener.bat 起的那个），直接接管它、只做存活监控，
    绝不新起一个去把它杀掉。老版本的批处理 :loop 没有这个判断，
    于是和 tg_listener 自带的「启动杀旧实例」互相残杀，
    每 20 秒打死一次正在处理指令的进程。
    """
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            pid = int((f.read() or "0").strip() or 0)
    except Exception:
        return None
    if pid and _pid_alive(pid):
        return pid
    return None


def log(msg):
    """只写文件，绝不 print。

    2026-11-10 事故：守护由 Guard.bat 用带控制台的 python.exe 启动，
    老版本这里先 `print(line, flush=True)` 再写文件。Windows 控制台一旦被
    鼠标点选进入「快速编辑/标记」状态（用户看窗口时极易触发），所有往该
    控制台写入的线程都会被**永久冻结**。当天 22:33:35 监听以 75 退出请求
    热重载，守护正好卡在这句 print 上：日志停在 20:36:51，进程 CPU 0.03、
    单线程、Responding=True，于是「拉起新版本」永远没执行，用户看到进程
    停在旧版本。守护是后台服务，本来也没人看控制台输出，直接去掉 print。
    """
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(GUARD_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _disk_version():
    """读磁盘上 pipeline.py 的版本号（守护不 import 业务模块，避免固化在内存）。"""
    try:
        import re
        with open(os.path.join(HERE, "pipeline.py"), "r", encoding="utf-8") as f:
            m = re.search(r'^__version__\s*=\s*["\']([^"\']+)', f.read(), re.M)
        return m.group(1) if m else None
    except Exception:
        return None


def _state_info():
    """读监听自己写的运行状态：{pid, version, started_at}。"""
    try:
        with open(os.path.join(HERE, "tg_listener.state.json"), "r",
                  encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _force_kill(pid):
    """监听热重载失效时，守护强制终止它（下一轮循环会拉起新版本）。"""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x0001, False, int(pid))     # PROCESS_TERMINATE
        if not h:
            return False
        try:
            return bool(k32.TerminateProcess(h, EXIT_RELOAD))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def acquire_guard_mutex():
    """抢守护互斥体；抢到返回句柄，抢不到返回 None。"""
    if os.name != "nt":
        return True
    try:
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateMutexW(None, False, GUARD_MUTEX)
        ERROR_ALREADY_EXISTS = 183
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            if h:
                k32.CloseHandle(h)
            return None
        return h
    except Exception as e:
        log("守护互斥体创建失败（降级为继续运行）：%s" % e)
        return True


def main():
    handle = acquire_guard_mutex()
    if handle is None:
        log("已有守护循环在运行，本实例退出（不干扰已在跑的监听）")
        return 1

    if not os.path.exists(LISTENER):
        log("找不到 %s，守护退出" % LISTENER)
        return 2

    # 记录守护自己的 pid：tgctl stop / status 要靠它才能准确找到守护，
    # 否则只能靠命令行匹配（慢且可能误伤）。
    try:
        with open(GUARD_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    def _bye():
        try:
            if os.path.exists(GUARD_PID_FILE):
                with open(GUARD_PID_FILE, "r", encoding="utf-8") as f:
                    if (f.read() or "").strip() == str(os.getpid()):
                        os.remove(GUARD_PID_FILE)
        except Exception:
            pass

    atexit.register(_bye)

    log("守护循环已启动（pid=%d），唯一实例，每 %d 秒检查一次" %
        (os.getpid(), RESTART_DELAY))

    fast_exits = 0
    last_hb = time.time()
    last_disk = _disk_version()
    pending_ver, pending_since = None, 0.0
    log("守护循环已启动（pid=%d），磁盘版本 v%s，每 %d 秒检查一次" %
        (os.getpid(), last_disk or "?", RESTART_DELAY))

    def _watch_version():
        """版本变更的「第二保险」：监听自己每 60 秒比对磁盘版本并热重载，
        但万一它卡在网络请求、或守护上一次卡死过，就会一直停在旧版本。
        给 150 秒宽限期，之后守护直接终止监听并在下一轮拉起新代码。
        """
        nonlocal last_disk, pending_ver, pending_since
        disk = _disk_version()
        if disk and disk != last_disk:
            last_disk = disk
            pending_ver, pending_since = disk, time.time()
            log("检测到磁盘版本变为 v%s，等待监听自行热重载" % disk)
        if pending_ver and time.time() - pending_since > 150:
            cur = (_state_info().get("version") or "")
            alive = _running_listener_pid()
            if alive and cur and cur != pending_ver:
                log("宽限期已过，监听仍是 v%s（磁盘 v%s），守护强制接管" % (cur, pending_ver))
                if _force_kill(alive):
                    log("已终止旧监听 pid=%d，准备拉起 v%s" % (alive, pending_ver))
            pending_ver = None

    def _heartbeat(pid):
        nonlocal last_hb
        if time.time() - last_hb > 600:
            last_hb = time.time()
            log("💓 守护心跳：监听 pid=%d 存活中，磁盘版本 v%s"
                % (pid, _disk_version() or "?"))

    while True:
        _watch_version()

        # 已有监听在跑 → 接管它，只做存活监控，不再另起一个
        existing = _running_listener_pid()
        if existing:
            log("检测到监听已在运行（pid=%d），守护只做存活监控，不重复拉起" % existing)
            while _running_listener_pid():
                time.sleep(2)
                _watch_version()
                _heartbeat(existing)
            log("监听从外部退出，%d 秒后拉起" % RESTART_DELAY)
            time.sleep(RESTART_DELAY)
            continue

        started = time.time()
        try:
            # 无窗口 + 脱离控制台：监听自己写 tg_listener.log，不需要继承 stdout。
            # 这样即使用户把守护所在的 cmd 窗口关掉，监听也不会一起被杀。
            flags = (_DETACHED | _NO_WINDOW) if os.name == "nt" else 0
            proc = subprocess.Popen(
                [_pythonw(), LISTENER], cwd=HERE,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
            log("已拉起监听 pid=%d（磁盘版本 v%s）" % (proc.pid, _disk_version() or "?"))
            # 不用 proc.wait() 长时间阻塞：每 2 秒醒一次写心跳，守护状态可观测
            while proc.poll() is None:
                time.sleep(2)
                _watch_version()
                _heartbeat(proc.pid)
            code = proc.returncode
        except KeyboardInterrupt:
            log("守护收到 Ctrl+C，停止")
            return 0
        except Exception as e:
            log("拉起监听失败：%s" % e)
            code = -1

        lived = time.time() - started
        log("监听过退出（code=%s，存活 %.0f 秒）" % (code, lived))

        # 热重载：监听检测到 pipeline.py 版本变了，会以 75 退出，要求立即重启
        if code == EXIT_RELOAD:
            log("收到热重载请求（代码已更新），%d 秒后拉起新版本" % RELOAD_DELAY)
            fast_exits = 0
            pending_ver = None
            time.sleep(RELOAD_DELAY)
            continue

        if lived < FAST_EXIT_SEC:
            fast_exits += 1
            if fast_exits >= FAST_EXIT_MAX:
                log("连续 %d 次秒退，判定已被其它实例接管或环境异常，守护退出"
                    "（如需恢复，请重新双击守护脚本）" % fast_exits)
                return 3
        else:
            fast_exits = 0

        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    sys.exit(main())
