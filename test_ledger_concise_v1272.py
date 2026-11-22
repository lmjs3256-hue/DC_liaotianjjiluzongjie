# -*- coding: utf-8 -*-
"""v1.27.2 自测：进展台账「时间·人物·事件·进展」四要素 + 精炼不贴原文 + 人物点名。"""
import re
import subnet_research as SR

PASS = 0
FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s" % label)


OFFICIAL = (
    '- 2026-11-17 · <font color="red">**📰 官方 X 连发三条宣布进入 Phase 4：'
    'Trishool 定位「Bittensor\'s first cyber model lab」并开始创收；'
    '计划 2-3 周内上线消融版 GLM 5.3 付费推理，随后训练超越 GLM 5.3 的去中心化网络模型；'
    '明确「100% of profits of Trishool go towards buybacks」且'
    '「No company sits between the customer and the token」；'
    '同日发文致谢 Subnet Summer 团队 [X]**</font> · ✅已验证（X 官方+官网同步）'
)
RUMOR = (
    '- 11-07 · 群内有人称（11-07，Nav）：Trishool 大部分收入（含 Astroware 支付的许可费）'
    '将用于回购并销毁 SN23 alpha，其余再投资子网；Astroware 与 Trishool 为独立实体，'
    '前者负责企业侧业务与股权融资 [群聊] · ⚠️未验证（仅群内口头承诺，无链上回购记录）'
)
SHORT = '- 11-11 · <font color="red">**📰 官方 X 发布 HaloGuard EU 演示视频 [X]**</font> · ✅已验证（X 官方）'
GITHUB = ('- 10-27 · 代码仓库最后一次更新在 10-27，改的是优化类改动；'
          '近 30 天仅 1 次提交，以优化、依赖与配置为主；本期无新版本 · ✅已验证（GitHub）')

print("\n[1] 官方 X 长原文：不贴原文 + 主体点名 + 计划不误判为已上线")
line = SR._tl_ledger_line(OFFICIAL, "已官宣计划，待落地", 2026, False)
print("   ->", line)
check("官方 X ·" in line, "主体列为官方 X")
check(line.count("官方 X") == 1, "官方 X 不重复（事件开头已去主语）")
check("No company sits between" not in line and "go towards buybacks" not in line,
      "整段英文原文未贴入台账")
check(len(line) < 110, "单条台账显著缩短（%d 字）" % len(line))
check("已官宣计划" in line, "「计划上线」未被误判成「已上线」")
check("进展：" in line, "进展列存在")

print("\n[2] 群聊传闻：人物点名 + 去泛化壳 + 去来源/验证噪音")
line2 = SR._tl_ledger_line(RUMOR, "已发生（链上/资金动作）", 2026, False)
print("   ->", line2)
check("Nav（群内）" in line2, "发言点名 Nav（群内）")
check("群内有人称" not in line2, "去掉「群内有人称」泛化壳")
check("称：" in line2, "事件以「称：」标明是观点而非事实")
check("[群聊]" not in line2 and "⚠️未验证（仅群内口头" not in line2,
      "来源标记与冗长验证理由未贴入")
check("待核实" in line2, "群内说法标为待核实")
check("Astroware 与 Trishool 为独立实体" not in line2, "次要分句被截断，不整段搬")

print("\n[3] 短句不硬截断 + 官方源进展带 ✅")
line3 = SR._tl_ledger_line(SHORT, "新内容发布", 2026, False)
print("   ->", line3)
check("HaloGuard EU 演示视频" in line3, "短句核心完整保留")
check("…" not in line3, "短句未被硬加省略号")
check("✅新内容发布" in line3, "已验证来源进展带 ✅")

print("\n[4] 无人物名时按来源点名主体（GitHub）")
line4 = SR._tl_ledger_line(GITHUB, "数据更新", 2026, False)
print("   ->", line4)
check("GitHub" in line4, "按来源点名主体 GitHub")
check("优化类改动" in line4, "保留「最后改了什么」核心信息")

print("\n[5] 截断标注 + 无日期兜底")
line5 = SR._tl_ledger_line(SHORT, "新内容发布", 2026, True)
check("主线未显示" in line5, "被截断条目标注主线未显示")
line6 = SR._tl_ledger_line("- 某事件发生了 [X] · ✅已验证", "已发生", 2026, False)
check("日期未标" in line6, "无日期条目有兜底")

print("\n[6] 集成：sort_cap_timeline 输出的台账段不再贴原文")
DIGEST = """**🗓 重要事件时间线**
- 2026-11-17 · <font color="red">**📰 官方 X 连发三条宣布进入 Phase 4：Trishool 定位 lab 并开始创收；计划 2-3 周内上线消融版 GLM 5.3 付费推理 [X]**</font> · ✅已验证（X 官方）
- 11-17 · 群内有人称（11-17，Nav）：Trishool 将成首个网络模型实验室，计划 2-3 周内上线付费推理 [群聊] · ⚠️未验证
注：<font color="red">**红色加粗**</font>为最新资讯

**📌 预期跟踪**
- ⏳进行中：盯 Phase 4 创收 → 尚无收入数据
"""
out, stats = SR.sort_cap_timeline(DIGEST, log=lambda *_: None)
i = out.find("进展台账")
seg = out[i:i+500] if i >= 0 else ""
print("   ->", seg[:300].replace("\n", " | "))
check(i >= 0, "台账段生成")
check("官方 X ·" in seg and "Nav（群内）" in seg, "集成后端到端点名主体")
check("群内有人称" not in seg, "集成后无泛化壳")
check(stats.get("total") == 2, "时间线条数统计未变")

print("\n==== 结果：%d PASS / %d FAIL ====" % (PASS, FAIL))
if FAIL:
    raise SystemExit(1)
