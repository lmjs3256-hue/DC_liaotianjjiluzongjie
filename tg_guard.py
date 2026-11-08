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

import ctypes
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

RESTART_DELAY = 20      # listener 正常退出后，等多久再拉起
FAST_EXIT_SEC = 10      # 小于这个存活时间算「秒退」
FAST_EXIT_MAX = 3       # 连续秒退多少次后守护自己退出


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
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(GUARD_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


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

    log("守护循环已启动（pid=%d），唯一实例，每 %d 秒检查一次" %
        (os.getpid(), RESTART_DELAY))

    fast_exits = 0
    while True:
        # 已有监听在跑 → 接管它，只做存活监控，不再另起一个
        existing = _running_listener_pid()
        if existing:
            log("检测到监听已在运行（pid=%d），守护只做存活监控，不重复拉起" % existing)
            while _running_listener_pid():
                time.sleep(RESTART_DELAY)
            log("监听从外部退出，%d 秒后拉起" % RESTART_DELAY)
            time.sleep(RESTART_DELAY)
            continue

        started = time.time()
        try:
            # 前端运行，日志由 tg_listener.py 自己写；这里只等它退出
            proc = subprocess.Popen([sys.executable, LISTENER], cwd=HERE)
            code = proc.wait()
        except KeyboardInterrupt:
            log("守护收到 Ctrl+C，停止")
            return 0
        except Exception as e:
            log("拉起监听失败：%s" % e)
            code = -1

        lived = time.time() - started
        log("监听过退出（code=%s，存活 %.0f 秒）" % (code, lived))

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
