#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dtt 社区消息取数模块（Bittensor 多平台关键词监控）
==========================================================
数据源：https://dtt.bigcatking.cc —— 聚合 Twitter/X、Telegram、Discord
上命中 Bittensor 关键词的消息（关键词已归到 SN 组）。

为什么不直接用 HTTP + 存 token：
    站点鉴权是 localStorage 里的 Bearer token，有效期由**服务端签发**（实测约 7
    天，本地代码改不了）。把 token 抠出来落到 config 里，过期就得手动换；而本机
    常驻的浏览器守护进程（pw-browser，Edge）里会话是活的。所以这里通过 CLI 的
    run-code 在**页面上下文**里 fetch，token 由浏览器现取。

    想「一次登录长期免手动干预」（等价用户说的 90 天）：在 config.json 里填本地
    dtt_email/dtt_password（不入库），会话失效时 auto_login() 自动重新登录续期。
    凭据只存本地、不进代码/对话；不填则失效时提示手动在 Edge 登录。

对外接口：
    fetch_day(day_str, cfg=None, log=print) -> dict
        取某自然日（北京时间 00:00–24:00）全量事件，落 JSON，返回统计信息。
    read_day(day_str, cfg=None) -> dict | None
        读已落地的取数结果（供日报生成侧复用，避免重复请求）。
    auth_status(cfg=None) -> dict
        检查浏览器会话是否有效（token 是否过期 / daemon 是否在线）。

落地路径：exports/community/community_YYYY-MM-DD.json
"""

import datetime
import json
import os
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 浏览器 CLI 路径（可用 config.pw_browser_cli 覆盖）
_DEFAULT_PW_CLI = os.path.join(
    os.path.expanduser("~"), ".workbuddy", "skills",
    "playwright-browser-use", "pw-browser",
)

# 站点与 API
SITE_ORIGIN = "https://dtt.bigcatking.cc"
TZ = datetime.timezone(datetime.timedelta(hours=8))

# 单次取数最多翻多少页（100 条/页，60 页 = 6000 条，足够覆盖单日 712 条量级）
MAX_PAGES = 60

# Windows 下从 pythonw（无控制台）监听拉起 node 等控制台子进程时，系统会自动弹一个
# 黑色命令窗口、进程退出后又自动关掉——会打断办公。加 CREATE_NO_WINDOW 彻底消除闪窗，
# 不影响 capture_output/PIPE 捕获输出。非 Windows 平台为 0（无副作用）。
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _pw_cli(cfg=None):
    return (cfg or {}).get("pw_browser_cli") or _DEFAULT_PW_CLI


def _node_cmd(cli):
    """CLI 是 bash shim，Windows 下直接用 node + NODE_PATH 调底层 js。"""
    js = cli if cli.lower().endswith(".js") else cli + ".js"
    skill_dir = os.path.dirname(js)
    env = os.environ.copy()
    env["NODE_PATH"] = os.path.join(skill_dir, "node_modules")
    return ["node", js], env


def _out_dir(cfg=None):
    d = (cfg or {}).get("community_output_dir") or "exports/community"
    return d if os.path.isabs(d) else os.path.join(HERE, d)


def _day_path(day_str, cfg=None):
    return os.path.join(_out_dir(cfg), "community_%s.json" % day_str)


def _day_bounds(day_str, until_now=True, now=None):
    """北京时间某日边界 -> Unix 秒 [start, end)。

    until_now=True（默认，v1.20.0 起）：start = 所选日 00:00，end = 当前触发时刻。
    与 DC 日报口径一致——「从零点开始到当前触发时间」，不再取自然日 24:00。
    until_now=False：保留旧行为（该自然日 00:00–24:00），仅供历史回放。
    """
    d = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
    start = datetime.datetime(d.year, d.month, d.day, tzinfo=TZ)
    if until_now:
        now = now or datetime.datetime.now(TZ)
        end = max(now, start + datetime.timedelta(seconds=1))
    else:
        end = start + datetime.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


# 注入到浏览器页面上下文执行的 JS：会话内 fetch，token 现取，游标翻页取全天。
# 结果是一个 JSON 字符串（由 CLI 包在 result 字段里返回）。
_JS_FETCH = r"""
const r = await page.evaluate(async (args) => {
  const S = args.start, E = args.end;
  const sess = JSON.parse(localStorage.getItem('session') || 'null');
  if (!sess || !sess.token || (sess.expiresAt && Date.now() > sess.expiresAt)) {
    return { authExpired: true };
  }
  const H = { Authorization: 'Bearer ' + sess.token };
  const all = [];
  let ts = null, id = null, pages = 0, apiError = null;
  while (pages < args.maxPages) {
    let u = '/api/events/list?limit=100&sortBy=ingested_at&sortOrder=DESC' +
            '&startDate=' + S + '&endDate=' + E + '&useCursor=true';
    if (ts != null) {
      u += '&cursorTimestamp=' + ts + '&cursorId=' + encodeURIComponent(id);
    }
    let res;
    try { res = await fetch(u, { headers: H }); }
    catch (e) { apiError = 'network: ' + String(e); break; }
    if (res.status === 401) return { authExpired: true };
    if (!res.ok) { apiError = 'HTTP ' + res.status; break; }
    const d = (await res.json()).data || {};
    const items = d.items || [];
    all.push(...items);
    pages++;
    if (!d.hasMore || !d.nextCursor || items.length === 0) break;
    ts = d.nextCursor.timestamp;
    id = d.nextCursor.id;
  }
  // 关键词 -> SN 组映射（关键词形如 __group__:xxx 或普通词，groups[].name = snNN）
  const kwRes = await fetch('/api/keywords?limit=99999&offset=0', { headers: H });
  const kwD = (await kwRes.json()).data || {};
  const kmap = {};
  (kwD.items || []).forEach(k => {
    const sns = (k.groups || [])
      .map(g => String(g.name || ''))
      .filter(n => /^sn\d+$/i.test(n))
      .map(n => parseInt(n.slice(2), 10))
      .filter(n => n >= 1 && n <= 512);
    if (sns.length) kmap[k.id] = Array.from(new Set(sns));
  });
  const platRes = await fetch('/api/platforms', { headers: H });
  const plats = (await platRes.json()).data || [];
  return { ok: true, events: all, keywordSN: kmap,
           platforms: plats, pages: pages, apiError: apiError };
}, __ARGS__);
return r;
"""


def _run_cli(cli, code, timeout=180):
    """调用 pw-browser run-code，返回解析后的 result（dict）。"""
    base, env = _node_cmd(cli)
    cmd = base + ["run-code", code]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env,
                           creationflags=NO_WINDOW)
    except FileNotFoundError:
        raise RuntimeError("找不到浏览器 CLI：%s（检查 config.pw_browser_cli 或技能是否安装）" % cli)
    except subprocess.TimeoutExpired:
        raise RuntimeError("浏览器取数超时（%ds）：守护进程是否还在运行？" % timeout)
    raw = (p.stdout or "").strip()
    if not raw:
        raise RuntimeError("浏览器 CLI 无输出（stderr: %s）" % (p.stderr or "")[:300])
    try:
        wrapper = json.loads(raw)
    except Exception as e:
        raise RuntimeError("浏览器 CLI 返回非 JSON：%s … (%s)" % (raw[:200], e))
    if not wrapper.get("ok"):
        err = wrapper.get("error") or {}
        raise RuntimeError("浏览器 CLI 执行失败：%s" % (err.get("message") or wrapper))
    result = wrapper.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            raise RuntimeError("取数结果无法解析：%s" % result[:200])
    return result


def _run_cli_cmd(cli, args, timeout=60):
    """调用 pw-browser 的子命令（cookies/storage/open 等），返回解析后的 JSON。"""
    base, env = _node_cmd(cli)
    cmd = base + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env,
                           creationflags=NO_WINDOW)
    except FileNotFoundError:
        raise RuntimeError("找不到浏览器 CLI：%s" % cli)
    except subprocess.TimeoutExpired:
        raise RuntimeError("浏览器命令超时（%ds）：%s" % (timeout, args))
    raw = (p.stdout or "").strip()
    if not raw:
        raise RuntimeError("浏览器命令无输出（%s）: %s" % (args, (p.stderr or "")[:200]))
    try:
        return json.loads(raw)
    except Exception:
        raise RuntimeError("浏览器命令返回非 JSON：%s" % raw[:200])


# ---------------------------------------------------------------- CDP 取数后端（v1.30.6）
# 背景：dtt 站点加了 Cloudflare Turnstile，自动化浏览器（pw-browser 守护，webdriver=true）
# 取 API 一律 403；真人 Edge（webdriver=false）放行。改为连「真人 Edge 的远程调试端口」，
# 在其页面上下文 fetch。CDP 连接是 attach，不改 webdriver 指纹。
# dtt_cdp_guard.py 持久托管一个带 --remote-debugging-port 的 Edge（专用 profile，保留登录态）。
_CDP_SCRIPT = os.path.join(HERE, "dtt_cdp.js")
_CDP_PORT_DEFAULT = 9444


def _cdp_port(cfg=None):
    return int((cfg or {}).get("dtt_cdp_port") or os.environ.get("DTT_CDP_PORT") or _CDP_PORT_DEFAULT)


def cdp_available(cfg=None, timeout=3):
    """真人 Edge 的 CDP 调试端口是否在线。"""
    import urllib.request
    port = _cdp_port(cfg)
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _run_cdp(cmd, *args, timeout=240, cfg=None):
    """调 dtt_cdp.js，返回解析后的 result dict。"""
    if not os.path.exists(_CDP_SCRIPT):
        raise RuntimeError("缺少 CDP 取数脚本：%s" % _CDP_SCRIPT)
    base, env = _node_cmd(_pw_cli(cfg))
    # _node_cmd 给的是 pw-browser.js 路径，替换为 dtt_cdp.js，保留 node 可执行与环境
    cmd_list = [base[0], _CDP_SCRIPT, cmd] + [str(a) for a in args]
    env = dict(env)
    env["DTT_CDP_PORT"] = str(_cdp_port(cfg))
    # 输出可能数百 KB，用文件重定向避免管道缓冲死锁（Windows subprocess 已知问题）
    import tempfile
    fd, out_path = tempfile.mkstemp(prefix="dtt_cdp_", suffix=".json")
    err_path = out_path + ".err"
    os.close(fd)
    try:
        with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
            proc = subprocess.Popen(cmd_list, stdout=fo, stderr=fe, env=env,
                                    creationflags=NO_WINDOW)
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise RuntimeError("CDP 取数超时（%ds）" % timeout)
        raw = open(out_path, encoding="utf-8", errors="replace").read().strip()
        if not raw:
            err = open(err_path, encoding="utf-8", errors="replace").read()[:300]
            raise RuntimeError("CDP 脚本无输出（stderr: %s）" % err)
        # 只取第一个 JSON 对象起的位置
        idx = raw.find("{")
        if idx > 0:
            raw = raw[idx:]
        try:
            return json.loads(raw)
        except Exception:
            raise RuntimeError("CDP 结果非 JSON：%s" % raw[:200])
    finally:
        for p in (out_path, err_path):
            try:
                os.remove(p)
            except Exception:
                pass


def cdp_fetch_events(start, end, cfg=None, log=print):
    """用真人 Edge 取指定时间戳范围（秒）的 events/keywords/platforms。"""
    res = _run_cdp("fetch", start, end, timeout=240, cfg=cfg)
    if res.get("authExpired"):
        raise RuntimeError("dtt 登录已失效，请在真人 Edge 重新登录 dtt")
    if not res.get("ok"):
        raise RuntimeError("CDP 取数失败：%s" % (res.get("error") or res.get("apiError") or res))
    return res


def cdp_sn_channel(args, cfg=None, log=print):
    """用真人 Edge 按 SN 关键词组取子网频道消息（args: sn/windows/platformId）。"""
    res = _run_cdp("snchannel", json.dumps(args, ensure_ascii=False), timeout=180, cfg=cfg)
    if res.get("authExpired"):
        raise RuntimeError("dtt 登录已失效，请在真人 Edge 重新登录 dtt")
    if res.get("err") or res.get("apiErr") or res.get("netErr"):
        raise RuntimeError("CDP 子网频道取数失败：%s" %
                           (res.get("err") or res.get("apiErr") or res.get("netErr")))
    if not res.get("ok"):
        raise RuntimeError("CDP 子网频道取数失败：%s" % str(res)[:200])
    return res


# 会话凭证落盘目录：必须在 ~/.pw-browser/ 内（CLI 的 export/import 路径限制）。
# 文件 0600（CLI 写），只存本机、已 gitignore，绝不一入库/不进对话。
_SESSION_DIR = os.path.join(os.path.expanduser("~"), ".pw-browser")
_COOKIES_FILE = os.path.join(_SESSION_DIR, "dtt_session_cookies.json")
_STORAGE_FILE = os.path.join(_SESSION_DIR, "dtt_session_localStorage.json")


def _session_files_exist():
    return os.path.exists(_COOKIES_FILE) and os.path.exists(_STORAGE_FILE)


_USERNAME_SELECTORS = (
    "input[type=email]", "input[name=email]", "input[placeholder*='邮箱']",
    "input[autocomplete='email']", "input[type=text]")
_PASSWORD_SELECTORS = (
    "input[type=password]", "input[name=password]",
    "input[placeholder*='密码']", "input[autocomplete='current-password']")

# 自动重新登录：用 config 里的本地凭据在浏览器里登录 dtt，续期会话。
# 站点 token 有效期由服务端签发（实测约 7 天，本地无法改）；凭据填本地后，
# 会话失效可自动重登，用户实际「登录一次」即可长期免手动干预，等价于更长周期。
_JS_LOGIN = r"""
// 导航/等待在 daemon 沙箱里做（这里才有 page）；DOM 操作在页面上下文里做。
// 注意：page.evaluate 里没有 page 对象，也不能 await DOM 方法——之前的实现踩了这两个坑。
const sess0 = await page.evaluate(() => JSON.parse(localStorage.getItem('session') || 'null'));
if (sess0 && sess0.token) return { ok: true, already: true };
// 确保在登录页
const host = await page.evaluate(() => location.hostname);
if (!host) {
  await page.goto(__SITE__, { waitUntil: 'domcontentloaded', timeout: 60000 });
}
if (await page.evaluate(() => location.pathname) !== '/login') {
  await page.goto(__SITE__ + '/login', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(800);
}
// 在页面里填表并点击（纯同步 DOM 操作 + 原生 setter，兼容 React/Vue）
const fill = await page.evaluate((args) => {
  const pick = (sels) => {
    for (const s of sels) { const el = document.querySelector(s); if (el) return el; }
    return null;
  };
  const setVal = (el, v) => {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const u = pick(args.userSels), p = pick(args.pwdSels);
  if (!u || !p) return { ok: false, err: '找不到登录输入框（页面结构可能变了）',
                          html: (document.body.innerHTML || '').slice(0, 600) };
  setVal(u, args.email);
  setVal(p, args.password);
  try { const rm = document.querySelector('input[type=checkbox]'); if (rm && !rm.checked) rm.click(); } catch (e) {}
  const btn = Array.from(document.querySelectorAll('button'))
    .find(b => /登录|sign in|login/i.test((b.innerText || b.textContent || '')));
  if (!btn) return { ok: false, err: '找不到登录按钮' };
  btn.click();
  return { ok: true };
}, __ARGS__);
if (!fill.ok) return fill;
// 等 session 落 localStorage（daemon 上下文轮候）
let sess = null;
for (let i = 0; i < 20; i++) {
  await page.waitForTimeout(500);
  sess = await page.evaluate(() => JSON.parse(localStorage.getItem('session') || 'null'));
  if (sess && sess.token) break;
}
if (sess && sess.token) {
  const email = await page.evaluate(() => (JSON.parse(localStorage.getItem('user') || '{}') || {}).email || '');
  return { ok: true, renewed: true, email: email };
}
const bodyTxt = await page.evaluate(() => (document.body.innerText || '').slice(0, 300));
return { ok: false, err: '登录后未见 session；页面提示：' + bodyTxt };
"""


def auto_login(cfg=None, log=print):
    """用 config 里的本地凭据自动登录 dtt 续期。返回 {ok, reason}。

    - 未配置 dtt_email/dtt_password：返回 ok=False 并提示填本地凭据或手动登录。
    - 配置了：在浏览器里填表登录（勾「记住我」），成功后 session 由浏览器现取。
    凭据只存本地 config.json（已 gitignore），不进项目代码、不落对话。
    """
    cfg = cfg or {}
    email = (cfg.get("dtt_email") or "").strip()
    password = cfg.get("dtt_password") or ""
    if not email or not password:
        return {"ok": False, "reason":
                "dtt 会话已失效。要在本地配置 dtt_email/dtt_password 后自动续期"
                "（等价于长期免手动登录），或在 Edge 里手动登录 dtt.bigcatking.cc。"}
    cli = _pw_cli(cfg)
    code = _JS_LOGIN.replace("__SITE__", json.dumps(SITE_ORIGIN)).replace("__ARGS__",
        json.dumps({"email": email, "password": password,
                    "userSels": list(_USERNAME_SELECTORS),
                    "pwdSels": list(_PASSWORD_SELECTORS)}, ensure_ascii=False))
    try:
        res = _run_cli(cli, code, timeout=90)
    except Exception as e:
        return {"ok": False, "reason": "自动登录失败：%s" % e}
    if res.get("ok"):
        if res.get("renewed"):
            log("dtt 会话已用本地凭据自动续期%s" %
                ("（%s）" % res.get("email") if res.get("email") else ""))
        return {"ok": True}
    return {"ok": False, "reason": res.get("err") or "自动登录未成功"}


def save_session(cfg=None, log=print):
    """把当前浏览器登录态（cookies + localStorage）导出落盘，供守护重启后恢复。

    必须先在有窗口模式人工登录 dtt（过 Cloudflare Turnstile）后调用。
    凭证文件落在 ~/.pw-browser/ 内（CLI 路径限制），0600，不入库。
    返回 {ok, reason, cookies, keys}。
    """
    cli = _pw_cli(cfg)
    try:
        ck = _run_cli_cmd(cli, ["cookies", "export", "--path", _COOKIES_FILE], timeout=60)
        st = _run_cli_cmd(cli, ["storage", "export", "--path", _STORAGE_FILE], timeout=60)
    except Exception as e:
        return {"ok": False, "reason": "导出会话失败：%s" % e}
    if not ck.get("ok") or not st.get("ok"):
        return {"ok": False, "reason": "导出会话失败：cookies=%s storage=%s"
                % (ck.get("error"), st.get("error"))}
    log("dtt 会话已保存：cookies %d 条 + localStorage %d 项 → ~/.pw-browser/"
        % (ck.get("count", 0), st.get("count", 0)))
    return {"ok": True, "cookies": ck.get("count", 0), "keys": st.get("count", 0)}


def restore_session(cfg=None, log=print):
    """从落盘凭证恢复登录态到当前浏览器 context。返回 {ok, reason}。

    守护重启后是全新临时 profile（无 session），必须先打开 dtt 站点再 import
    （localStorage 绑定 origin，about:blank 下写入无效）。
    """
    if not _session_files_exist():
        return {"ok": False, "reason": "无已保存的 dtt 会话凭证"}
    cli = _pw_cli(cfg)
    ensure_site = r"""
const h = await page.evaluate(() => location.hostname);
if (h !== 'dtt.bigcatking.cc') {
  await page.goto(%s, {waitUntil:'domcontentloaded', timeout:60000});
  await page.waitForTimeout(800);
}
return {ok:true};
""" % json.dumps(SITE_ORIGIN)
    try:
        _run_cli(cli, ensure_site, timeout=90)
        ck = _run_cli_cmd(cli, ["cookies", "import", _COOKIES_FILE], timeout=60)
        st = _run_cli_cmd(cli, ["storage", "import", _STORAGE_FILE], timeout=60)
        # import 后需刷新页面让前端从 localStorage 读到 token
        _run_cli(cli, "await page.reload({waitUntil:'domcontentloaded',timeout:60000}); "
                      "await page.waitForTimeout(600); return {ok:true};", timeout=90)
    except Exception as e:
        return {"ok": False, "reason": "恢复会话失败：%s" % e}
    if not ck.get("ok") or not st.get("ok"):
        return {"ok": False, "reason": "恢复会话失败：cookies=%s storage=%s"
                % (ck.get("error"), st.get("error"))}
    # 恢复后用 auth_status 校验 token 是否仍有效（可能服务端已过期）
    stt = auth_status_raw(cfg)
    if not stt.get("ok"):
        return {"ok": False, "reason": "已恢复凭证但 token 服务端已失效，需人工重新登录"}
    log("dtt 会话已从本地凭证恢复")
    return {"ok": True, "email": stt.get("email", "")}


def _renew_session(cfg=None, log=print):
    """会话恢复统一入口：先从本地凭证恢复，失败再试凭据自动登录。

    v1.30.5：Cloudflare Turnstile 后 headless 自动登录基本不可用，恢复凭证
    是主要手段；auto_login 仅保留给尚未加 Turnstile 的站点。
    """
    rr = restore_session(cfg, log=log)
    if rr.get("ok"):
        return rr
    return auto_login(cfg, log=log)


def auth_status_raw(cfg=None):
    """只检查 localStorage session 是否有效，不做自动登录/恢复（避免递归）。"""
    cli = _pw_cli(cfg)
    code = r"""
const r = await page.evaluate(() => {
  const s = JSON.parse(localStorage.getItem('session') || 'null');
  const expired = !s || !s.token || (s.expiresAt && Date.now() > s.expiresAt);
  return { ok: !expired, host: location.hostname,
           email: (JSON.parse(localStorage.getItem('user') || '{}') || {}).email || '' };
});
return r;
"""
    try:
        return _run_cli(cli, code, timeout=30) or {"ok": False}
    except Exception:
        return {"ok": False}


def ping_daemon(cfg=None, timeout=10):
    """轻量 ping 浏览器 daemon（GET /health），保持其空闲计时器归零。

    daemon 默认 15 分钟无命令就退出、丢失会话；定时调度每 10 分钟 ping 一次，
    可避免到点取数时才发现会话没了。返回 daemon 是否在线。
    """
    import urllib.request
    cli = _pw_cli(cfg)
    state = os.path.join(os.path.expanduser("~"), ".pw-browser", "daemon.json")
    try:
        with open(state, encoding="utf-8") as f:
            d = json.load(f)
        host, port = d.get("host", "127.0.0.1"), int(d.get("port", 19223))
        with urllib.request.urlopen("http://%s:%d/health" % (host, port), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start_daemon(cfg=None, log=print, timeout=45):
    """按需拉起浏览器守护进程（离线时）；已在线则直接返回。

    守护默认 15 分钟空闲自动退出（不常驻），命令触发取数时若发现离线，
    这里临时拉起一次：后台启动 pw-browser daemon（Edge），轮询 /health 直到就绪。
    返回 True/False（是否在线）。
    """
    if ping_daemon(cfg, timeout=5):
        return True
    cli = _pw_cli(cfg)
    if not os.path.exists(cli):
        log("浏览器守护离线，且 CLI 不存在：%s" % cli)
        return False
    base, env = _node_cmd(cli)
    # 日常取数 headless 避免弹窗；人工登录（config.dtt_headless=0）时用有窗口过验证。
    # v1.30.5：dtt 登录页有 Cloudflare Turnstile，headless 过不了；会话靠 save/restore 持久化。
    _headless = (cfg or {}).get("dtt_headless", 1)
    env["PW_BROWSER_HEADLESS"] = "0" if str(_headless) in ("0", "false", "False") else "1"
    # Windows 下用 CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS 让守护脱离当前进程，
    # 避免 TG 监听线程结束/守护重启时把它一起带走；stdout/stderr 丢弃防止管道阻塞。
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
                getattr(subprocess, "DETACHED_PROCESS", 0)
    log("浏览器守护离线，正在按需拉起（Edge，不常驻）…")
    try:
        subprocess.Popen(base + ["daemon"], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags,
                         cwd=os.path.dirname(os.path.abspath(base[1])))
    except Exception as e:
        log("拉起浏览器守护失败：%s" % e)
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.5)
        if ping_daemon(cfg, timeout=4):
            log("浏览器守护已就绪")
            return True
    log("浏览器守护启动超时（%ds）：仍无法连接，检查 Edge 是否可启动 / 端口 19223 是否被占用"
        % timeout)
    return False


def ensure_ready(cfg=None, log=print):
    """取数前置：守护在线 + 浏览器停在 dtt 站点 + 会话有效（失效则自动登录）。

    守护空闲退出后浏览器是全新空白 context（localStorage 无 token），所以拉起后
    必须先 open dtt 站点，再检查/续期会话，否则页面上下文 fetch 会拿到 about:blank。
    """
    if not start_daemon(cfg, log=log):
        raise RuntimeError("浏览器守护无法启动（Edge / 端口 19223 不可用）")
    cli = _pw_cli(cfg)
    # 确认/打开 dtt 站点：auth_status 内部会检查 host，不在站点会返回失败。
    # 先轻量探测页面 host；不对就 open 过去。
    check = r"""
const r = await page.evaluate(() => ({ host: location.hostname, url: location.href }));
return r;
"""
    try:
        cur = _run_cli(cli, check, timeout=30)
    except Exception:
        cur = {}
    if "dtt.bigcatking.cc" not in str(cur.get("host", "")):
        log("浏览器未停在 dtt 站点，正在打开 %s …" % SITE_ORIGIN)
        open_code = "await page.goto(%s, {waitUntil:'domcontentloaded', timeout:60000}); " \
                    "await page.waitForTimeout(1200); return {ok:true};" % json.dumps(SITE_ORIGIN)
        _run_cli(cli, open_code, timeout=90)
    st = auth_status(cfg)
    if not st.get("ok"):
        raise RuntimeError(st.get("reason") or "dtt 会话不可用")
    return True


def auth_status(cfg=None):
    """检查浏览器会话是否有效。返回 {ok, reason}。"""
    cli = _pw_cli(cfg)
    if not os.path.exists(cli):
        return {"ok": False, "reason": "浏览器 CLI 不存在：%s" % cli}
    code = r"""
const r = await page.evaluate(() => {
  const s = JSON.parse(localStorage.getItem('session') || 'null');
  const expired = !s || !s.token || (s.expiresAt && Date.now() > s.expiresAt);
  return { ok: !expired, host: location.hostname,
           email: (JSON.parse(localStorage.getItem('user') || '{}') || {}).email || '' };
});
return r;
"""
    try:
        res = _run_cli(cli, code, timeout=30)
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    if res.get("ok") and "dtt.bigcatking.cc" not in str(res.get("host", "")):
        return {"ok": False, "reason": "浏览器当前不在 dtt.bigcatking.cc 站点，请先打开并登录"}
    if not res.get("ok"):
        # 会话失效（v1.30.5）：恢复本地凭证为主，自动登录兜底
        lr = _renew_session(cfg, log=lambda *_: None)
        if lr.get("ok"):
            return {"ok": True, "email": lr.get("email", "") or (cfg or {}).get("dtt_email", ""),
                    "restored": bool(lr.get("email"))}
        return {"ok": False, "reason":
                "dtt 会话失效且自动恢复失败。请在有窗口模式人工登录一次："
                "python community_fetch.py login（会过 Cloudflare 验证并保存会话）。"
                "详情：%s" % (lr.get("reason", "")[:120])}
    return {"ok": True, "email": res.get("email", "")}


def fetch_day(day_str, cfg=None, log=print, until_now=True):
    """取某日社区事件，落 JSON，返回统计。

    v1.20.0 起：默认 until_now=True —— 北京时间「所选日 00:00 → 当前触发时刻」，
    与 DC 日报口径一致（不再是固定自然日 24:00）。当日已落地且边界不同会重取。
    """
    start, end = _day_bounds(day_str, until_now=until_now)
    cli = _pw_cli(cfg)
    code = _JS_FETCH.replace("__ARGS__",
                             '{"start":%d,"end":%d,"maxPages":%d}' % (start, end, MAX_PAGES))
    _end_label = (datetime.datetime.fromtimestamp(end, TZ).strftime("%m-%d %H:%M")
                  + "（当前）") if until_now else "次日00:00"
    log("社区取数：%s（北京时间 %s 00:00 → %s，正在从 dtt 拉取…）"
        % (day_str, day_str, _end_label))
    # v1.30.6：优先走 CDP 真人 Edge（webdriver=false，过 Cloudflare）；
    # CDP 守护不在线时回退旧 pw-browser 自动化守护（可能 403，仅兜底）。
    res = None
    cdp_on = cdp_available(cfg)
    if cdp_on:
        try:
            log("CDP 真人浏览器取数（端口 %d）…" % _cdp_port(cfg))
            res = cdp_fetch_events(start, end, cfg=cfg, log=log)
        except Exception as e:
            log("CDP 取数失败（%s），回退自动化守护…" % str(e)[:120])
    if res is None:
        # 守护空闲退出后会话会丢，取数前先确保：守护在线 + 浏览器在 dtt 站点 + 会话有效。
        ensure_ready(cfg, log=log)
        res = _run_cli(cli, code, timeout=240)
        if res.get("authExpired"):
            # 会话失效：先恢复本地凭证，成功后重跑取数
            lr = _renew_session(cfg, log=log)
            if lr.get("ok"):
                log("dtt 会话已恢复，重新取数…")
                res = _run_cli(cli, code, timeout=240)
    if res.get("authExpired"):
        raise RuntimeError("dtt 登录已失效。请在真人 Edge 重新登录 dtt，"
                           "或运行 python community_fetch.py login。")
    if not res.get("ok"):
        raise RuntimeError("取数失败：%s" % (res.get("apiError") or res.get("error") or res))

    events = res.get("events") or []
    kmap = res.get("keywordSN") or {}
    plats = res.get("platforms") or []

    # 组装：事件带上 keyword 文本、SN 归属、平台名，便于日报分组与溯源
    plat_name = {p.get("id"): p.get("code") for p in plats if isinstance(p, dict)}
    for e in events:
        sns = kmap.get(e.get("keywordId")) or []
        e["sn"] = sns
        e["platform"] = plat_name.get(e.get("platformId")) or e.get("platformId")
    events.sort(key=lambda e: e.get("occurredAt") or 0)

    by_p, by_sn = {}, {}
    for e in events:
        p = e.get("platform") or "unknown"
        by_p[p] = by_p.get(p, 0) + 1
        for sn in e.get("sn") or []:
            by_sn[str(sn)] = by_sn.get(str(sn), 0) + 1

    payload = {
        "day": day_str,
        "source": SITE_ORIGIN,
        "fetched_at": datetime.datetime.now(TZ).isoformat(timespec="seconds"),
        "bounds": {"start": start, "end": end, "until_now": until_now},
        "pages": res.get("pages"),
        "stats": {
            "total": len(events),
            "with_sn": sum(1 for e in events if e.get("sn")),
            "by_platform": by_p,
            "by_sn": dict(sorted(by_sn.items(), key=lambda x: -x[1])),
        },
        "events": events,
    }
    out = _day_path(day_str, cfg)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out)
    log("社区取数完成：%s 共 %d 条（命中 SN %d 条；%s），落盘 %s"
        % (day_str, len(events), payload["stats"]["with_sn"],
           " ".join("%s=%d" % (k, v) for k, v in by_p.items()), out))
    return payload


def read_day(day_str, cfg=None):
    """读已落地的取数结果；不存在返回 None。"""
    p = _day_path(day_str, cfg)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_covers_now(payload, cfg=None, slack_sec=120):
    """已落地结果是否覆盖到「当前触发时刻」（容差 slack_sec 秒）。

    v1.20.0：取数窗口是 00:00→当前，所以缓存会随触发时间推移而过期。
    超过容差就应重取，避免复用旧快照漏掉临近触发时刻的新消息。
    """
    if not payload:
        return False
    b = payload.get("bounds") or {}
    end = b.get("end")
    if not end:
        return False
    return int(datetime.datetime.now(TZ).timestamp()) - int(end) <= slack_sec


# ---------------------------------------------------------------- 子网频道补充取数（v1.27.0）
# 背景：社区日报的 fetch_day() 抓的是全平台关键词流，某个子网当天可能一条都没命中
# （实测 SN117 当日/昨日 DC=0、仅 X/TG 各 3 条），导致「重要事件跟踪报告」里
# 缺少该子网频道的直接讨论。dtt 没有「按 DC 频道抓全天聊天」的接口（/api/sources、
# /api/discord/channels 均 404，群消息库只有 5 个 TG 群），但关键词是按 SN 组维护的：
# 用「该 SN 关键词组的 keywordId」精确过滤 + 按平台取当日/昨日事件，就能把该子网
# 频道里被关键词命中的讨论捞出来，作为全网抓取的补充与交叉核验素材。
_JS_SN_CHANNEL = r"""
const r = await page.evaluate(async (args) => {
  const sess = JSON.parse(localStorage.getItem('session') || 'null');
  if (!sess || !sess.token || (sess.expiresAt && Date.now() > sess.expiresAt)) {
    return { authExpired: true };
  }
  const H = { Authorization: 'Bearer ' + sess.token };
  const out = { sn: args.sn };
  // 1) 该 SN 关键词组的所有 keywordId（groups[].name = snNN）
  let kwIds = [];
  try {
    const kwRes = await fetch('/api/keywords?limit=99999&offset=0', { headers: H });
    const kwD = (await kwRes.json()).data || {};
    kwIds = (kwD.items || [])
      .filter(k => (k.groups || []).some(g =>
          String(g.name || '').toLowerCase() === 'sn' + args.sn))
      .map(k => k.id);
  } catch (e) { return { err: 'keywords: ' + String(e) }; }
  out.keywordIds = kwIds;
  if (!kwIds.length) return { ok: true, events: [], keywordIds: [] };

  // 2) 每个日期窗口 × 每个关键词，取事件（命中即收录；游标翻页）
  const all = [];
  const seen = new Set();
  for (const win of args.windows) {
    for (const kw of kwIds) {
      let ts = null, id = null, pages = 0;
      while (pages < 6) {
        let u = '/api/events/list?limit=100&sortBy=ingested_at&sortOrder=DESC&useCursor=true' +
                '&keywordId=' + encodeURIComponent(kw) +
                '&startDate=' + win.start + '&endDate=' + win.end +
                '&platformId=' + args.platformId;
        if (ts != null) u += '&cursorTimestamp=' + ts + '&cursorId=' + encodeURIComponent(id);
        let res;
        try { res = await fetch(u, { headers: H }); }
        catch (e) { out.netErr = String(e); break; }
        if (res.status === 401) return { authExpired: true };
        if (!res.ok) { out.apiErr = 'HTTP ' + res.status; break; }
        const d = (await res.json()).data || {};
        const items = d.items || [];
        for (const e of items) {
          if (seen.has(e.id)) continue;
          seen.add(e.id);
          e._kw = kw; e._win = win.label;
          all.push(e);
        }
        pages++;
        if (!d.hasMore || !d.nextCursor || !items.length) break;
        ts = d.nextCursor.timestamp; id = d.nextCursor.id;
      }
    }
  }
  // 3) 平台名映射
  let platName = {};
  try {
    const pr = await fetch('/api/platforms', { headers: H });
    const plats = (await pr.json()).data || [];
    plats.forEach(p => { platName[p.id] = p.code; });
  } catch (e) {}
  all.forEach(e => { e.platform = platName[e.platformId] || e.platformId; });
  all.sort((a, b) => (b.occurredAt || 0) - (a.occurredAt || 0));
  return { ok: true, events: all, keywordIds: kwIds, platformName: args.platformId,
           platformId: args.platformId };
}, __ARGS__);
return r;
"""


def sn_channel_path(sn, day_str, cfg=None, platform="discord"):
    """子网频道补充取数的落盘路径（独立于社区日报缓存）。"""
    return os.path.join(_out_dir(cfg), "sn_channel_SN%s_%s_%s.json" % (sn, platform, day_str))


def fetch_sn_channel(sn, day_str, cfg=None, log=print, platform="discord", days=2,
                     max_items=200):
    """按子网关键词组取该子网「当日+昨日」频道消息（v1.27.0）。

    与 fetch_day() 的区别：
      - fetch_day 抓全平台关键词流（某个子网当天可能 0 命中）；
      - 本函数用「该 SN 关键词组的 keywordId」精确过滤，只取该子网命中的消息，
        默认取当日 + 昨日两个自然日（days=2），平台默认 discord（DC 子网频道）。

    落盘到独立文件 sn_channel_SN{sn}_{platform}_{day}.json，不覆盖社区日报缓存。
    取数失败抛异常，由调用方（投研）决定降级——不阻断投研主流程。

    返回 payload：{sn, day, platform, windows, keyword_ids, events, stats}
    """
    sn = int(sn)
    # 窗口：当日 00:00→当前，昨日 00:00→24:00（与第二轮 TG 日报口径一致）
    windows = []
    for back in range(max(1, int(days))):
        d = datetime.datetime.strptime(day_str, "%Y-%m-%d").date() - datetime.timedelta(days=back)
        start, end = _day_bounds(d.strftime("%Y-%m-%d"), until_now=(back == 0))
        windows.append({"label": d.strftime("%Y-%m-%d"), "start": start, "end": end})
    cli = _pw_cli(cfg)
    plat_id = {"discord": "plt_discord", "telegram": "plt_telegram",
               "twitter": "plt_twitter"}.get(platform, platform)
    args = {"sn": sn, "windows": windows, "platformId": plat_id}
    code = _JS_SN_CHANNEL.replace("__ARGS__", json.dumps(args, ensure_ascii=False))
    log("子网频道补充：取 SN%d 的 %s 频道消息（%s，关键词组精确匹配）…"
        % (sn, platform, "、".join(w["label"] for w in windows)))
    # v1.30.6：优先 CDP 真人 Edge，失败回退 pw-browser 自动化守护
    res = None
    if cdp_available(cfg):
        try:
            res = cdp_sn_channel(args, cfg=cfg, log=log)
        except Exception as e:
            log("CDP 子网频道取数失败（%s），回退自动化守护…" % str(e)[:120])
    if res is None:
        ensure_ready(cfg, log=log)
        res = _run_cli(cli, code, timeout=180)
        if res.get("authExpired"):
            lr = _renew_session(cfg, log=log)
            if lr.get("ok"):
                log("dtt 会话已恢复，重新取子网频道消息…")
                res = _run_cli(cli, code, timeout=180)
    if res.get("authExpired"):
        raise RuntimeError("dtt 登录已失效，子网频道补充取数跳过。请在真人 Edge 重新登录 dtt")
    if res.get("err") or res.get("apiErr") or res.get("netErr"):
        raise RuntimeError("子网频道取数失败：%s" %
                           (res.get("err") or res.get("apiErr") or res.get("netErr")))
    if not res.get("ok"):
        raise RuntimeError("子网频道取数失败：%s" % str(res)[:200])
    events = []
    for e in (res.get("events") or [])[:int(max_items)]:
        t = e.get("occurredAt")
        events.append({
            "id": e.get("id"), "author": e.get("author") or "", "content": e.get("content") or "",
            "sourceUrl": e.get("sourceUrl") or "", "platform": e.get("platform") or platform,
            "platformId": e.get("platformId") or plat_id,
            "occurredAt": t, "kw": e.get("_kw"), "window": e.get("_win"),
            "time": (datetime.datetime.fromtimestamp(t / 1000, TZ).strftime("%m-%d %H:%M")
                     if t else "??:??"),
        })
    payload = {
        "sn": sn, "day": day_str, "platform": platform, "platform_id": plat_id,
        "fetched_at": datetime.datetime.now(TZ).isoformat(timespec="seconds"),
        "windows": windows, "keyword_ids": res.get("keywordIds") or [],
        "stats": {"total": len(events),
                  "by_day": {w["label"]: sum(1 for e in events if e.get("window") == w["label"])
                             for w in windows}},
        "events": events,
    }
    out = sn_channel_path(sn, day_str, cfg, platform=platform)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out)
    log("子网频道补充完成：SN%d %s 频道取到 %d 条（%s），落盘 %s"
        % (sn, platform, len(events),
           " ".join("%s=%d" % (k, v) for k, v in payload["stats"]["by_day"].items()), out))
    return payload


def read_sn_channel(sn, day_str, cfg=None, platform="discord"):
    """读已落地的子网频道补充取数结果；不存在返回 None。"""
    p = sn_channel_path(sn, day_str, cfg, platform=platform)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def login_interactive(cfg=None, log=print, wait_seconds=300):
    """有窗口人工登录 dtt（过 Cloudflare Turnstile）并保存会话。

    流程：关闭旧 headless 守护 → 以有窗口模式重启 → 打开登录页 →
    等待人工完成验证+登录（轮询 localStorage.session）→ save_session 落盘 →
    关闭有窗口守护（下次取数会 headless 重启并从凭证恢复）。
    """
    cli = _pw_cli(cfg)
    log("① 关闭旧守护（如有）…")
    try:
        _run_cli_cmd(cli, ["shutdown"], timeout=20)
    except Exception:
        pass
    time.sleep(2)
    log("② 以有窗口模式启动浏览器守护…")
    _login_cfg = dict(cfg or {})
    _login_cfg["dtt_headless"] = 0
    if not start_daemon(_login_cfg, log=log, timeout=60):
        return {"ok": False, "reason": "有窗口守护启动失败"}
    log("③ 打开 dtt 登录页，请在弹出的 Edge 窗口里完成 Cloudflare 验证并登录…")
    _run_cli(cli, "await page.goto(%s,{waitUntil:'domcontentloaded',timeout:60000}); "
                  "return {ok:true};" % json.dumps(SITE_ORIGIN + "/login"), timeout=90)
    deadline = time.time() + wait_seconds
    logged = False
    while time.time() < deadline:
        stt = auth_status_raw(cfg)
        if stt.get("ok"):
            logged = True
            break
        time.sleep(3)
    if not logged:
        return {"ok": False, "reason": "等待 %ds 未见登录成功，请重试 login" % wait_seconds}
    log("④ 检测到登录成功，正在保存会话凭证…")
    saved = save_session(cfg, log=log)
    if not saved.get("ok"):
        return {"ok": False, "reason": "登录成功但保存会话失败：%s" % saved.get("reason")}
    log("✅ 完成。以后社区日报取数会自动恢复该会话；token 过期后重跑本命令即可。")
    return {"ok": True, **saved}


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    cfg = None
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    if args and args[0].lower() == "login":
        # 人工登录一次（有窗口过 Cloudflare 验证）+ 保存会话
        r = login_interactive(cfg)
        print("登录结果：", json.dumps(r, ensure_ascii=False))
        sys.exit(0 if r.get("ok") else 1)
    day = args[0] if args else (
        datetime.datetime.now(TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    st = auth_status(cfg)
    print("会话状态：", st)
    if st.get("ok"):
        d = fetch_day(day, cfg)
        print("统计：", json.dumps(d["stats"], ensure_ascii=False)[:600])
