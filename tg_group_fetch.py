#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TG 群消息取数模块（dtt 管理员群消息库）
==========================================================
数据源：https://dtt.bigcatking.cc 的 `/api/group-messages`（管理员账号升级后开放）。

与 community_fetch（关键词事件流 `/api/events`）**完全隔离**：
    - events 表靠关键词命中，只有命中 885 个关键词的消息才入库；
    - group-messages 表是 TG 群的原始聊天全文，无关键词字段。

管理员接口：
    GET /api/group-messages/groups         已入库群列表（groupId/groupName/messageCount）
    GET /api/group-messages                群消息正文（groupId/startDate/endDate/游标翻页）
        - startDate/endDate 为**毫秒**时间戳
        - limit 服务端封顶 100，必须游标翻页（cursorTimestamp + cursorId）
        - sortBy=occurredAt&sortOrder=DESC

鉴权：仍走本机 Edge headless 守护的页面上下文 fetch（token 现取，不落项目文件），
复用 community_fetch 的 ensure_ready / auto_login / _run_cli / CREATE_NO_WINDOW。

对外接口：
    fetch_day(day_str, cfg=None, log=print, until_now=True) -> dict
    read_day(day_str, cfg=None) -> dict | None
    cache_covers_now(payload, cfg=None) -> bool
    list_groups(cfg=None, log=print) -> list

落地路径：exports/tg_group/tg_group_YYYY-MM-DD.json
"""

import datetime
import json
import os

import community_fetch as CF

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = datetime.timezone(datetime.timedelta(hours=8))

# 默认接入的 4 个活跃 TG 群（跳过仅 1 条的空群）。config.tg_group_ids 可覆盖。
# 顺序即日报展示顺序：官方动态 → 实质讨论 → 子网观点 → 交易闲聊。
DEFAULT_GROUP_IDS = [
    "8116469191",      # TAO-update-DC（各 SN 官方动态转发）
    "1003968896337",   # Hanlin.AI（回购/Chutes 争议实质讨论）
    "1002480957486",   # Subnet Summer - τ（SN 编号观点、行情）
    "1001965130471",   # Bittensor TAO Price Chat（交易员闲聊，含 const 相关）
]
# 服务端 limit 封顶 100
PAGE_LIMIT = 100
# 单日单群最多翻多少页（100 条/页，60 页=6000 条，足够覆盖最活跃群单日量级）
MAX_PAGES_PER_GROUP = 60


def _out_dir(cfg=None):
    d = (cfg or {}).get("tg_group_output_dir") or "exports/tg_group"
    return d if os.path.isabs(d) else os.path.join(HERE, d)


def _day_path(day_str, cfg=None):
    return os.path.join(_out_dir(cfg), "tg_group_%s.json" % day_str)


def _group_ids(cfg=None):
    ids = (cfg or {}).get("tg_group_ids")
    if not ids:
        return list(DEFAULT_GROUP_IDS)
    if isinstance(ids, str):
        ids = [ids]
    return [str(x).strip() for x in ids if str(x).strip()]


def _day_bounds(day_str, until_now=True, now=None):
    """北京时间某日 00:00 →（当前触发时刻 | 次日00:00），返回毫秒 [start, end)。"""
    d = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
    start = datetime.datetime(d.year, d.month, d.day, tzinfo=TZ)
    if until_now:
        now = now or datetime.datetime.now(TZ)
        end = max(now, start + datetime.timedelta(seconds=1))
    else:
        end = start + datetime.timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


# 注入页面上下文：管理员群消息库取数（带游标翻页）。token 现取。
_JS_LIST_GROUPS = r"""
const r = await page.evaluate(async () => {
  const sess = JSON.parse(localStorage.getItem('session') || 'null');
  if (!sess || !sess.token || (sess.expiresAt && Date.now() > sess.expiresAt)) {
    return { authExpired: true };
  }
  const H = { Authorization: 'Bearer ' + sess.token };
  const res = await fetch('/api/group-messages/groups', { headers: H });
  if (res.status === 401) return { authExpired: true };
  const d = (await res.json()).data || {};
  return { ok: true, groups: d.groups || [] };
});
return r;
"""

_JS_FETCH = r"""
const r = await page.evaluate(async (args) => {
  const sess = JSON.parse(localStorage.getItem('session') || 'null');
  if (!sess || !sess.token || (sess.expiresAt && Date.now() > sess.expiresAt)) {
    return { authExpired: true };
  }
  const H = { Authorization: 'Bearer ' + sess.token };
  const all = [];
  let apiError = null;
  for (const gid of args.groupIds) {
    let ts = null, id = null, pages = 0;
    while (pages < args.maxPages) {
      let u = '/api/group-messages?limit=' + args.limit +
              '&groupId=' + encodeURIComponent(gid) +
              '&startDate=' + args.start + '&endDate=' + args.end +
              '&sortBy=occurredAt&sortOrder=DESC&useCursor=true';
      if (ts != null) {
        u += '&cursorTimestamp=' + ts + '&cursorId=' + encodeURIComponent(id);
      }
      let res;
      try { res = await fetch(u, { headers: H }); }
      catch (e) { apiError = 'network gid=' + gid + ': ' + String(e); break; }
      if (res.status === 401) return { authExpired: true };
      if (!res.ok) { apiError = 'HTTP ' + res.status + ' gid=' + gid; break; }
      const d = (await res.json()).data || {};
      const items = d.items || [];
      all.push(...items);
      pages++;
      if (!d.hasMore || !d.nextCursor || items.length === 0) break;
      ts = d.nextCursor.timestamp;
      id = d.nextCursor.id;
    }
  }
  return { ok: true, events: all, pages: args.groupIds.length, apiError: apiError };
}, __ARGS__);
return r;
"""


def _run_js(cli, code, timeout=240):
    return CF._run_cli(cli, code, timeout=timeout)


def list_groups(cfg=None, log=print):
    """列已入库 TG 群（含 messageCount）。"""
    CF.ensure_ready(cfg, log=log)
    res = _run_js(CF._pw_cli(cfg), _JS_LIST_GROUPS, timeout=60)
    if res.get("authExpired"):
        raise RuntimeError("dtt 登录已失效，请重新登录或检查本地凭据")
    if not res.get("ok"):
        raise RuntimeError("列群失败：%s" % res)
    return res.get("groups") or []


def fetch_day(day_str, cfg=None, log=print, until_now=True):
    """取某日 TG 群消息（北京时间 00:00→当前），落盘，返回 payload。"""
    start, end = _day_bounds(day_str, until_now=until_now)
    gids = _group_ids(cfg)
    cli = CF._pw_cli(cfg)
    args = {"start": start, "end": end, "groupIds": gids,
            "limit": PAGE_LIMIT, "maxPages": MAX_PAGES_PER_GROUP}
    code = _JS_FETCH.replace("__ARGS__", json.dumps(args, ensure_ascii=False))
    _end_label = ((datetime.datetime.fromtimestamp(end / 1000, TZ).strftime("%m-%d %H:%M") + "（当前）")
                  if until_now else "次日00:00")
    log("TG群取数：%s（北京时间 %s 00:00 → %s，群 %d 个，正在从 dtt 拉取…）"
        % (day_str, day_str, _end_label, len(gids)))

    CF.ensure_ready(cfg, log=log)
    res = _run_js(cli, code, timeout=300)
    if res.get("authExpired"):
        lr = CF.auto_login(cfg, log=log)
        if lr.get("ok"):
            log("dtt 会话已续期，重新取数…")
            res = _run_js(cli, code, timeout=300)
    if res.get("authExpired"):
        raise RuntimeError("dtt 登录已失效，请在 Edge 重新登录或检查本地凭据")
    if not res.get("ok"):
        raise RuntimeError("TG群取数失败：%s" % (res.get("apiError") or res))

    events = res.get("events") or []
    # 保留群名/作者/正文/时间，带北京时间人读字段
    for e in events:
        t = e.get("occurredAt")
        e["_time"] = (datetime.datetime.fromtimestamp(t / 1000, TZ).strftime("%H:%M")
                      if t else "??:??")
    events.sort(key=lambda e: e.get("occurredAt") or 0)

    by_group = {}
    for e in events:
        g = e.get("groupName") or e.get("groupId") or "未知群"
        by_group[g] = by_group.get(g, 0) + 1

    payload = {
        "day": day_str,
        "source": CF.SITE_ORIGIN + "/api/group-messages",
        "fetched_at": datetime.datetime.now(TZ).isoformat(timespec="seconds"),
        "bounds": {"start": start, "end": end, "until_now": until_now,
                   "unit": "ms"},
        "group_ids": gids,
        "pages": res.get("pages"),
        "stats": {
            "total": len(events),
            "by_group": dict(sorted(by_group.items(), key=lambda x: -x[1])),
        },
        "events": events,
    }
    out = _day_path(day_str, cfg)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out)
    log("TG群取数完成：%s 共 %d 条（%s），落盘 %s"
        % (day_str, len(events),
           " ".join("%s=%d" % (k, v) for k, v in by_group.items()), out))
    return payload


def read_day(day_str, cfg=None):
    p = _day_path(day_str, cfg)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_covers_now(payload, cfg=None, slack_sec=120):
    if not payload:
        return False
    b = payload.get("bounds") or {}
    end = b.get("end")
    if not end:
        return False
    # 本模块 bounds 是毫秒
    unit = b.get("unit", "s")
    end_sec = end / 1000 if unit == "ms" else end
    return int(datetime.datetime.now(TZ).timestamp()) - int(end_sec) <= slack_sec


if __name__ == "__main__":
    import sys
    cfg = None
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    print("已入库群：")
    for g in list_groups(cfg, log=lambda *_: None):
        print("  %-28s %s (%s 条)" % (g.get("groupName"), g.get("groupId"), g.get("messageCount")))
    print("取数：", day)
    p = fetch_day(day, cfg)
    print("统计：", p["stats"])
