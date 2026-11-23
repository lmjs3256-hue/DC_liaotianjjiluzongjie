#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.30.2 自测：已人工确认的冲突，重新总结时不再重复标「待人工确认」。

回归 SN55：收入数字冲突已确认（以原始报告为准、社区仅作情绪参考），
但重跑社区日报 / 投研报告时模型不知道，又标了一次冲突。

本测试用 mock 档案，不碰真实档案。
"""
import os
import sys
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import subnet_research as SR
import subnet_archive as SA

PASS, FAIL = [], []


def ck(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s %s" % ("PASS" if cond else "FAIL", name, extra))


print("=== v1.30.2 已确认冲突不再重复提示测试 ===")

# ---- 1. 构造带/不带确认的档案 ----
arc_confirmed = {
    "sn": 55, "name": "NIOME",
    "community_digests": [
        {"date": "2026-11-23", "platforms": ["twitter"],
         "excerpt": "多人引用累计收入 $120K，逼近注销线",
         "confirm": {"status": "confirmed",
                     "note": "以原始报告为准，目前尚无公开可核验数据。但社区观点可作为社区情绪参考。",
                     "at": "2026-11-23 18:09"}},
        {"date": "2026-11-22", "platforms": ["discord"],
         "excerpt": "有人讨论 alpha 价格",
         "confirm": {"status": "refuted", "note": "该说法已推翻", "at": "2026-11-22 10:00"}},
        {"date": "2026-11-21", "platforms": ["twitter"],
         "excerpt": "未确认的传闻", "confirm": {}},
    ],
}

# ---- 2. 投研：_community_block 注入确认状态 ----
blk = SR._community_block(arc_confirmed, limit=4)
ck("✅ 人工已核实" in blk, "投研线索块含已核实标记")
ck("❌ 人工已推翻" in blk, "投研线索块含已推翻标记")
ck("以原始报告为准" in blk, "投研线索块带上人工批注")
ck("禁止" in blk and "待人工确认" in blk, "投研规则明令禁止重复标待确认",
   blk.split("**使用规则**")[-1][:80])
ck("已人工确认的线索" in blk, "投研规则点名已确认条目")
ck("未确认的传闻" in blk, "未确认线索仍保留")

# 无确认时不应出现「禁止重复标」段（避免误导模型）
arc_plain = {"sn": 1, "community_digests": [
    {"date": "2026-11-23", "excerpt": "普通线索", "platforms": ["twitter"]}]}
blk2 = SR._community_block(arc_plain)
ck("✅ 人工已核实" not in blk2 and "已人工确认的线索" not in blk2,
   "无确认时不注入已确认段")
ck("必须在报告里并列写出" in blk2, "无确认时原冲突规则仍在")

# ---- 3. 投研全局提示词含「不再重复挂起」规则 ----
import inspect
src = inspect.getsource(SR)
ck("已人工裁决的冲突不再重复挂起" in src, "投研提示词含不重复挂起规则")

# ---- 4. SA.get_all_confirms：跨日期 + 指定SN ----
tmp = tempfile.mkdtemp(prefix="cfm_")
cfg = {"archive_dir": tmp}
os.makedirs(os.path.join(tmp, "SN55"), exist_ok=True)
raw = {
    "sn": 55, "name": "NIOME",
    "community_digests": [
        {"date": "2026-11-23", "confirm": {"status": "confirmed", "note": "N1", "at": "t1"}},
        {"date": "2026-11-21", "confirm": {"status": "refuted", "note": "N2", "at": "t2"}},
        {"date": "2026-11-20", "confirm": {}},
    ],
}
with open(os.path.join(tmp, "SN55", "archive.json"), "w", encoding="utf-8") as f:
    json.dump(raw, f, ensure_ascii=False)

allc = SA.get_all_confirms(cfg)
ck(55 in allc and len(allc[55]) == 2, "get_all_confirms 跨日期取全部确认")
ck(allc[55][0]["date"] == "2026-11-23", "确认按日期倒序（最新在前）")
ck(allc[55][0]["status"] == "confirmed" and allc[55][0]["note"] == "N1", "确认字段完整")
only = SA.get_all_confirms(cfg, sn=55)
ck(set(only) == {55}, "get_all_confirms 支持指定SN")
empty = SA.get_all_confirms({"archive_dir": os.path.join(tmp, "none")})
ck(empty == {}, "档案目录缺失返回空")
os.path.join(tmp, "SN55", "archive.json")
import shutil
shutil.rmtree(tmp, ignore_errors=True)

# ---- 5. 社区日报：build_input 注入已确认清单（mock SA.get_all_confirms）----
import community_digest as CD
_orig = SA.get_all_confirms
SA.get_all_confirms = lambda c: {
    55: [{"date": "2026-11-23", "status": "confirmed", "note": "以原始报告为准", "at": "2026-11-23 18:09"}]
}
payload = {"day": "2026-11-23", "events": [
    {"sn": [55], "author": "someone", "content": "SN55 收入 $120K 的讨论",
     "platform": "twitter", "occurredAt": 1795400000000}]}
try:
    text, sns, _ex = CD.build_input(payload, {})
    ck("已人工裁决的历史冲突" in text, "社区日报输入含已确认冲突段")
    ck("✅ 人工已核实" in text, "社区日报输入含已核实标记")
    ck("以原始报告为准" in text, "社区日报输入带人工批注")
    ck("禁止" in text and "再写成" in text, "社区日报输入规则禁止重复标")
finally:
    SA.get_all_confirms = _orig

src2 = inspect.getsource(CD)
ck("已人工裁决的冲突不再重复挂起" in src2, "社区日报提示词含不重复挂起规则")

print("\n=== 结果：%d 通过 / %d 失败 ===" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：" + "、".join(FAIL))
    sys.exit(1)
