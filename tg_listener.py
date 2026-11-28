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
  /社区今日（/community_today） 今日社区日报：今日00:00→当前
  /社区昨日（/community_yesterday） 昨日社区日报：昨日00:00→当前
  /社区确认                     确认冲突：点命令后 bot 依次问 SN、结论（分两步，少打字）
                                 也可一条写完：/社区确认 55 收入属实；末尾「已推翻」=推翻
  /社区重推                      按已确认结论重推社区日报确认版（不重新取数）
  /模型（/model）                大模型平台/模型切换：无参列出；/模型 sn28 或 /模型 sn53 切换
                                 （档案名不区分大小写，旧名 engy=sn53）；/模型 添加/删除 <名>
  /水聊（/water）                水聊侧记：只挑被日报过滤掉的闲聊，单独成一条
  /水聊 昨日 3                   指定日期 / 连续天数（只读已有导出，不重新导出）
  /watch                       查看潜力观察（默认最新一天，可跟天数 /watch 7 或 /watch all；同步推飞书）
  /repush 2026-10-23           补推（飞书+TG）
  /repush 2026-10-23 tg        仅补推 TG
  /repush 2026-10-23 feishu    仅补推飞书
  <ss58地址>，<SN编号> <备注>  补录回购地址（如 5FX6...，8回购地址）

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
import llm_profiles as LP
import tg_group_fetch as TGF

# 与 pipeline/community_fetch 一致：从 pythonw（无控制台）拉起 powershell/taskkill/
# pipeline 等控制台子进程时抑制黑色命令窗口闪现，不影响 PIPE 捕获输出。
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

def log(msg):
    """只写文件，不 print。

    与 tg_guard 同理：给带控制台的解释器留着 print，一旦窗口进入「快速编辑」
    标记状态就会把整个监听冻死在日志语句上。监听是后台服务，日志落盘即可。
    """
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = "[%s] %s\n" % (ts, msg)
        with open(os.path.join(HERE, "tg_listener.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _arc_label():
    """给回执用的档案路径文字（跟随 config.archive_dir，避免写死旧路径）。"""
    try:
        import subnet_archive as SA
        return SA.get_arc_dir(P._CFG)
    except Exception:
        return "config.json 里的 archive_dir"


HELP = (
    "🤖 DTAO 日报指令\n"
    "/digest 导出前一天并双推\n"
    "/digest 2026-10-23 导出指定日\n"
    "/digest 2026-10-22 2026-10-23 多日\n"
    "/today 导出并总结今天的聊天记录（北京时间今天 00:00 到现在）\n"
    "/water 或 /水聊 水聊侧记（群里闲聊/梗/跑题，单独一条，不写投研）\n"
    "   /水聊 昨日 指定日期；/水聊 昨日 3 连续 3 天（只读已有导出，不重新导出）\n"
    "/weekly 周报\n"
    "🌐 /社区今日、/社区昨日 Bittensor社区消息日报（dtt版），命令触发不定时：\n"
    "   /社区今日     取今日00:00→当前，总结后飞书+TG双推\n"
    "   /社区昨日     取昨日00:00→当前（/社区 不带参数也=昨日）\n"
    "   可加日期：/社区 2026-11-20；末尾加 nopush 只生成不推送\n"
    "   🧑‍⚖️ 冲突人工确认闭环（专区冲突核对后）：\n"
    "     /社区确认                       ← 点命令后 bot 分两步问：①SN编号 ②核实结论\n"
    "       例：发 /社区确认 → 回「55」→ 回「收入属实，已查链上」即可（5分钟内）\n"
    "       判定不实：结论结尾加「已推翻」；随时发 /取消 退出\n"
    "     /社区确认 55 收入属实            ← 也可一条写完（老写法仍有效）\n"
    "     /社区重推                        ← 按已确认结论重推确认版（不重新取数/不调AI）\n"
    "     确认后该结论会被投研和确认版日报引用。\n"
    "💬 /tg群、/群消息 TG群消息日报（dtt管理员群消息库，独立日报）：\n"
    "   /tg群（裸命令）     ← 两步：①回执 4 群按钮选群 ②再选「昨日/今日」→生成后推送\n"
    "   /tg群今日          取今日4个活跃群聊天，严格相关筛选后按群总结，飞书+TG双推\n"
    "   /tg群 2026-11-20   也可带日期；末尾加 nopush 只生成不推送\n"
    "   总结含：话题(标SN)/人物/观点三段式；转发的推特链接会抓正文核实后再提炼\n"
    "   （与 /社区 关键词日报完全隔离：这是群的原始聊天，只留SN/专名/关键人物相关）\n"
    "🤖 /模型 大模型平台切换（档案名不区分大小写：sn53/sn28；旧名 engy=sn53）：\n"
    "   /模型              列出档案+当前；/模型 sn28 切到 SN28 SayGM\n"
    "   /模型 sn53         切回 SN53 Engy（旧写法 /模型 engy 也认）\n"
    "   /模型 添加 <名> <base_url> <模型名> <key> 现场加平台（key会留聊天记录，注意）\n"
    "   /模型 删除 <名>    删除档案（不能删当前在用的）；/模型 当前 看当前\n"
    "   切换写盘即生效，下次日报/投研就是新模型，无需重启\n"
    "📊 /gamma Gamma 情报汇总（taostats 链上数据，两条线分列、不混算）：\n"
    "   /gamma（裸命令）    ← 分步菜单（7 项：列表/收支/周/月/跨网三类）\n"
    "   ── 机制1：钱包 → 子网 ──\n"
    "   /gamma 列表        已开通 Gamma 的子网（专门入口）\n"
    "   /gamma 收支        当前累计收支（铸造/花费/未花/烧alpha）\n"
    "   /gamma 周|月       北京时间自然周/月统计\n"
    "   /gamma SN51        单子网明细\n"
    "   ── 机制2：源子网 emission → 目标子网 ──\n"
    "   /gamma 跨网        跨子网支付总览（配置比例 + 实际流水）\n"
    "   /gamma 跨网周|跨网月  跨子网自然周/月统计\n"
    "   /gamma 跨网 SN120  指定源子网明细（也可 /gamma 跨网周 SN120）\n"
    "   末尾加 nopush 只生成不推送。Gamma=子网美元服务信用，1Γ≈1美元；\n"
    "   跨网是子网间买服务、非转账；钱包/子网均为链上标识，不推断身份\n"
    "━━ 子网档案 / 投研 ━━\n"
    "（建档全自动：每次导出日报时自动把提到的子网存档，已建档的增量补充、\n"
    "  未建档的自动新建，无需手动操作）\n"
    "★ 直接发子网编号即可出投研：发「29」= 生成 SN29 重要事件跟踪报告\n"
    "   （不发斜杠也行；SN29 / #29 / subnet 29 都认，发「29 30」= 回看 30 天）\n"
    "   /投研 29 与上面等价；也认子网名：/投研 hotfloat\n"
    "   报告内容：先调本地子网档案，再结合官网/推特/GitHub/新闻/全网检索交叉核验，\n"
    "   分 7 段（结论/事实基线/时间线/交叉验证/预期差/风险/跟踪清单）并双推\n"
    "/档案 列出所有已建档子网（不知道编号先发这个）；/档案 29 看某子网档案\n"
    "★ 补录回购地址：直接发「ss58地址，SN编号 备注」，如\n"
    "   5FX6...Ehk2，8回购地址  → 自动收录为 SN8 的回购地址（人工补录，不会被覆盖）\n"
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
# 运行状态：pid + 启动时加载的 pipeline 版本 + 启动时间。
# 为什么要落盘：新进程要能判断「现在跑着的是哪个版本」，
# 才能做到「同版本不打扰、旧版本杀掉接管」。只看 pid 文件是判断不出版本的。
_STATE_FILE = os.path.join(HERE, "tg_listener.state.json")
EXIT_RELOAD = 75        # 约定：代码有新版本，请守护立即重启我
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

    快路径优先：pid 文件里的进程活着就直接返回，**不启动 PowerShell**。
    为什么要这样：启动 PowerShell 拿进程列表要 5-15 秒（冷启动更久），
    每次拉起监听都白等这么久，日志里会长时间没有「已加载」这一行，
    看起来就像「进程起不来」。只有 pid 文件缺失/进程已死时，才去枚举兜底。
    """
    pids = set()
    me = os.getpid()

    # --- 快路径 ---
    try:
        with open(_PID_FILE, "r", encoding="utf-8") as f:
            pid = int((f.read() or "0").strip() or 0)
        if pid and pid != me and _pid_alive(pid):
            return {pid}
    except Exception:
        pass

    # 兜底枚举交给 _enum_listener_pids（慢，5-15 秒）：
    # 这里不做，是因为「pid 文件过期」是常态（进程被强杀就会留下），
    # 每次启动都为它等十几秒，反而让「已加载」那行迟迟不出现、看起来像起不来。
    # 真有拿不到 pid 的孤儿进程时，互斥体会先挡住我们，再走 _kill_stale_instances 枚举。
    return pids


def _enum_listener_pids():
    """枚举所有监听进程（慢：要起 PowerShell 拿命令行，5-15 秒）。

    只在互斥体被占、需要清理「pid 文件里没有的孤儿进程」时才调用。
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
            capture_output=True, text=True, timeout=25, creationflags=NO_WINDOW,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                if pid != me and _pid_alive(pid):
                    pids.add(pid)
    except Exception as e:
        log("枚举监听进程失败（改用 pid 文件）：%s" % e)
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
    """杀掉所有旧监听实例（含守护脚本留下的、pid 文件里没有的多重实例）。"""
    killed = []
    targets = set(_list_listener_pids()) | _enum_listener_pids()
    for pid in sorted(targets):
        if _kill_pid(pid):
            killed.append(pid)
            log("已终止旧监听进程 pid=%d" % pid)
    if killed:
        # 给旧进程一点时间释放互斥体和端口
        time.sleep(1.5)
    return killed


def _disk_version():
    """磁盘上 pipeline.py 的版本号。

    正则读文件而不是 import：import 拿到的是**进程内存里的旧模块**，
    这里要的恰恰是「磁盘上现在是哪一版」，两者不同才说明需要热重载。
    """
    try:
        with open(os.path.join(HERE, "pipeline.py"), "r", encoding="utf-8") as f:
            m = re.search(r'^__version__\s*=\s*["\']([^"\']+)', f.read(), re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _running_version(pid):
    """某个监听 pid 启动时加载的版本；读不到就返回 None（当作旧版本）。"""
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        if int(st.get("pid") or 0) == int(pid):
            return st.get("version") or None
    except Exception:
        pass
    return None


def _write_state():
    """把「本进程跑的是哪个版本」落盘，供 tgctl 与下一个新进程判断。"""
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(),
                       "version": getattr(P, "__version__", "?"),
                       "started_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)
    except Exception:
        pass


def _cleanup_state():
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            if int(json.load(f).get("pid") or 0) == os.getpid():
                os.remove(_STATE_FILE)
    except Exception:
        pass


def _respawn_on_exit(my_pid):
    """热重载的「独立接管进程」——不把身家性命全押在守护上。

    2026-11-10 事故：监听正确检测到新版本并以 75 退出，但守护卡在控制台
    print 上，13 分钟没把新版本拉起来，用户看到进程面板一直是 v1.11.0。
    现在退出前先拉一个完全独立的短命进程：等本进程死透 → 让守护先试
    2~3 秒 → 若仍没有监听，就由它直接用新代码拉起。正常情况下守护先得手，
    接管进程发现已有监听便安静退出，不会造成双实例。
    """
    try:
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        pyw = pyw if os.path.exists(pyw) else exe
        flags = (0x00000008 | 0x08000000) if os.name == "nt" else 0
        subprocess.Popen(
            [pyw, os.path.join(HERE, "tgctl.py"), "respawn", str(my_pid)],
            cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        log("已安排独立接管进程（守护无响应时由它拉起新版本）")
    except Exception as e:
        log("安排独立接管进程失败（仍依赖守护）：%s" % e)


def acquire_single_instance(retries=6, delay=0.8):
    """申请全局唯一实例，并且做「按版本接管」。

    规则（用户明确要求：更新版本后新进程杀掉旧进程）：
      · 已有**同版本**监听在跑 → 直接退出，绝不去打断它
        （以前是无条件杀，两个启动入口一叠加就互相残杀，
        表现是发了指令永远等不到回复）；
      · 已有**旧版本**监听在跑 → 杀掉它，由本进程接管
        （这样改完代码重启一次就生效，不用手工清进程）。

    抢锁顺序仍是「先抢锁，抢不到再杀旧进程，然后重抢」：反过来会有竞态 ——
    旧进程刚 TerminateProcess 时内核对象还引用着互斥体，新进程此时
    CreateMutex 会拿到 ERROR_ALREADY_EXISTS，于是刚要启动就判自己「已有实例」。
    """
    global _mutex_handle
    mine = getattr(P, "__version__", "?")
    disk = _disk_version() or mine

    existing = _list_listener_pids()
    if existing:
        same, old = [], []
        for pid in sorted(existing):
            v = _running_version(pid)
            (same if (v and v == disk) else old).append((pid, v))
        if same and not old:
            log("已有同版本（v%s）监听在跑（pid=%s），本进程退出，不打扰它"
                % (disk, "、".join(str(p) for p, _ in same)))
            return False
        for pid, v in old:
            if _kill_pid(pid):
                log("检测到旧版本（v%s）监听 pid=%d，已终止，由 v%s 接管"
                    % (v or "未知", pid, mine))
        if old:
            time.sleep(1.5)     # 等它释放互斥体/端口，别急着抢

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
    _write_state()
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
    _cleanup_state()


def tg_api(token, method, params=None, timeout=None):
    """Telegram API 单次调用。

    2026-11-10 修「发指令后很久没回执」：以前所有方法一律 timeout=40
    （那是给 getUpdates 长轮询留的），结果 sendMessage 也拿 40 秒去撞一条
    死通道，撞完才切到 2 秒就能通的代理 —— 实测一条即时回执要 **35.54 秒**
    才发出去。现在按方法分层：长轮询 40 秒，其余短请求 10 秒。
    """
    if timeout is None:
        # 短请求给 12 秒：境外请求统一代理优先（pipeline.net_open），
        # 代理正常时 1-2 秒返回，但节点偶发抖动会到 7-8 秒（实测样本
        # 1.6 / 2.3 / 4.0 / 6.9 / 7.4 / 7.6 秒）。给 8 秒会把「正在生成」
        # 这条回执抖丢；12 秒足以覆盖抖动，且代理真挂时还有 5 秒直连兜底，
        # 总上限 17 秒，远低于旧版直连优先的 30-40 秒。
        timeout = 40 if method == "getUpdates" else 12
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    params = dict(params or {})
    # 通道还没定（刚启动 / 两条都不通）时不要长轮询：长轮询要 30 秒，
    # 而探测陌生通道只给 10 秒，会把自己打断、永远走不到成功那一步。
    if method == "getUpdates" and P._TG_LINK.get("mode") is None:
        if int(params.get("timeout") or 0) > 0:
            params["timeout"] = 0
            timeout = min(timeout, 12)
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    # 走 pipeline.net_open：境外请求固定「代理优先 → 直连兜底」。
    # v1.13.0 前用通道记忆动态排序，实测直连/代理反复翻转，getUpdates 连挂十几次；
    # 现在顺序确定，代理地址由 proxy_url() 自动发现并定期复检。
    return json.loads(P.tg_open(req, timeout=timeout).decode("utf-8"))


# 底部按钮栏：默认不再常驻（v1.5.3 起，每次 bot 回复自动收起键盘）。
# 需要 时发 /keyboard 可重新唤出。
_KEYBOARD = {
    "keyboard": [
        ["/digest 昨日", "/repush 最新"],
        ["/投研 29", "/档案"],
        ["/社区今日", "/社区昨日"],
        ["/tg群今日", "/群消息"],
        ["/gamma 列表", "/gamma 跨网"],
        ["/模型", "/watch 7"],
        ["/weekly", "/help"]
    ],
    "resize_keyboard": True
}

_REMOVE_KB = {"remove_keyboard": True}


# ---------------------------------------------------------------------------
# 分步对话（多轮引导）：点一下 BotFather 菜单里的短命令，再按提示逐步填内容。
# 目前用于「社区冲突人工确认」——原写法要一整条 /社区确认 55 收入属实… 太长，
# 手机上难打。现在发 /community_confirm（裸命令）后，bot 依次问 SN 编号、
# 核实结论，用户分两条消息回复即可。
#
# 设计要点：
#   - 按 chat_id 隔离，互不干扰；每轮刷新超时（默认 5 分钟），超时自动作废。
#   - 拦截必须放在 handle() 最前面，早于「裸编号=投研」「ss58地址=补录」，
#     否则分步输入「55」会被当成投研指令抢走。
#   - 保留一次性写法：/社区确认 55 结论 仍然有效（老用户不受影响）。
#   - /取消、/cancel、/退出 随时中止；发其它命令则自动放弃当前流程。
# ---------------------------------------------------------------------------
_CONFIRM_FLOW = {}          # chat_id -> {"step": "sn"|"note", "ts":..., "sn":...}
_CONFIRM_FLOW_TTL = 300     # 秒；每轮操作都会重置


def _confirm_flow_clear(chat_id):
    _CONFIRM_FLOW.pop(str(chat_id), None)


def _confirm_flow_active(chat_id):
    st = _CONFIRM_FLOW.get(str(chat_id))
    if not st:
        return None
    if time.time() - st.get("ts", 0) > _CONFIRM_FLOW_TTL:
        _confirm_flow_clear(chat_id)
        return None
    return st


def _confirm_flow_consume(text, chat_id):
    """若该会话正处于分步确认中，消费本条消息并返回 True；否则返回 False。

    返回 True 时调用方直接 return，不再走命令分发/裸编号逻辑。
    """
    st = _confirm_flow_active(chat_id)
    if not st:
        return False
    t = (text or "").strip()
    low = t.lstrip("/").lower().split("@")[0]
    # 取消：中英文都认
    if low in ("取消", "cancel", "退出", "quit", "q"):
        _confirm_flow_clear(chat_id)
        safe_send(chat_id, "已取消社区冲突确认，未写入任何档案。")
        return True
    # 发来其它斜杠命令：放弃当前流程，让该命令正常执行
    if t.startswith("/"):
        _confirm_flow_clear(chat_id)
        return False
    # 第 1 步：输入 SN 编号
    if st["step"] == "sn":
        m = re.match(r"^(?:SN|#|subnet\s*)?(\d{1,4})$", t, re.I)
        if not m:
            safe_send(chat_id, "请回复子网编号（如 55），或发 /取消 退出。")
            return True
        st["sn"] = m.group(1)
        st["step"] = "note"
        st["ts"] = time.time()
        safe_send(chat_id,
                  "✅ 已记录 SN%s。\n"
                  "现在请回复人工核实结论，例如：\n"
                  "　收入数字属实，已查链上审计\n"
                  "若判定社区消息不实，结尾加「已推翻」，例如：\n"
                  "　取消回购是谣言，已推翻\n"
                  "发 /取消 可随时退出。" % st["sn"])
        return True
    # 第 2 步：输入核实结论 → 调用与一次性写法相同的执行体
    if not t:
        safe_send(chat_id, "结论不能为空，请重新输入，或发 /取消 退出。")
        return True
    sn = st.get("sn") or ""
    _confirm_flow_clear(chat_id)
    _run_community_confirm(sn, t, chat_id)
    return True


def _run_community_confirm(sn, note, chat_id):
    """社区冲突确认的统一执行体：分步对话与一次性写法共用，避免逻辑分叉。"""
    note = (note or "").strip()
    status = "confirmed"
    if note.endswith("已推翻") or note.endswith("不实"):
        status = "refuted"
        note = re.sub(r"(已推翻|不实)\s*$", "", note).strip()
        note = note.rstrip("，,。.；;、 \t")   # 去掉标记后清掉残留标点
    if not note:
        note = "（无补充结论）"
    day = _latest_community_day()
    send_async(chat_id, "🧑‍⚖️ 收到，正在把确认结论写入 SN%s 档案…" % sn)
    run_pipeline(["community_confirm", day, str(sn), note, status], chat_id, quiet=True)


# ---------------------------------------------------------------------------
# TG 群日报「分步选群 → 选日期」：裸命令 → 回执 4 群按钮 → 选群后回执「昨日/今日」
#   → 再选日期 → 才真正生成并推送。两轮都按 chat_id 隔离、5 分钟有效。
#   - callback_data：第1轮 tggrp:<groupId>|all|cancel；第2轮 tgday:yesterday|today|cancel
#   - 状态挡在 handle 最前面：期间文本按「群选项 / 日期选项」解析，不落到
#     「裸编号=投研」「ss58=地址补录」逻辑（与社区确认流程同理）。
# ---------------------------------------------------------------------------
_TGG_FLOW = {}          # chat_id -> {"step": "group"|"day", "gid":..., "ts":...}
_TGG_FLOW_TTL = 300


def _tgg_flow_clear(chat_id):
    _TGG_FLOW.pop(str(chat_id), None)


def _tgg_flow_active(chat_id):
    st = _TGG_FLOW.get(str(chat_id))
    if not st:
        return None
    if time.time() - st.get("ts", 0) > _TGG_FLOW_TTL:
        _tgg_flow_clear(chat_id)
        return None
    return st


def _tgg_group_keyboard():
    """第 1 轮：4 群 + 全部 + 取消。"""
    rows = [[{"text": TGF.GROUP_LABELS[gid], "callback_data": "tggrp:" + gid}]
            for gid in TGF.DEFAULT_GROUP_IDS]
    rows.append([{"text": "📊 全部 4 群", "callback_data": "tggrp:all"},
                 {"text": "❌ 取消", "callback_data": "tggrp:cancel"}])
    return {"inline_keyboard": rows}


def _tgg_day_keyboard():
    """第 2 轮：昨日 / 今日 / 取消。"""
    return {"inline_keyboard": [[
        {"text": "📅 昨日", "callback_data": "tgday:yesterday"},
        {"text": "☀️ 今日", "callback_data": "tgday:today"},
        {"text": "❌ 取消", "callback_data": "tgday:cancel"},
    ]]}


def _tgg_prompt(chat_id):
    """裸命令回执第 1 轮：抛出 4 个群组可选项。"""
    _TGG_FLOW[str(chat_id)] = {"step": "group", "gid": None, "ts": time.time()}
    lines = ["💬 第 1 步：请选择要总结的 TG 群（点按钮，5 分钟内有效）："]
    for i, gid in enumerate(TGF.DEFAULT_GROUP_IDS, 1):
        lines.append("  %d️⃣ %s" % (i, TGF.GROUP_LABELS[gid]))
    lines.append("  点「全部 4 群」维持原样；也可直接回复序号 1-4 / 群名。")
    safe_send(chat_id, "\n".join(lines), markup=_tgg_group_keyboard())


def _tgg_ask_day(chat_id, gid=None):
    """第 2 轮：选定群后，回执「昨日/今日」让用户选时间范围。"""
    _TGG_FLOW[str(chat_id)] = {"step": "day", "gid": gid, "ts": time.time()}
    label = TGF.group_label(gid) if gid else "全部 4 群"
    safe_send(chat_id,
              "🗓 第 2 步：【%s】已选中，请选择总结哪天的消息：\n"
              "  点「昨日」/「今日」，或回复 昨日 / 今日 / 具体日期（如 2026-11-20）。" % label,
              markup=_tgg_day_keyboard())


def _tgg_run(chat_id, gid=None, day="昨日"):
    """真正触发单群（gid 非空）或全群总结，day 走 pipeline 的日期别名解析。"""
    _tgg_flow_clear(chat_id)
    label = TGF.group_label(gid) if gid else "全部 4 群"
    day_txt = {"yesterday": "昨日", "today": "今日"}.get(day, day)
    args = ["tg_group", day_txt] + ([gid] if gid else [])
    start_msg = ("💬 收到，正在总结【%s】%s的群消息，生成完毕后推送（飞书 + TG）…"
                 % (label, ("「%s」" % day_txt)))
    # 不在入队瞬间发「正在总结」——排队时那条会和「已排队」自相矛盾。
    # 交给 run_pipeline：立即执行 or 从队列拉起时才发。
    run_pipeline(args, chat_id, quiet=True, start_msg=start_msg)


def _tgg_callback(cq, chat_id, from_id, allowed_users):
    """处理内联按钮 callback_query（两轮）。返回 True 表示已消费该 update。"""
    data = (cq.get("data") or "").strip()
    if not (data.startswith("tggrp:") or data.startswith("tgday:")):
        return False
    cqid = cq.get("id") or ""

    def _ack(text=""):
        try:
            p = {"callback_query_id": cqid}
            if text:
                p["text"] = text
            # 回调可能比主流程的配置装载更早到达；P._CFG 为 None 时必须兜底读盘，
            # 否则按钮会一直转圈并刷「'NoneType' object is not subscriptable」。
            _cfg = P._CFG or P.load_config()
            tg_api(_cfg["telegram_bot_token"], "answerCallbackQuery", p, timeout=10)
        except Exception as e:
            log("answerCallbackQuery 失败（忽略）：%s" % e)

    if allowed_users and from_id not in allowed_users:
        _ack("无权限")
        return True
    active = _tgg_flow_active(chat_id)
    val = data.split(":", 1)[1].strip()
    if val == "cancel":
        _tgg_flow_clear(chat_id)
        _ack("已取消")
        safe_send(chat_id, "已取消 TG 群日报。")
        return True
    if not active:
        _ack("已超时，请重发命令")
        safe_send(chat_id, "⏱ 选群已超时（5 分钟），请重新发 /群消息 再选。")
        return True

    # 第 1 轮：选群 → 追问日期（不直接生成）
    if data.startswith("tggrp:"):
        if val == "all":
            _ack("已选全部 4 群，请选日期")
            _tgg_ask_day(chat_id, None)
            return True
        if val in TGF.GROUP_LABELS:
            _ack("已选 %s，请选日期" % TGF.GROUP_LABELS[val])
            _tgg_ask_day(chat_id, val)
            return True
        _ack("无效选项")
        return True

    # 第 2 轮：选日期 → 真正生成
    if active.get("step") != "day":
        _ack("请先选群")
        safe_send(chat_id, "请先选群（点上一轮的群按钮），再选昨日/今日。")
        return True
    gid = active.get("gid")
    if val in ("yesterday", "today"):
        _ack("正在总结" + ("昨日" if val == "yesterday" else "今日"))
        _tgg_run(chat_id, gid, val)
        return True
    _ack("无效选项")
    return True


def _tgg_flow_consume(text, chat_id):
    """两轮状态下消费文本回复：第1轮=群选项，第2轮=日期选项。不在流程则返回 False。"""
    st = _tgg_flow_active(chat_id)
    if not st:
        return False
    t = (text or "").strip()
    low = t.lstrip("/").lower().split("@")[0]
    if low in ("取消", "cancel", "退出", "quit", "q"):
        _tgg_flow_clear(chat_id)
        safe_send(chat_id, "已取消 TG 群日报。")
        return True
    if t.startswith("/"):
        _tgg_flow_clear(chat_id)
        return False

    if st.get("step") == "group":
        if low in ("全部", "all", "5", "全群"):
            _tgg_ask_day(chat_id, None)
            return True
        if low.isdigit() and 1 <= int(low) <= len(TGF.DEFAULT_GROUP_IDS):
            _tgg_ask_day(chat_id, TGF.DEFAULT_GROUP_IDS[int(low) - 1])
            return True
        for gid in TGF.DEFAULT_GROUP_IDS:
            name = TGF.GROUP_LABELS[gid].lower()
            if low and (low in name or name in low):
                _tgg_ask_day(chat_id, gid)
                return True
        safe_send(chat_id, "请回复 1-4 选群、5 全部，或点上方按钮；发 /取消 退出。")
        return True

    # 第 2 轮：日期
    gid = st.get("gid")
    if low in ("1", "昨日", "昨天", "yesterday", "y"):
        _tgg_run(chat_id, gid, "yesterday")
        return True
    if low in ("2", "今日", "今天", "today", "t"):
        _tgg_run(chat_id, gid, "today")
        return True
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        _tgg_run(chat_id, gid, t)
        return True
    safe_send(chat_id, "请回复「昨日」或「今日」（也可写 2026-11-20 这样的日期）；发 /取消 退出。")
    return True


# ---------------------------------------------------------------------------
# Gamma 情报汇总：分段式命令（照 TG 群日报两轮状态机模式）
#   第 1 轮：功能菜单（开通子网列表 / 收支总览 / 周统计 / 月统计）
#   第 2 轮：统计类再选范围（全网 / 指定 SN）；列表直接出结果。
#   callback 前缀 gama: (功能) / gaday: (范围)，与 tggrp/tgday 隔离。
# ---------------------------------------------------------------------------
_GA_FLOW = {}          # chat_id -> {"step": "action"|"scope", "act":..., "ts":...}
_GA_FLOW_TTL = 300


def _ga_flow_clear(chat_id):
    _GA_FLOW.pop(str(chat_id), None)


def _ga_flow_active(chat_id):
    st = _GA_FLOW.get(str(chat_id))
    if not st:
        return None
    if time.time() - st.get("ts", 0) > _GA_FLOW_TTL:
        _ga_flow_clear(chat_id)
        return None
    return st


_GA_ACTS = [
    ("list", "📋 开通子网列表"),
    ("snapshot", "💰 当前收支总览"),
    ("week", "📅 周统计"),
    ("month", "🗓 月统计"),
    ("cross", "🔁 跨子网支付"),
    ("crossweek", "📆 跨子网周统计"),
    ("crossmonth", "🗓 跨子网月统计"),
]
_GA_ACT_LABEL = dict(_GA_ACTS)


def _ga_action_keyboard():
    rows = [[{"text": lab, "callback_data": "gama:" + k}] for k, lab in _GA_ACTS]
    rows.append([{"text": "❌ 取消", "callback_data": "gama:cancel"}])
    return {"inline_keyboard": rows}


def _ga_scope_keyboard():
    return {"inline_keyboard": [[
        {"text": "🌐 全网", "callback_data": "gaday:all"},
        {"text": "🔍 指定子网", "callback_data": "gaday:sn"},
        {"text": "❌ 取消", "callback_data": "gaday:cancel"},
    ]]}


def _ga_prompt(chat_id):
    """裸命令第 1 轮：抛功能菜单。"""
    _GA_FLOW[str(chat_id)] = {"step": "action", "act": None, "ts": time.time()}
    lines = ["📊 第 1 步：Gamma 情报要查什么？（点按钮，5 分钟内有效）："]
    for i, (_, lab) in enumerate(_GA_ACTS, 1):
        lines.append("  %d️⃣ %s" % (i, lab))
    lines.append("  也可直接回复序号 1-7 或关键词（列表/收支/周/月/跨网/跨网周/跨网月）。")
    safe_send(chat_id, "\n".join(lines), markup=_ga_action_keyboard())


def _ga_ask_scope(chat_id, act):
    """第 2 轮：统计类选全网/指定子网；列表类直接出结果。"""
    if act == "list":
        _ga_run(chat_id, "list", None)
        return
    _GA_FLOW[str(chat_id)] = {"step": "scope", "act": act, "ts": time.time()}
    is_cross = str(act).startswith("cross")
    who = "源子网编号" if is_cross else "SN 编号"
    safe_send(chat_id,
              "📊 第 2 步：【%s】请选择范围：点「全网」或「指定子网」，"
              "也可回复 全网 / 具体 %s（如 51）。" % (
                  _GA_ACT_LABEL.get(act, act), who),
              markup=_ga_scope_keyboard())


def _ga_run(chat_id, act, sn=None):
    """真正触发 Gamma 查询。

    关键（v1.29.1 修复）：查完不清状态，而是回到第 1 轮功能菜单并刷新 5 分钟计时。
    这样用户在时效内可连续点菜单里任意多项（列表→收支→周→跨网…），
    不再出现「查完一项后再点其他项就报已超时」。只有主动取消 / 真正超时才清。
    """
    _GA_FLOW[str(chat_id)] = {"step": "action", "act": None, "ts": time.time()}
    act_txt = _GA_ACT_LABEL.get(act, act)
    scope_txt = ("SN%s" % sn) if sn is not None else "全网"
    send_async(chat_id,
               "📊 收到，正在查询 Gamma【%s·%s】链上数据，生成完毕后推送（飞书 + TG）…\n"
               "（菜单仍保留，5 分钟内可继续点其他项）"
               % (act_txt, scope_txt))
    args = ["gamma", act] + ([str(sn)] if sn is not None else [])
    run_pipeline(args, chat_id, quiet=True)


def _ga_callback(cq, chat_id, from_id, allowed_users):
    """处理 Gamma 分段按钮回调。返回 True 表示已消费。"""
    data = (cq.get("data") or "").strip()
    if not (data.startswith("gama:") or data.startswith("gaday:")):
        return False
    cqid = cq.get("id") or ""

    def _ack(text=""):
        try:
            p = {"callback_query_id": cqid}
            if text:
                p["text"] = text
            _cfg = P._CFG or P.load_config()
            tg_api(_cfg["telegram_bot_token"], "answerCallbackQuery", p, timeout=10)
        except Exception as e:
            log("answerCallbackQuery 失败（忽略）：%s" % e)

    if allowed_users and from_id not in allowed_users:
        _ack("无权限")
        return True
    val = data.split(":", 1)[1].strip()
    if val == "cancel":
        _ga_flow_clear(chat_id)
        _ack("已取消")
        safe_send(chat_id, "已取消 Gamma 查询。")
        return True
    active = _ga_flow_active(chat_id)
    if not active:
        _ack("已超时，请重发命令")
        safe_send(chat_id, "⏱ Gamma 查询已超时（5 分钟），请重新发 /gamma 再选。")
        return True

    if data.startswith("gama:"):
        if val not in _GA_ACT_LABEL:
            _ack("无效选项")
            return True
        _ack("已选 %s" % _GA_ACT_LABEL[val])
        _ga_ask_scope(chat_id, val)
        return True

    # 第 2 轮：范围
    if active.get("step") != "scope":
        _ack("请先选查询类型")
        safe_send(chat_id, "请先选查询类型（点上一轮的功能按钮）。")
        return True
    act = active.get("act")
    if val == "all":
        _ack("正在查询全网")
        _ga_run(chat_id, act, None)
        return True
    if val == "sn":
        _ack("请回复 SN 编号")
        safe_send(chat_id, "请直接回复要查询的子网编号（如 51），5 分钟内有效；发 /取消 退出。")
        return True
    _ack("无效选项")
    return True


def _ga_flow_consume(text, chat_id):
    """Gamma 分段状态下消费文本回复。不在流程返回 False。"""
    st = _ga_flow_active(chat_id)
    if not st:
        return False
    t = (text or "").strip()
    low = t.lstrip("/").lower().split("@")[0]
    if low in ("取消", "cancel", "退出", "quit", "q"):
        _ga_flow_clear(chat_id)
        safe_send(chat_id, "已取消 Gamma 查询。")
        return True
    if t.startswith("/"):
        _ga_flow_clear(chat_id)
        return False

    if st.get("step") == "action":
        alias = {"1": "list", "列表": "list", "开通": "list",
                 "2": "snapshot", "收支": "snapshot", "总览": "snapshot",
                 "3": "week", "周": "week", "周统计": "week",
                 "4": "month", "月": "month", "月统计": "month",
                 "5": "cross", "跨网": "cross", "跨子网": "cross", "跨子网支付": "cross",
                 "6": "crossweek", "跨网周": "crossweek", "跨子网周": "crossweek",
                 "7": "crossmonth", "跨网月": "crossmonth", "跨子网月": "crossmonth"}
        act = alias.get(low)
        if not act:
            safe_send(chat_id, "请回复 1-7 或 列表/收支/周/月/跨网；发 /取消 退出。")
            return True
        _ga_ask_scope(chat_id, act)
        return True

    # 第 2 轮：范围
    act = st.get("act")
    if low in ("全网", "all", "全部", "1"):
        _ga_run(chat_id, act, None)
        return True
    m = re.fullmatch(r"(?:sn)?(\d{1,4})", low)
    if m:
        _ga_run(chat_id, act, int(m.group(1)))
        return True
    safe_send(chat_id, "请回复「全网」或 SN 编号（如 51）；发 /取消 退出。")
    return True


def send(chat_id, text, markup=None):
    # 纯文本发送，避免日志里的特殊字符破坏 Markdown 解析；
    # 默认随回复附 remove_keyboard —— bot 每次回复后底部按钮栏自动收起
    tg_api(P._CFG["telegram_bot_token"], "sendMessage",
           {"chat_id": chat_id, "text": text, "disable_web_page_preview": True,
            "reply_markup": json.dumps(markup or _REMOVE_KB)})


def send_async(chat_id, text, markup=None):
    """后台线程发消息：网络慢时不要把主流程卡在一条回执上。

    Telegram 抖动时一次 sendMessage 能阻塞 4-10 秒，而回执只是「告知」，
    没有任何后续逻辑依赖它 —— 放后台发，流水线可以立刻开始跑。

    复用 safe_send（带一次短退避重试），回执照样尽力送达，
    但**绝不阻塞主循环**：用户发完指令立刻就能看到「收到」，工作也已开始。
    """
    def _go():
        try:
            safe_send(chat_id, text, markup, retries=2)
        except Exception as e:
            log("异步发送失败（忽略）：%s" % e)
    threading.Thread(target=_go, daemon=True).start()


def safe_send(chat_id, text, markup=None, retries=1):
    """发回执专用：失败只记日志，**绝不向上抛**，并短退避重试。

    为什么必须单独包一层（2026-11-10 实测的假故障）：
    用户发 71，投研 20:40:09 **已经全部跑完并推送成功**，监听却在 20:40:40
    记「流水线线程异常：<urlopen error timed out>」并给用户发了「❌ 触发异常」——
    失败的根本不是投研，而是跑完以后那条「✅ 已生成」的**收尾回执**超时。
    回执挂在 try 里一路抛到 _pipeline_worker，被当成整条流水线失败，用户白担心。

    回执没有下游依赖，正确语义是「尽力发，发不出去就算了」。
    """
    for i in range(max(1, int(retries))):
        try:
            send(chat_id, text, markup)
            return True
        except Exception as e:
            if i + 1 >= max(1, int(retries)):
                log("回执发送失败（已忽略，不影响任务本身）：%s" % str(e)[:120])
                return False
            time.sleep(2 * (i + 1))
    return False


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
# 忙时不丢请求：FIFO 队列 [(args, chat_id, quiet, start_msg)]。
# 2026-11-26 实测：daily 正文推送完后还在跑档案翻译/落档（执行锁仍占用），
# 此时点群日报被旧逻辑直接丢弃，用户只看到「上一个任务仍在执行中」，
# 以为机器人坏了。现在排队，前序结束自动执行，无需重复发命令。
_PIPELINE_QUEUE = []


def _kill_proc_tree(proc):
    """超时后杀掉子进程及其整个进程树（Windows 用 taskkill /T）。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
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


def _daily_push_flags(out):
    """从流水线真实输出判断日报正文有没有推送成功。

    空报告（0 条）会走「只落档、不推送」分支，日志里没有这两行成功标记。
    回执必须据此说话，不能只凭「XX日报完成」就声称已推送。
    """
    return ("飞书推送完成" in out, "Telegram 推送分片" in out)


def _daily_receipt(out, args):
    """社区日报 / TG群日报回执：如实区分「已推送」「空报告未推送」「推送异常」。"""
    out = out or ""
    args = list(args or [])
    feishu_ok, tg_ok = _daily_push_flags(out)
    push_txt = ("（飞书 + TG）" if feishu_ok and tg_ok else
                "（飞书）" if feishu_ok else
                "（TG）" if tg_ok else "")
    kind = args[0] if args else ""

    if kind == "community":
        m = re.search(r"社区日报完成：\s*(\d{4}-\d{2}-\d{2})\s*（(\d+)\s*条消息，(\d+)\s*个子网落档）", out)
        if m:
            day, total, sns = m.group(1), m.group(2), m.group(3)
            if int(total or 0) == 0:
                receipt = ("🌐 社区日报已生成：当日无命中消息（空报告，未推送正文）\n"
                           "📅 %s　📊 %s 条消息 · %s 个子网落档\n"
                           "📄 exports/community/community_%s.digest.txt"
                           % (day, total, sns, day))
            elif not push_txt:
                receipt = ("🌐 社区日报已生成，但未见推送成功记录（请查 run.log）\n"
                           "📅 %s　📊 %s 条消息 · %s 个子网落档\n"
                           "📄 exports/community/community_%s.digest.txt"
                           % (day, total, sns, day))
            else:
                receipt = ("🌐 社区日报已生成并推送%s\n"
                           "📅 %s　📊 %s 条消息 · %s 个子网落档\n"
                           "📄 exports/community/community_%s.digest.txt"
                           % (push_txt, day, total, sns, day))
        elif "社区关键词监控当日无命中" in out:
            receipt = "🌐 社区日报已生成：当日无命中消息（空报告，未推送正文）"
        else:
            receipt = "🌐 社区日报流程已结束（详情见群内推送或 run.log）"
        mf = re.search(r"社区日报交叉核对：\s*(\d+)\s*处「待人工确认」", out)
        if mf:
            receipt += "\n⚠️ 含 %s 处「待人工确认」，请核对正文后再采信" % mf.group(1)
        return receipt

    if kind == "tg_group":
        if "TG群数据源停更：" in out:
            detail = out.split("TG群数据源停更：", 1)[1].splitlines()[0].strip()
            return ("💬 TG群消息日报未生成：数据源停更（非群内无消息）\n%s\n"
                    "请检查 dtt 群消息采集/上传机器人后重取" % detail)
        m = re.search(r"TG群日报完成：\s*(\d{4}-\d{2}-\d{2})\s*(?:【([^】]+)】)?\s*（群消息\s*(\d+)\s*条，严格相关\s*(\d+)\s*条，(\d+)\s*个子网落档）", out)
        if m:
            day, gname, total, rel, sns = m.groups()
            gid = next((x for x in args[1:] if re.fullmatch(r"\d{6,15}", str(x))), None)
            scope = ("【%s】" % gname) if gname else ""
            fname = "tg_group_%s%s.digest.txt" % (day, ("_" + gid) if gid else "")
            stat = ("📅 %s　📊 群消息 %s 条 → 严格相关 %s 条 · %s 个子网落档\n"
                    "📄 exports/tg_group/%s" % (day, total, rel, sns, fname))
            if int(total or 0) == 0 or int(rel or 0) == 0:
                return ("💬 TG群消息日报%s已生成：当日无相关内容（空报告，未推送正文）\n%s"
                        % (scope, stat))
            if not push_txt:
                return ("💬 TG群消息日报%s已生成，但未见推送成功记录（请查 run.log）\n%s"
                        % (scope, stat))
            return ("💬 TG群消息日报%s已生成并推送%s\n%s" % (scope, push_txt, stat))
        if "当日无消息" in out or "无与 Bittensor 子网相关" in out:
            return "💬 TG群日报已生成：当日无相关内容（空报告，未推送正文）"
        return "💬 TG群日报流程已结束（详情见群内推送或 run.log）"

    return "✅ 已完成：%s" % " ".join(args)


def _run_pipeline_sync(args, chat_id):
    """真正执行流水线（同步逻辑，供后台线程调用）。"""
    cmd = [sys.executable, os.path.join(HERE, "pipeline.py")] + args
    # 强制子进程以 UTF-8 输出：中文 Windows 默认 GBK，父进程按 UTF-8 解码会变乱码
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONFAULTHANDLER"] = "1"   # 进程级崩溃时能留下堆栈，便于排查
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=env,
                            creationflags=NO_WINDOW)
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
            safe_send(chat_id, "⏱ 执行超时（>%d 分钟），已强制终止并清理子进程，请查 run.log"
                 % (timeout // 60))
        except Exception:
            pass
        return
    out = out or ""
    if rc == 0:
        # 成功：日报已由 pipeline 自行推送到飞书+TG，这里只回简洁确认，不回传大段日志
        if args and args[0] == "chat":
            safe_send(chat_id, "✅ 水聊侧记已生成并推送（飞书 + TG）\n"
                          "只取被日报过滤掉的闲聊，不写投研；全程未重新导出聊天记录。")
            return
        if args and args[0] == "archive":
            # 从日志里抓建档统计（persistent 归档了多少子网、新增多少条）
            m = re.search(r"子网建档完成：\d+ 个子网有档案，本次新增 (\d+) 条（累计 (\d+) 条）", out)
            detail = ("\n📊 本次新增 %s 条，档案累计 %s 条" % (m.group(1), m.group(2))) if m else ""
            safe_send(chat_id, "✅ 子网建档完成%s\n"
                          "按 SN 编号逐个存放于 %s\\SNxx\\（archive.json + archive.md，"
                          "含原文与中文译文、按时间排序的人物观点索引）。\n"
                          "发 /档案 看清单，发 /投研 29 做单子网跟踪报告。" % (detail, _arc_label()))
            return
        if args and args[0] == "research":
            m = re.search(r"报告已存：(.+?)（(\d+) 字）", out)
            detail = ("\n📄 %s\n📝 %s 字" % (m.group(1), m.group(2))) if m else ""
            # v1.21.3：回执补一句「当日 dtt 社区消息统计」——数字来自投研子进程固定日志行，
            # 不重新取数、不调 AI；无命中/取数失败/关闭也如实写明。
            mdtt = re.search(r"当日dtt社区消息：([^\n]+)", out)
            dtt_line = ("\n📊 当日 dtt 社区消息：%s" % mdtt.group(1).strip()) if mdtt else ""
            safe_send(chat_id, "✅ 子网投研报告已生成并推送（飞书 + TG）%s%s\n"
                          "已归档到 %s（下次同子网可直接复用档案）。"
                          % (detail, dtt_line, _arc_label()))
            return
        if args and args[0] in ("community", "tg_group"):
            # v1.30.7：回执按真实推送日志说话——空报告没推送就绝不能说「已推送」。
            safe_send(chat_id, _daily_receipt(out, args))
            return
        if args and args[0] == "community_confirm":
            mc = re.search(r"社区冲突人工确认完成：\s*(\S+)\s+SN(\d+)\s+(已核实|已推翻)", out)
            if mc:
                day, sn, tag = mc.groups()
                safe_send(chat_id, "✅ %s SN%s 冲突已标记「%s」并写入子网档案。\n"
                          "该结论会被投研和确认版日报引用。发 /社区重推 可重推带确认标记的确认版。"
                          % (day, sn, tag))
            else:
                safe_send(chat_id, "✅ 人工确认已写入档案（详情见 run.log）")
            return
        if args and args[0] == "community_reconfirm":
            mr = re.search(r"社区确认版重推完成：\s*(\S+)（(\d+) 处已人工确认）", out)
            if mr:
                day, cnt = mr.groups()
                safe_send(chat_id, "✅ 社区日报确认版已重推（飞书 + TG）：%s\n"
                          "已带 %s 处人工确认标记，专区置顶显示谁被核实/推翻。" % (day, cnt))
            elif "社区确认版重推跳过" in out:
                safe_send(chat_id, "⚠️ " + _clean_tail(out).split("跳过：")[-1].strip()
                          + "\n（先用 /社区确认 SN编号 结论 写入确认，再 /社区重推）")
            else:
                safe_send(chat_id, "🌐 确认版重推流程已结束（详情见群内推送或 run.log）")
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
        safe_send(chat_id, "✅ 已完成：%s%s%s" % (" ".join(args), stat, mode))
    else:
        # 失败才回传错误尾部（清洗乱码、只留最后几行有意义的）
        tail = _clean_tail(out)
        if args and args[0] == "community":
            hint = ("\n💡 若是「连接 19223 / 守护离线」，取数时已尝试自动拉起 Edge 守护；"
                    "仍失败请确认 Edge 可启动、dtt 账号密码正确（config 本地配置），详见 run.log。")
            safe_send(chat_id, "❌ 社区日报生成失败\n%s%s" % (tail or "（无输出，详见 run.log）", hint))
            return
        if args and args[0] == "tg_group":
            hint = ("\n💡 若是「连接 19223 / 守护离线」，取数时已尝试自动拉起 Edge 守护；"
                    "若报「登录已失效」，请在 Edge 重新登录 dtt 或检查 config 凭据。详见 run.log。")
            safe_send(chat_id, "❌ TG群日报生成失败\n%s%s" % (tail or "（无输出，详见 run.log）", hint))
            return
        safe_send(chat_id, "❌ 失败：%s\n%s" % (" ".join(args), tail or "（无输出，详见 run.log）"))


def _pipeline_worker(args, chat_id):
    """后台线程主体：任何异常都在这里消化，绝不向外抛到主循环。

    结束时调用 _pump_queue()：先释放执行锁，再自动拉起队列里的下一个任务。
    """
    try:
        _run_pipeline_sync(args, chat_id)
    except Exception as e:
        # 走到这里说明**流水线本身**真的出错了（子进程崩溃/启动失败等）。
        # 回执发送已由 safe_send 兜住，不会再拿网络超时冒充任务失败。
        log("流水线线程异常：%s" % e)
        safe_send(chat_id, "❌ 触发异常：%s" % e)
    finally:
        log("流水线线程结束（已释放执行锁）：%s" % " ".join(args))
        _pump_queue()


def _pump_queue():
    """释放执行锁；若队列有待执行任务，原子地占用锁并拉起下一个。

    放在 worker 的 finally 里调用，保证「释放锁」和「占下一个」之间
    不会被新来的命令插队，也不会出现队列有任务却没人跑的死等。
    """
    global _BUSY
    with _BUSY_LOCK:
        _BUSY = False
        nxt = _PIPELINE_QUEUE.pop(0) if _PIPELINE_QUEUE else None
        if nxt:
            _BUSY = True
    if nxt:
        nargs, nchat, nquiet, start_msg = nxt
        try:
            if start_msg:
                safe_send(nchat, start_msg)
            elif not nquiet:
                safe_send(nchat, "⏳ 排队任务开始执行：%s" % " ".join(nargs))
        except Exception:
            pass
        threading.Thread(target=_pipeline_worker, args=(nargs, nchat), daemon=True).start()
        log("已从队列拉起流水线：%s（剩余 %d 个）" % (" ".join(nargs), len(_PIPELINE_QUEUE)))


def _queue_has(args, chat_id):
    """同一会话的同款任务是否已在队列（防按钮连点排满重复导出）。"""
    return any(a == list(args) and str(c) == str(chat_id)
               for a, c, _q, _m in _PIPELINE_QUEUE)


def run_pipeline(args, chat_id, quiet=False, start_msg=None):
    """触发流水线：后台线程执行，监听主循环立刻继续收指令。

    busy 时不再丢弃请求，而是入队；前一个任务结束由 _pump_queue 自动拉起。
    quiet=True 表示调用方已经自己发过回执了，这里不再重复发一条
    「⏳ 开始执行」—— 每条 send 都是一次网络往返，Telegram 抖动时
    一次能卡 4-10 秒，两条回执就是白等十几秒（2026-11-10 实测）。
    start_msg：排队场景下「真正开始执行时」才发的自定义回执（如群日报选群提示），
               入队瞬间不发，避免「正在总结 + 仍在排队」自相矛盾。
    """
    global _BUSY
    args = list(args)
    with _BUSY_LOCK:
        if _BUSY:
            if _queue_has(args, chat_id):
                log("流水线忙且同类任务已排队，忽略重复提交：%s" % " ".join(args))
                return "duplicate"
            _PIPELINE_QUEUE.append((args, chat_id, quiet, start_msg))
            started_now = False
        else:
            _BUSY = True
            started_now = True
    return _run_or_report(args, chat_id, quiet, start_msg, started_now)


def _run_or_report(args, chat_id, quiet, start_msg, started_now):
    """根据是否立即启动，发对应回执并启动线程。"""
    if not started_now:
        pos = len(_PIPELINE_QUEUE)
        try:
            safe_send(chat_id,
                      "📥 已排队：%s\n前面还有 %d 个任务，上一个完成后会自动执行，"
                      "不用重复发命令。" % (" ".join(args), pos))
        except Exception:
            pass
        log("流水线忙，任务已入队（第 %d 位）：%s" % (pos, " ".join(args)))
        return "queued"
    if start_msg:
        try:
            safe_send(chat_id, start_msg)
        except Exception as e:
            log("发送开始提示失败（忽略）：%s" % e)
    elif not quiet:
        try:
            safe_send(chat_id, "⏳ 开始执行：%s" % " ".join(args))
        except Exception as e:
            log("发送开始提示失败（忽略）：%s" % e)
    threading.Thread(target=_pipeline_worker, args=(args, chat_id), daemon=True).start()
    log("已在后台线程启动流水线：%s" % " ".join(args))
    return "started"


def _pipeline_busy():
    """给周更调度器用的让路判断：日报/建档在跑时别同时改档案。"""
    return bool(_BUSY)


def _kb_worker(chat_id):
    """后台线程：立即跑一次知识库校对（同步档案），结果推给发指令的会话。"""
    try:
        import kb_weekly as K
        res = K.run_now(P._CFG, notify=lambda t: safe_send(chat_id, t),
                        log=log, reason="TG 手动触发")
        if not res.get("ok"):
            safe_send(chat_id, "❌ 更新失败：%s" % (res.get("error") or "未知错误"))
    except Exception as e:
        log("知识库手动更新线程异常：%s" % e)
        try:
            safe_send(chat_id, "❌ 更新异常：%s" % e)
        except Exception:
            pass
    finally:
        # 与流水线共用执行锁：结束时同样要拉起排队任务，否则队列会死等。
        _pump_queue()


def run_kb_update(chat_id):
    """`/kb update`：占执行锁后用后台线程跑，避免和日报同时写档案。"""
    global _BUSY
    with _BUSY_LOCK:
        if _BUSY:
            try:
                safe_send(chat_id, "⏳ 有任务正在执行，请稍后再试（避免并发写档案）")
            except Exception:
                pass
            return
        _BUSY = True
    try:
        send_async(chat_id, "📚 开始校对子网知识库（拉链上身份 + 同步已建档案），约 1-2 分钟…")
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


def _start_community_scheduler():
    """启动社区消息日报的每日后台调度（监听启动时调用一次）。"""
    try:
        import community_daily as C
        C.start_thread(
            P._CFG,
            notify=lambda t: send(P._CFG.get("telegram_chat_id") or _last_chat_id(), t),
            log=log,
            busy=_pipeline_busy,
        )
    except Exception as e:
        log("社区日报排程启动失败（忽略，手动 python pipeline.py community 仍可用）：%s" % e)


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


def _latest_community_day():
    """扫描 exports/community/community_YYYY-MM-DD.digest.txt，返回最新日期。"""
    out_dir = os.path.join(HERE, "exports", "community")
    files = glob.glob(os.path.join(out_dir, "community_*.digest.txt"))
    dates = []
    for f in files:
        m = re.search(r"community_(\d{4}-\d{2}-\d{2})\.digest\.txt$", f)
        if m:
            dates.append(m.group(1))
    return max(dates) if dates else (datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")


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
        safe_send(chat_id, "🧠 正在按你的反馈重总结 %s（独立进程，使用最新提示词）…" % day)
        _run_pipeline_sync(["resummarize", day, feedback], chat_id)
    except Exception as e:
        log("反馈重总结失败：%s" % e)
        try:
            safe_send(chat_id, "❌ 重总结失败：%s" % e)
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
    _want_research = bool(re.search(r"投研|跟踪报告|重要事件|研究报告|事件跟踪", feedback))

    # 0.5) 反馈在**引用日报里的原句 + 批评其表述/逻辑** → 走日报重总结，不是投研纠正。
    #      事故 2026-11-27：用户 /retry 引用日报第9行「iuac 因 orange 未买入 SN50，
    #      推断 5GuRL 提前获知...」并说「这句话表述有问题，5GURL就是iuac」——旧逻辑只看
    #      到「SN50 + 有新投研报告」就误判成投研重做，白跑一次投研并推送。
    #      /retry 的默认语义本来就是「重总结日报」，只有明确是投研报告的事实纠正才该走投研。
    #      判据：引号包裹的较长引用 + 措辞/逻辑批评词，且未明确指向投研。
    _quoted = bool(re.search(r"[\"“「『][^\"”」』]{8,}[\"”」』]", feedback))
    _wording_critic = bool(re.search(
        r"这句话|这句|表述|说法|不通|矛盾|逻辑|怎么会|读不懂|看不懂|歧义|"
        r"误解|重复|张冠李戴|搞反|弄反|主体|指代|语义|无中生有", feedback))
    if _quoted and _wording_critic and not _want_research:
        log("反馈引用日报原句并批评表述（非投研纠正），走日报重总结")
        return None

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
        # 区分「认不出」和「已注销」：后者是知识库里的合法历史编号，只是旧档已封存
        m = re.match(r"(?i)^(?:sn|subnet)?\s*#?\s*(\d{1,3})$", (sn_arg or "").strip())
        if m and SA.is_retired_sn(int(m.group(1))):
            safe_send(chat_id, "⛔ SN%d 已注销，旧档案已归档，不再用于投研；"
                              "若同号重新注册，会自动新建档案。" % int(m.group(1)))
            return
        safe_send(chat_id, "❌ 认不出子网：%s\n"
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
    # 回执里如实列出本次实际会跑的 dtt 采集环节（v1.27.1）：按配置开关动态生成，
    # 免得关了某一路还说「正在采集」。默认两路都开。
    _comm_on = cfg.get("research_community_today", True)
    _chn_on = cfg.get("research_sn_channel", True)
    if _comm_on and _chn_on:
        _dtt_step = " → dtt 社区消息 + 本子网 DC 频道（当日/昨日）"
    elif _comm_on:
        _dtt_step = " → dtt 社区消息"
    elif _chn_on:
        _dtt_step = " → dtt 本子网 DC 频道（当日/昨日）"
    else:
        _dtt_step = ""
    if feedback:
        send_async(chat_id, "🔬 收到，**按你的反馈重做 SN%d%s 的投研报告**（回看 %d 天）。\n"
                            "反馈：%s\n"
                            "会重新核对名称/事实并重抓全网%s，约 1-3 分钟…"
                            % (sn, (" " + name) if name else "", days, feedback[:200], _dtt_step))
        # 反馈必须原样传（含空格标点）：pipeline 侧靠整句理解「改名为 X」这种句式，
        # 拆成 token 再拼回去容易丢中文标点和语气词。`~fb` 是分隔符（不能用 `--fb`，
        # argparse 会把 `-` 开头的 token 当选项直接报错）。
        run_pipeline(["research", str(sn)] + ([str(days_n)] if days_n else [])
                     + ["~fb", feedback], chat_id, quiet=True)
        return
    send_async(chat_id, "🔬 收到，正在做 **SN%d%s** 的重要事件跟踪报告（回看 %d 天档案）。\n"
                        "流程：调取本地子网档案 → 抓取链上数据站/官网/GitHub/新闻/搜索/X%s "
                        "→ 交叉核验 → 生成报告，约 1-3 分钟…"
                        % (sn, (" " + name) if name else "", days, _dtt_step))
    run_pipeline(["research", str(sn)] + ([days_n] if days_n else []), chat_id, quiet=True)


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


# ---- 发地址自动收录回购地址 -------------------------------------------
# 格式：ss58 地址 + SN 编号 + 可选备注。例：
#   5FX6...Ehk2，8回购地址       -> SN8, 备注「回购地址」
#   5FX6...Ehk2 SN8 回购钱包     -> SN8, 备注「回购钱包」
#   5FX6...Ehk2 #8 已链上核对     -> SN8, 备注「已链上核对」
# 必须同时给出地址与 SN 编号才收录；只有地址、没编号时不处理（避免误判）。
_WALLET_SS58 = re.compile(r"\b5[1-9A-HJ-NP-Za-km-z]{47}\b")
_WALLET_SN = re.compile(r"(?:SN\s*|#\s*)?(\d{1,3})(?![\dA-Za-z])", re.I)
_WALLET_SEP = re.compile(r"[\s,，;；:：|、]+")


def parse_wallet_add(text):
    """从一条消息里解析「ss58地址 + SN编号 + 备注」，返回 (sn, address, note) 或 None。

    保守规则：
      · 必须有且只有一个合法 ss58 地址（严格 48 位，与 archive 的 SS58 一致）；
      · SN 编号只能出现在**地址之后**的文本里，范围 1-255；
      · 备注取 SN 编号之后的文字（去掉分隔符），没有就填默认值。
    """
    t = (text or "").strip()
    addrs = _WALLET_SS58.findall(t)
    if len(addrs) != 1:
        return None
    addr = addrs[0]
    tail = t[t.index(addr) + len(addr):]           # 只看地址后面的文字
    tail = tail.strip(" \t,，;；:：|、-—")
    if not tail:
        return None
    m = _WALLET_SN.search(tail)
    if not m:
        return None                # 没写子网编号，不猜
    sn = int(m.group(1))
    if not (1 <= sn <= 255):
        return None
    note = tail[m.end():].strip(" \t,，;；:：|、-—")
    note = re.sub(r"\s+", " ", note)[:120] or "TG 补录的回购地址"
    return sn, addr, note


def _wallet_add(sn, address, note, chat_id):
    """把地址收录到 SN 档案（无档案则先建最小档案），并给 TG 回执。"""
    import subnet_archive as SA
    cfg = P.load_config()
    arc_dir = SA.get_arc_dir(cfg)
    arc = SA.load_archive(arc_dir, sn)
    if not arc:
        # 无档案：建一个最小档案，保证「发地址即收录」不因没建档而失败
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        arc = {
            "sn": int(sn), "created_at": now, "updated_at": now,
            "name": "", "biz": "", "url": "", "github": "",
            "days": {}, "weak_days": {}, "reports": [], "buyback_wallets": [],
            "note": "本档案由 TG 补录回购地址时自动创建（尚无群聊归档）",
        }
        SA.save_archive(arc_dir, arc)
        SA.save_md(arc_dir, arc)
    before = len(arc.get("buyback_wallets") or [])
    SA.add_manual_wallet(arc_dir, sn, address, note=note, log=log)
    arc2 = SA.load_archive(arc_dir, sn) or {}
    after = len(arc2.get("buyback_wallets") or [])
    existed = before >= after          # 地址已存在时只是升级/改备注，数量不变
    name = (arc2.get("name") or "").strip()
    tag = ("SN%d %s" % (sn, name)) if name else ("SN%d" % sn)
    safe_send(chat_id,
              "✅ 已收录回购地址 → **%s**\n"
              "地址：`%s`\n备注：%s\n%s"
              % (tag, address, note,
                 "（该地址此前已登记，本次按人工补录更新）" if existed else
                 "（来源标记为人工补录 source=manual，后续自动归档不会覆盖）"))


def handle(text, chat_id):
    text = (text or "").strip()
    if not text:
        return
    # 分步对话优先拦截：正在引导社区确认时，用户回复的「55」「结论…」必须
    # 先被这里消费，绝不能落到下面的「裸编号=投研」「ss58=地址补录」逻辑。
    if _confirm_flow_consume(text, chat_id):
        return
    # TG 群日报选群状态：期间回复的「1」「Hanlin.AI」「全部」按群选项消费，
    # 绝不能落到下面的「裸编号=投研」「ss58=地址补录」逻辑。
    if _tgg_flow_consume(text, chat_id):
        return
    # Gamma 查询分段状态：期间回复的「1」「列表」「全网」「51」按 gamma 选项消费。
    if _ga_flow_consume(text, chat_id):
        return
    parts = text.split()
    # 群里发指令可能带 @机器人名（/digest@Dtao_ribao_bot），剥掉后缀再匹配
    cmd = parts[0].lstrip("/").lower().split("@")[0]
    # 发「ss58地址 + SN编号 + 备注」= 自动收录为该子网的回购地址（人工补录）。
    # 放在裸编号之前：地址消息含数字，先让地址解析器判定，避免任何歧义。
    if not parts[0].startswith("/"):
        wa = parse_wallet_add(text)
        if wa:
            sn, address, note = wa
            try:
                _wallet_add(sn, address, note, chat_id)
            except Exception as e:
                log("TG 地址收录失败：%s" % e)
                safe_send(chat_id, "❌ 回购地址收录失败：%s" % e)
            return
    # 裸编号 = 子网投研：整条消息只有「29」/「SN29」/「#29」/「29 30」时直接开工。
    # 不带斜杠是为了手机上少打几个字（用户原话：从 TG 发子网编号就触发该子网投研）。
    if not parts[0].startswith("/"):
        hit = parse_bare_sn(text)
        if hit:
            sn, days_n = hit
            _start_research(str(sn), days_n, chat_id)
            return
    if cmd in ("help", "start"):
        safe_send(chat_id, HELP)
    elif cmd in ("keyboard", "按钮"):
        # 重新唤出底部按钮栏（平时随回复自动收起）
        safe_send(chat_id, "⌨️ 按钮栏已恢复（bot 回复后会自动收起；发 /keyboard 可再次唤出）",
             markup=_KEYBOARD)
    elif cmd in ("hidekeyboard", "收起", "关闭按钮", "nokeyboard"):
        # 显式收起底部按钮栏（等价于任何一个后续回复，但立刻生效、不用等）
        safe_send(chat_id, "⌨️ 按钮栏已收起。需要时发 /keyboard 唤出。",
             markup=_REMOVE_KB)
    elif cmd == "digest":
        # 回执走后台线程，主流程立刻启动，用户马上看到「收到」
        send_async(chat_id, "☀️ 收到，正在导出并总结聊天记录，稍等…")
        run_pipeline(["daily"] + parts[1:], chat_id, quiet=True)
    elif cmd in ("today", "jinri"):
        # 总结今天的聊天记录（北京时间今天 00:00 -> 现在）
        send_async(chat_id, "☀️ 收到，正在导出并总结今天的聊天记录，稍等…")
        run_pipeline(["daily", "今日"], chat_id, quiet=True)
    elif cmd == "weekly":
        send_async(chat_id, "📅 收到，正在生成周报，稍等…")
        run_pipeline(["weekly"], chat_id, quiet=True)
    elif cmd in ("community", "社区", "社区日报", "dtt",
                 "社区昨日", "社区昨天", "community_yesterday",
                 "社区今日", "社区今天", "community_today"):
        # Bittensor社区消息日报（dtt版）：命令触发，不定时推送。
        # 区分昨日/今日：/社区 默认昨日；/社区今日（或 /社区 今日）取今日；
        # /社区昨日 显式昨日。其余参数（日期/nopush）原样透传 pipeline.py community。
        _today_cmds = ("社区今日", "社区今天", "community_today")
        _yest_cmds = ("社区昨日", "社区昨天", "community_yesterday")
        if cmd in _today_cmds:
            preset = ["今日"]
        elif cmd in _yest_cmds:
            preset = ["昨日"]
        else:
            preset = []
        send_async(chat_id, "🌐 收到，正在生成 Bittensor 社区消息日报（dtt版），稍等…")
        run_pipeline(["community"] + preset + parts[1:], chat_id, quiet=True)
    elif cmd in ("tg群", "群消息", "tg_group", "tg群消息", "tgqun",
                 "tg群今日", "tg群今天", "tg_group_today",
                 "tg群昨日", "tg群昨天", "tg_group_yesterday"):
        # TG 群消息日报（dtt 管理员群消息库，与关键词社区日报隔离）。
        # v1.25.0 裸命令 → 分步选群（回执 4 群按钮 → 点击 → 单群总结 → 推送）。
        # 带参数（今日/昨日/日期/nopush）→ 老写法直通全群，不受分步影响。
        _today_cmds = ("tg群今日", "tg群今天", "tg_group_today")
        _yest_cmds = ("tg群昨日", "tg群昨天", "tg_group_yesterday")
        _bare_cmds = ("tg群", "群消息", "tg_group", "tg群消息", "tgqun")
        if cmd in _bare_cmds and len(parts) == 1:
            _tgg_prompt(chat_id)
            return
        if cmd in _today_cmds:
            preset = ["今日"]
        elif cmd in _yest_cmds:
            preset = ["昨日"]
        else:
            preset = []
        send_async(chat_id, "💬 收到，正在生成 TG 群消息日报（4 个活跃群，严格相关筛选），稍等…")
        run_pipeline(["tg_group"] + preset + parts[1:], chat_id, quiet=True)
    elif cmd in ("gamma", "gama", "伽马", "g情报"):
        # Gamma 情报汇总（v1.28.0，taostats 链上数据）。
        # 裸命令 → 分段菜单（功能 → 范围/子网）；带参数直通：
        #   /gamma 列表     开通子网列表（专门入口）
        #   /gamma 收支     当前收支总览
        #   /gamma 周|月    北京时间自然周/月统计（机制1 钱包→子网）
        #   /gamma SN51     单子网明细（机制1）
        #   /gamma 跨网      跨子网支付总览（机制2 源子网 emission→目标子网）
        #   /gamma 跨网周|跨网月  跨子网自然周/月统计
        #   /gamma 跨网 SN120    指定源子网明细；末尾 nopush 只生成不推。
        push_off = any(x.lower() in ("nopush", "--no-push") for x in parts[1:])
        rest = [x for x in parts[1:] if x.lower() not in ("nopush", "--no-push")]
        if not rest:
            _ga_prompt(chat_id)
            return
        alias = {"列表": "list", "开通": "list", "list": "list", "子网": "list",
                 "收支": "snapshot", "总览": "snapshot", "snapshot": "snapshot",
                 "周": "week", "week": "week", "周统计": "week",
                 "月": "month", "month": "month", "月统计": "month",
                 "跨网": "cross", "跨子网": "cross", "cross": "cross", "跨子网支付": "cross",
                 "跨网周": "crossweek", "跨子网周": "crossweek", "crossweek": "crossweek",
                 "跨网月": "crossmonth", "跨子网月": "crossmonth", "crossmonth": "crossmonth"}
        act = None
        sn = None
        for x in rest:
            low = x.lower()
            if act is None and low in alias:
                act = alias[low]
            else:
                m = re.fullmatch(r"(?:sn)?(\d{1,4})", low)
                if m:
                    sn = int(m.group(1))
        if act is None:
            act = "detail" if sn is not None else "list"
        args = ["gamma", act] + ([str(sn)] if sn is not None else [])
        if push_off:
            args.append("nopush")
        send_async(chat_id, "📊 收到，正在查询 Gamma 情报（%s）%s，稍等…"
                   % (_GA_ACT_LABEL.get(act, "子网明细" if act == "detail" else act),
                      "（只生成不推送）" if push_off else "（生成后推送飞书 + TG）"))
        run_pipeline(args, chat_id, quiet=True)
    elif cmd in ("社区确认", "community_confirm", "确认冲突", "核实"):
        # 人工确认社区日报冲突，两种写法：
        #   ① 分步（推荐）：只发 /community_confirm → bot 依次问 SN、核实结论
        #   ② 一次性（兼容）：/社区确认 55 收入数字属实，已查链上 [已推翻]
        # 两种写法最终都走 _run_community_confirm，逻辑不重复。
        rest = parts[1:]
        if not rest:
            _CONFIRM_FLOW[str(chat_id)] = {"step": "sn", "ts": time.time(), "sn": ""}
            safe_send(chat_id,
                      "🧑‍⚖️ 社区冲突确认（分两步，5 分钟内有效）\n"
                      "第 1 步 请回复子网编号，如：55\n"
                      "第 2 步 再回复核实结论，如：收入数字属实，已查链上\n"
                      "（判定不实则结论结尾加「已推翻」）\n"
                      "发 /取消 随时退出；也可一条写完：/社区确认 55 结论")
            return
        if len(rest) < 2 or not re.match(r"^\d{1,4}$", rest[0]):
            send_async(chat_id, "用法：/社区确认 SN编号 结论 [已推翻]\n"
                          "例：/社区确认 55 收入数字属实，已查链上\n"
                          "    /社区确认 64 取消回购是谣言 已推翻（默认为「已核实」）\n"
                          "也可只发 /社区确认 进入分步引导。")
            return
        _run_community_confirm(rest[0], " ".join(rest[1:]).strip(), chat_id)
    elif cmd in ("社区重推", "community_reconfirm", "确认版重推"):
        # 按档案人工确认重推社区日报确认版（不重新取数/不调 AI）
        rest = parts[1:]
        day = rest[0] if rest and re.match(r"^\d{4}-\d{2}-\d{2}$", rest[0]) else _latest_community_day()
        push = not any(x.lower() in ("nopush", "--no-push") for x in rest)
        args = ["community_reconfirm", day] + ([] if push else ["nopush"])
        send_async(chat_id, "🌐 收到，正在按已确认结论重推社区日报确认版，稍等…")
        run_pipeline(args, chat_id, quiet=True)
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
        send_async(chat_id, "💬 收到，正在整理%s的水聊侧记（只读已有导出记录，不重新导出）…"
             % (day or "最近一次导出"))
        run_pipeline(args, chat_id, quiet=True)
    elif cmd in ("archive", "建档", "存档", "archive_subnets"):
        # 建档不再作为日常指令（导出时已自动增量建档，见 pipeline.run_one_day）。
        # 保留这个入口只做一件事：**全量重建**（换机器 / 清过档案 / 想补历史）。
        safe_send(chat_id, "ℹ️ 建档已自动化：每次导出日报时会自动把提到的子网写进档案"
                      "（已建档的增量补充，未建档的自动新建），无需手动触发。\n"
                      "如需用现有全部导出记录**全量重建**一次，请执行：\n"
                      "python pipeline.py archive\n"
                      "查看清单发 /档案，生成投研发 /投研 29。")
    elif cmd in ("research", "投研", "研报", "分析"):
        # /投研 29 [天数]        —— 子网重要事件跟踪报告
        # 数据链：本地档案 + 链上数据站 + 官网/GitHub/新闻/搜索/X 交叉验证
        rest = parts[1:]
        if not rest:
            safe_send(chat_id, "用法：直接给我一个子网编号就行 ——\n"
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
                safe_send(chat_id, "❌ 无法识别子网：%s" % parts[1])
                return
            arc = SA.load_archive(arc_dir, sn)
            if not arc:
                retired = SA.retired_archive_info(arc_dir, sn)
                if retired:
                    ra = retired["archive"] or {}
                    safe_send(chat_id,
                              "⛔ **SN%d %s** 已注销（%s），旧档案已归档：\n`%s`\n"
                              "不再用于投研/事件报告；同号重新注册后会自动新建档案。"
                              % (sn, ra.get("name") or "",
                                 ra.get("deregistered_on") or "日期未记录",
                                 retired["path"]))
                    return
                safe_send(chat_id, "SN%d 还没有档案。建档是自动的——跑一次 /digest 就会把它建起来；"
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
                     "档案：%s\\SN%d\\archive.md" % (arc_dir, sn), "", "近期要点："]
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
                safe_send(chat_id, _tg_bold(ch))
            return
        idx = SA.load_all_index(arc_dir)
        subs = idx.get("subnets") or {}
        if not subs:
            safe_send(chat_id, "还没有任何子网档案。建档是自动的——跑一次 /digest 就会建起来。")
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
            safe_send(chat_id, ch)
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
                safe_send(chat_id, "（还没有潜力观察记录，先发 /digest 跑几天日报）")
                return
            txt = open(files[-1], encoding="utf-8").read()
            safe_send(chat_id, "📊 最新潜力观察\n" + _tg_bold(txt))
            _feishu_watch(P._CFG, "📊 DTAO 潜力观察 · 最新", txt)
        else:
            head, merged = P.watch_summary(n)
            full = _tg_bold(head + "\n\n" + merged)
            chunks = P.chunk_text(full, 3800)
            for i, ch in enumerate(chunks, 1):
                safe_send(chat_id, (("【%d/%d】\n" % (i, len(chunks))) if len(chunks) > 1 else "") + ch)
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
            safe_send(chat_id, "用法：/retry 反馈或要补充的事实\n"
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
            safe_send(chat_id, "还没有可修订的日报，请先发 /digest 生成一次。")
            return
        run_feedback(day, feedback, chat_id)
    elif cmd in ("kb", "知识库", "子网表"):
        # /kb          查看周更状态（排程、上次成功、当前快照、待补跑原因）
        # /kb update   立即校对一次（拉链上身份 + 同步已建档案）
        arg = parts[1].lower() if len(parts) > 1 else ""
        if arg in ("update", "now", "run", "更新", "立即"):
            run_kb_update(chat_id)
        elif arg in ("off", "关"):
            safe_send(chat_id, "周更开关在 config.json：kb_auto_update=false 即关闭"
                          "（改完需重启监听）。本次仍可通过 /kb update 手动更新。")
        else:
            try:
                import kb_weekly as K
                safe_send(chat_id, K.status_text(P._CFG))
            except Exception as e:
                log("读取知识库状态失败：%s" % e)
                safe_send(chat_id, "读取状态失败：%s" % e)
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
            safe_send(chat_id, "没有可用的日报文件，请先发 /digest 生成一次。")
            return
        send_async(chat_id, "📤 收到，正在重推 %s%s…" % (day, ("（%s）" % "/".join(targets)) if targets else ""))
        run_pipeline(["repush", day] + targets, chat_id, quiet=True)
    elif cmd in ("模型", "model", "llm"):
        # 大模型平台/模型切换（v1.21.1）：点命令列出档案、按名字切换、现场添加。
        # 档案名大小写不敏感（SN28/sn28 等价），旧名 engy 兼容为 sn53 别名。
        # 写盘立即生效（日报是子进程重读 config.json），key 一律脱敏回显。
        rest = parts[1:]
        sub = rest[0].lower() if rest else ""
        try:
            if not rest or sub in ("列表", "list", "l"):
                profs, act = LP.list_profiles()
                cur = LP.current()
                lines = ["🤖 当前生效：%s（%s · %s，key %s）"
                         % (act or "主配置直填", cur["base_url"], cur["model"], cur["key_mask"]),
                         "可用档案："]
                for p in profs:
                    lines.append("  %s %s · %s · %s · key %s"
                                 % ("✅" if p["active"] else "　", p["name"],
                                    p["label"], p["base_url"], p["key_mask"]))
                lines += ["", "切换：/模型 <档案名>（sn28/SN28 都行；sn53 旧名 engy 也认）",
                          "添加：/模型 添加 <名> <base_url> <模型名> <api_key>",
                          "删除：/模型 删除 <名>；当前：/模型 当前",
                          "⚠️ 添加会把 key 存进 config.json 明文、并留在 TG 聊天记录里，注意保管。"]
                safe_send(chat_id, "\n".join(lines))
            elif sub in ("当前", "current", "c"):
                cur = LP.current()
                safe_send(chat_id, "🤖 当前：%s\n地址：%s\n模型：%s\nkey：%s"
                          % (cur["active"] or "主配置直填", cur["base_url"],
                             cur["model"], cur["key_mask"]))
            elif sub in ("添加", "add", "a"):
                # /模型 添加 <名> <base_url> <模型名> <api_key>
                if len(rest) < 5:
                    safe_send(chat_id,
                              "用法：/模型 添加 <档案名> <base_url> <模型名> <api_key>\n"
                              "例：/模型 添加 sn28 https://api.saygm.com/v1 kimi-k3 gm_live_xxx\n"
                              "⚠️ key 会明文存进 config.json 并留在聊天记录，注意保管。")
                    return
                item = LP.add(rest[1], rest[2], rest[3], rest[4])
                safe_send(chat_id, "✅ 已%s档案「%s」\n地址：%s\n模型：%s\nkey：%s\n"
                              "发 /模型 <档案名> 即可切换（不区分大小写，下次日报生效，无需重启）"
                              % ("覆盖" if item["replaced"] else "添加", item["name"],
                                 item["base_url"], item["model"], item["key_mask"]))
            elif sub in ("删除", "remove", "rm", "d"):
                if len(rest) < 2:
                    safe_send(chat_id, "用法：/模型 删除 <档案名>（不能删当前正在用的）")
                    return
                gone = LP.remove(rest[1])
                safe_send(chat_id, "🗑 已删除档案「%s」" % gone)
            else:
                # 其余按「切换」处理：/模型 sn28
                r = LP.switch(rest[0])
                safe_send(chat_id,
                          "🔄 已切换到「%s」\n地址：%s\n模型：%s\nkey：%s\n"
                          "✅ 配置已落盘，下次触发日报/投研即生效，无需重启监听。"
                          % (r["label"], r["base_url"], r["model"], r["key_mask"]))
        except (KeyError, ValueError) as e:
            safe_send(chat_id, "❌ %s" % e)
        except Exception as e:
            log("模型命令失败：%s" % e)
            safe_send(chat_id, "❌ 操作失败：%s（详见 tg_listener.log）" % e)
    else:
        safe_send(chat_id, "未知指令：%s\n%s" % (text, HELP))


def main():
    # 单实例：抢锁 → 抢不到就杀掉旧监听 → 重抢（acquire_single_instance 内部完成）。
    # 绝不允许两个进程同时轮询同一个 bot，否则一条指令会被执行多次、推多份日报。
    if not acquire_single_instance():
        log("❌ 已有同版本监听在运行，本进程退出（不打断正在执行的指令）")
        try:
            print("已有同版本监听在运行，本进程退出。用 python tgctl.py status 查看状态。")
        except Exception:
            pass
        return
    atexit.register(release_single_instance)

    cfg = P.load_config()
    P._CFG = cfg
    # 记录本次加载的 pipeline 版本：常驻进程会把代码固化在内存里，
    # 之后若 pipeline.py 有更新，必须重启监听才会同步（这条日志便于排查）
    log("已加载 pipeline 模块 v%s（代码已固化在内存）；"
        "之后 pipeline.py 若更新，本进程会在 60 秒内自动退出并要求守护重启，"
        "也可直接跑 `python tgctl.py restart` 立即生效"
        % getattr(P, "__version__", "?"))
    token = cfg.get("telegram_bot_token")
    if not token:
        log("未配置 telegram_bot_token，退出"); return
    # 可选白名单：配了 allowed_user_ids 才限制仅这些用户触发；不配（默认）则开放给任何人。
    # 每日真实导出次数仍受 pipeline 内置节流保护，多人反复触发也不会敞开口消耗 token。
    allowed_users = [str(u) for u in (cfg.get("allowed_user_ids") or []) if u]

    # 启动即探测通道 + 跳过历史消息，只处理之后的新指令
    try:
        P.tg_detect(token)
    except Exception as e:
        log("通道探测异常（继续运行）：%s" % e)
    try:
        # 一次把待处理的历史全部取出来，offset 直接跳到最大那条之后。
        # 老代码 limit=1：只确认最早的一条，剩下的仍会在主循环里被当成新指令执行 ——
        # 于是每次重启都可能把停机期间的指令重跑一遍（同一条 105 被跑两次）。
        init = tg_api(token, "getUpdates", {"limit": 100, "timeout": 0}, timeout=20)
        offset = 0
        if init.get("ok") and init.get("result"):
            offset = max(int(u["update_id"]) for u in init["result"]) + 1
            log("启动清空历史：跳过 %d 条待处理消息（offset=%d）"
                % (len(init["result"]), offset))
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
    # 社区日报 v1.20.0 起改为命令触发（python pipeline.py community），不再定时推送。
    # 若用户日后在 config 里手动把 community_auto 设 true，可取消下一行注释恢复调度。
    # _start_community_scheduler()

    last_hb = time.time()
    last_ver_check = time.time()
    while True:
        try:
            # 代码热重载：磁盘 pipeline.py 的版本变了就主动退出，让守护用新代码拉起。
            # 为什么需要：常驻进程把代码固化在内存里，改完代码不重启就一直是旧逻辑，
            # 而用户完全看不出「改了没生效」。以前只能人工杀进程。
            if time.time() - last_ver_check > 60:
                last_ver_check = time.time()
                _disk = _disk_version()
                _mine = getattr(P, "__version__", "?")
                if _disk and _disk != _mine:
                    log("检测到代码已更新：内存 v%s → 磁盘 v%s，退出并请求热重载"
                        % (_mine, _disk))
                    release_single_instance()
                    # os._exit 不走 atexit，状态文件必须手工清掉，否则面板会把
                    # 已经死掉的旧进程版本当成「运行版本」一直显示（本次事故现象之一）
                    _cleanup_state()
                    _respawn_on_exit(os.getpid())
                    # 75 是给守护的暗号：别等 20 秒，立刻拉起
                    os._exit(EXIT_RELOAD)

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
                # 内联按钮回调（TG 群日报分步选群）：独立于文本消息分发。
                # 必须 answerCallbackQuery，否则客户端按钮一直转圈。
                cq = u.get("callback_query") or {}
                if cq:
                    cq_chat = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
                    cq_from = str((cq.get("from") or {}).get("id", ""))
                    log("收到按钮回调 chat=%s data=%s" % (cq_chat, cq.get("data")))
                    try:
                        _tgg_callback(cq, cq_chat, cq_from, allowed_users)
                        _ga_callback(cq, cq_chat, cq_from, allowed_users)
                    except Exception as e:
                        log("处理按钮回调异常（已忽略）：%s" % e)
                    continue
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
                # 退避封顶 10 秒：双通道已经在自动择优，还连着失败多半是整体断网，
                # 此时把重试间隔拉到 30 秒只会让恢复更慢（以前就是这么卡住的）。
                wait = min(10, 2 ** min(_net_fail - 1, 4))
                log("网络异常第 %d 次（%s: %s），%d 秒后重连"
                    % (_net_fail, type(e).__name__, str(e)[:110], wait))
                time.sleep(wait)
            else:
                globals()["_NET_FAIL"] = 0
                log("监听异常(BaseException，已忽略继续运行)：%s" % e)
                time.sleep(10)


if __name__ == "__main__":
    main()
