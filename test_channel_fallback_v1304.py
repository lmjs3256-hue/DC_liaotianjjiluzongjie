#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.30.4 自测：频道节上游400重试 + 提炼失败中文兜底（禁止英文伪装提炼）。"""
import os, sys, json, re, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import community_fetch as CF
import subnet_research as SR
import pipeline as P

PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print("  PASS %s" % name)
    else: FAIL += 1; print("  FAIL %s %s" % (name, extra))

print("=== v1.30.4 频道节提炼失败兜底测试 ===")

# ---- 1. call_llm 上游瞬时错误识别（用 HTTPError 模拟，验证不抛出而是重试） ----
print("-- 1. 上游400重试判定 --")
import urllib.error, io

def _http_err(code, body):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body.encode()))

# 通过临时 monkeypatch opener 验证：上游400 会重试，最终用第二次成功返回
_orig_opener = P._opener_for
_state = {"n": 0}
class _Resp:
    def __init__(self, data): self._d = data
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._d
class _Opener:
    def open(self, req, timeout=None):
        _state["n"] += 1
        if _state["n"] == 1:
            raise _http_err(400, '{"error":{"message":"invalid request","type":"server_error","code":"upstream_400"}}')
        ok = {"choices":[{"message":{"content":"OK"}}], "usage":{}}
        return _Resp(json.dumps(ok).encode())
P._opener_for = lambda url: _Opener()
try:
    cfg = {"deepseek_api_key": "x", "deepseek_base_url": "http://x/v1", "deepseek_model": "m",
           "deepseek_timeout": 2, "deepseek_total_budget": 60}
    t0 = __import__("time").time()
    res = P.call_llm(cfg, "hi", system_prompt="sp", tries=3)
    check("上游400后重试成功", res == "OK" and _state["n"] == 2, "n=%s res=%s" % (_state["n"], res))
finally:
    P._opener_for = _orig_opener

# 真正 key 错误 4xx 仍直接抛（不重试）
_state2 = {"n": 0}
class _OpenerKey:
    def open(self, req, timeout=None):
        _state2["n"] += 1
        raise _http_err(401, '{"error":{"message":"invalid api key","type":"authentication_error"}}')
P._opener_for = lambda url: _OpenerKey()
try:
    cfg2 = dict(cfg)
    try:
        P.call_llm(cfg2, "hi", system_prompt="sp", tries=3)
        check("key错误直接抛", False, "未抛异常")
    except RuntimeError:
        check("key错误直接抛", _state2["n"] == 1, "n=%s" % _state2["n"])
finally:
    P._opener_for = _orig_opener

# ---- 2. 频道节：提炼彻底失败 → 中文兜底 ----
print("-- 2. 回退中文兜底 --")
payload = {
    "sn": 53, "day": "2026-11-24", "platform": "discord", "platform_id": "p",
    "windows": [{"label": "2026-11-24"}], "keyword_ids": [], "stats": {},
    "events": [
        {"author": "crabby_tt,备注:11", "content": "How Engy emission is split 80% baseline pool B300 70% B200 30%", "occurredAt": 2},
        {"author": "tao_kodiak,备注:dtao", "content": "Engy to flip chutes? You guys convinced?", "occurredAt": 1},
    ],
}
tmp = tempfile.mkdtemp(prefix="chfb_")
cfg_t = {"community_output_dir": tmp}
p = CF.sn_channel_path(53, "2026-11-24", cfg_t, platform="discord")
os.makedirs(os.path.dirname(p), exist_ok=True)
json.dump(payload, open(p, "w", encoding="utf-8"), ensure_ascii=False)
_orig_read = CF.read_sn_channel
CF.read_sn_channel = lambda sn, day, cfg2=None, platform="discord": _orig_read(sn, day, cfg_t, platform=platform)

def _boom(body, sp=None): raise RuntimeError("kimi-k3 HTTP 400 upstream_400")

sec = SR._sn_channel_report_section(cfg_t, 53, "2026-11-24", {"sn": 53, "name": "Engy"},
                                    log=lambda *a: None, llm=_boom)
check("回退节生成", bool(sec) and "🔎 本子网频道讨论补充" in sec)
check("有小结", "小结：" in sec)
check("不伪装成提炼(无'表示：')", "表示：" not in sec)
check("标注未翻译", "未翻译原文" in sec and "临时故障" in sec)
check("中文概括存在", "自动中文提炼暂未成功" in sec)
check("原文保留但未冒充译文", "How Engy emission is split" in sec and "未翻译原文：" in sec)
check("小结无英文疑问词", "How、" not in sec)
check("保留未验证标记", "⚠️未验证" in sec)

# ---- 3. 小模型返回英文原文 → 视为失败走兜底 ----
print("-- 3. 英文照抄兜底 --")
def _english(body, sp=None):
    return ("<11> 表示：How Engy emission is split 80 percent baseline pool for stable GPU supply B300 B200\n"
            "<tao_kodiak> 表示：Engy to flip chutes you guys convinced\n"
            "Summary: people discuss engy emission")
sec2 = SR._sn_channel_report_section(cfg_t, 53, "2026-11-24", {"sn": 53, "name": "Engy"},
                                     log=lambda *a: None, llm=_english)
check("英文输出被拦截走兜底", "自动中文提炼暂未成功" in sec2)
check("英文输出无伪装表示", "表示：" not in sec2)

# ---- 4. 正常中文提炼不受影响（回归 v1.30.0/v1.27） ----
print("-- 4. 正常提炼回归 --")
def _good(body, sp=None):
    return ("- 11(dc) 表示：Engy 自 epoch 23 起排放 80% 进基础池，B300 占 70%、B200 占 30%\n"
            "- tao_kodiak(dc) 表示：质疑 Engy 能否反超 chutes\n"
            "小结：讨论聚焦 Engy 排放分配与竞争格局。")
sec3 = SR._sn_channel_report_section(cfg_t, 53, "2026-11-24", {"sn": 53, "name": "Engy"},
                                     log=lambda *a: None, llm=_good)
check("正常提炼含中文要点", "排放" in sec3 and "基础池" in sec3)
check("正常提炼有小结", "小结：" in sec3 and "竞争格局" in sec3)
check("正常提炼不误入兜底", "自动中文提炼暂未成功" not in sec3)
check("正常提炼无'未翻译原文'", "未翻译原文" not in sec3)

CF.read_sn_channel = _orig_read

print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
