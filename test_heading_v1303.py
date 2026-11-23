# -*- coding: utf-8 -*-
"""v1.30.3 自测：报告标题识别兼容纯文本 emoji 标题，杜绝两个「当日社区动态」板块。"""
import sys
import subnet_research as R

OK = 0
FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  OK  %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


print("== 1. _heading_like 标题识别 ==")
heads = ["📢 当日社区动态", "📌 预期跟踪", "🗓 重要事件时间线", "📊 当前数据",
         "**📢 当日社区动态（按人提炼）**", "**🔎 本子网频道讨论补充（DC 频道 · 2026-11-23）**"]
for h in heads:
    check("识别标题 %s" % h[:16], R._heading_like(h))
notheads = ["- [社区] 说英文原文", "⚠️未验证", "✅已验证", "❌推翻",
            "- 2026-11-23 · 事件", "> 引用行", "| a | b |",
            "当日抓到相关消息但提炼失败，暂不列出", ""]
for n in notheads:
    check("不误判 %s" % (n[:16] or "<空>"), not R._heading_like(n))


print("== 2. 纯文本标题原节能被删掉 ==")
rep = """🗓 重要事件时间线
- 2026-11-23 · 事件A

📢 当日社区动态
- [社区][Discord] <93> 称：「we have a new product out soon」
- [社区][X] <OB> 称：「SN93 has booked $493K in revenue」

频道讨论补充
- [DC频道] <93> 同上

📌 预期跟踪
- 盯什么 → 测试
"""
rest, taken = R._take_section(rep, "📢 当日社区动态")
check("抠出的节含英文原文", "we have a new product" in taken)
check("抠出的节含子标题", "频道讨论补充" in taken)
check("正文已无社区动态标题", "📢 当日社区动态" not in rest)
check("正文保留时间线", "🗓 重要事件时间线" in rest)
check("正文保留预期跟踪", "📌 预期跟踪" in rest)
check("正文已删英文原文", "we have a new product" not in rest)


print("== 3. 提炼节插回纯文本预期跟踪之前 ==")
cs = "**📢 当日社区动态（按人提炼）**\n- const(dc) 表示：想合作跑活动 ⚠️未验证"
new = R._insert_section(rest, cs, "📌 预期跟踪")
lines = new.splitlines()
i_cs = next(i for i, l in enumerate(lines) if "按人提炼" in l)
i_track = next(i for i, l in enumerate(lines) if l == "📌 预期跟踪")
check("提炼节在预期跟踪之前", i_cs < i_track)
check("全文只有一个社区动态板块",
      sum(1 for l in lines if "当日社区动态" in l) == 1)
check("提炼内容是中文短句", "想合作跑活动" in new)
check("英文原文不再出现", "we have a new product" not in new)


print("== 4. 多份同名节一起删（历史遗留） ==")
rep2 = """📢 当日社区动态
- 第一份英文原文

📊 当前数据
- 数据

**📢 当日社区动态（按人提炼）**
- 第二份旧提炼

📌 预期跟踪
- 跟踪
"""
rest2, taken2 = R._take_section(rep2, "📢 当日社区动态")
check("两份都被抠掉", taken2.count("当日社区动态") == 2, repr(taken2[:80]))
check("正文只留其它节", "当日社区动态" not in rest2)
check("正文保留当前数据", "📊 当前数据" in rest2)


print("== 5. 加粗标题仍兼容（旧报告） ==")
rep3 = """**📢 当日社区动态**
- 旧报告内容

**📌 预期跟踪**
- 跟踪
"""
rest3, taken3 = R._take_section(rep3, "📢 当日社区动态")
check("加粗原节能删", "旧报告内容" in taken3 and "当日社区动态" not in rest3)
new3 = R._insert_section(rest3, cs, "📌 预期跟踪")
check("加粗预期跟踪前能插", "按人提炼" in new3 and
      new3.index("按人提炼") < new3.index("**📌 预期跟踪**"))


print("== 6. 找不到 before 时兜底追加（不丢节） ==")
weird = "🗓 重要事件时间线\n- 事件\n"
new4 = R._insert_section(weird, cs, "📌 预期跟踪")
check("兜底保留提炼节", cs in new4)


print("\n结果：%d OK / %d FAIL" % (OK, FAIL))
sys.exit(0 if FAIL == 0 else 1)
