#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""子网知识库周更调度器
=======================
背景（2026-11-09 现场）：
    `subnets.json` 是从 taostats 抓的**离线快照**，只有手动跑
    `python update_subnets.py` 才会刷新。它一停，全部下游都跟着错：
    投研按旧名写报告（SN127 写成 Astrid，链上早已是 Zils）、
    自动建档用旧名当档案名、`/retry` 的别名反查也从旧名起步。
    所以「记得手动跑」不能作为依赖，必须自动。

调度规则（不引入第三方库，纯 stdlib）：
    - 周期：每 7 天一次，默认**周一 09:00**（北京时间，可在 config 改）。
    - 补跑：机器关机/监听重启错过时间点后，下次启动只要发现「上次成功
      早于最近一次计划时间」就立刻补跑，不会漏一周。
    - 幂等：成功写 last_success；失败写 last_attempt 并退避 30 分钟，
      避免页面结构变了以后每 10 分钟撞一次网络。
    - 不打扰：日报流水线正在跑（_BUSY）就跳过本轮，10 分钟后再看。

对外接口：
    status_text(cfg)          给 `/kb` 用的状态文本
    run_now(cfg, notify=...)  立即执行一次（`/kb update` 或手动）
    start_thread(cfg, ...)    启动后台调度线程（tg_listener 启动时调用）
    due(cfg)                  现在是否该跑（也用于自测）
"""

import datetime
import json
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "exports", "_kb_state.json")

TZ = datetime.timezone(datetime.timedelta(hours=8))
CHECK_INTERVAL = 600        # 每 10 分钟检查一次
RETRY_AFTER_FAIL = 1800     # 失败后 30 分钟才重试
DEFAULT_WEEKDAY = 0         # 0=周一（Python weekday 口径）
DEFAULT_HOUR = 9

_thread_started = False
_thread_lock = threading.Lock()


# ---------------------------------------------------------------- 配置 / 状态
def _cfg_get(cfg, key, default):
    v = (cfg or {}).get(key)
    return default if v is None else v


def settings(cfg):
    """归一化后的周更设置。config 里写几天点就按几天点，写错就用默认值。"""
    try:
        wd = int(_cfg_get(cfg, "kb_update_weekday", DEFAULT_WEEKDAY))
    except Exception:
        wd = DEFAULT_WEEKDAY
    try:
        hr = int(_cfg_get(cfg, "kb_update_hour", DEFAULT_HOUR))
    except Exception:
        hr = DEFAULT_HOUR
    if not 0 <= wd <= 6:
        wd = DEFAULT_WEEKDAY
    if not 0 <= hr <= 23:
        hr = DEFAULT_HOUR
    return {
        "enabled": bool(_cfg_get(cfg, "kb_auto_update", True)),
        "weekday": wd,
        "hour": hr,
        "notify": bool(_cfg_get(cfg, "kb_update_notify", True)),
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
    """状态里的时间串（带 +08:00）解析成 aware datetime；坏数据返回 None。"""
    if not s:
        return None
    try:
        d = datetime.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=TZ)
    except Exception:
        return None


def last_occurrence(now=None, weekday=None, hour=None):
    """最近一次「已经过去」的计划时间点（含今天）。"""
    now = now or now_tz()
    if weekday is None or hour is None:
        weekday, hour = DEFAULT_WEEKDAY, DEFAULT_HOUR
    cand = (now - datetime.timedelta(days=(now.weekday() - weekday) % 7)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    if cand > now:                      # 今天是计划日但时间还没到 -> 退到上周
        cand -= datetime.timedelta(days=7)
    return cand


def due(cfg, now=None):
    """返回 (是否该跑, 原因文本)。"""
    now = now or now_tz()
    s = settings(cfg)
    if not s["enabled"]:
        return False, "周更已关闭（config.kb_auto_update = false）"

    occ = last_occurrence(now, s["weekday"], s["hour"])
    st = load_state()
    succ = _parse_dt(st.get("last_success"))
    attempt = _parse_dt(st.get("last_attempt"))

    # 上次**没成功**才退避；成功过就让它按周期走，别把成功也当失败拦 30 分钟。
    failed = bool(attempt) and not (succ and succ >= attempt)
    if failed and now - attempt < datetime.timedelta(seconds=RETRY_AFTER_FAIL):
        return False, "上次尝试失败，%d 分钟后重试" % int(
            (RETRY_AFTER_FAIL - (now - attempt).total_seconds()) // 60)

    if succ and succ >= occ:
        return False, "本周已更新（%s）" % succ.strftime("%m-%d %H:%M")
    return True, "到点未更新（计划 %s，上次成功 %s）" % (
        occ.strftime("%m-%d %H:%M"),
        succ.strftime("%m-%d %H:%M") if succ else "从未")


# ---------------------------------------------------------------- 执行
def _summarize(res):
    """把 update_subnets.run() 的结果压成一段人话，供 TG 通知。"""
    lines = []
    changes = res.get("changes") or []
    if changes:
        by_kind = {}
        for kind, sn, name, old in changes:
            by_kind.setdefault(kind, []).append((sn, name, old))
        lines.append("知识库变更 %d 项：" % len(changes))
        for kind in ("改名", "新增", "注销", "重新注册", "业务变更"):
            items = by_kind.get(kind) or []
            if not items:
                continue
            show = "、".join(
                ("SN%d %s%s" % (sn, name, ("（旧名 %s）" % old) if old else ""))
                for sn, name, old in items[:6])
            more = "" if len(items) <= 6 else " 等 %d 个" % len(items)
            lines.append("· %s：%s%s" % (kind, show, more))
    else:
        lines.append("知识库与上次一致，无变更")

    synced = res.get("archive_synced") or []
    if synced:
        lines.append("档案同步 %d 份：%s" % (
            len(synced),
            "、".join("SN%s" % x[0] for x in synced[:8]) + ("" if len(synced) <= 8 else " 等")))
    return "\n".join(lines)


def run_now(cfg, notify=None, log=print, reason="手动触发"):
    """立即执行一次。返回 update_subnets.run() 的结果字典（含 ok）。"""
    import update_subnets as U          # 延迟导入：避免与 pipeline 循环依赖

    st = load_state()
    st["last_attempt"] = now_tz().isoformat(timespec="seconds")
    save_state(st)

    log("===== 知识库周更开始（%s）=====" % reason)
    t0 = time.time()
    try:
        res = U.run(cfg=cfg, use_llm=True, dry=False, sync_archive=True, log=log)
    except Exception as e:
        res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
               "changes": [], "archive_synced": [], "fetched": 0}
        log("知识库周更异常：%s" % res["error"])

    st = load_state()
    if res.get("ok"):
        st["last_success"] = now_tz().isoformat(timespec="seconds")
        st["fetched"] = res.get("fetched") or 0
        st["changes"] = len(res.get("changes") or [])
        st.pop("last_error", None)
    else:
        st["last_error"] = res.get("error") or "未知错误"
    save_state(st)

    log("===== 知识库周更结束（%s，%.0f 秒）=====" %
        ("成功" if res.get("ok") else "失败", time.time() - t0))

    if notify:
        try:
            if res.get("ok"):
                notify("📚 子网知识库已更新\n" + _summarize(res))
            else:
                notify("⚠️ 子网知识库更新失败：%s\n（知识库保持原样，未做任何修改）"
                       % (res.get("error") or "未知错误"))
        except Exception as e:
            log("周更通知发送失败（忽略）：%s" % e)
    return res


# ---------------------------------------------------------------- 后台线程
def _loop(cfg, notify, log, busy=None):
    while True:
        try:
            ok, why = due(cfg)
            if ok:
                # 日报流水线在跑就先让路（会同时写档案，避免互相踩）
                if busy and busy():
                    log("知识库周更到期，但流水线正在执行，10 分钟后再试")
                else:
                    run_now(cfg, notify=notify, log=log, reason=why)
            else:
                log("知识库周更检查：%s" % why)
        except Exception as e:
            log("知识库周更线程异常（已忽略，继续调度）：%s" % e)
        time.sleep(CHECK_INTERVAL)


def start_thread(cfg, notify=None, log=print, busy=None):
    """启动后台调度线程（幂等：重复调用只会有一个线程）。"""
    global _thread_started
    with _thread_lock:
        if _thread_started:
            return False
        s = settings(cfg)
        if not s["enabled"]:
            log("知识库周更未启用（config.kb_auto_update = false）")
            return False
        names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        log("知识库周更已排程：每 %s %02d:00（北京时间）；到点未更新会自动补跑"
            % (names[s["weekday"]], s["hour"]))
        t = threading.Thread(target=_loop, args=(cfg, notify, log, busy),
                             name="kb-weekly", daemon=True)
        t.start()
        _thread_started = True
        return True


# ---------------------------------------------------------------- 状态文本
def status_text(cfg):
    s = settings(cfg)
    st = load_state()
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    nxt = last_occurrence(now_tz(), s["weekday"], s["hour"]) + datetime.timedelta(days=7)

    lines = ["📚 子网知识库周更",
             "排程：每%s %02d:00（北京时间）" % (names[s["weekday"]], s["hour"]) +
             ("" if s["enabled"] else "  ⛔ 已关闭"),
             "下次：%s" % nxt.strftime("%m-%d %H:%M")]
    succ = _parse_dt(st.get("last_success"))
    lines.append("上次成功：%s" % (succ.strftime("%Y-%m-%d %H:%M") if succ else "从未"))
    if st.get("changes") is not None and succ:
        lines.append("上次变更：%d 项（解析 %s 个子网）"
                     % (int(st.get("changes") or 0), st.get("fetched") or "?"))
    if st.get("last_error"):
        lines.append("⚠️ 上次失败：%s" % st["last_error"])

    try:
        import update_subnets as U
        kb = U.load_data()
        lines.append("")
        lines.append("当前知识库：%d 个子网，快照日期 %s"
                     % (len(kb.get("subnets") or {}), kb.get("updated_at") or "未知"))
    except Exception:
        pass

    ok, why = due(cfg)
    lines.append("")
    lines.append("状态：%s" % ("待补跑（" + why + "）" if ok else why))
    lines.append("手动立即更新：/kb update")
    return "\n".join(lines)


if __name__ == "__main__":
    # 自测：python kb_weekly.py [status|run|due]
    import sys
    _cfg = {}
    try:
        import pipeline as P
        _cfg = P.load_config()
    except Exception as e:
        print("读配置失败（用默认值自测）：%s" % e)
    act = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if act == "run":
        r = run_now(_cfg, notify=None, log=print, reason="手动触发")
        print("结果 ok=%s 变更=%d 档案同步=%d"
              % (r.get("ok"), len(r.get("changes") or []), len(r.get("archive_synced") or [])))
    elif act == "due":
        print(due(_cfg))
    else:
        print(status_text(_cfg))
