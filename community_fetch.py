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
                           encoding="utf-8", errors="replace", timeout=timeout, env=env)
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
const r = await page.evaluate(async (args) => {
  const pick = (sels) => {
    for (const s of sels) {
      const el = document.querySelector(s);
      if (el) return el;
    }
    return null;
  };
  // 已登录则直接成功
  let sess = JSON.parse(localStorage.getItem('session') || 'null');
  if (sess && sess.token) return { ok: true, already: true };
  try {
    if (location.pathname !== '/login') {
      await page.goto(location.origin + '/login', { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(600);
    }
    const u = pick(args.userSels);
    const p = pick(args.pwdSels);
    if (!u || !p) return { ok: false, err: '找不到登录输入框（页面结构可能变了）' };
    await u.fill(args.email);
    await p.fill(args.password);
    // 勾「记住我」（若存在），尽量让服务端签更长的会话
    try {
      const rm = document.querySelector("input[type=checkbox]");
      if (rm && !rm.checked) await rm.click();
    } catch (e) {}
    const btn = Array.from(document.querySelectorAll('button'))
      .find(b => /登录|sign in|login/i.test((b.innerText || b.textContent || '')));
    if (!btn) return { ok: false, err: '找不到登录按钮' };
    await btn.click();
    await page.waitForTimeout(2500);
    sess = JSON.parse(localStorage.getItem('session') || 'null');
    if (sess && sess.token) return { ok: true, renewed: true,
      email: (JSON.parse(localStorage.getItem('user') || '{}') || {}).email || '' };
    const bodyTxt = (document.body.innerText || '').slice(0, 300);
    return { ok: false, err: '登录后未见 session；页面提示：' + bodyTxt };
  } catch (e) {
    return { ok: false, err: '自动登录异常：' + String(e) };
  }
}, __ARGS__);
return r;
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
    code = _JS_LOGIN.replace("__ARGS__",
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
        # 会话失效：配置了本地凭据就自动续期，免得用户每隔几天手动登录
        lr = auto_login(cfg, log=lambda *_: None)
        if lr.get("ok"):
            return {"ok": True, "email": (cfg or {}).get("dtt_email", ""), "renewed": True}
        return {"ok": False, "reason": lr.get("reason") or
                "dtt 会话已失效，请在 Edge 重新登录或配置本地凭据自动续期"}
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
    res = _run_cli(cli, code, timeout=240)
    if res.get("authExpired"):
        # 会话失效：先尝试用本地凭据自动续期，成功后重跑取数
        lr = auto_login(cfg, log=log)
        if lr.get("ok"):
            log("dtt 会话已续期，重新取数…")
            res = _run_cli(cli, code, timeout=240)
    if res.get("authExpired"):
        raise RuntimeError("dtt 登录已失效。%s" %
                           (auto_login(cfg, log=lambda *_: None).get("reason") or
                            "请在 Edge 重新登录或配置本地凭据自动续期。"))
    if not res.get("ok"):
        raise RuntimeError("取数失败：%s" % (res.get("apiError") or res))

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


if __name__ == "__main__":
    import sys
    day = sys.argv[1] if len(sys.argv) > 1 else (
        datetime.datetime.now(TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    cfg = None
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    st = auth_status(cfg)
    print("会话状态：", st)
    if st.get("ok"):
        d = fetch_day(day, cfg)
        print("统计：", json.dumps(d["stats"], ensure_ascii=False)[:600])
