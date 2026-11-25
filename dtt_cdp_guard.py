"""维持一个带远程调试端口的「真人 Edge」实例，供 dtt CDP 取数使用。

与 pw-browser 守护同理：本脚本作为持久任务前台托管，持续 ping 调试端口，
端口掉了就重启 Edge，从而让浏览器进程不被会话回收。

Edge 用真实 profile（默认目录），webdriver=false，Cloudflare 放行。
取数通过 dtt_cdp.js 用 connectOverCDP 连上，在真人浏览器上下文 fetch。
"""
import os, sys, time, json, subprocess, datetime

# 单例保护：Windows 命名互斥体，避免多个守护同时限频重启 Edge 互相冲突。
_MUTEX = None
if os.name == "nt":
    import ctypes
    try:
        _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "DTAO_DTT_CDP_GUARD")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            sys.exit(0)
    except Exception:
        _MUTEX = None

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = int(os.environ.get("DTT_CDP_PORT", "9444"))
PROFILE = os.environ.get("DTT_EDGE_PROFILE", "")  # 空=默认 profile
SITE = "https://dtt.bigcatking.cc/"
ENDPOINT = "http://127.0.0.1:%d/json/version" % PORT
NO_WINDOW = 0x08000000

import urllib.request

def alive():
    try:
        with urllib.request.urlopen(ENDPOINT, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False

def start_edge(log):
    args = [EDGE, "--remote-debugging-port=%d" % PORT, "--remote-allow-origins=*",
            "--no-first-run", "--no-default-browser-check",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding", "--disable-background-timer-throttling"]
    if PROFILE:
        args.append("--user-data-dir=%s" % PROFILE)
    args.append(SITE)
    log("启动 Edge CDP 实例 (port=%d, profile=%s)" % (PORT, PROFILE or "默认"))
    try:
        subprocess.Popen(args, creationflags=NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log("启动失败: %s" % e)

def main():
    def log(msg):
        print("[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg), flush=True)
    log("dtt CDP 守护启动，端口 %d" % PORT)
    last_start = 0
    while True:
        if alive():
            time.sleep(10)
            continue
        # 端口不通：限频重启（至少间隔 15 秒）
        if time.time() - last_start > 15:
            start_edge(log)
            last_start = time.time()
            time.sleep(6)
        else:
            time.sleep(3)

if __name__ == "__main__":
    main()
