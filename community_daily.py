#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""社区消息日报 · 每日定时调度器
==================================
仿 kb_weekly 的纯 stdlib 调度：每天固定时间（北京时间，默认 08:30）取
dtt 前一日社区消息、生成日报、落档、双推。挂在 tg_listener 启动时。

规则：
    - 周期：每天一次，默认 08:30（config.community_hour，可改小时）。
    - 补跑：错过时间点后，下次启动发现「上次成功早于最近计划点」立即补跑。
    - 失败退避 30 分钟；日报流水线在跑（busy）时让路，10 分钟后再试。
    - 幂等：重复 start_thread 只有一个线程。

对外接口：
    due(cfg)                    现在是否该跑
    run_now(cfg, ...)           立即跑一次
    status_text(cfg)            /社区状态 用
    start_thread(cfg, ...)      启动后台调度线程
"""

import datetime
import json
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "exports", "community", "_state.json")
TZ = datetime.timezone(datetime.timedelta(hours=8))

CHECK_INTERVAL = 600          # 每 10 分钟检查
RETRY_AFTER_FAIL = 1800       # 失败后 30 分钟重试
DEFAULT_HOUR = 8
DEFAULT_MINUTE = 30

_thread_started = False
_thread_lock = threading.Lock()


def _cfg_get(cfg, key, default):
    v = (cfg or {}).get(key)
    return default if v is None else v


def settings(cfg):
    try:
        hr = int(_cfg_get(cfg, "community_hour", DEFAULT_HOUR))
    except Exception:
        hr = DEFAULT_HOUR
    try:
        mi = int(_cfg_get(cfg, "community_minute", DEFAULT_MINUTE))
    except Exception:
        mi = DEFAULT_MINUTE
    if not 0 <= hr <= 23:
        hr = DEFAULT_HOUR
    if not 0 <= mi <= 59:
        mi = DEFAULT_MINUTE
    return {
        "enabled": bool(_cfg_get(cfg, "community_auto", True)),
        "hour": hr, "minute": mi,
    }


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def now_tz():
    return datetime.datetime.now(TZ)


def _parse_dt(s):
    if not s:
        return None
    try:
        d = datetime.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=TZ)
    except Exception:
        return None


def last_occurrence(now=None, hour=None, minute=None):
    """最近一次「已经过去」的每日计划时间点（含今天）。"""
    now = now or now_tz()
    if hour is None or minute is None:
        hour, minute = DEFAULT_HOUR, DEFAULT_MINUTE
    cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand > now:
        cand -= datetime.timedelta(days=1)
    return cand


def target_day(now=None):
    """本次要总结哪天：计划点所在自然日的前一天。"""
    now = now or now_tz()
    return (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")


def due(cfg, now=None):
    now = now or now_tz()
    s = settings(cfg)
    if not s["enabled"]:
        return False, "社区日报已关闭（config.community_auto = false）"
    occ = last_occurrence(now, s["hour"], s["minute"])
    st = load_state()
    succ = _parse_dt(st.get("last_success"))
    attempt = _parse_dt(st.get("last_attempt"))
    failed = bool(attempt) and not (succ and succ >= attempt)
    if failed and now - attempt < datetime.timedelta(seconds=RETRY_AFTER_FAIL):
        return False, "上次失败，%d 分钟后重试" % int(
            (RETRY_AFTER_FAIL - (now - attempt).total_seconds()) // 60)
    if succ and succ >= occ:
        return False, "今天已生成（%s）" % succ.strftime("%m-%d %H:%M")
    return True, "到点未生成（计划 %02d:%02d，上次成功 %s）" % (
        s["hour"], s["minute"], succ.strftime("%m-%d %H:%M") if succ else "从未")


def run_now(cfg, notify=None, log=print, busy=None, reason=""):
    import community_digest as CD
    t0 = time.time()
    log("===== 社区消息日报开始（%s）=====" % (reason or "手动"))
    st = load_state()
    st["last_attempt"] = now_tz().isoformat(timespec="seconds")
    save_state(st)
    res = {"ok": False}
    try:
        day = target_day()
        res = CD.run_community_day(cfg, day, log=log, push=True)
        if res.get("ok"):
            st["last_success"] = now_tz().isoformat(timespec="seconds")
            st["last_day"] = day
            save_state(st)
    except Exception as e:
        log("社区日报失败：%s" % e)
        res = {"ok": False, "error": str(e)}
    log("===== 社区消息日报结束（%s，%.0f 秒）====="
        % ("成功" if res.get("ok") else "失败", time.time() - t0))
    if notify:
        try:
            if res.get("ok"):
                notify("🌐 社区消息日报已生成并推送（%s，%d 条，%d 个子网）"
                       % (res.get("day", ""), res.get("total", 0), res.get("archived_sn", 0)))
            else:
                notify("⚠️ 社区消息日报生成失败：%s" % (res.get("error") or "未知"))
        except Exception:
            pass
    return res


def _loop(cfg, notify, log, busy=None):
    while True:
        try:
            # 保活：daemon 默认 15 分钟空闲退出（会话随之丢失），每 10 分钟 ping 一次归零计时器。
            import community_fetch as CF
            if not CF.ping_daemon(cfg):
                log("浏览器 daemon 不在线：社区日报取数需要它。请在 Edge 登录 dtt 后保持守护运行。")
            ok, why = due(cfg)
            if ok:
                if busy and busy():
                    log("社区日报到期，但流水线在跑，10 分钟后再试")
                else:
                    run_now(cfg, notify=notify, log=log, busy=busy, reason=why)
            else:
                log("社区日报检查：%s" % why)
        except Exception as e:
            log("社区日报调度线程异常（忽略）：%s" % e)
        time.sleep(CHECK_INTERVAL)


def start_thread(cfg, notify=None, log=print, busy=None):
    global _thread_started
    with _thread_lock:
        if _thread_started:
            return False
        s = settings(cfg)
        if not s["enabled"]:
            log("社区日报未启用（config.community_auto = false）")
            return False
        log("社区日报已排程：每天 %02d:%02d（北京时间）；错过会自动补跑" % (s["hour"], s["minute"]))
        t = threading.Thread(target=_loop, args=(cfg, notify, log, busy),
                             name="community-daily", daemon=True)
        t.start()
        _thread_started = True
        return True


def status_text(cfg):
    s = settings(cfg)
    st = load_state()
    ok, why = due(cfg)
    lines = ["🌐 社区消息日报（dtt 多平台）",
             "排程：每天 %02d:%02d 北京时间（%s）" % (s["hour"], s["minute"], "启用" if s["enabled"] else "关闭"),
             "状态：%s" % why]
    if st.get("last_day"):
        lines.append("上次生成：%s" % st["last_day"])
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    if len(sys.argv) > 1 and sys.argv[1] == "now":
        print(run_now(cfg))
    else:
        print(status_text(cfg))
        print("due:", due(cfg))
