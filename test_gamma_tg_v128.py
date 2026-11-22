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
ck(T._ga_flow_active(CHAT) and T._ga_flow_active(CHAT)["step"] == "action",
   "列表跑完保留菜单(不超时)")

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

# --- v1.29.0 跨子网支付（机制2）命令覆盖 ---
# 14) 菜单含跨子网按钮
reset()
T.handle("/gamma", CHAT)
kb = str(_sent[-1][2])
ck("gama:cross" in kb and "gama:crossweek" in kb and "gama:crossmonth" in kb,
   "菜单含跨子网三项按钮")

# 15) 文本「跨网」→ 第2轮 scope（跨子网也走范围选择）
reset()
T.handle("/gamma", CHAT)
T.handle("跨网", CHAT)
ck(T._ga_flow_active(CHAT)["step"] == "scope" and T._ga_flow_active(CHAT)["act"] == "cross",
   "文本跨网→cross进scope")
ck(any("源子网编号" in s[1] for s in _sent), "跨子网第2轮提示源子网编号")

# 16) 跨网第2轮全网 → gamma cross
reset()
T.handle("/gamma", CHAT); T.handle("跨网", CHAT); T.handle("全网", CHAT)
ck(len(_runs) == 1 and _runs[0][0] == ["gamma", "cross"], "跨网全网→gamma cross")

# 17) 跨网第2轮指定源子网 → gamma cross + SN
reset()
T.handle("/gamma", CHAT); T.handle("跨网", CHAT); T.handle("120", CHAT)
ck(len(_runs) == 1 and _runs[0][0] == ["gamma", "cross", "120"], "跨网指定SN→cross+120")

# 18) 快捷 /gamma 跨网 直通
reset()
T.handle("/gamma 跨网", CHAT)
ck(len(_runs) == 1 and _runs[0][0] == ["gamma", "cross"], "快捷跨网直通")
ck(not T._ga_flow_active(CHAT), "跨网快捷不进分段")

# 19) 快捷 /gamma 跨网周 / 跨网月
reset(); T.handle("/gamma 跨网周", CHAT)
ck(len(_runs) == 1 and _runs[0][0][:2] == ["gamma", "crossweek"], "快捷跨网周直通")
reset(); T.handle("/gamma 跨网月", CHAT)
ck(len(_runs) == 1 and _runs[0][0][:2] == ["gamma", "crossmonth"], "快捷跨网月直通")

# 20) 快捷 /gamma 跨网 SN120 + nopush
reset(); T.handle("/gamma 跨网 SN120 nopush", CHAT)
ck(len(_runs) == 1 and _runs[0][0][:3] == ["gamma", "cross", "120"]
   and "nopush" in _runs[0][0], "跨网SN+nopush透传")

# 21) callback 选跨子网功能
reset()
T.handle("/gamma", CHAT)
T._ga_callback({"id": "x", "data": "gama:crossweek"}, CHAT, "u1", [])
ck(T._ga_flow_active(CHAT) and T._ga_flow_active(CHAT)["act"] == "crossweek",
   "callback选跨网周进scope")

# --- v1.29.1 修复：查完一次后菜单仍有效，5分钟内可连续点其他项 ---
# 22) 文本路径连续查询：列表 → 收支（第二次不再报超时）
reset()
T.handle("/gamma", CHAT)
T.handle("列表", CHAT)                       # 第一次查列表，跑完后状态回到菜单
ck(len(_runs) == 1, "首次列表查询成功")
T.handle("收支", CHAT)                       # 立刻再点收支
ck(T._ga_flow_active(CHAT) and T._ga_flow_active(CHAT)["step"] == "scope",
   "查完列表后菜单仍有效→收支进第2轮(未超时)")
T.handle("全网", CHAT)
ck(len(_runs) == 2 and _runs[1][0] == ["gamma", "snapshot"],
   "5分钟内连续查询：列表→收支全网都成功")

# 23) callback 路径连续查询：查完 week 后再点 cross
reset()
T.handle("/gamma", CHAT)
T._ga_callback({"id": "x", "data": "gama:week"}, CHAT, "u1", [])
T._ga_callback({"id": "x", "data": "gaday:all"}, CHAT, "u1", [])
ck(len(_runs) == 1 and _runs[0][0] == ["gamma", "week"], "首查 week 成功")
T._ga_callback({"id": "x", "data": "gama:cross"}, CHAT, "u1", [])   # 再点跨网
ck(T._ga_flow_active(CHAT) and T._ga_flow_active(CHAT)["step"] == "scope",
   "查完 week 后 callback 再点跨网仍有效(未超时)")
T._ga_callback({"id": "x", "data": "gaday:all"}, CHAT, "u1", [])
ck(len(_runs) == 2 and _runs[1][0] == ["gamma", "cross"], "callback 连续查询：week→cross 都成功")

# 24) 真超时仍生效：菜单放太久（ts 过期）再点应提示超时
reset()
T._GA_FLOW[CHAT] = {"step": "action", "act": None, "ts": time.time() - 400}
ck(T._ga_flow_active(CHAT) is None, "超过5分钟真超时仍清除(不被误保留)")

print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for n in FAIL:
    print("  FAIL:", n)
if FAIL:
    raise SystemExit(1)
