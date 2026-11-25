#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.30.6 自测：dtt CDP 真人 Edge 取数后端（Cloudflare Turnstile 后）。

不依赖真人 Edge：mock subprocess/CDP 端点，覆盖取数优先级、结果解析、
失败回退、子网频道、会话注入路径。
"""
import os, sys, json, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import community_fetch as CF

PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print("  PASS %s" % name)
    else: FAIL += 1; print("  FAIL %s %s" % (name, extra))

print("=== v1.30.6 dtt CDP 取数测试 ===")

# ---- 1. CDP 配置与可用性检测 ----
print("-- 1. CDP 配置 --")
check("默认端口 9444", CF._cdp_port({}) == 9444, CF._cdp_port({}))
check("config 可覆盖端口", CF._cdp_port({"dtt_cdp_port": 9555}) == 9555)
check("CDP 脚本存在", os.path.exists(CF._CDP_SCRIPT), CF._CDP_SCRIPT)

# mock urllib 探测
import urllib.request
_orig_urlopen = urllib.request.urlopen
class _FakeResp:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False
def fake_urlopen_ok(url, timeout=None):
    return _FakeResp()
urllib.request.urlopen = fake_urlopen_ok
check("cdp_available 在线=True", CF.cdp_available({}) is True)
def fake_urlopen_fail(url, timeout=None):
    raise OSError("refused")
urllib.request.urlopen = fake_urlopen_fail
check("cdp_available 离线=False", CF.cdp_available({}) is False)
urllib.request.urlopen = _orig_urlopen

# ---- 2. _run_cdp 输出解析（mock subprocess.Popen，写临时输出文件） ----
print("-- 2. _run_cdp 结果解析 --")
_orig_popen = CF.subprocess.Popen
class FakeProc:
    def __init__(self, result_obj):
        self.result_obj = result_obj
    def __call__(self, cmd_list, stdout, stderr, env, creationflags):
        self._fo = stdout
        return self
    def wait(self, timeout=None):
        self._fo.write(json.dumps(self.result_obj).encode("utf-8"))
        self._fo.flush()
        return 0
# 需让 Popen 返回可调用且带 wait 的对象
def make_popen(result_obj):
    class P:
        def __init__(self, cmd_list, stdout, stderr, env, creationflags):
            self._fo = stdout
        def wait(self, timeout=None):
            self._fo.write(json.dumps(result_obj).encode("utf-8")); self._fo.flush(); return 0
        def kill(self): pass
    return P
CF.subprocess.Popen = make_popen({"ok": True, "events": [{"id": "x"}], "pages": 1, "keywordSN": {}, "platforms": []})
r = CF._run_cdp("fetch", 1, 2, cfg={})
check("_run_cdp 解析 JSON", r.get("ok") is True and r.get("pages") == 1, str(r)[:120])

# 输出带前缀日志行也能解析
def make_popen2(result_obj):
    class P:
        def __init__(self, cmd_list, stdout, stderr, env, creationflags): self._fo = stdout
        def wait(self, timeout=None):
            self._fo.write(("some log\n" + json.dumps(result_obj)).encode("utf-8")); self._fo.flush(); return 0
        def kill(self): pass
    return P
CF.subprocess.Popen = make_popen2({"ok": True, "events": []})
r = CF._run_cdp("fetch", 1, 2, cfg={})
check("_run_cdp 跳过前缀日志", r.get("ok") is True, str(r)[:120])

# authExpired 直接抛
def make_popen3(result_obj):
    class P:
        def __init__(self, cmd_list, stdout, stderr, env, creationflags): self._fo = stdout
        def wait(self, timeout=None):
            self._fo.write(json.dumps(result_obj).encode("utf-8")); self._fo.flush(); return 0
        def kill(self): pass
    return P
CF.subprocess.Popen = make_popen3({"authExpired": True})
try:
    CF.cdp_fetch_events(1, 2, cfg={})
    check("authExpired 抛错", False)
except RuntimeError as e:
    check("authExpired 抛错", "登录已失效" in str(e), str(e)[:100])
CF.subprocess.Popen = _orig_popen

# ---- 3. fetch_day 优先走 CDP ----
print("-- 3. fetch_day 取数优先级 --")
_orig_avail = CF.cdp_available
_orig_cfetch = CF.cdp_fetch_events
_orig_renew = CF._renew_session
_orig_runcli = CF._run_cli
CF.cdp_available = lambda cfg=None: True
CF.cdp_fetch_events = lambda start, end, cfg=None, log=print: {
    "ok": True, "events": [{"id": "e1", "keywordId": "k1", "platformId": "plt_discord",
                            "occurredAt": 1795449600000, "content": "hi"}],
    "keywordSN": {"k1": [51]}, "platforms": [{"id": "plt_discord", "code": "discord"}],
    "pages": 1}
def should_not_call(*a, **k):
    raise AssertionError("走 CDP 时不应调 pw-browser/_renew_session")
CF._run_cli = should_not_call
CF._renew_session = should_not_call
payload = CF.fetch_day("2026-11-25", cfg={"community_output_dir": os.path.join(HERE, "_test_out_cdp")},
                       log=lambda *a: None, until_now=False)
check("fetch_day 走 CDP 取到事件", payload["stats"]["total"] == 1, str(payload["stats"]))
check("fetch_day SN 归属正确", payload["events"][0].get("sn") == [51], str(payload["events"][0])[:150])
check("fetch_day 平台映射正确", payload["events"][0].get("platform") == "discord")
CF._run_cli = _orig_runcli
CF._renew_session = _orig_renew

# ---- 4. CDP 失败回退 pw-browser ----
print("-- 4. CDP 失败回退 --")
CF.cdp_available = lambda cfg=None: True
CF.cdp_fetch_events = lambda start, end, cfg=None, log=print: (_ for _ in ()).throw(RuntimeError("CDP fail"))
CF.ensure_ready = lambda cfg=None, log=print: None
CF._run_cli = lambda cli, code, timeout=240: {
    "ok": True, "events": [{"id": "e2", "occurredAt": 1, "keywordId": "k2"}],
    "keywordSN": {"k2": [7]}, "platforms": [], "pages": 1}
payload2 = CF.fetch_day("2026-11-25", cfg={"community_output_dir": os.path.join(HERE, "_test_out_cdp")},
                        log=lambda *a: None, until_now=False)
check("CDP 失败回退到 pw-browser", payload2["stats"]["total"] == 1, str(payload2["stats"]))
CF._run_cli = _orig_runcli

# ---- 5. sn_channel 走 CDP ----
print("-- 5. 子网频道 CDP --")
CF.cdp_available = lambda cfg=None: True
CF.cdp_sn_channel = lambda args, cfg=None, log=print: {
    "ok": True, "events": [{"id": "s1", "author": "a", "content": "c", "occurredAt": 1795449600000,
                            "platformId": "plt_discord", "platform": "discord", "_kw": "k", "_win": "2026-11-25"}],
    "keywordIds": ["k"]}
CF.ensure_ready = lambda cfg=None, log=print: None
d = CF.fetch_sn_channel(51, "2026-11-25", cfg={"community_output_dir": os.path.join(HERE, "_test_out_cdp")},
                        log=lambda *a: None, platform="discord")
check("sn_channel 走 CDP", len(d.get("events", [])) == 1, str(d)[:150])
CF.cdp_available = _orig_avail

# 清理测试输出
import shutil
try: shutil.rmtree(os.path.join(HERE, "_test_out_cdp"))
except Exception: pass

print("\n=== 结果: PASS=%d FAIL=%d ===" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
