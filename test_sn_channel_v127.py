#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.27.0 自测：子网 DC 频道补充取数 + 投研注入 + 报告节落地。

用 mock 的 community_fetch，避免依赖浏览器守护与真实网络。
"""
import os, sys, json, datetime, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import community_fetch as CF
import subnet_research as SR

PASS = 0
FAIL = 0

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))

print("=== v1.27.0 子网 DC 频道补充测试 ===")

# ---- 1. 落盘路径独立（不覆盖社区日报缓存）
p = CF.sn_channel_path(51, "2026-11-22", platform="discord")
check("独立落盘路径", "sn_channel_SN51_discord_2026-11-22.json" in p
      and "community_2026-11-22" not in p, p)

# ---- 2. read_sn_channel 缺失返回 None
check("缺失返回 None", CF.read_sn_channel(999999, "2020-01-01") is None)

# ---- 3. mock fetch_sn_channel 落盘 + read 往返
payload = {
    "sn": 51, "day": "2026-11-22", "platform": "discord", "platform_id": "plt_discord",
    "fetched_at": "2026-11-22T15:00:00+08:00",
    "windows": [{"label": "2026-11-22", "start": 1, "end": 2},
                {"label": "2026-11-21", "start": 0, "end": 1}],
    "keyword_ids": ["k1", "k2"],
    "stats": {"total": 2, "by_day": {"2026-11-22": 2, "2026-11-21": 0}},
    "events": [
        {"id": "e1", "author": "alice,备注:51", "content": "SN51 本周收入创新高，社区认为估值偏低",
         "sourceUrl": "", "platform": "discord", "platformId": "plt_discord",
         "occurredAt": 1795200000000, "kw": "k1", "window": "2026-11-22",
         "time": "11-22 14:00"},
        {"id": "e2", "author": "bob", "content": "团队在 DC 频道回应了回购质疑，称下月公布细节",
         "sourceUrl": "", "platform": "discord", "platformId": "plt_discord",
         "occurredAt": 1795190000000, "kw": "k2", "window": "2026-11-22",
         "time": "11-22 11:00"},
    ],
}
tmpdir = tempfile.mkdtemp(prefix="snch_")
cfg_test = {"community_output_dir": tmpdir}
# 直接写落盘
out = CF.sn_channel_path(51, "2026-11-22", cfg_test, platform="discord")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False)
got = CF.read_sn_channel(51, "2026-11-22", cfg_test, platform="discord")
check("落盘/读取往返", got and got["stats"]["total"] == 2)

# ---- 4. 报告节生成（mock 读取指向临时目录）
_orig_read = CF.read_sn_channel
_read_cfg = cfg_test
CF.read_sn_channel = lambda sn, day, cfg=None, platform="discord": _orig_read(sn, day, _read_cfg, platform=platform)
try:
    sec = SR._sn_channel_report_section(_read_cfg, 51, "2026-11-22",
                                        {"sn": 51, "name": "LAIR"}, log=lambda *a: None)
    check("报告节生成", bool(sec) and "🔎 本子网频道讨论补充" in sec)
    check("报告节含观点", bool(sec) and "估值偏低" in sec and "回购质疑" in sec)
    check("报告节带未验证标记", bool(sec) and "⚠️未验证" in sec)
    check("报告节不贴链接", bool(sec) and "http" not in sec)
    # 无命中返回空
    CF.read_sn_channel = lambda *a, **k: None
    sec0 = SR._sn_channel_report_section({}, 117, "2026-11-22",
                                         {"sn": 117, "name": "Everyframe"}, log=lambda *a: None)
    check("无命中不生成节", sec0 == "")
finally:
    CF.read_sn_channel = _orig_read

# ---- 5. build_input 含 sn_channel 段
arc = {"days": {}, "reports": []}
sources = {}
meta = {"sn": 51, "name": "LAIR", "desc": "", "url": "", "github": ""}
txt = SR.build_input(arc, sources, meta, sn_channel="【测试频道段SN51】")
check("build_input 注入 sn_channel", "本子网频道近两日讨论" in txt and "测试频道段SN51" in txt)
txt2 = SR.build_input(arc, sources, meta)
check("build_input 无 sn_channel 不带段", "本子网频道近两日讨论" not in txt2)

# ---- 6. prompt 说明含频道规则
pr = SR.research_prompt(days=14, max_chars=3000)
check("prompt 含频道补充规则", "本子网频道近两日讨论" in pr and "[DC频道]" in pr)

print("\nv1.27.0 tests: %d/%d passed" % (PASS, PASS + FAIL))
sys.exit(0 if FAIL == 0 else 1)
