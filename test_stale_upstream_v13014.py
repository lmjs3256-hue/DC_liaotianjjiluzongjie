#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.30.14 自测：dtt 群消息库停更时，0 条不得说成「群内无消息」。"""
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tg_group_fetch as TGF
import tg_group_digest as TGD
import tg_listener as TL
import pipeline as P

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


print("=== v1.30.14 群消息上游停更识别 ===")

now = datetime.datetime.now(TGF.TZ)
old_ms = int((now - datetime.timedelta(hours=54)).timestamp() * 1000)
fresh_ms = int((now - datetime.timedelta(hours=2)).timestamp() * 1000)

up = TGF._upstream_info([{"groupId": "1", "lastOccurredAt": old_ms}], now=now)
check("滞后 54h 判停更", up["stale"] is True, up)
check("落最后入库北京时间", bool(up["last_bj"]), up)

up2 = TGF._upstream_info([{"groupId": "1", "lastOccurredAt": fresh_ms}], now=now)
check("滞后 2h 不判停更", up2["stale"] is False, up2)

check("无群水位不误判", TGF._upstream_info([], now=now)["stale"] is False)

# 取数脚本必须一并取水位（两个通道都要）
js = open(os.path.join(HERE, "dtt_cdp.js"), encoding="utf-8").read()
check("CDP 脚本取群水位", "group-messages/groups" in js[js.find("async function groupMessages"):])
py = open(os.path.join(HERE, "tg_group_fetch.py"), encoding="utf-8").read()
check("回退脚本取群水位", "group-messages/groups" in py[py.find("_JS_FETCH"):py.find("def _run_js")])

# 空 + 停更：不调 AI、不推送，明确提示
covering = {"start": int((now - datetime.timedelta(days=1)).timestamp() * 1000),
            "end": int(now.timestamp() * 1000), "unit": "ms", "until_now": True}
stale_payload = {"day": "2026-11-27", "events": [], "bounds": covering,
                 "upstream": up, "stats": {"total": 0}}
orig_read, orig_llm = TGF.read_day, P.call_llm
logs = []
try:
    TGF.read_day = lambda *a, **k: stale_payload
    P.call_llm = lambda *a, **k: (_ for _ in ()).throw(AssertionError("停更时不应调 AI"))
    r = TGD.run_tg_group_day({}, "2026-11-27", log=logs.append, push=True,
                             group_id="1003968896337")
finally:
    TGF.read_day, P.call_llm = orig_read, orig_llm

check("停更返回 stale 标记", r.get("stale") is True, r)
check("停更未声称当日无消息", "当日无消息" not in (r.get("digest") or ""), r.get("digest"))
check("停更说明最后入库时间", up["last_bj"] in (r.get("digest") or ""), r.get("digest"))
check("停更写日志", any("TG群数据源停更" in x for x in logs), logs)
check("单群标题保留", "Hanlin.AI" in (r.get("digest") or ""), r.get("digest"))

out = "\n".join(logs + ["TG群日报完成：2026-11-27【Hanlin.AI】（群消息 0 条，严格相关 0 条，0 个子网落档）",
                        "TG群数据源停更：" + r["digest"]])
receipt = TL._daily_receipt(out, ["tg_group", "2026-11-27", "1003968896337"])
check("回执不称已生成日报", "未生成" in receipt and "已生成" not in receipt, receipt)
check("回执说明非群内无消息", "非群内无消息" in receipt, receipt)

# 空但上游新鲜：仍走原来的「无消息」口径
fresh_payload = dict(stale_payload, upstream=up2)
try:
    TGF.read_day = lambda *a, **k: fresh_payload
    P.call_llm = lambda *a, **k: (_ for _ in ()).throw(AssertionError("空数据不应调 AI"))
    r2 = TGD.run_tg_group_day({}, "2026-11-27", log=lambda *_: None, push=False)
finally:
    TGF.read_day, P.call_llm = orig_read, orig_llm
check("真·空群仍报无消息", r2.get("empty") is True and not r2.get("stale")
      and "无消息" in r2["digest"], r2)

print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
