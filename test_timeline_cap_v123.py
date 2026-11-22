# -*- coding: utf-8 -*-
"""v1.23.1 回归：时间线倒序（最新在前）+ 最多10条 + 全部有进展事件进台账（时间/事情/现状）。"""
import sys
import subnet_research as S

LOG = lambda m: None
FAIL = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ((" | " + detail) if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def dates(digest):
    import re
    seg = digest.split("**🗓 重要事件时间线**")[1].split("**")[0]
    return re.findall(r"^[-*•]?\s*(?:(\d{4})-)?(\d{1,2})-(\d{2})\s*[·•]", seg, re.M)


print("== 1) 乱序 → 日期倒序，首条带完整年份 ==")
d1 = """**🗓 重要事件时间线**
- 2026-11-05 · 旧事件A · ✅已验证
- 11-18 · 新事件B · ✅已验证
- 11-11 · 中间事件C · ✅已验证
注：<font color="red">**红色加粗**</font>为最新资讯

**📊 当前数据**
当前价格：0.002 τ [bittensor.ai]

**📌 预期跟踪**
- 盯 X → Y 算兑现"""
r1, st1 = S.sort_cap_timeline(d1, log=LOG, max_events=10)
seg1 = [l for l in r1.split("\n") if l.strip().startswith("-") and "事件" in l]
check("日期倒序(18/11/05)", "2026-11-18" in seg1[0] and "11-11" in seg1[1] and "11-05" in seg1[2], str(seg1))
check("首条带完整年份", r1.count("2026-") == 1 and "2026-11-18" in seg1[0], str(seg1))
check("图例仍在段尾", r1.index("为最新资讯") > r1.rindex("旧事件A"))
check("统计 total=3", st1["total"] == 3 and st1["dropped"] == 0)

print("== 2) 超过10条 → 只留最新10条，最旧的被截 ==")
lines = ["**🗓 重要事件时间线**"]
for day in range(1, 14):  # 11-01 .. 11-13，共13条
    lines.append("- 2026-11-%02d · 事件%02d · ✅已验证" % (day, day))
lines += ["注：<font color=\"red\">**红色加粗**</font>为最新资讯", "", "**📊 当前数据**", "当前价格：0.002 τ", "", "**📌 预期跟踪**", "- 盯 X → Y"]
d2 = "\n".join(lines)
r2, st2 = S.sort_cap_timeline(d2, log=LOG, max_events=10)
seg2 = [l for l in r2.split("\n") if l.strip().startswith("-") and "事件" in l]
check("只保留10条", len(seg2) == 10, "剩%d条" % len(seg2))
check("最新11-13在最前", "11-13" in seg2[0], seg2[0])
check("最旧11-01~11-03被截", all(("11-%02d" % d) not in l for l in seg2 for d in (1, 2, 3)), str(seg2))
check("统计 dropped=3", st2["dropped"] == 3 and st2["kept"] == 10)
check("图例未被计数/保留", "为最新资讯" in r2)

print("== 3) 进展台账：全部有进展事件都体现，被截断的标注主线未显示 ==")
lines = ["**🗓 重要事件时间线**"]
# 11-01 最旧、会被截掉，是有进展的「owner 解仓」；11-02 是普通群聊（无线索）
lines.append("- 2026-11-01 · owner 首次解仓，unwind_pct 上升 · ✅已验证")
lines.append("- 2026-11-02 · 群内闲聊某话题 · ⚠️未验证")
# 11-13 是最新、时间线会保留，也是有进展事件（新合作发布）
lines.append("- 2026-11-13 · 官方宣布新合作上线 · ✅已验证")
for day in range(3, 13):
    lines.append("- 2026-11-%02d · 普通事件%02d · ✅已验证" % (day, day))
lines += ["注：<font color=\"red\">**红色加粗**</font>为最新资讯", "", "**📊 当前数据**", "当前价格：0.002 τ", "",
          "**📌 预期跟踪**", "- ⏳进行中：owner 解仓 → 解仓比例再升即兑现"]
d3 = "\n".join(lines)
r3, st3 = S.sort_cap_timeline(d3, log=LOG, max_events=10)
ledger3 = r3.split("**⏳ 进展台账**")[1].split("**📌 预期跟踪**")[0] if "**⏳ 进展台账**" in r3 else ""
check("进展台账节出现", "进展台账" in r3, r3[-700:])
check("台账紧挨预期跟踪前", r3.index("进展台账") < r3.index("📌 预期跟踪"))
check("被截断的解仓事件进台账", "owner 首次解仓" in ledger3)
check("被截断的标注主线未显示", "主线未显示" in ledger3 and "owner 首次解仓" in ledger3)
check("保留在时间线的进展事件也进台账", "新合作上线" in ledger3)
check("每条含'进展'字段", ledger3.count("进展：") >= 2, ledger3)
check("无进展的群聊不进台账", "群内闲聊" not in ledger3, ledger3)
check("台账无重复日期前缀", "· 2026-11-01 · owner" not in ledger3, ledger3)
check("台账无 markdown 加粗/📰标记", "**" not in ledger3 and "📰" not in ledger3, ledger3)
check("台账按日期倒序(11-13在11-01前)", ledger3.index("11-13") < ledger3.index("11-01"), ledger3)
check("进展状态是明确短语", "进展：" in ledger3 and "新进展：首次" not in ledger3, ledger3)
check("统计 backfilled>=1", st3["backfilled"] >= 1)
check("被截解仓事件不在时间线", "owner 首次解仓" not in r3.split("**🗓 重要事件时间线**")[1].split("**📊")[0])

print("== 4) 跨年：12月旧 / 1月新，倒序仍正确 ==")
d4 = """**🗓 重要事件时间线**
- 2026-01-02 · 新年新事件 · ✅已验证
- 2025-12-30 · 年末旧事件 · ✅已验证
注：<font color="red">**红色加粗**</font>为最新资讯

**📌 预期跟踪**
- 盯 X → Y"""
r4, st4 = S.sort_cap_timeline(d4, log=LOG, max_events=10)
seg4 = [l for l in r4.split("\n") if l.strip().startswith("-") and ("事件" in l)]
check("跨年倒序(01-02在12-30前)", "01-02" in seg4[0] and "12-30" in seg4[1], str(seg4))
check("跨年旧条目保留年份", "2025-12-30" in seg4[1], str(seg4))

print("== 5) 无日期条目兜底不报错、排末尾 ==")
d5 = """**🗓 重要事件时间线**
- 本期无外部资讯（X/新闻/官网 均未获取）· ⚠️未验证
- 11-15 · 有日期事件 · ✅已验证
注：<font color="red">**红色加粗**</font>为最新资讯

**📌 预期跟踪**
- 盯 X"""
r5, st5 = S.sort_cap_timeline(d5, log=LOG, max_events=10)
seg5 = [l for l in r5.split("\n") if l.strip().startswith("-") and ("事件" in l or "无外部资讯" in l)]
check("无日期条目排末尾", "无外部资讯" in seg5[-1], str(seg5))
check("有日期在前", "11-15" in seg5[0], str(seg5))

print("\n" + ("全部通过 ✅" if not FAIL else "失败项: %s" % FAIL))
sys.exit(1 if FAIL else 0)
