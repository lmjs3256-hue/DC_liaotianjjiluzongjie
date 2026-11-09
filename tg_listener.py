#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TG 指令监听：在你的 Telegram 对话里发命令，直接触发 pipeline（半自动核心）。

支持的指令（仅允许授权来源发送）：
  /help                         显示帮助
  /digest                       导出前一天并双推
  /digest 2026-10-23           导出指定日
  /digest 2026-10-22 2026-10-23 一次导出多天（逐日推送）
  /weekly                       周报
  /水聊（/water）                水聊侧记：只挑被日报过滤掉的闲聊，单独成一条
  /水聊 昨日 3                   指定日期 / 连续天数（只读已有导出，不重新导出）
  /watch                       查看潜力观察（默认最新一天，可跟天数 /watch 7 或 /watch all；同步推飞书）
  /repush 2026-10-23           补推（飞书+TG）
  /repush 2026-10-23 tg        仅补推 TG
  /repush 2026-10-23 feishu    仅补推飞书

授权规则（开放模式）：
  - 任意会话、任意用户发来的指令都会被处理（已取消「仅本人可触发」限制）。
  - 每日真实导出仍受 pipeline 内置节流保护（默认每天最多 2 次、间隔 ≥12h），
    因此即使多人反复触发，单日 user token 实际导出次数也被限制，不会敞开口消耗。

运行（建议本机开机自启 / 后台常驻，且 Clash 系统代理开着）：
  python tg_listener.py
停止：Ctrl+C
"""
import os
import re
import ssl
import sys
import json
import glob
import time
import atexit
import datetime
import subprocess
import threading
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pipeline as P

def log(msg):
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = "[%s] %s\n" % (ts, msg)
        with open(os.path.join(HERE, "tg_listener.log"), "a", encoding="utf-8") as f:
            f.write(line)
        print(line, end="", flush=True)
    except Exception:
        pass


HELP = (
    "🤖 DTAO 日报指令\n"
    "/digest 导出前一天并双推\n"
    "/digest 2026-10-23 导出指定日\n"
    "/digest 2026-10-22 2026-10-23 多日\n"
    "/today 导出并总结今天的聊天记录（北京时间今天 00:00 到现在）\n"
    "/water 或 /水聊 水聊侧记（群里闲聊/梗/跑题，单独一条，不写投研）\n"
    "   /水聊 昨日 指定日期；/水聊 昨日 3 连续 3 天（只读已有导出，不重新导出）\n"
    "/weekly 周报\n"
    "━━ 子网档案 / 投研 ━━\n"
    "（建档全自动：每次导出日报时自动把提到的子网存档，已建档的增量补充、\n"
    "  未建档的自动新建，无需手动操作）\n"
    "★ 直接发子网编号即可出投研：发「29」= 生成 SN29 重要事件跟踪报告\n"
    "   （不发斜杠也行；SN29 / #29 / subnet 29 都认，发「29 30」= 回看 30 天）\n"
    "   /投研 29 与上面等价；也认子网名：/投研 hotfloat\n"
    "   报告内容：先调本地子网档案，再结合官网/推特/GitHub/新闻/全网检索交叉核验，\n"
    "   分 7 段（结论/事实基线/时间线/交叉验证/预期差/风险/跟踪清单）并双推\n"
    "/档案 列出所有已建档子网（不知道编号先发这个）；/档案 29 看某子网档案\n"
    "📚 /kb 子网知识库周更状态（排程/上次更新/待补跑）；/kb update 立即校对\n"
    "   （每周自动跑一次：拉链上身份核对子网名与业务，并同步到已建档案）\n"
    "━━ 其它 ━━\n"
    "/watch 查看潜力观察(默认最新; /watch 7 近7天热点; /watch all 全部)\n"
    "/repush 补推最近一次日报(无需日期)\n"
    "/repush 2026-10-23 补推指定日(飞书+TG)\n"
    "/repush 2026-10-23 tg 仅补推 TG\n"
    "/repush 2026-10-23 feishu 仅补推飞书\n"
    "/retry 反馈内容 按你的意见/补充事实重总结并重推\n"
    "   例：/retry SN34 是宣布转型，不是跑路\n"
    "   可指定日期：/retry 2026-10-30 反馈内容\n"
    "★ 反馈里带子网编号、且该子网最近做过投研 → **重做投研报告**（不是重跑日报）\n"
    "   例：/retry 127 已改名为 Zils，报告名字错了\n"
    "   投研会以链上身份为准核对名称，并按你的反馈逐条修正后重推\n"
    "   反馈带「专题」二字 = 专题总结模式：不修正日报，而是从最近几天已有记录里\n"
    "   还原该专题的原文人物观点并推送。例：/retry 总结SN127专题并推送，还原原文人物观点\n"
    "/help 显示本帮助\n"
    "/keyboard 重新显示底部按钮栏（回复后会自动收起）\n"
    "/收起 立即收起底部按钮栏（/hidekeyboard 同义）\n"
)


# ---------------------------------------------------------------------------
# 单实例保护
#
# 为什么必须有：Telegram 的 getUpdates 是「谁先抢到谁拿」，多个监听进程会各自
# 轮询同一个 bot —— 同一条 /digest 被 N 个进程各执行一遍，于是同一天推 N 份日报；
# 其余进程还会不断收到 HTTP 409 Conflict。历史上就是开机自启 + 手动启动 +
# 守护脚本三重叠加，跑出了 3 个实例、3 条日报。
#
# 做法：命名互斥体（Windows）/ 文件锁（其它平台）保证全局唯一；
# 并且**新进程启动时主动杀掉旧进程**，这样改完代码重启即可，不必手工清进程。
# ---------------------------------------------------------------------------
# 用 Local\ 而不是 Global\：创建 Global 对象需要 SeCreateGlobalPrivilege，
# 普通登录用户（非服务）经常拿不到，会导致互斥体形同虚设。
# Local\ 覆盖同一登录会话，正好是「同一台电脑同一个人多开」这个真实场景。
_MUTEX_NAME = "Local\\DTAO_TG_Listener_SingleInstance"
_mutex_handle = None
_PID_FILE = os.path.join(HERE, "tg_listener.pid")
# 已处理过的消息指纹（chat_id:message_id），进程内幂等去重
_SEEN_MSG_IDS = {}



def _pid_alive(pid):
    """进程是否真的还活着（OpenProcess 拿句柄成功并不代表活着，要查退出码）。"""
    try:
        import ctypes
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


def _list_listener_pids():
    """列出所有正在跑「本项目 tg_listener.py」的 python 进程 pid（不含自己）。

    优先用 PowerShell 的 CIM 拿命令行（能精确匹配本项目路径）；
    拿不到就退回 pid 文件里记录的那个 pid。两者都失败就什么都不杀——
    宁可留着旧进程让它被互斥体挡在外，也不要误杀用户的其它 python 程序。
    """
    pids = set()
    me = os.getpid()
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -like '*tg_listener.py*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=25,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                if pid != me and _pid_alive(pid):
                    pids.add(pid)
    except Exception as e:
        log("枚举监听进程失败（改用 pid 文件）：%s" % e)

    if not pids:
        try:
            with open(_PID_FILE, "r", encoding="utf-8") as f:
                pid = int((f.read() or "0").strip() or 0)
            if pid and pid != me and _pid_alive(pid):
                pids.add(pid)
        except Exception:
            pass
    return pids


def _kill_pid(pid):
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_TERMINATE = 0x0001
        h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        try:
            return bool(k32.TerminateProcess(h, 0))
        finally:
            k32.CloseHandle(h)
    except Exception as e:
        log("终止 pid=%s 失败（忽略）：%s" % (pid, e))
        return False


def _kill_stale_instances():
    """启动时杀掉所有旧监听实例（含守护脚本留下的多重实例）。"""
    killed = []
    for pid in sorted(_list_listener_pids()):
        if _kill_pid(pid):
            killed.append(pid)
            log("已终止旧监听进程 pid=%d" % pid)
    if killed:
        # 给旧进程一点时间释放互斥体和端口
        time.sleep(1.5)
    return killed


def acquire_single_instance(retries=6, delay=0.8):
    """申请全局唯一实例；已有实例则拒绝启动。返回 True 表示拿到锁。

    注意顺序：**先抢锁，抢不到再杀旧进程，然后重抢**。
    反过来（先杀再造）会有竞态——旧进程刚被 TerminateProcess 时，
    内核对象还引用着互斥体没释放，新进程这时去 CreateMutex 会拿到
    ERROR_ALREADY_EXISTS，于是刚要启动就把自己判成「已有实例」退出，
    表现就是「杀掉了旧的不见新的起来」。所以这里必须带重试。
    """
    global _mutex_handle
    for attempt in range(retries):
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CreateMutexW.restype = wintypes.HANDLE
                k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
                h = k32.CreateMutexW(None, False, _MUTEX_NAME)
                ERROR_ALREADY_EXISTS = 183
                err = ctypes.get_last_error()
                if err == ERROR_ALREADY_EXISTS:
                    if h:
                        k32.CloseHandle(h)
                    if attempt == 0:
                        # 第一次抢不到 → 说明确实有旧实例，杀掉再重抢
                        killed = _kill_stale_instances()
                        if not killed:
                            return False       # 有锁但没有可杀的进程（极少见）→ 放弃
                    time.sleep(delay * (attempt + 1))
                    continue
                _mutex_handle = h
                break
            except Exception as e:
                log("单实例互斥体创建失败（降级为继续运行）：%s" % e)
                break
        else:
            break
    else:
        return False
    try:
        with open(_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    return True


def release_single_instance():
    global _mutex_handle
    try:
        if _mutex_handle:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(_mutex_handle)
            _mutex_handle = None
    except Exception:
        pass
    try:
        if os.path.exists(_PID_FILE):
            with open(_PID_FILE, "r", encoding="utf-8") as f:
                if (f.read() or "").strip() == str(os.getpid()):
                    os.remove(_PID_FILE)
    except Exception:
        pass


def tg_api(token, method, params=None, timeout=40):
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with P._opener().open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# 底部按钮栏：默认不再常驻（v1.5.3 起，每次 bot 回复自动收起键盘）。
# 需要 时发 /keyboard 可重新唤出。
_KEYBOARD = {
    "keyboard": [
        ["/digest 昨日", "/repush 最新"],
        ["/投研 29", "/档案"],
        ["/watch 7", "/weekly"]
    ],
    "resize_keyboard": True
}

_REMOVE_KB = {"remove_keyboard": True}


def send(chat_id, text, markup=None):
    # 纯文本发送，避免日志里的特殊字符破坏 Markdown 解析；
    # 默认随回复附 remove_keyboard —— bot 每次回复后底部按钮栏自动收起
    tg_api(P._CFG["telegram_bot_token"], "sendMessage",
           {"chat_id": chat_id, "text": text, "disable_web_page_preview": True,
            "reply_markup": json.dumps(markup or _REMOVE_KB)})


def _tg_bold(s):
    # 把 lark_md 的 **加粗** 转成 TG 旧版 Markdown 的 *加粗*
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", s)


def _feishu_watch(cfg, title, content):
    """把潜力观察推到飞书卡片（失败不影响 TG 推送）。"""
    try:
        P.push_feishu(cfg, title, content)
    except Exception as e:
        log("飞书潜力观察推送失败（忽略）：%s" % e)


# ---------------------------------------------------------------------------
# 加固：流水线放到后台线程执行
#   - 主循环立即返回继续收指令，导出卡住 15 分钟也不会拖垮监听进程
#   - 超时后主动 kill 整个进程树，避免 DiscordChatExporter 僵尸残留
#   - _BUSY 锁防止并发重复导出（浪费 user token 配额）
# ---------------------------------------------------------------------------
PIPELINE_TIMEOUT = 900          # 单次流水线最长等待（秒）
# 建档/投研要联网抓多个外部站 + 两轮 LLM，比日报慢得多，单独放宽
_SLOW_CMDS = {"archive": 1800, "research": 2400}
_BUSY = False
_BUSY_LOCK = threading.Lock()


def _kill_proc_tree(proc):
    """超时后杀掉子进程及其整个进程树（Windows 用 taskkill /T）。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        else:
            proc.kill()
    except Exception as e:
        log("清理子进程树失败（忽略）：%s" % e)


def _clean_tail(out, limit=1200):
    """清洗子进程输出尾部：去掉乱码替换字符与控制字符，避免回传时刷屏。"""
    out = (out or "").replace("\ufffd", "")
    out = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", out)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    tail = "\n".join(lines[-25:])
    return tail[-limit:] if len(tail) > limit else tail


def _run_pipeline_sync(args, chat_id):
    """真正执行流水线（同步逻辑，供后台线程调用）。"""
    cmd = [sys.executable, os.path.join(HERE, "pipeline.py")] + args
    # 强制子进程以 UTF-8 输出：中文 Windows 默认 GBK，父进程按 UTF-8 解码会变乱码
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONFAULTHANDLER"] = "1"   # 进程级崩溃时能留下堆栈，便于排查
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=env)
    timeout = _SLOW_CMDS.get(args[0] if args else "", PIPELINE_TIMEOUT)
    try:
        out, _ = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_proc_tree(proc)
        try:
            out, _ = proc.communicate(timeout=30)
        except Exception:
            out = ""
        log("流水线超时（>%ds），已终止子进程树：%s" % (timeout, " ".join(args)))
        try:
            send(chat_id, "⏱ 执行超时（>%d 分钟），已强制终止并清理子进程，请查 run.log"
                 % (timeout // 60))
        except Exception:
            pass
        return
    out = out or ""
    if rc == 0:
        # 成功：日报已由 pipeline 自行推送到飞书+TG，这里只回简洁确认，不回传大段日志
        if args and args[0] == "chat":
            send(chat_id, "✅ 水聊侧记已生成并推送（飞书 + TG）\n"
                          "只取被日报过滤掉的闲聊，不写投研；全程未重新导出聊天记录。")
            return
        if args and args[0] == "archive":
            # 从日志里抓建档统计（persistent 归档了多少子网、新增多少条）
            m = re.search(r"子网建档完成：\d+ 个子网有档案，本次新增 (\d+) 条（累计 (\d+) 条）", out)
            detail = ("\n📊 本次新增 %s 条，档案累计 %s 条" % (m.group(1), m.group(2))) if m else ""
            send(chat_id, "✅ 子网建档完成%s\n"
                          "按 SN 编号逐个存放于 subnet_archive/SNxx/（archive.json + archive.md，"
                          "含原文与中文译文、按时间排序的人物观点索引）。\n"
                          "发 /档案 看清单，发 /投研 29 做单子网跟踪报告。" % detail)
            return
        if args and args[0] == "research":
            m = re.search(r"报告已存：(.+?)（(\d+) 字）", out)
            detail = ("\n📄 %s\n📝 %s 字" % (m.group(1), m.group(2))) if m else ""
            send(chat_id, "✅ 子网投研报告已生成并推送（飞书 + TG）%s\n"
                          "已归档到 subnet_archive/（下次同子网可直接复用档案）。" % detail)
            return
        stat = ""
        m = re.search(r"过滤后保留实质消息\s*(\d+)\s*条", out)
        if m:
            stat = "\n📊 当日实质消息 %s 条" % m.group(1)
        # 自动建档的成果也回执一句，用户才知道子网档案在长
        ma = re.search(r"子网建档（自动）：涉及 (\d+) 个子网，新增 (\d+) 条，累计 (\d+) 条", out)
        if ma:
            stat += "\n🗂 子网档案：本批涉及 %s 个子网，新增 %s 条（累计 %s 条）" % (
                ma.group(1), ma.group(2), ma.group(3))
        mode = ""
        mm = re.search(r"节流模式：\s*\S+\s*（(.+?)）", out)
        if mm:
            mode = "\n🔧 %s" % mm.group(1)
        send(chat_id, "✅ 已完成：%s%s%s" % (" ".join(args), stat, mode))
    else:
        # 失败才回传错误尾部（清洗乱码、只留最后几行有意义的）
        tail = _clean_tail(out)
        send(chat_id, "❌ 失败：%s\n%s" % (" ".join(args), tail or "（无输出，详见 run.log）"))


def _pipeline_worker(args, chat_id):
    """后台线程主体：任何异常都在这里消化，绝不向外抛到主循环。"""
    global _BUSY
    try:
        _run_pipeline_sync(args, chat_id)
    except Exception as e:
        log("流水线线程异常：%s" % e)
        try:
            send(chat_id, "❌ 触发异常：%s" % e)
        except Exception:
            pass
    finally:
        with _BUSY_LOCK:
            _BUSY = False
        log("流水线线程结束（已释放执行锁）：%s" % " ".join(args))


def run_pipeline(args, chat_id):
    """触发流水线：后台线程执行，监听主循环立刻继续收指令。"""
    global _BUSY
    with _BUSY_LOCK:
        if _BUSY:
            try:
                send(chat_id, "⏳ 上一个任务仍在执行中，请稍候再试（避免并发导出浪费 token）")
            except Exception:
                pass
            return
        _BUSY = True
    try:
        send(chat_id, "⏳ 开始执行：%s" % " ".join(args))
    except Exception as e:
        log("发送开始提示失败（忽略）：%s" % e)
    t = threading.Thread(target=_pipeline_worker, args=(args, chat_id), daemon=True)
    t.start()
    log("已在后台线程启动流水线：%s" % " ".join(args))


def _pipeline_busy():
    """给周更调度器用的让路判断：日报/建档在跑时别同时改档案。"""
    return bool(_BUSY)


def _kb_worker(chat_id):
    """后台线程：立即跑一次知识库校对（同步档案），结果推给发指令的会话。"""
    try:
        import kb_weekly as K
        res = K.run_now(P._CFG, notify=lambda t: send(chat_id, t),
                        log=log, reason="TG 手动触发")
        if not res.get("ok"):
            send(chat_id, "❌ 更新失败：%s" % (res.get("error") or "未知错误"))
    except Exception as e:
        log("知识库手动更新线程异常：%s" % e)
        try:
            send(chat_id, "❌ 更新异常：%s" % e)
        except Exception:
            pass
    finally:
        global _BUSY
        with _BUSY_LOCK:
            _BUSY = False


def run_kb_update(chat_id):
    """`/kb update`：占执行锁后用后台线程跑，避免和日报同时写档案。"""
    global _BUSY
    with _BUSY_LOCK:
        if _BUSY:
            try:
                send(chat_id, "⏳ 有任务正在执行，请稍后再试（避免并发写档案）")
            except Exception:
                pass
            return
        _BUSY = True
    try:
        send(chat_id, "📚 开始校对子网知识库（拉链上身份 + 同步已建档案），约 1-2 分钟…")
    except Exception:
        pass
    threading.Thread(target=_kb_worker, args=(chat_id,), daemon=True).start()


def _start_kb_scheduler():
    """启动知识库周更后台调度（监听启动时调用一次）。"""
    try:
        import kb_weekly as K
        K.start_thread(
            P._CFG,
            notify=lambda t: send(P._CFG.get("telegram_chat_id") or _last_chat_id(), t),
            log=log,
            busy=_pipeline_busy,
        )
    except Exception as e:
        log("知识库周更排程启动失败（忽略，手动 /kb update 仍可用）：%s" % e)


def _last_chat_id():
    """没有配置 telegram_chat_id 时，退回到最近一次收到指令的会话。"""
    return _LAST_CHAT_ID[0] if _LAST_CHAT_ID[0] else ""


_LAST_CHAT_ID = [""]


def _latest_digest_date():
    """扫描 exports/dtao_YYYY-MM-DD.digest.txt，返回最新日期；无则返回 None。"""
    files = glob.glob(os.path.join(HERE, "exports", "dtao_*.digest.txt"))
    dates = []
    for f in files:
        m = re.search(r"dtao_(\d{4}-\d{2}-\d{2})\.digest\.txt$", f)
        if m:
            dates.append(m.group(1))
    return max(dates) if dates else None


YESTERDAY_ALIASES = {"昨日", "昨天", "yesterday", "latest", "最新", "y"}


def _resolve_day(ds):
    """把「昨日/最新」等别名解析成 YYYY-MM-DD。"""
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return P._resolve_day_alias(ds, tz)


def _feedback_worker(day, feedback, chat_id):
    """后台线程：按用户反馈重总结并双推。

    必须走**独立子进程**（不能像早期版本那样直接调用 P.resummarize_with_feedback）：
    监听是常驻进程，import 时已把 pipeline.py 当时的代码固化在内存里，
    之后修改提示词/模板都不会生效。历史事故：/retry 用了旧提示词，
    把日报里已删除的「重点子网动向」「潜力观察」板块又写了回来。
    """
    try:
        send(chat_id, "🧠 正在按你的反馈重总结 %s（独立进程，使用最新提示词）…" % day)
        _run_pipeline_sync(["resummarize", day, feedback], chat_id)
    except Exception as e:
        log("反馈重总结失败：%s" % e)
        try:
            send(chat_id, "❌ 重总结失败：%s" % e)
        except Exception:
            pass


def run_feedback(day, feedback, chat_id):
    threading.Thread(target=_feedback_worker, args=(day, feedback, chat_id), daemon=True).start()
    log("已按用户反馈启动重总结：%s / %s" % (day, feedback[:60]))


def _research_sn_of_feedback(feedback, day=None, within_hours=72):
    """判断这条反馈针对的是不是某个子网的**投研报告**；是则返回 SN 编号。

    规则（三条同时满足才返回，宁可退回日报也不能误判）：
      1. 反馈里出现了子网编号（`127` / `SN127` / `#127`）或已建档子网名；
      2. 该子网档案里有投研报告；
      3. 最近一份报告足够新（默认 72 小时内）—— 用户是对刚推出的报告提意见，
         而不是翻三个月前的旧账（那种情况更可能是想改当天日报）。
    day 给了日期时，只认那天出的报告。
    """
    import subnet_archive as SA
    cfg = P._CFG or P.load_config()
    arc_dir = SA.get_arc_dir(cfg)

    # 0) 反馈里明确指向日报 → 一律走日报重总结，不要抢。
    #    实测「今天日报漏了 SN23 的进展」被误判成投研重做，用户要改的是日报。
    if re.search(r"日报|水聊|周报|今天的总结|当日总结", feedback):
        return None
    # 明确指向投研的措辞，可提高后续判定权重（用在最后一步的兜底）
    _want_research = bool(re.search(r"投研|跟踪报告|重要事件|研究报告", feedback))

    # 1) 从反馈里找编号：优先 SNxx / #xx，其次裸数字（1-255）
    sn = None
    m = re.search(r"(?i)\b(?:sn|subnet)\s*#?\s*(\d{1,3})\b", feedback) or \
        re.search(r"#\s*(\d{1,3})\b", feedback)
    if m:
        sn = int(m.group(1))
    if sn is None:
        # 裸数字：只认 1-255，且排除日期样式的数字（2026 之类）
        for tok in re.findall(r"(?<!\d)(\d{1,3})(?!\d)", feedback):
            n = int(tok)
            if 1 <= n <= 255:
                sn = n
                break
    if sn is None:
        # 也可能只写了子网名（hotfloat / Zils）
        try:
            kb = SA.load_kb()
            nidx = SA.name_index(kb)
            for tok in re.findall(r"[A-Za-z][\w.\-]{2,30}", feedback):
                low = SA.ascii_name(tok).lower()
                if low in nidx:
                    sn = nidx[low]
                    break
        except Exception:
            sn = None
    if sn is None:
        return None

    # 2) 该子网必须有投研报告
    arc = SA.load_archive(arc_dir, sn) or {}
    reps = [r for r in (arc.get("reports") or []) if r.get("date")]
    if not reps:
        return None

    # 3) 报告要够新（或正好是指定那天）
    reps.sort(key=lambda r: r.get("date") or "", reverse=True)
    newest = reps[0].get("date") or ""
    if day:
        if not any(r.get("date") == day for r in reps):
            return None
    else:
        try:
            d = datetime.datetime.strptime(newest, "%Y-%m-%d")
            age_h = (datetime.datetime.now() - d).total_seconds() / 3600.0
        except Exception:
            return None
        # 72 小时内算「刚出的报告」；超过 72 小时但用户明确说「投研/跟踪报告」也认，
        # 因为那是在点名要改投研而不是日报。
        if age_h > within_hours and not _want_research:
            return None
    log("反馈命中投研：SN%d（最近报告 %s），走投研重做而非日报重总结" % (sn, newest))
    return sn


def _start_research(sn_arg, days_n, chat_id, feedback=""):
    """解析子网 → 回执 → 后台跑投研。裸编号、/投研、/retry 投研修正共用这一条路径。

    先在这里解析一次的原因：① 认不出就当场回提示，不让用户等 1 分钟才收到报错；
    ② 输入是名字（hotfloat）时，回执要显示真正的 SN 编号，不能拼成「SNhotfloat」。
    feedback 非空表示这是对上一份报告的修正重做，回执要说明「按你的反馈重做」。
    """
    import subnet_archive as SA
    cfg = P._CFG or P.load_config()
    sn = SA.resolve_sn(sn_arg, cfg)
    if sn is None:
        send(chat_id, "❌ 认不出子网：%s\n"
                      "可以直接写编号：/投研 29\n"
                      "也可以发 /档案 看全部已建档子网（含编号）。" % sn_arg)
        return
    name = ""
    try:
        idx = SA.load_all_index(SA.get_arc_dir(cfg)).get("subnets") or {}
        name = (idx.get(str(sn)) or {}).get("name") or ""
        # 知识库里有些名字是用希腊字母冒充拉丁字母写的（hoτfloaτ），
        # 回执里换成可读写法，免得用户看到一串奇怪符号。
        name = SA.ascii_name(name) or name
    except Exception:
        pass
    days = int(days_n) if days_n else int(cfg.get("research_days", 14) or 14)
    if feedback:
        send(chat_id, "🔬 收到，**按你的反馈重做 SN%d%s 的投研报告**（回看 %d 天）。\n"
                      "反馈：%s\n"
                      "会重新核对名称/事实并重抓全网，约 1-3 分钟…"
                      % (sn, (" " + name) if name else "", days, feedback[:200]))
        # 反馈必须原样传（含空格标点）：pipeline 侧靠整句理解「改名为 X」这种句式，
        # 拆成 token 再拼回去容易丢中文标点和语气词。`~fb` 是分隔符（不能用 `--fb`，
        # argparse 会把 `-` 开头的 token 当选项直接报错）。
        run_pipeline(["research", str(sn)] + ([str(days_n)] if days_n else [])
                     + ["~fb", feedback], chat_id)
        return
    send(chat_id, "🔬 收到，正在做 **SN%d%s** 的重要事件跟踪报告（回看 %d 天档案）。\n"
                  "流程：调取本地子网档案 → 抓取链上数据站/官网/GitHub/新闻/搜索/X "
                  "→ 交叉核验 → 生成报告，约 1-3 分钟…"
                  % (sn, (" " + name) if name else "", days))
    run_pipeline(["research", str(sn)] + ([days_n] if days_n else []), chat_id)


# 裸编号触发：群里直接发「29」「SN29」「#29」「subnet 29」「29 30」即可。
# 刻意**只认编号、不认裸子网名** —— 群里英文词太多（score / liquidity / instant…），
# 裸名字误触的概率远大于便利收益，名字统一走 /投研 <名字>。
_BARE_SN_RE = re.compile(r"^(?:sn|subnet)?\s*#?\s*(\d{1,3})(?:\s+(\d{1,3}))?$", re.I)


def parse_bare_sn(text):
    """把「整条消息就是一个子网编号」解析成 (sn, days_or_None)，否则 None。"""
    m = _BARE_SN_RE.match((text or "").strip())
    if not m:
        return None
    sn = int(m.group(1))
    if not (1 <= sn <= 255):      # 子网编号合理范围，防止「2026」这类数字误触
        return None
    return sn, m.group(2)


def handle(text, chat_id):
    text = (text or "").strip()
    if not text:
        return
    parts = text.split()
    # 群里发指令可能带 @机器人名（/digest@Dtao_ribao_bot），剥掉后缀再匹配
    cmd = parts[0].lstrip("/").lower().split("@")[0]
    # 裸编号 = 子网投研：整条消息只有「29」/「SN29」/「#29」/「29 30」时直接开工。
    # 不带斜杠是为了手机上少打几个字（用户原话：从 TG 发子网编号就触发该子网投研）。
    if not parts[0].startswith("/"):
        hit = parse_bare_sn(text)
        if hit:
            sn, days_n = hit
            _start_research(str(sn), days_n, chat_id)
            return
    if cmd in ("help", "start"):
        send(chat_id, HELP)
    elif cmd in ("keyboard", "按钮"):
        # 重新唤出底部按钮栏（平时随回复自动收起）
        send(chat_id, "⌨️ 按钮栏已恢复（bot 回复后会自动收起；发 /keyboard 可再次唤出）",
             markup=_KEYBOARD)
    elif cmd in ("hidekeyboard", "收起", "关闭按钮", "nokeyboard"):
        # 显式收起底部按钮栏（等价于任何一个后续回复，但立刻生效、不用等）
        send(chat_id, "⌨️ 按钮栏已收起。需要时发 /keyboard 唤出。",
             markup=_REMOVE_KB)
    elif cmd == "digest":
        run_pipeline(["daily"] + parts[1:], chat_id)
    elif cmd in ("today", "jinri"):
        # 总结今天的聊天记录（北京时间今天 00:00 -> 现在）
        send(chat_id, "☀️ 收到，正在导出并总结今天的聊天记录，稍等…")
        run_pipeline(["daily", "今日"], chat_id)
    elif cmd == "weekly":
        run_pipeline(["weekly"], chat_id)
    elif cmd in ("chat", "shuichat", "water", "水聊", "闲聊", "水贴"):
        # /水聊                  -> 最近一份导出记录的水聊侧记
        # /水聊 昨日              -> 指定日期（可跟天数：/水聊 昨日 3）
        # 只读本地已有导出，不重新导出、不消耗配额
        rest = parts[1:]
        day = None
        if rest and re.match(r"^\d{4}-\d{2}-\d{2}$", rest[0]):
            day = rest[0]
            rest = rest[1:]
        elif rest and rest[0] in YESTERDAY_ALIASES | {"今日", "今天", "today", "t"}:
            day = _resolve_day(rest[0])
            rest = rest[1:]
        days_back = 1
        if rest and rest[0].isdigit():
            days_back = max(1, min(14, int(rest[0])))
        args = ["chat"] + ([day] if day else []) + [str(days_back)]
        send(chat_id, "💬 收到，正在整理%s的水聊侧记（只读已有导出记录，不重新导出）…"
             % (day or "最近一次导出"))
        run_pipeline(args, chat_id)
    elif cmd in ("archive", "建档", "存档", "archive_subnets"):
        # 建档不再作为日常指令（导出时已自动增量建档，见 pipeline.run_one_day）。
        # 保留这个入口只做一件事：**全量重建**（换机器 / 清过档案 / 想补历史）。
        send(chat_id, "ℹ️ 建档已自动化：每次导出日报时会自动把提到的子网写进档案"
                      "（已建档的增量补充，未建档的自动新建），无需手动触发。\n"
                      "如需用现有全部导出记录**全量重建**一次，请执行：\n"
                      "python pipeline.py archive\n"
                      "查看清单发 /档案，生成投研发 /投研 29。")
    elif cmd in ("research", "投研", "研报", "分析"):
        # /投研 29 [天数]        —— 子网重要事件跟踪报告
        # 数据链：本地档案 + 链上数据站 + 官网/GitHub/新闻/搜索/X 交叉验证
        rest = parts[1:]
        if not rest:
            send(chat_id, "用法：直接给我一个子网编号就行 ——\n"
                          "  29　　　（不带斜杠也可以，我认得出这是 SN29）\n"
                          "  /投研 29　/投研 SN29　/投研 hotfloat（子网名）\n"
                          "  /投研 29 30　第二个数字 = 回看多少天档案（默认 14）\n"
                          "不知道编号就先发 /档案 看清单。\n"
                          "本地没档案也能出报告：直接全网核验，出完自动新建档案。")
            return
        sn_arg = rest[0]
        rest = rest[1:]
        days_n = rest[0] if rest and rest[0].isdigit() else ""
        _start_research(sn_arg, days_n, chat_id)
    elif cmd in ("archive_list", "档案", "建档列表"):
        # /档案        列出所有已建档子网
        # /档案 29     查看该子网档案摘要
        import subnet_archive as SA
        cfg = P._CFG or P.load_config()
        arc_dir = SA.get_arc_dir(cfg)
        if len(parts) > 1:
            sn = SA.resolve_sn(parts[1], cfg)
            if sn is None:
                send(chat_id, "❌ 无法识别子网：%s" % parts[1])
                return
            arc = SA.load_archive(arc_dir, sn)
            if not arc:
                send(chat_id, "SN%d 还没有档案。建档是自动的——跑一次 /digest 就会把它建起来；"
                              "也可以直接发 /投研 %d 出报告，报告会自动新建档案。" % (sn, sn))
                return
            st = SA.stats_of(arc)
            rep = arc.get("reports") or []
            head = "🗂 **SN%d %s**" % (sn, arc.get("name") or "未知")
            if arc.get("note"):
                head += "\n_%s_" % arc["note"]
            lines = [head,
                     "业务：%s" % (arc.get("biz") or "未知"),
                     "群聊条目：%d 条（疑似裸编号 %d 条）· 覆盖 %s ~ %s · %d 人"
                     % (st["strong"], st["weak"], st["first"] or "-", st["last"] or "-", st["people"]),
                     "投研报告：%d 份%s" % (len(rep), ("（最近 %s）" % rep[0].get("date")) if rep else ""),
                     "档案：subnet_archive/SN%d/archive.md" % sn, "", "近期要点："]
            recent = []
            for day in sorted(arc.get("days") or {})[-2:]:
                for e in (arc["days"][day] or [])[-4:]:
                    recent.append("- %s %s **%s**：%s" % (
                        day[5:], e.get("time"), e.get("author"),
                        (e.get("zh") or e.get("text") or "")[:90]))
            for r in rep[:2]:
                recent.append("- %s 投研：%s" % (r.get("date", "")[5:], (r.get("title") or "")[:80]))
            lines += recent or ["（暂无群聊归档，可用 /投研 出报告）"]
            lines += ["", "发 /投研 %d 可生成跟踪报告" % sn]
            for ch in P.chunk_text("\n".join(lines), 3800):
                send(chat_id, _tg_bold(ch))
            return
        idx = SA.load_all_index(arc_dir)
        subs = idx.get("subnets") or {}
        if not subs:
            send(chat_id, "还没有任何子网档案。建档是自动的——跑一次 /digest 就会建起来。")
            return
        # 只有「疑似提及（weak）」或「只有投研报告」的档案单独归一组：
        # 只看条数容易误以为它在群里被认真讨论过，实际只是同名英文词擦到。
        real = [(sn, v) for sn, v in subs.items() if v.get("strong", 0) > 0]
        faint = [(sn, v) for sn, v in subs.items() if v.get("strong", 0) == 0]
        real.sort(key=lambda kv: -kv[1]["strong"])
        faint.sort(key=lambda kv: -(kv[1].get("reports") or 0))
        lines = ["🗂 已建档子网 %d 个（有实档 %d 个 / 共 %d 条）· 更新 %s"
                 % (len(subs), len(real), sum(v["strong"] for v in subs.values()),
                    idx.get("updated_at") or "-"), ""]
        for sn, v in real[:35]:
            lines.append("SN%-4s %-20s %3d 条 / %2d 人  %s~%s"
                         % (sn, (v["name"] or "未知")[:20], v["strong"], v["people"],
                            (v["first"] or "-")[5:], (v["last"] or "-")[5:]))
        if len(real) > 35:
            lines.append("…（有实档的共 %d 个，只显示前 35）" % len(real))
        if faint:
            lines += ["", "── 仅有疑似提及 / 仅投研报告（%d 个）──" % len(faint),
                      "、".join("SN%s" % sn for sn, _ in faint[:30])
                      + ("…" if len(faint) > 30 else "")]
        lines += ["", "查看单个：/档案 29　　生成投研：/投研 29"]
        for ch in P.chunk_text("\n".join(lines), 3800):
            send(chat_id, ch)
    elif cmd == "watch":
        n = None
        if len(parts) > 1:
            a = parts[1].lower()
            if a in ("all", "最新", "latest"):
                n = None
            elif a.isdigit():
                n = int(a)
        if n is None:
            files = sorted(glob.glob(os.path.join(HERE, "exports", "watchlist_*.txt")))
            if not files:
                send(chat_id, "（还没有潜力观察记录，先发 /digest 跑几天日报）")
                return
            txt = open(files[-1], encoding="utf-8").read()
            send(chat_id, "📊 最新潜力观察\n" + _tg_bold(txt))
            _feishu_watch(P._CFG, "📊 DTAO 潜力观察 · 最新", txt)
        else:
            head, merged = P.watch_summary(n)
            full = _tg_bold(head + "\n\n" + merged)
            chunks = P.chunk_text(full, 3800)
            for i, ch in enumerate(chunks, 1):
                send(chat_id, (("【%d/%d】\n" % (i, len(chunks))) if len(chunks) > 1 else "") + ch)
            # 飞书：跨日汇总较长，只推「热点统计」核心信号（完整明细已在 TG）
            _feishu_watch(P._CFG, "📊 DTAO 潜力观察 · 近%d天热点" % n, head)
    elif cmd in ("retry", "fix", "resummarize", "重总结", "修正"):
        # /retry 反馈内容             -> 对最近一次日报重总结
        # /retry 2026-10-30 反馈内容  -> 对指定日期重总结
        # /retry 127 已改名为 Zils    -> 若 127 刚做过投研，则**重做投研**（不是日报）
        rest = parts[1:]
        day = None
        if rest and re.match(r"^\d{4}-\d{2}-\d{2}$", rest[0]):
            day = rest[0]
            rest = rest[1:]
        elif rest and rest[0] in YESTERDAY_ALIASES:
            day = _resolve_day(rest[0])
            rest = rest[1:]
        feedback = " ".join(rest).strip()
        if not feedback:
            send(chat_id, "用法：/retry 反馈或要补充的事实\n"
                          "例：/retry SN34 是团队宣布转型，不是跑路\n"
                          "    /retry 127 已改名为 Zils   ← 127 刚做过投研时，会重做投研报告\n"
                          "也可指定日期：/retry 2026-10-30 反馈内容")
            return
        # 反馈带子网编号且该子网刚做过投研 → 走投研重做。
        # 为什么需要这一步：用户对「SN127 投研报告」提意见，如果落到日报重总结路径，
        # 会去重跑 11-07 的日报（466 条消息），既慢又完全没碰到投研报告 —— 实测就是这么错的。
        _sn_hint = _research_sn_of_feedback(feedback, day)
        if _sn_hint:
            _start_research(str(_sn_hint), None, chat_id, feedback=feedback)
            return
        if not day:
            day = _latest_digest_date()
        if not day:
            send(chat_id, "还没有可修订的日报，请先发 /digest 生成一次。")
            return
        run_feedback(day, feedback, chat_id)
    elif cmd in ("kb", "知识库", "子网表"):
        # /kb          查看周更状态（排程、上次成功、当前快照、待补跑原因）
        # /kb update   立即校对一次（拉链上身份 + 同步已建档案）
        arg = parts[1].lower() if len(parts) > 1 else ""
        if arg in ("update", "now", "run", "更新", "立即"):
            run_kb_update(chat_id)
        elif arg in ("off", "关"):
            send(chat_id, "周更开关在 config.json：kb_auto_update=false 即关闭"
                          "（改完需重启监听）。本次仍可通过 /kb update 手动更新。")
        else:
            try:
                import kb_weekly as K
                send(chat_id, K.status_text(P._CFG))
            except Exception as e:
                log("读取知识库状态失败：%s" % e)
                send(chat_id, "读取状态失败：%s" % e)
    elif cmd == "repush":
        day = None
        targets = []
        for a in parts[1:]:
            if re.match(r"^\d{4}-\d{2}-\d{2}$", a):
                day = a
            elif a in ("tg", "feishu"):
                targets.append(a)
        if not day:
            day = _latest_digest_date()
        if not day:
            send(chat_id, "没有可用的日报文件，请先发 /digest 生成一次。")
            return
        run_pipeline(["repush", day] + targets, chat_id)
    else:
        send(chat_id, "未知指令：%s\n%s" % (text, HELP))


def main():
    # 单实例：抢锁 → 抢不到就杀掉旧监听 → 重抢（acquire_single_instance 内部完成）。
    # 绝不允许两个进程同时轮询同一个 bot，否则一条指令会被执行多次、推多份日报。
    if not acquire_single_instance():
        log("❌ 已有 tg_listener 实例在运行，本进程退出（避免同一条指令被重复执行）")
        try:
            print("已有监听实例在运行，本进程退出。")
        except Exception:
            pass
        return
    atexit.register(release_single_instance)

    cfg = P.load_config()
    P._CFG = cfg
    # 记录本次加载的 pipeline 版本：常驻进程会把代码固化在内存里，
    # 之后若 pipeline.py 有更新，必须重启监听才会同步（这条日志便于排查）
    log("已加载 pipeline 模块 v%s（代码已固化在内存；pipeline.py 更新后需重启监听，"
        "杀进程即可，tg_guard 会在 20 秒后自动拉起）"
        % getattr(P, "__version__", "?"))
    token = cfg.get("telegram_bot_token")
    if not token:
        log("未配置 telegram_bot_token，退出"); return
    # 可选白名单：配了 allowed_user_ids 才限制仅这些用户触发；不配（默认）则开放给任何人。
    # 每日真实导出次数仍受 pipeline 内置节流保护，多人反复触发也不会敞开口消耗 token。
    allowed_users = [str(u) for u in (cfg.get("allowed_user_ids") or []) if u]

    # 启动即跳过历史消息，只处理之后的新指令
    try:
        init = tg_api(token, "getUpdates", {"limit": 1, "timeout": 0}, timeout=20)
        offset = 0
        if init.get("ok") and init.get("result"):
            offset = init["result"][-1]["update_id"] + 1
    except Exception as e:
        offset = 0
        log("启动清空历史失败（忽略）：%s" % e)

    if allowed_users:
        log("TG 指令监听已启动（白名单模式：仅 %s 可触发）... Ctrl+C 停止" % allowed_users)
    else:
        log("TG 指令监听已启动（开放模式：任意会话/任意用户均可触发，每日导出节流保护）... Ctrl+C 停止")

    # 知识库周更：每 7 天校对一次子网名/业务并同步已建档案。
    # 交给监听进程托管（而不是另注册一个计划任务），这样只需保证监听活着，
    # 且与日报共用同一把执行锁，不会同时写档案。
    if cfg.get("telegram_chat_id"):
        _LAST_CHAT_ID[0] = str(cfg.get("telegram_chat_id"))
    _start_kb_scheduler()

    last_hb = time.time()
    while True:
        try:
            # 心跳：每 10 分钟一行，排查「收不到指令」时可确认监听是否真的活着
            if time.time() - last_hb > 600:
                log("💓 心跳：监听存活中（offset=%s）" % offset)
                last_hb = time.time()
            resp = tg_api(token, "getUpdates", {"offset": offset, "timeout": 30}, timeout=40)
            if not resp.get("ok"):
                log("getUpdates 异常返回：%s" % resp)
                time.sleep(5)
                continue
            for u in resp.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat_id = str(msg.get("chat", {}).get("id", ""))
                from_id = str(msg.get("from", {}).get("id", ""))
                if not msg.get("text"):
                    continue
                if allowed_users and from_id not in allowed_users:
                    log("忽略非白名单用户 %s 的指令（开放模式需清空 allowed_user_ids）" % from_id)
                    continue
                # 幂等去重：同一条消息（同一 update 或同 chat+message_id）只处理一次。
                # 多实例、或进程重启后 offset 回退时，靠这一层兜底避免重复推送。
                key = "%s:%s" % (chat_id, msg.get("message_id"))
                if key in _SEEN_MSG_IDS:
                    log("跳过重复指令（已处理过 %s）" % key)
                    continue
                _SEEN_MSG_IDS[key] = u["update_id"]
                if len(_SEEN_MSG_IDS) > 500:
                    for _k in sorted(_SEEN_MSG_IDS, key=_SEEN_MSG_IDS.get)[:200]:
                        _SEEN_MSG_IDS.pop(_k, None)
                log("收到指令 chat=%s from=%s text=%s" % (chat_id, from_id, (msg.get("text") or "")[:80]))
                # 记住最近活跃会话：周更通知在没配 telegram_chat_id 时用它兜底
                if chat_id:
                    _LAST_CHAT_ID[0] = chat_id
                # 单条指令处理异常绝不影响主循环（第二层兜底）
                try:
                    handle(msg.get("text"), chat_id)
                except Exception as e:
                    log("处理指令异常（已忽略，监听继续）：%s" % e)
        except KeyboardInterrupt:
            log("已停止"); break
        except BaseException as e:
            # 兜底：连 BaseException 也不让监听退出（KeyboardInterrupt 上面已处理）
            # 网络抖动（SSL: UNEXPECTED_EOF / ConnectionReset）在国内访问 Telegram
            # 很常见，属于可自愈的小故障：退避从 1 秒起翻倍、封顶 30 秒，
            # 连续失败才拉长间隔。老代码固定 sleep(10)，指令高峰期会白白
            # 拖慢响应 10 秒。
            net_err = isinstance(e, (ssl.SSLError, OSError)) or \
                      "urlopen" in str(e) or "SSL" in str(e)
            if net_err:
                _net_fail = globals().get("_NET_FAIL", 0) + 1
                globals()["_NET_FAIL"] = _net_fail
                wait = min(30, 2 ** min(_net_fail - 1, 5))
                log("网络异常第 %d 次（%s），%d 秒后重连"
                    % (_net_fail, type(e).__name__, wait))
                time.sleep(wait)
            else:
                globals()["_NET_FAIL"] = 0
                log("监听异常(BaseException，已忽略继续运行)：%s" % e)
                time.sleep(10)


if __name__ == "__main__":
    main()
