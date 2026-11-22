# -*- coding: utf-8 -*-
"""Gamma TG 分段命令状态机单测（v1.28.0）。mock 掉发送/流水线，只验证分发逻辑。"""
import sys
import time

import tg_listener as T

PASS, FAIL = [], []


def ck(cond, name):
    (PASS if cond else FAIL).append(name)


# mock 发送与流水线，避免真实发消息/触发 pipeline
_sent = []
_runs = []
T.safe_send = lambda chat, text, markup=None: _sent.append((str(chat), text, markup))
T.send_async = lambda chat, text, markup=None: _sent.append((str(chat), text, markup))
T.run_pipeline = lambda args, chat, quiet=False: _runs.append((list(args), str(chat)))
T.allowed_users_dummy = None


def reset():
    _sent.clear(); _runs.clear()
    T._GA_FLOW.clear()
    T._CONFIRM_FLOW.clear()
    T._TGG_FLOW.clear()


CHAT = "99999"

# 1) 裸命令 → 第1轮菜单
reset()
T.handle("/gamma", CHAT)
ck(T._ga_flow_active(CHAT) and T._ga_flow_active(CHAT)["step"] == "action", "裸命令进第1轮")
ck(_sent and "第 1 步" in _sent[-1][1], "第1轮抛功能菜单")
ck(_sent[-1][2] and "gama:list" in str(_sent[-1][2]), "菜单含开通列表按钮")

# 2) 文本回复「列表」→ 直接出 list（列表类不进第2轮）
reset()
T.handle("/gamma", CHAT)
T.handle("列表", CHAT)
ck(len(_runs) == 1 and _runs[0][0][:2] == ["gamma", "list"], "文本选列表→直接跑gamma list")
ck(not T._ga_flow_active(CHAT), "列表跑完清空状态")

# 3) 文本回复「周」→ 第2轮 scope
reset()
T.handle("/gamma", CHAT)
T.handle("周", CHAT)
ck(T._ga_flow_active(CHAT)["step"] == "scope", "选周统计进第2轮")
ck(T._ga_flow_active(CHAT)["act"] == "week", "第2轮记住act=week")

# 4) 第2轮回复「全网」→ 跑 week 全网
reset()
T.handle("/gamma", CHAT)
T.handle("周", CHAT)
T.handle("全网", CHAT)
ck(len(_runs) == 1 and _runs[0][0] == ["gamma", "week"], "第2轮全网→gamma week")

# 5) 第2轮回复 SN 编号 → 带子网参数
reset()
T.handle("/gamma", CHAT)
T.handle("月", CHAT)
T.handle("51", CHAT)
ck(len(_runs) == 1 and _runs[0][0] == ["gamma", "month", "51"], "第2轮SN→month带子网")

# 6) 快捷命令 /gamma 列表 直通（不进分段）
reset()
T.handle("/gamma 列表", CHAT)
ck(len(_runs) == 1 and _runs[0][0][:2] == ["gamma", "list"], "快捷/gamma列表直通")
ck(not T._ga_flow_active(CHAT), "快捷命令不进分段状态")

# 7) 快捷命令 /gamma SN51 → detail
reset()
T.handle("/gamma SN51", CHAT)
ck(len(_runs) == 1 and _runs[0][0][:2] == ["gamma", "detail"] and "51" in _runs[0][0], "快捷SN→detail")

# 8) nopush 透传
reset()
T.handle("/gamma 周 nopush", CHAT)
ck(len(_runs) == 1 and "nopush" in _runs[0][0], "nopush透传pipeline")

# 9) 取消
reset()
T.handle("/gamma", CHAT)
T.handle("取消", CHAT)
ck(not T._ga_flow_active(CHAT) and not _runs, "取消清空状态不触发")

# 10) 超时清除
reset()
T._GA_FLOW[CHAT] = {"step": "action", "act": None, "ts": time.time() - 400}
ck(T._ga_flow_active(CHAT) is None, "5分钟超时自动清除")

# 11) callback 分发：先发裸命令建状态，再 gama:week 进 scope
reset()
T.handle("/gamma", CHAT)
T._ga_callback({"id": "x", "data": "gama:week"}, CHAT, "u1", [])
ck(T._ga_flow_active(CHAT) and T._ga_flow_active(CHAT)["act"] == "week", "callback选周进scope")

# 12) callback gaday:all → 跑查询
reset()
T._GA_FLOW[CHAT] = {"step": "scope", "act": "month", "ts": time.time()}
T._ga_callback({"id": "x", "data": "gaday:all"}, CHAT, "u1", [])
ck(len(_runs) == 1 and _runs[0][0] == ["gamma", "month"], "callback全网→跑month")

# 13) 无权限用户被拒
reset()
T.handle("/gamma", CHAT)
T._ga_callback({"id": "x", "data": "gama:list"}, CHAT, "bad", ["good"])
ck(T._ga_flow_active(CHAT)["step"] == "action", "无权限callback不改状态")

print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for n in FAIL:
    print("  FAIL:", n)
if FAIL:
    raise SystemExit(1)
