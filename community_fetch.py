#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dtt 社区消息取数模块（Bittensor 多平台关键词监控）
==========================================================
数据源：https://dtt.bigcatking.cc —— 聚合 Twitter/X、Telegram、Discord
上命中 Bittensor 关键词的消息（关键词已归到 SN 组）。

为什么不直接用 HTTP + 存 token：
    站点鉴权是 localStorage 里的 Bearer token，**7 天过期**。把 token 抠出来
    落到 config 里，过期就得手动换；而本机常驻的浏览器守护进程（pw-browser，
    Edge）里会话是活的。所以这里通过 CLI 的 run-code 在**页面上下文**里 fetch，
    token 由浏览器现取——用户只需在浏览器里重新登录一次即可续期，不用改代码、
    不用把凭证落到项目文件。

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


def _day_bounds(day_str):
    """北京时间某日 00:00–24:00 -> Unix 秒 [start, end)。"""
    d = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
    start = datetime.datetime(d.year, d.month, d.day, tzinfo=TZ)
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
        return {"ok": False, "reason": "dtt 登录已过期，请在 Edge 里重新登录 https://dtt.bigcatking.cc/keywords"}
    return {"ok": True, "email": res.get("email", "")}


def fetch_day(day_str, cfg=None, log=print):
    """取某自然日（北京时间）全量社区事件，落 JSON，返回统计。"""
    start, end = _day_bounds(day_str)
    cli = _pw_cli(cfg)
    code = _JS_FETCH.replace("__ARGS__",
                             '{"start":%d,"end":%d,"maxPages":%d}' % (start, end, MAX_PAGES))
    log("社区取数：%s（北京时间 %s 00:00–24:00，正在从 dtt 拉取…）" % (day_str, day_str))
    res = _run_cli(cli, code, timeout=240)
    if res.get("authExpired"):
        raise RuntimeError("dtt 登录已过期（token 7 天有效）。请在 Edge 浏览器重新登录 "
                           "https://dtt.bigcatking.cc/keywords 后重试；代码与配置都不用改。")
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
        "bounds": {"start": start, "end": end},
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
