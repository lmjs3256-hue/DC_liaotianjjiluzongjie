#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.30.8 回归：流水线排队机制 + 日报标题粘连清洗。"""
import os
import sys
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

failed = []


def check(cond, name):
    print(("  OK  " if cond else " FAIL ") + name)
    if not cond:
        failed.append(name)


# ---------------------------------------------------------------- 标题粘连
print("== 标题粘连清洗 ==")
import pipeline as P

cases = [
    ("**📢 子网动态SN44 SN44 Score**（3 条）：**Max**宣布双轨战略",
     "**📢 子网动态**", "SN44 Score（3 条）"),
    ("**💡 一句话总结TAO**价格跌破**$200**引发不满",
     "**💡 一句话总结**", "TAO价格跌破"),
    ("**💡 一句话总结Const**一日两拳——扩容**160**名额",
     "**💡 一句话总结**", "Const一日两拳"),
]
for raw, want_title, want_body in cases:
    got = P.sanitize_digest(raw)
    check(want_title in got, "拆出标题：%s" % want_title)
    check(want_body in got, "正文回位：%s" % want_body)
    check("SN44 SN44" not in got, "SN 重复去重" if "SN44" in raw else "（无 SN 重复）")

normal = "**📢 子网动态**（3 条）：正常标题"
check(P.sanitize_digest(normal) == normal, "正常子块标题不被误拆")

headline = "**🔥 群内热点**：**TAO**价格暴跌"
check(P.sanitize_digest(headline) == headline, "正常群日报标题不被误拆")

# ---------------------------------------------------------------- 排队机制
print("== 流水线排队机制 ==")
import tg_listener as T

# 完全隔离：替换发送/落盘/流水线执行，避免真发消息或真起 pipeline 子进程
sent = []
executed = []
T.safe_send = lambda chat, text, **kw: sent.append((chat, text))
T.send_async = lambda chat, text, **kw: sent.append((chat, text))
gate = threading.Event()


def fake_run(args, chat_id):
    executed.append((list(args), chat_id))
    if len(executed) == 1:
        gate.wait(10)          # 第一个任务卡住，模拟 daily 仍在跑


T._run_pipeline_sync = fake_run
with T._BUSY_LOCK:
    T._BUSY = False
    T._PIPELINE_QUEUE.clear()

# 1) 第一个任务立即启动
st = T.run_pipeline(["daily"], -100, quiet=True)
check(st == "started", "空闲任务立即启动")
for _ in range(50):
    if executed:
        break
    time.sleep(0.02)
check(len(executed) == 1 and executed[0][0] == ["daily"], "首个任务进入执行")

# 2) busy 时入队，不再说「上一个任务仍在执行中」
sent.clear()
st = T.run_pipeline(["tg_group", "昨日", "1003968896337"], -100, quiet=True,
                    start_msg="💬 正在总结【Hanlin.AI】")
check(st == "queued", "忙时任务入队")
joined = "\n".join(t for _c, t in sent)
check("已排队" in joined, "回执明确告知已排队")
check("上一个任务仍在执行中" not in joined, "不再出现假 busy 提示")
check("正在总结" not in joined, "入队瞬间不发「正在总结」")
check(len(executed) == 1, "入队不会并发执行")

# 3) 重复提交被去重
sent.clear()
st = T.run_pipeline(["tg_group", "昨日", "1003968896337"], -100, quiet=True)
check(st == "duplicate", "同会话同任务去重")
check(len(T._PIPELINE_QUEUE) == 1, "队列长度未膨胀")

# 4) 前一个结束 → 自动拉起队列任务，并发出自定义回执
sent.clear()
gate.set()
for _ in range(100):
    if len(executed) >= 2:
        break
    time.sleep(0.05)
check(len(executed) == 2, "前序完成后自动拉起排队任务")
check(executed[1][0] == ["tg_group", "昨日", "1003968896337"], "拉起的是排队的那条")
joined = "\n".join(t for _c, t in sent)
check("正在总结【Hanlin.AI】" in joined, "拉起时才发自定义开始回执")
check("已排队" not in joined, "拉起回执不再重复排队提示")

# 等第二个任务线程收尾，避免污染后续测试
for _ in range(100):
    with T._BUSY_LOCK:
        if not T._BUSY:
            break
    time.sleep(0.05)
check(not T._BUSY, "队列清空后执行锁正确释放")
check(not T._PIPELINE_QUEUE, "队列已清空")

print("\n结果：%d 项失败" % len(failed))
if failed:
    for f in failed:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
