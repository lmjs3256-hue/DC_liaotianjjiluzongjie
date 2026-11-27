#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v1.30.9 回归：/retry 路由判别 + 日报标题/表述反馈不得误判为投研重做。

事故背景：2026-11-27 用户 /retry 引用日报原句「iuac 因 orange 未买入 SN50，
推断 5GuRL 提前获知...」并说「这句话表述有问题，5GURL就是iuac」，旧逻辑
只看到「SN50 + 有新投研报告」就误判成投研重做，白跑一次投研并推送。
"""
import sys
sys.path.insert(0, '.')
import tg_listener as T

PASS = 0
FAIL = 0


def check(name, got, expect):
    global PASS, FAIL
    if got == expect:
        PASS += 1
        print("  OK  %-34s -> %r" % (name, got))
    else:
        FAIL += 1
        print("  FAIL %-34s -> %r (expect %r)" % (name, got, expect))


# 让日期判定通过：把时间窗放到极大，确保 SN 编号本身能被识别；
# 我们要测的是「是否引用日报原句」这一层，而不是报告新旧。
KW = dict(within_hours=10 ** 8)

FB_DIGEST_WORDING = (
    '" 5GuRL 是否有内幕信息？：iuac 因 orange 未买入 SN50，'
    '推断 5GuRL 提前获知 Const 接管消息（推断）。"这句话表述有问题，'
    '5GURL就是iuac，他怎么会推断自己提前获知const接管50'
)

print("\n[1] 引用日报原句 + 措辞批评 -> 必须走日报重总结（None）")
check("本次事故原话", T._research_sn_of_feedback(FB_DIGEST_WORDING, **KW), None)
check("英文引号引用", T._research_sn_of_feedback(
    '"SN50 那段说 iuac 推断 5GuRL 不合逻辑"这句话表述有问题', **KW), None)
check("中文引号引用+怎么会", T._research_sn_of_feedback(
    '“orange 未买入 SN50 推断 5GuRL 有内幕”这句怎么会有这种逻辑错误', **KW), None)
check("日报+SN 表述", T._research_sn_of_feedback(
    '日报里 SN50 那句表述有问题', **KW), None)
check("明确日报关键词", T._research_sn_of_feedback(
    '今天日报漏了 SN23 的进展', **KW), None)

print("\n[2] 纯事实纠正 / 明确投研 -> 仍走投研（返回 SN）")
check("SN50 改名纠正", T._research_sn_of_feedback('SN50 已改名为 SynthX', **KW), 50)
check("裸编号改名", T._research_sn_of_feedback('127 已改名为 Zils', **KW), 127)
check("明确投研报告+引用", T._research_sn_of_feedback(
    'SN50 的投研报告里这句话说错了，orange 为什么不买要写清楚', **KW), 50)
check("明确事件跟踪", T._research_sn_of_feedback(
    'SN50 事件跟踪报告里那段表述有歧义', **KW), 50)

print("\n[3] 无 SN / 无档案 -> 走日报")
check("无任何编号", T._research_sn_of_feedback('这段总结不清晰，改一下', **KW), None)
check("括号数字但非SN", T._research_sn_of_feedback(
    '"价格跌破（推断）"这句话表述有问题', **KW), None)

print("\n结果：%d 通过, %d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
