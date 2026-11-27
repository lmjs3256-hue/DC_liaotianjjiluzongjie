#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v1.30.10 回归：人工反馈重总结的保真兜底 + 改写不删除铁律。

事故：2026-11-27 用户 /retry 要求保留 5GuRL 内幕自述，模型两次整条删掉。
本测试覆盖：锚点抽取（去重/去子串/不含英文原文通用词）、保真判定，
以及 FEEDBACK_HINT / SYSTEM_PROMPT 的铁律文本确实在提示词里。
"""
import sys
sys.path.insert(0, '.')
import pipeline as P

PASS = 0
FAIL = 0


def check(name, got, expect):
    global PASS, FAIL
    if got == expect:
        PASS += 1
        print("  OK  %-38s -> %r" % (name, got))
    else:
        FAIL += 1
        print("  FAIL %-38s -> %r (expect %r)" % (name, got, expect))


print("\n[1] 提示词铁律文本存在")
check("FEEDBACK_HINT 优先改写保留", "优先改写保留" in P.FEEDBACK_HINT, True)
check("FEEDBACK_HINT 输出前自检", "输出前自检" in P.FEEDBACK_HINT, True)
check("SYSTEM_PROMPT 禁拆同一人", "禁止把同一个人拆成两个主体" in P.SYSTEM_PROMPT, True)

print("\n[2] 锚点抽取：只取专名+事件词，去重去子串，不含英文原文通用词")
FB = ('上一版日报漏掉了 5GuRL 关于内幕的这条信息，请把它**保留并改写正确**后写回必读重点。'
      '5GuRL 就是 iuac，SN50 相关，是他本人自述「orange 不再买入印证了传言」。'
      '原文 "Orange didn\'t buy SN50 anymore, so we can conclude the rumours confirm that 5GuRL had insider info"')
a = P._feedback_keep_anchors(FB)
print("  anchors:", a)
check("含 5GuRL", "5GuRL" in a, True)
check("含 iuac", "iuac" in a, True)
check("含 SN50", "SN50" in a, True)
check("含事件词内幕", "内幕" in a, True)
check("含事件词传言", "传言" in a, True)
check("含事件词自述", "自述" in a, True)
check("不含通用词 didn", "didn" in a, False)
check("不含通用词 conclude", "conclude" in a, False)
check("不含通用词 buy", "buy" in a, False)
check("无 GuRL 子串", "GuRL" in a, False)
check("5GuRL 只出现一次", a.count("5GuRL"), 1)
check("SN50 只出现一次", a.count("SN50"), 1)

print("\n[3] 无保留信号 → 不抽锚点")
check("普通反馈", P._feedback_keep_anchors("SN50 改名为 SynthX"), [])
check("日报反馈词", P._feedback_keep_anchors("今天日报 SN50 那句表述有问题"), [])
check("保留信号触发", len(P._feedback_keep_anchors("把 5GuRL 内幕那条保留并改写")) > 0, True)

print("\n结果：%d 通过, %d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
