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

授权规则（config.json）：
  - 若配置了 allowed_user_ids（数组，填你的 TG 数字 user id），只有这些用户能触发；
  - 否则，只要消息来自 telegram_chat_id（通常是你建的那个群）就允许。
    ⚠️ 若 chat_id 是群，则群内任何人都能触发；只想你自己用，就私聊 bot 并把
       telegram_chat_id 改成你的个人私聊 id，或填 allowed_user_ids。

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


def run_pipeline(args, chat_id):
    cmd = [sys.executable, os.path.join(HERE, "pipeline.py")] + args
    send(chat_id, "⏳ 开始执行：%s" % " ".join(args))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=900)
        out = (r.stdout or "") + (r.stderr or "")
        if len(out) > 3500:
            out = "...(截断)...\n" + out[-3500:]
        ok = r.returncode == 0
        send(chat_id, ("✅ 完成：%s\n%s" % (" ".join(args), out)) if ok
              else ("❌ 失败：%s\n%s" % (" ".join(args), out)))
    except subprocess.TimeoutExpired:
        send(chat_id, "⏱ 执行超时（>15min），请查本机 run.log")
    except Exception as e:
        send(chat_id, "❌ 触发异常：%s" % e)


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
    allowed_chat = str(cfg.get("telegram_chat_id", ""))
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

    log("TG 指令监听已启动（allowed_chat=%s, allowed_users=%s）... Ctrl+C 停止"
        % (allowed_chat, allowed_users or "无(按群授权)"))
    while True:
        try:
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
                if allowed_users:
                    if from_id not in allowed_users:
                        log("忽略未授权用户 %s 的消息" % from_id)
                        continue
                elif chat_id != allowed_chat:
                    log("忽略非授权会话 %s 的消息" % chat_id)
                    continue
                log("收到指令 chat=%s from=%s text=%s" % (chat_id, from_id, (msg.get("text") or "")[:80]))
                handle(msg.get("text"), chat_id)
        except KeyboardInterrupt:
            log("已停止"); break
        except Exception as e:
            log("监听异常：%s" % e)
            time.sleep(10)


if __name__ == "__main__":
    main()
