#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.25.0 回归：TG群日报分步选群 + 推文核实 + 话题人物观点三段式。

不触发真实 LLM / 网络抓取；用假数据与 monkeypatch 验证纯逻辑。
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import tweet_fetch as TF
import tg_group_digest as TGD
import tg_group_fetch as TGF

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅", name)
    else:
        FAIL += 1
        print("  ❌", name, "|", str(detail)[:220])


# ---------------------------------------------------------------- 1) 推文链接抽取
print("== 1) 推文链接抽取 ==")
cases = [
    ("https://x.com/bitcoinnewscom/status/2124111559332856196?s=10", ["2124111559332856196"]),
    ("看这个 https://twitter.com/foo/status/1234567890123456789 和 x.com/bar/status/9876543210987654321",
     ["1234567890123456789", "9876543210987654321"]),
    ("https://fxtwitter.com/i/web/status/1111111111111111111", ["1111111111111111111"]),
    ("纯文本没有链接", []),
    ("https://x.com/user/status/1234567890123456789 / x.com/user/status/1234567890123456789",
     ["1234567890123456789"]),  # 去重
]
for text, expect in cases:
    got = [tid for _, tid in TF.extract_tweet_urls(text)]
    check("抽取 %s" % text[:28], got == expect, "got=%s expect=%s" % (got, expect))

# ---------------------------------------------------------------- 2) 缓存命中不抓网络
print("== 2) 缓存命中/失败标注 ==")
import time as _time
fake_cache_path = os.path.join(TGF._out_dir(), "tweet_cache.json")
# 用内存缓存函数替换（避免污染真实缓存文件）
_orig_load, _orig_save = TF._load_cache, TF._save_cache
_mem = {"2222222222222222222": {"ok": True, "text": "cached tweet body", "author": "SomeOne",
                                 "ts": _time.time()}}
TF._load_cache = lambda cfg=None: _mem
TF._save_cache = lambda cache, cfg=None: None
ev = [{"content": "https://x.com/u/status/2222222222222222222"}]
st = TF.verify_links(ev, cfg={}, log=lambda *_: None)
check("缓存命中统计", st["cached"] == 1 and st["fetched"] == 0, st)
check("缓存正文注入 event", ev[0]["_tweets"][0]["text"] == "cached tweet body", ev[0])

# 失败标注：budget 用尽 → 标注未核实
ev2 = [{"content": "https://x.com/u/status/%d" % (3000000000000000000 + i)} for i in range(3)]
st2 = TF.verify_links(ev2, cfg={}, log=lambda *_: None)
# _mem 里没有这些 id，会真的去抓（budget=40 足够）——用 monkeypatch _fetch_one 避免网络
TF._fetch_one = lambda tid, log=None: {"ok": False, "note": "内容未能核实（fake）"}
_mem.clear()
st3 = TF.verify_links([{"content": "https://x.com/u/status/4444444444444444444"}],
                      cfg={}, log=lambda *_: None)
check("失败如实标注", st3["failed"] >= 1, st3)
TF._load_cache, TF._save_cache = _orig_load, _orig_save

# ---------------------------------------------------------------- 3) 推文核实行注入输入
print("== 3) 推文核实行注入 build_input ==")
sample = {
    "day": "2026-11-22",
    "bounds": {},
    "events": [{
        "groupId": "8116469191", "groupName": "TAO-update-DC", "author": "TAO-update-DC@x",
        "content": "⛳sn:91 Gscade https://x.com/MSBIntel/status/5555555555555555555",
        "occurredAt": 1763800000000, "_time": "02:26",
        "_tweets": [{"url": "https://x.com/MSBIntel/status/5555555555555555555",
                     "id": "5555555555555555555", "ok": True,
                     "text": "Gscade achieved SOTA on SN91 with 19 days uptime",
                     "author": "MSB Intel"}],
    }],
}
text, stats = TGD.build_input(sample, cfg={})
check("推文核实行出现", "🔗推文核实" in text, text[-500:])
check("核实正文进输入", "Gscade achieved SOTA" in text, text[-400:])
check("不贴原始链接正文", "https://x.com" not in text.split("🔗推文核实")[-1], text[-300:])
# 失败分支
sample["events"][0]["_tweets"][0] = {"url": "x", "id": "555", "ok": False,
                                     "note": "内容未能核实（fake）"}
text2, _ = TGD.build_input(sample, cfg={})
check("失败标注进输入", "内容未能核实" in text2 and "禁止编造" in text2, text2[-400:])

# ---------------------------------------------------------------- 4) 三段式提示词
print("== 4) 三段式提示词 ==")
sp = TGD.TG_GROUP_SYSTEM_PROMPT
check("话题段", "①话题" in sp and "话题（标 SN）" in sp)
check("人物段", "②人物" in sp)
check("观点段", "③观点" in sp)
check("推文核实节", "🔗 推文核实" in sp)
check("禁止只写转发链接", "严禁只写「某人转发了一条推特链接」" in sp)
check("字数上调3000", "3000 字" in sp)

# ---------------------------------------------------------------- 5) 分组映射与键盘
print("== 5) 分组映射 ==")
check("4群映射齐全", len(TGF.GROUP_LABELS) == 4, TGF.GROUP_LABELS)
check("groupId→名", TGF.group_label("8116469191") == "TAO-update-DC")
check("未登记回退", TGF.group_label("999", "兜底") == "兜底")

import tg_listener as L
kb = L._tgg_group_keyboard()
rows = kb["inline_keyboard"]
datas = [b["callback_data"] for row in rows for b in row]
check("群键盘4群+全部+取消", len(datas) == 6, datas)
check("群回调 data 格式", all(d.startswith("tggrp:") for d in datas), datas)
check("含具体群ID", "tggrp:8116469191" in datas, datas)
kb2 = L._tgg_day_keyboard()
d2 = [b["callback_data"] for row in kb2["inline_keyboard"] for b in row]
check("日期键盘昨日/今日/取消", d2 == ["tgday:yesterday", "tgday:today", "tgday:cancel"], d2)

# ---------------------------------------------------------------- 6) 选群状态机与回调分发
print("== 6) 选群状态机 ==")
sent, runs = [], []
L.safe_send = lambda chat, text, markup=None: sent.append((chat, text, markup))
L.send_async = lambda chat, text, markup=None: sent.append((chat, text, markup))
L.run_pipeline = lambda args, chat, quiet=True: runs.append((args, chat))
L.tg_api = lambda *a, **kw: {"ok": True}

# 第 1 轮：裸命令回执 4 群按钮
L._tgg_flow_clear("c1")
L._tgg_prompt("c1")
check("裸命令回执群列表", "TAO-update-DC" in sent[-1][1], sent[-1])
check("回执带内联群键盘", sent[-1][2] and "inline_keyboard" in sent[-1][2], sent[-1][2])
check("状态进入 group 步", L._tgg_flow_active("c1")["step"] == "group")

# 点具体群 → 进入第 2 轮「选日期」，此时绝不生成
L._tgg_callback({"id": "q1", "data": "tggrp:1003968896337"}, "c1", "u1", [])
check("选群后追问日期", "昨日" in sent[-1][1] and "今日" in sent[-1][1], sent[-1])
check("选群后带日期键盘", "tgday:today" in json.dumps(sent[-1][2], ensure_ascii=False))
check("选群后尚未生成", runs == [], runs)
check("状态进入 day 步且记住群", L._tgg_flow_active("c1")["step"] == "day"
      and L._tgg_flow_active("c1")["gid"] == "1003968896337")

# 第 2 轮：点「今日」→ 才真正生成（回执 + run_pipeline 带 groupId + 今日）
L._tgg_callback({"id": "q1b", "data": "tgday:today"}, "c1", "u1", [])
check("选日期后触发 pipeline", any(a[0][0] == "tg_group" and "1003968896337" in a[0] for a in runs), runs)
check("今日参数透传", any("今日" in a[0] for a in runs), runs)
check("生成回执含群名+今日", "Hanlin.AI" in sent[-1][1] and "今日" in sent[-1][1], sent[-1])
check("完成后状态清除", L._tgg_flow_active("c1") is None)

# 选「昨日」→ 传昨日
runs.clear()
L._tgg_prompt("c2")
L._tgg_callback({"id": "q2", "data": "tggrp:8116469191"}, "c2", "u1", [])
L._tgg_callback({"id": "q2b", "data": "tgday:yesterday"}, "c2", "u1", [])
check("昨日参数透传", any("昨日" in a[0] for a in runs), runs)

# 全部 4 群（选群 all → 再选日期）
runs.clear()
L._tgg_prompt("c2b")
L._tgg_callback({"id": "qa", "data": "tggrp:all"}, "c2b", "u1", [])
L._tgg_callback({"id": "qab", "data": "tgday:today"}, "c2b", "u1", [])
check("全部群无 groupId", any(a[0] == ["tg_group", "今日"] for a in runs), runs)

# 取消（第1轮）
L._tgg_prompt("c3")
L._tgg_callback({"id": "q3", "data": "tggrp:cancel"}, "c3", "u1", [])
check("取消清状态", L._tgg_flow_active("c3") is None)
check("取消回执", "已取消" in sent[-1][1], sent[-1])

# 超时无效
L._TGG_FLOW["c4"] = {"step": "group", "ts": 0}
L._tgg_callback({"id": "q4", "data": "tggrp:8116469191"}, "c4", "u1", [])
check("超时提示重发", "超时" in sent[-1][1], sent[-1])

# 文本两轮：第1轮序号选群 → 第2轮文本日期
runs.clear()
L._tgg_prompt("c5")
c1 = L._tgg_flow_consume("1", "c5")
check("文本序号选群进入date步", c1 and L._tgg_flow_active("c5")["step"] == "day")
check("文本选群未生成", runs == [])
c2 = L._tgg_flow_consume("2026-11-20", "c5")
check("文本日期消费并生成", c2 and any("2026-11-20" in a[0] for a in runs), (c2, runs))

# ---------------------------------------------------------------- 7) pipeline 单群参数解析（日志正则）
print("== 7) 回执正则兼容群名 ==")
line_all = "TG群日报完成：2026-11-22（群消息 164 条，严格相关 73 条，16 个子网落档）"
line_one = "TG群日报完成：2026-11-22【Hanlin.AI】（群消息 35 条，严格相关 9 条，3 个子网落档）"
pat = r"TG群日报完成：\s*(\d{4}-\d{2}-\d{2})\s*(?:【([^】]+)】)?\s*（群消息\s*(\d+)\s*条，严格相关\s*(\d+)\s*条，(\d+)\s*个子网落档）"
import re
m1, m2 = re.search(pat, line_all), re.search(pat, line_one)
check("全群日志匹配", m1 and m1.group(2) is None, m1.groups() if m1 else None)
check("单群日志匹配群名", m2 and m2.group(2) == "Hanlin.AI", m2.groups() if m2 else None)

print("\n=== 结果：%d 通过 / %d 失败 ===" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
