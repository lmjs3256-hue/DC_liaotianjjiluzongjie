#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.30.1 自测：推送头部范围来源修复。

背景：v1.30.0 及以前，push_feishu/push_telegram 只要标题含日期且判为「日报」，
就去读 _digest_state.json 的 range（DC 日报导出边界）。社区/TG群/Gamma 日报
复用推送层，导致头部显示 DC 日报的 10:44，而非自身取数时刻（17:58 / 16:38）。

本测试 mock 掉真实网络，只验证范围文本的来源与优先级。
"""
import sys
import datetime
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pipeline as P

PASS, FAIL = [], []


def ck(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s %s" % ("PASS" if cond else "FAIL", name, extra))


TZ = datetime.timezone(datetime.timedelta(hours=8))

print("=== v1.30.1 推送范围来源测试 ===")

# ---- 1. bounds_range_text：秒（社区日报）/ 毫秒（TG群日报） ----
start = int(datetime.datetime(2026, 11, 22, 0, 0, tzinfo=TZ).timestamp())
end = int(datetime.datetime(2026, 11, 23, 17, 58, tzinfo=TZ).timestamp())
sec_bounds = {"start": start, "end": end, "until_now": True}
ms_bounds = {"start": start * 1000, "end": end * 1000, "until_now": True, "unit": "ms"}

ck(P.bounds_range_text(sec_bounds) == "11-22 00:00 → 11-23 17:58",
   "秒级bounds范围正确", P.bounds_range_text(sec_bounds))
ck(P.bounds_range_text(ms_bounds) == "11-22 00:00 → 11-23 17:58",
   "毫秒bounds范围正确", P.bounds_range_text(ms_bounds))
ck(P.bounds_range_text({}) is None, "空bounds返回None")
ck(P.bounds_range_text({"start": start}) is None, "缺end返回None")
ck(P.bounds_range_text({"start": "bad", "end": end}) is None, "异常值返回None")

# 同日范围不误显示成次日 23:59（_fmt_range 只在 end 恰好为跨日 00:00 时压缩）
same_start = int(datetime.datetime(2026, 11, 22, 0, 0, tzinfo=TZ).timestamp())
same_end = int(datetime.datetime(2026, 11, 22, 23, 59, tzinfo=TZ).timestamp())
ck(P.bounds_range_text({"start": same_start, "end": same_end}) == "11-22 00:00 → 11-22 23:59",
   "同日范围正确")

# ---- 2. 标题白名单：只有 DC 日报能查 _digest_state.json ----
ck(P._is_dc_daily_title("🤖 DTAO price-talk 日报 · 2026-11-22") is True, "DC日报标题命中白名单")
ck(P._is_dc_daily_title("🤖 DTAO price-talk 日报 · 2026-11-22（按反馈修订）") is True,
   "DC修订版标题命中白名单")
ck(P._is_dc_daily_title("🌐 Bittensor 社区消息日报（dtt版）· 2026-11-22（昨日）") is False,
   "社区日报标题排除")
ck(P._is_dc_daily_title("💬 Bittensor TG 群消息日报 · 2026-11-22（昨日）") is False,
   "TG群日报标题排除")
ck(P._is_dc_daily_title("📋 Gamma 开通子网列表 · 2026-11-23 17:30") is False, "Gamma标题排除")

# ---- 3. push_feishu / push_telegram：显式 range_text 优先，不再读 DC 范围 ----
_orig_post = P._post_json
_seen = []


def fake_post(url, payload, **kw):
    _seen.append((url, json.loads(json.dumps(payload, default=str))))
    return {}


P._post_json = fake_post
cfg = {
    "feishu_webhook_url": "https://example.invalid/hook",
    "telegram_bot_token": "dummy",
    "telegram_chat_id": "123456",
    "llm_label": "测试模型",
}
orig_day_range = P._day_range_text
P._day_range_text = lambda c, t: "11-22 00:00 → 11-23 10:44"  # 模拟 DC 的旧边界

com_title = "🌐 Bittensor 社区消息日报（dtt版）· 2026-11-22（昨日）"
P.push_feishu(cfg, com_title, "**🔥 社区热点**\n- 测试", range_text="11-22 00:00 → 11-23 17:58")
card_text = _seen[-1][1]["card"]["elements"][0]["text"]["content"]
ck("11-23 17:58" in card_text and "10:44" not in card_text,
   "飞书社区日报头部用自身边界", card_text.splitlines()[1][:60])

P.push_telegram(cfg, com_title, "**🔥 社区热点**\n- 测试", range_text="11-22 00:00 → 11-23 17:58")
tg_text = _seen[-1][1]["text"]
ck("📅 11-22 00:00 → 11-23 17:58" in tg_text and "10:44" not in tg_text,
   "TG社区日报头部用自身边界", tg_text.splitlines()[1][:60])

# 非 DC 日报标题、不传 range_text：不应串到 DC 范围，只显示日期
_seen.clear()
P.push_feishu(cfg, com_title, "**🔥 社区热点**\n- 测试")
card_text2 = _seen[-1][1]["card"]["elements"][0]["text"]["content"]
ck("10:44" not in card_text2, "不带range_text时社区日报不串DC范围")

_seen.clear()
P.push_telegram(cfg, "📋 Gamma 开通子网列表 · 2026-11-23 17:30", "- 测试")
ck("10:44" not in _seen[-1][1]["text"], "Gamma标题不串DC范围")

# DC 日报不传 range_text：仍走 _day_range_text（原有行为不变）
_seen.clear()
dc_title = "🤖 DTAO price-talk 日报 · 2026-11-22"
P.push_feishu(cfg, dc_title, "- 测试")
card_text3 = _seen[-1][1]["card"]["elements"][0]["text"]["content"]
ck("10:44" in card_text3, "DC日报默认仍读state范围（向后兼容）")

_seen.clear()
P.push_telegram(cfg, dc_title, "- 测试")
ck("10:44" in _seen[-1][1]["text"], "DC日报TG默认仍读state范围")

# 显式 range_text 对 DC 日报也优先
_seen.clear()
P.push_feishu(cfg, dc_title, "- 测试", range_text="11-22 00:00 → 11-22 23:59")
card_text4 = _seen[-1][1]["card"]["elements"][0]["text"]["content"]
ck("23:59" in card_text4 and "10:44" not in card_text4, "显式range_text覆盖DC默认范围")

P._day_range_text = orig_day_range
P._post_json = _orig_post

print("\n=== 结果：%d 通过 / %d 失败 ===" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：" + "、".join(FAIL))
    sys.exit(1)
