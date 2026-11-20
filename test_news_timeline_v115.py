# -*- coding: utf-8 -*-
"""v1.15.0 回归：最新资讯并入时间线 + 红色加粗 + 双平台降级。"""
import sys
import subnet_research as S
import pipeline as P

LOG = lambda m: None
FAIL = []

def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ((" | " + detail) if detail and not cond else ""))
    if not cond:
        FAIL.append(name)

def nred(s):
    return s.count('<font color="red">')

print("== 1) 残留独立资讯段 → 并入时间线并标红 ==")
d1 = """**🗓 重要事件时间线**
- 11-18 · SN51 一周回购 330 万美元 · ✅已验证（群聊多人）
- 11-17 · 群内讨论 null 共识机制 · ⚠️未验证（仅群内称）

**📊 当前数据**
当前价格：0.002504 τ [bittensor.ai]

**📰 最新资讯**
- 11-18 · @ms_computer · 官推发布推理网络新细节 [X]
- 11-16 · @Bittensor · 官方宣布新合作 [新闻]

**💰 收入与回购**
- 所有者收入：<font color="red">160.59 τ/日（+10.8%）</font>

**📌 预期跟踪**
- 盯 SN110 排放 → 恢复即兑现"""
r1, c1 = S.tidy_news_in_timeline(d1, log=LOG)
check("独立资讯段已删除", "**📰 最新资讯**" not in r1)
check("官推并入时间线且标红", "📰 @ms_computer · 官推发布推理网络新细节" in r1 and nred(r1) >= 3, "red=%d" % nred(r1))
check("新闻并入时间线且标红", "📰 @Bittensor · 官方宣布新合作" in r1)
check("图例注明存在且唯一", r1.count("为最新资讯") == 1, "count=%d" % r1.count("为最新资讯"))
check("收入段红字未被破坏", "160.59 τ/日（+10.8%）" in r1)
check("没有重复插入(官推只出现1次)", r1.count("官推发布推理网络新细节") == 1,
      "count=%d" % r1.count("官推发布推理网络新细节"))

print("== 2) 已写进时间线但带 [X] 漏标红 → 补标红 ==")
d2 = """**🗓 重要事件时间线**
- 11-18 · 官方发帖宣布子网上线新功能 [X] · ✅已验证（官方X+官网）
- 11-17 · 群内有人称某子网要换方向 · ⚠️未验证（仅群内称）

**📊 当前数据**
当前价格：0.002 τ [bittensor.ai]

**💰 收入与回购**
- 协议回购：10 τ

**📌 预期跟踪**
- 盯上线 → 兑现

注：<font color="red">**红色加粗**</font>为最新资讯"""
r2, c2 = S.tidy_news_in_timeline(d2, log=LOG)
check("漏标红的[X]条目已补红字", "<font color=\"red\">**📰 官方发帖宣布子网上线新功能 [X]**</font>" in r2, r2)
check("验证状态留在红字外", "</font> · ✅已验证（官方X+官网）" in r2)
check("群聊条目未被误标红", "群内有人称某子网要换方向" in r2 and "<font color=\"red\">**📰 群内" not in r2)
check("图例未重复", r2.count("为最新资讯") == 1)

print("== 3) 时间线内部同件事写两遍 → 去重 ==")
d3 = """**🗓 重要事件时间线**
- 11-18 · SN51 用营收回购销毁约 330 万美元的代币 · ✅已验证（群聊多人）
- 11-18 · SN51 一周花 330 万美元回购销毁 · ✅已验证（Fish）

**📊 当前数据**
当前价格：0.002 τ [bittensor.ai]

**💰 收入与回购**
- 协议回购：10 τ

**📌 预期跟踪**
- 盯回购 → 兑现"""
r3, c3 = S.tidy_news_in_timeline(d3, log=LOG)
ev3 = [l for l in r3.split("\n") if l.startswith("- 11-18")]
check("重复事件删掉一条", len(ev3) == 1, "剩 %d 条: %s" % (len(ev3), ev3))

print("== 4) 飞书通道：红字原样保留 ==")
fs = P.mark_changes_feishu('<font color=red>📰 官方发帖 [X]</font>')
check("飞书保留 red 标签", '<font color=\'red\'>' in fs or '<font color="red">' in fs, fs)

print("== 5) TG 降级：资讯不误判成跌，收入变化用彩色圆点 ==")
news_tg = P.mark_changes_tg('<font color="red">**📰 11-18 官方发帖宣布合作 [X]**</font>')
inc_tg = P.mark_changes_tg('<font color="red">160.59 τ/日（+10.8%）</font>')
check("资讯无 🟢 误判", "🟢" not in news_tg and "🔴" not in news_tg and "📰" in news_tg, news_tg)
check("资讯保留加粗", "**" in news_tg, news_tg)
check("上涨用 🔴 红点", "🔴" in inc_tg, inc_tg)
down_tg = P.mark_changes_tg('<font color="red">-3.2%（下跌）</font>')
check("下跌用 🟢 绿点", "🟢" in down_tg and "🔴" not in down_tg, down_tg)
flat_tg = P.mark_changes_tg('<font color="red">128.5 τ/日</font>')
check("无涨跌号用 ⚪ 中性点", "⚪" in flat_tg, flat_tg)
check("无 HTML 残渣", "<font" not in P.mark_changes_tg(news_tg) and "</font>" not in news_tg, news_tg)
legend_tg = P._tg_md(P.mark_changes_tg('<font color="red">**红色加粗**</font>为最新资讯'))
check("图例降级正常(无⚪/****)", "⚪" not in legend_tg and "****" not in legend_tg, legend_tg)
check("图例保留加粗说明", "红色加粗" in legend_tg and "*红色加粗*" in legend_tg, legend_tg)

print("== 6) 五段旧模板兼容：不会再出现独立资讯段标题 ==")
check("prompt 已改四段", "只有这四段" in S.RESEARCH_SYSTEM_PROMPT)
check("prompt 无独立资讯段定义", "**📰 最新资讯**（外部世界" not in S.RESEARCH_SYSTEM_PROMPT)

print("\n" + ("全部通过 ✅" if not FAIL else "失败项: %s" % FAIL))
sys.exit(1 if FAIL else 0)
