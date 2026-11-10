#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TG 监听控制台：status / start / stop / restart / logs

为什么需要这个文件
------------------
监听器和守护都是 pythonw 后台进程，没有任何窗口。以前有两个体验死角：

1. 双击 DTAO_TG_Listener.bat —— 里面是 `start "" pythonw ...`，
   窗口一闪就没了，用户完全无法判断「到底起来没有」；
2. 双击 DTAO_TG_Listener_Guard.bat —— 若守护已在跑，脚本打印一行
   「已有守护循环在运行」后 `exit /b`，窗口同样一闪就关。
   表现就是「监听进程打不开、一直自动关闭」。

本文件把「查状态 / 启动 / 停止 / 重启」做成有明确输出、明确退出码的命令，
bat 只负责调用它并在结尾 pause，窗口永远不会自己消失。

用法
----
    python tgctl.py status      # 看是否在跑、跑的哪个版本、最近日志
    python tgctl.py start       # 没跑就拉起守护（守护再拉起监听），等到确实活着才返回
    python tgctl.py stop        # 先停守护（否则它会把监听再拉起来），再停监听
    python tgctl.py restart     # 等价 stop + start，改完代码用它
    python tgctl.py logs        # 打印最近 30 行日志
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "tg_guard.py")
LISTENER = os.path.join(HERE, "tg_listener.py")
PID_LISTENER = os.path.join(HERE, "tg_listener.pid")
PID_GUARD = os.path.join(HERE, "tg_guard.pid")
STATE = os.path.join(HERE, "tg_listener.state.json")
LOG_LISTENER = os.path.join(HERE, "tg_listener.log")
LOG_GUARD = os.path.join(HERE, "tg_guard.log")

PROJ_TAG = os.path.basename(HERE)          # 用来限定「只动本项目的进程」
DETACHED = 0x00000008                      # DETACHED_PROCESS：不继承控制台
NO_WINDOW = 0x08000000                     # CREATE_NO_WINDOW


# --------------------------------------------------------------- 基础工具
def _alive(pid):
    """进程是否真的活着。OpenProcess 成功不代表活着，必须看退出码。"""
    if not pid:
        return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
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


def _kill(pid):
    if not _alive(pid):
        return False
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=20)
        return True
    except Exception:
        return False


def _read_pid(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except Exception:
        return 0


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _disk_version():
    """磁盘上 pipeline.py 的版本（正则读，不 import，避免把旧模块固化进内存）。"""
    try:
        with open(os.path.join(HERE, "pipeline.py"), "r", encoding="utf-8") as f:
            m = re.search(r'^__version__\s*=\s*["\']([^"\']+)', f.read(), re.M)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def _procs(keyword):
    """列出命令行同时含关键字与本项目目录名的 python 进程 pid（排除自己）。"""
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -like '*%s*' -and "
        "$_.CommandLine -like '*%s*' } | ForEach-Object { $_.ProcessId }"
        % (keyword, PROJ_TAG)
    )
    pids = set()
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=25,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit() and int(line) != os.getpid():
                pids.add(int(line))
    except Exception:
        pass
    return pids


def _tail(path, n=30):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return lines[-n:]
    except Exception:
        return []


def _pythonw():
    """优先用 pythonw.exe（无控制台），拿不到就退回当前解释器。"""
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def _listener_info():
    """返回 (pid, alive, version, started_at)。version 取自运行中进程写的状态文件。"""
    pid = _read_pid(PID_LISTENER)
    alive = _alive(pid)
    if not alive:
        # pid 文件可能过期：再从进程列表捞，避免「明明在跑却报没跑」
        found = _procs("tg_listener.py")
        if found:
            pid = sorted(found)[0]
            alive = True
    st = _read_json(STATE)
    ver = st.get("version") if st.get("pid") == pid else None
    return pid, alive, ver, st.get("started_at")


def _guard_pid():
    pid = _read_pid(PID_GUARD)
    if _alive(pid):
        return pid
    found = _procs("tg_guard.py")
    return sorted(found)[0] if found else 0


def _fmt_uptime(started_at):
    if not started_at:
        return "?"
    try:
        t0 = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
        sec = int((datetime.now() - t0).total_seconds())
        if sec < 60:
            return "%d 秒" % sec
        if sec < 3600:
            return "%d 分 %d 秒" % (sec // 60, sec % 60)
        return "%d 小时 %d 分" % (sec // 3600, sec % 3600 // 60)
    except Exception:
        return "?"


# --------------------------------------------------------------- 各子命令
def cmd_status(quiet=False):
    pid, alive, ver, started = _listener_info()
    gpid = _guard_pid()
    disk = _disk_version()
    print("项目目录：%s" % HERE)
    print("磁盘版本：v%s" % disk)
    print("-" * 56)
    if alive:
        print("监听进程：✅ 运行中（pid=%d，已运行 %s）" % (pid, _fmt_uptime(started)))
        print("运行版本：%s" % ("v" + ver if ver else "未知（老进程启动时还没记录版本）"))
        if ver and disk != "?" and ver != disk:
            print("⚠️  代码已更新到 v%s，但进程里还是 v%s —— 需要重启才生效" % (disk, ver))
    else:
        print("监听进程：❌ 未运行")
    print("守护进程：%s" % ("✅ 运行中（pid=%d）" % gpid if gpid else "❌ 未运行"))
    print("-" * 56)
    t = _tail(LOG_LISTENER, 3)
    if t:
        print("最近日志：")
        for ln in t:
            print("  " + ln)
    return 0 if alive else 1


def cmd_start(wait=30):
    pid, alive, ver, _ = _listener_info()
    if alive:
        disk = _disk_version()
        print("✅ 监听已在运行（pid=%d，版本 v%s）" % (pid, ver or "未知"))
        if ver and disk != "?" and ver != disk:
            print("⚠️  磁盘已是 v%s，进程还是 v%s —— 建议执行 restart 让新代码生效。" % (disk, ver))
        return 0

    print("启动守护（无窗口后台）…")
    logf = open(os.path.join(HERE, "tgctl.log"), "a", encoding="utf-8")
    logf.write("[%s] tgctl start\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logf.flush()
    try:
        subprocess.Popen([_pythonw(), GUARD], cwd=HERE,
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                         creationflags=DETACHED | NO_WINDOW, close_fds=True)
    except Exception as e:
        print("❌ 拉起守护失败：%s" % e)
        return 2
    finally:
        try:
            logf.close()
        except Exception:
            pass

    # 等「真的活着」再报成功：只看到进程创建不算数（可能秒退）
    deadline = time.time() + max(5, wait)
    while time.time() < deadline:
        p, ok, v, _ = _listener_info()
        if ok:
            print("✅ 已启动：监听 pid=%d，版本 v%s" % (p, v or _disk_version()))
            print("   日志：tg_listener.log（其中「已加载 pipeline 模块」那行是启动确认）")
            return 0
        time.sleep(0.5)

    print("⚠️  %d 秒内没等到监听进程起来。诊断：" % wait)
    g = _guard_pid()
    print("   守护进程：%s" % ("在（pid=%d）" % g if g else "不在（自己就退了）"))
    print("   ---- tg_guard.log 最近 5 行 ----")
    for ln in _tail(LOG_GUARD, 5):
        print("   " + ln)
    print("   ---- tg_listener.log 最近 5 行 ----")
    for ln in _tail(LOG_LISTENER, 5):
        print("   " + ln)
    return 3


def cmd_stop():
    killed = []
    g = _guard_pid()
    if g:
        if _kill(g):
            killed.append(("守护", g))
    for p in _procs("tg_guard.py"):
        if _kill(p):
            killed.append(("守护", p))
    # 守护先停干净，否则它 20 秒后又把监听拉起来
    time.sleep(1.0)
    pid, alive, _, _ = _listener_info()
    targets = set(_procs("tg_listener.py"))
    if alive and pid:
        targets.add(pid)
    for p in sorted(targets):
        if _kill(p):
            killed.append(("监听", p))

    if killed:
        for kind, p in killed:
            print("已停止%s进程 pid=%d" % (kind, p))
    else:
        print("没有在运行的本项目进程。")

    # 清掉过期记录，避免下次 status 误判
    for f in (PID_LISTENER, PID_GUARD, STATE):
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception:
            pass
    time.sleep(0.5)
    _, alive2, _, _ = _listener_info()
    print("结果：%s" % ("❌ 仍有监听在跑" if alive2 else "✅ 已全部停止"))
    return 0 if not alive2 else 4


def cmd_restart():
    print("=== 停止 ===")
    cmd_stop()
    print()
    print("=== 启动 ===")
    return cmd_start()


def cmd_version():
    _, alive, ver, _ = _listener_info()
    print("磁盘版本：v%s" % _disk_version())
    print("运行版本：%s" % (("v" + ver) if ver else ("v?" if alive else "（未运行）")))
    return 0


def main(argv):
    sub = (argv[1] if len(argv) > 1 else "status").lower()
    if sub in ("status", "st", "状态"):
        return cmd_status()
    if sub in ("start", "up", "启动"):
        return cmd_start()
    if sub in ("stop", "down", "停止"):
        return cmd_stop()
    if sub in ("restart", "reload", "重启"):
        return cmd_restart()
    if sub in ("logs", "log", "日志"):
        for ln in _tail(LOG_LISTENER, 30):
            print(ln)
        return 0
    if sub in ("version", "ver", "版本"):
        return cmd_version()
    print(__doc__)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv))
