#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.30.7 自测：日报回执如实反映推送 + TG群取数优先走真人 Edge CDP。

不依赖真人 Edge：mock CDP 调用，覆盖回执三态、命令接线、取数优先级。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tg_listener as TL
import community_fetch as CF
import tg_group_fetch as TGF

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


print("=== v1.30.7 回执 + 群消息 CDP 测试 ===")

# ---- 1. 回执三态：空报告绝不声称已推送 ----
print("-- 1. 日报回执 --")
empty = ("TG群取数完成：2026-11-24 共 0 条（）\n"
         "TG群日报完成：2026-11-24【Hanlin.AI】（群消息 0 条，严格相关 0 条，0 个子网落档）")
r = TL._daily_receipt(empty, ["tg_group", "yesterday", "1003968896337"])
check("空报告回执不含已推送", "已生成并推送" not in r, r)
check("空报告回执说明未推送", "未推送正文" in r, r)

ok = ("飞书推送完成\nTelegram 推送分片 1 完成\n"
      "TG群日报完成：2026-11-24【Hanlin.AI】（群消息 35 条，严格相关 8 条，0 个子网落档）")
r = TL._daily_receipt(ok, ["tg_group"])
check("有推送标记才说已推送", "已生成并推送（飞书 + TG）" in r, r)

no_push = "TG群日报完成：2026-11-24【Hanlin.AI】（群消息 35 条，严格相关 8 条，0 个子网落档）"
r = TL._daily_receipt(no_push, ["tg_group"])
check("有数据无推送标记报异常", "未见推送成功记录" in r and "已生成并推送" not in r, r)

cempty = "社区日报完成：2026-11-24（0 条消息，0 个子网落档）"
r = TL._daily_receipt(cempty, ["community"])
check("社区空报告也不误报", "未推送正文" in r and "已生成并推送" not in r, r)

cok = ("飞书推送完成\nTelegram 推送分片 1 完成\n"
       "社区日报完成：2026-11-24（1319 条消息，12 个子网落档）\n"
       "社区日报交叉核对： 2 处「待人工确认」")
r = TL._daily_receipt(cok, ["community"])
check("社区有数据回执已推送", "已生成并推送（飞书 + TG）" in r, r)
check("社区冲突标志保留", "2 处「待人工确认」" in r, r)

# ---- 2. CDP 命令接线 ----
print("-- 2. CDP 群消息命令 --")
js = open(os.path.join(HERE, "dtt_cdp.js"), encoding="utf-8").read()
check("JS 支持 groupmessages", "cmd === 'groupmessages'" in js)
check("JS 调 group-messages 接口", "/api/group-messages" in js)
check("Python 层有 cdp_group_messages", callable(CF.cdp_group_messages))

orig_run_cdp = CF._run_cdp
captured = {}


def fake_run_cdp(cmd, arg, **kw):
    captured["cmd"] = cmd
    captured["args"] = json.loads(arg)
    return {"ok": True, "events": [{"id": 1}], "pages": 1}


CF._run_cdp = fake_run_cdp
res = CF.cdp_group_messages({"start": 1, "end": 2, "groupIds": ["123"],
                             "limit": 100, "maxPages": 1}, cfg={})
check("CDP 调用命令正确", captured.get("cmd") == "groupmessages", captured)
check("CDP 参数含群列表", captured.get("args", {}).get("groupIds") == ["123"])
check("CDP 结果解析", len(res.get("events") or []) == 1)
CF._run_cdp = orig_run_cdp

# ---- 3. 群消息取数优先 CDP，失败才回退 ----
print("-- 3. 取数优先级 --")
orig_avail, orig_cdp, orig_ready, orig_js = (
    CF.cdp_available, CF.cdp_group_messages, CF.ensure_ready, TGF._run_js)
tmpdir = tempfile.mkdtemp(prefix="tggrp_test_")
cfg = {"tg_group_output_dir": tmpdir}

CF.cdp_available = lambda cfg=None, **kw: True
CF.cdp_group_messages = lambda args, cfg=None, log=print: {
    "ok": True, "events": [], "pages": 1}
CF.ensure_ready = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应走旧守护"))
TGF._run_js = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应走旧 CLI"))
p = TGF.fetch_day("2026-11-24", cfg=cfg, log=lambda *a: None)
check("CDP 在线时不走旧守护", p.get("pages") == 1 and p["stats"]["total"] == 0)

CF.cdp_group_messages = lambda args, cfg=None, log=print: (_ for _ in ()).throw(RuntimeError("CDP 挂了"))
CF.ensure_ready = lambda *a, **kw: None
TGF._run_js = lambda *a, **kw: {"ok": True, "events": [{"occurredAt": 1, "id": "x"}], "pages": 1}
p = TGF.fetch_day("2026-11-24", cfg=cfg, log=lambda *a: None)
check("CDP 失败回退旧通道", p["stats"]["total"] == 1)

CF.cdp_available, CF.cdp_group_messages = orig_avail, orig_cdp
CF.ensure_ready, TGF._run_js = orig_ready, orig_js

print("\n结果：%d 通过，%d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
