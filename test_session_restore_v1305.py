#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.30.5 自测：dtt 会话凭证导出/导入/恢复优先级（Cloudflare Turnstile 后）。"""
import os, sys, json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import community_fetch as CF

PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print("  PASS %s" % name)
    else: FAIL += 1; print("  FAIL %s %s" % (name, extra))

print("=== v1.30.5 dtt 会话持久化测试 ===")

# ---- 1. 凭证路径 confined 在 ~/.pw-browser ----
print("-- 1. 凭证路径 --")
check("cookies 在 .pw-browser 内", CF._COOKIES_FILE.endswith(".pw-browser/dtt_session_cookies.json")
      or CF._COOKIES_FILE.endswith(".pw-browser\\dtt_session_cookies.json"), CF._COOKIES_FILE)
check("storage 在 .pw-browser 内", CF._STORAGE_FILE.endswith(".pw-browser/dtt_session_localStorage.json")
      or CF._STORAGE_FILE.endswith(".pw-browser\\dtt_session_localStorage.json"), CF._STORAGE_FILE)

# ---- 2. save_session 导出 cookies + storage ----
print("-- 2. save_session --")
_orig_cmd = CF._run_cli_cmd
calls = []
def mock_cmd(cli, args, timeout=60):
    calls.append(args)
    if args[0] == "cookies": return {"ok": True, "count": 3}
    if args[0] == "storage": return {"ok": True, "count": 2}
    return {"ok": True}
CF._run_cli_cmd = mock_cmd
r = CF.save_session({}, log=lambda *a: None)
check("save_session 成功", r.get("ok") and r.get("cookies") == 3 and r.get("keys") == 2, str(r))
check("导出两条命令", len([c for c in calls if c[0] in ("cookies", "storage")]) == 2, str(calls))
check("cookies export 带正确路径", any(c[0]=="cookies" and c[1]=="export" and CF._COOKIES_FILE in c for c in calls))
check("storage export 带正确路径", any(c[0]=="storage" and c[1]=="export" and CF._STORAGE_FILE in c for c in calls))

# ---- 3. save_session 部分失败 ----
def mock_cmd_fail(cli, args, timeout=60):
    if args[0] == "cookies": return {"ok": False, "error": "denied"}
    return {"ok": True}
CF._run_cli_cmd = mock_cmd_fail
r2 = CF.save_session({}, log=lambda *a: None)
check("导出失败被报告", not r2.get("ok") and "denied" in r2.get("reason", ""), str(r2))

# ---- 4. restore_session 先 open 站点再 import 再 reload，并校验 token ----
print("-- 3. restore_session --")
CF._run_cli_cmd = mock_cmd  # import 成功
# 伪造凭证文件存在
_orig_exists = CF._session_files_exist
CF._session_files_exist = lambda: True
_rcalls = []
_orig_runcli = CF._run_cli
def mock_runcli(cli, code, timeout=180):
    _rcalls.append(code)
    if "authExpired" in code or "localStorage.getItem('session')" in code:
        # auth_status_raw 的探测码：返回有效 session
        return {"ok": True, "host": "dtt.bigcatking.cc", "email": "a@b.com"}
    return {"ok": True}
CF._run_cli = mock_runcli
r3 = CF.restore_session({}, log=lambda *a: None)
check("restore_session 成功", r3.get("ok"), str(r3))
check("有 cookies import", any(c[0]=="cookies" and c[1]=="import" for c in calls[-2:]), str(calls[-4:]))
check("有 storage import", any(c[0]=="storage" and c[1]=="import" for c in calls[-2:]), str(calls[-4:]))
check("import 前有 goto/open", any("goto" in c for c in _rcalls))
check("import 后有 reload", any("reload" in c for c in _rcalls))

# ---- 5. token 已过期 → restore 报失败 ----
def mock_runcli_expired(cli, code, timeout=180):
    if "localStorage.getItem('session')" in code:
        return {"ok": False, "host": "dtt.bigcatking.cc"}
    return {"ok": True}
CF._run_cli = mock_runcli_expired
r4 = CF.restore_session({}, log=lambda *a: None)
check("token过期判失败", not r4.get("ok") and "失效" in r4.get("reason", ""), str(r4))

# ---- 6. _renew_session 优先 restore，成功时不调 auto_login ----
print("-- 4. 恢复优先级 --")
CF._run_cli = mock_runcli
CF._session_files_exist = lambda: True
_orig_restore = CF.restore_session
_orig_auto = CF.auto_login
auto_called = {"v": False}
CF.restore_session = lambda cfg=None, log=print: {"ok": True, "email": "ok@x.com"}
CF.auto_login = lambda cfg=None, log=print: auto_called.update(v=True) or {"ok": False}
r5 = CF._renew_session({}, log=lambda *a: None)
check("恢复成功即返回", r5.get("ok") and not auto_called["v"], str(r5))

# 恢复失败才调 auto_login
CF.restore_session = lambda cfg=None, log=print: {"ok": False, "reason": "no file"}
CF.auto_login = lambda cfg=None, log=print: {"ok": True, "email": "auto@x.com"}
r6 = CF._renew_session({}, log=lambda *a: None)
check("恢复失败才自动登录", r6.get("ok") and r6.get("email") == "auto@x.com", str(r6))

# ---- 7. auth_status 失效优先走 restore（不直接 auto_login） ----
print("-- 5. auth_status 失效顺序 --")
CF._run_cli = mock_runcli_expired  # session 失效
CF.restore_session = lambda cfg=None, log=print: {"ok": True, "email": "restored@x.com"}
CF.auto_login = lambda cfg=None, log=print: auto_called.update(v=True) or {"ok": False}
_orig_acl = CF.auth_status_raw
CF.auth_status_raw = lambda cfg=None: {"ok": False, "host": "dtt.bigcatking.cc"}
st = CF.auth_status({})
check("auth_status 走恢复成功", st.get("ok") and st.get("restored"), str(st))

# ---- 还原 ----
CF._run_cli_cmd = _orig_cmd
CF._run_cli = _orig_runcli
CF._session_files_exist = _orig_exists
CF.restore_session = _orig_restore
CF.auto_login = _orig_auto
CF.auth_status_raw = _orig_acl

print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
