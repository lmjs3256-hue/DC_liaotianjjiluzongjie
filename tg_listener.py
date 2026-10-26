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
import sys
import json
import glob
import time
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
    "/weekly 周报\n"
    "/watch 查看潜力观察(默认最新; /watch 7 近7天热点; /watch all 全部)\n"
    "/repush 补推最近一次日报(无需日期)\n"
    "/repush 2026-10-23 补推指定日(飞书+TG)\n"
    "/repush 2026-10-23 tg 仅补推 TG\n"
    "/repush 2026-10-23 feishu 仅补推飞书\n"
    "/help 显示本帮助\n"
    "💡 也可直接点下方按钮栏操作"
)


def tg_api(token, method, params=None, timeout=40):
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with P._opener().open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# 常驻按钮栏：每条 bot 回复都带这个键盘，点按钮即发对应指令（无需打字/输日期）
_KEYBOARD = {
    "keyboard": [
        ["/digest 昨日", "/repush 最新"],
        ["/watch 7", "/weekly"]
    ],
    "resize_keyboard": True
}


def send(chat_id, text):
    # 纯文本发送，避免日志里的特殊字符破坏 Markdown 解析
    tg_api(P._CFG["telegram_bot_token"], "sendMessage",
           {"chat_id": chat_id, "text": text, "disable_web_page_preview": True,
            "reply_markup": json.dumps(_KEYBOARD)})


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


def _run_pipeline_sync(args, chat_id):
    """真正执行流水线（同步逻辑，供后台线程调用）。"""
    cmd = [sys.executable, os.path.join(HERE, "pipeline.py")] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    try:
        out, _ = proc.communicate(timeout=PIPELINE_TIMEOUT)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_proc_tree(proc)
        try:
            out, _ = proc.communicate(timeout=30)
        except Exception:
            out = ""
        log("流水线超时（>%ds），已终止子进程树：%s" % (PIPELINE_TIMEOUT, " ".join(args)))
        try:
            send(chat_id, "⏱ 执行超时（>%d 分钟），已强制终止并清理子进程，请查 run.log"
                 % (PIPELINE_TIMEOUT // 60))
        except Exception:
            pass
        return
    out = out or ""
    if rc == 0:
        # 成功：日报已由 pipeline 自行推送到飞书+TG，这里只回简洁确认，不回传大段日志
        stat = ""
        m = re.search(r"过滤后保留实质消息\s*(\d+)\s*条", out)
        if m:
            stat = "\n📊 当日实质消息 %s 条" % m.group(1)
        mode = ""
        mm = re.search(r"节流模式：\s*\S+\s*（(.+?)）", out)
        if mm:
            mode = "\n🔧 %s" % mm.group(1)
        send(chat_id, "✅ 已完成：%s%s%s" % (" ".join(args), stat, mode))
    else:
        # 失败才回传错误尾部，便于排错
        tail = out[-1500:] if len(out) > 1500 else out
        send(chat_id, "❌ 失败：%s\n%s" % (" ".join(args), tail))


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


def _latest_digest_date():
    """扫描 exports/dtao_YYYY-MM-DD.digest.txt，返回最新日期；无则返回 None。"""
    files = glob.glob(os.path.join(HERE, "exports", "dtao_*.digest.txt"))
    dates = []
    for f in files:
        m = re.search(r"dtao_(\d{4}-\d{2}-\d{2})\.digest\.txt$", f)
        if m:
            dates.append(m.group(1))
    return max(dates) if dates else None


def handle(text, chat_id):
    text = (text or "").strip()
    if not text:
        return
    parts = text.split()
    # 群里发指令可能带 @机器人名（/digest@Dtao_ribao_bot），剥掉后缀再匹配
    cmd = parts[0].lstrip("/").lower().split("@")[0]
    if cmd in ("help", "start"):
        send(chat_id, HELP)
    elif cmd == "digest":
        run_pipeline(["daily"] + parts[1:], chat_id)
    elif cmd == "weekly":
        run_pipeline(["weekly"], chat_id)
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
    cfg = P.load_config()
    P._CFG = cfg
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
                log("收到指令 chat=%s from=%s text=%s" % (chat_id, from_id, (msg.get("text") or "")[:80]))
                # 单条指令处理异常绝不影响主循环（第二层兜底）
                try:
                    handle(msg.get("text"), chat_id)
                except Exception as e:
                    log("处理指令异常（已忽略，监听继续）：%s" % e)
        except KeyboardInterrupt:
            log("已停止"); break
        except BaseException as e:
            # 兜底：连 BaseException 也不让监听退出（KeyboardInterrupt 上面已处理）
            log("监听异常(BaseException，已忽略继续运行)：%s" % e)
            time.sleep(10)


if __name__ == "__main__":
    main()
