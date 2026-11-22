# -*- coding: utf-8 -*-
"""Gamma 情报汇总（v1.28.0）

数据源：taostats 官方 Gamma API（/v1/gamma/subnets|activity|deposits）。
功能：开通子网列表、当前收支总览、自然周/月统计、单子网明细。
口径：本次只做机制 1（客户钱包 → 子网支付）；机制 2（子网 emission 分配）
      的 gamma_emission_shares 字段已实测可取，留作后续扩展，不在本报告混算。

单位（实测校验）：gamma/alpha/tao/tao_value 均为 rao(1e-9)，展示前 ÷1e9；
      usd_value 已是美元（四舍五入到分）；emission share 是 u64 比例 ÷(2^64-1)。

输出：数据表为主 + 一段只引用表内数字的解读（代码模板生成，不调大模型、不编造）。
"""
import datetime
import json
import urllib.parse
import urllib.request

import pipeline as P

API_BASE = "https://api.taostats.io"
_TZ8 = datetime.timezone(datetime.timedelta(hours=8))
_PAGE = 200          # taostats 单页上限
_MAX_PAGES = 20      # 防失控：单个端点最多翻 20 页（4000 行）
_U64MAX = 18446744073709551615
# runtime 493（2026-11-20）把 Gamma 付费从 spender 扣款改为客户 hotkey 自签；
# 只用于报告里标注数据截止时间，不做跨机制字段混算。
_V493_BLOCK = 9549577


def _log(fn, msg):
    if fn:
        try:
            fn(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def _api_get(cfg, path, params=None, log=None):
    """GET 一个 Gamma 端点。必带 User-Agent（实测无 UA 返回 403）；复用 pipeline 代理。"""
    key = str((cfg or {}).get("taostats_api_key") or "").strip()
    if not key:
        raise RuntimeError("未配置 taostats_api_key，无法查询 Gamma 情报")
    qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = API_BASE + path + ("?" + qs if qs else "")
    req = urllib.request.Request(url, headers={
        "Authorization": key,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    })
    P._CFG = cfg                      # net_open 的代理策略读这个全局
    raw = P.net_open(req, timeout=30, url=url)
    obj = json.loads(raw.decode("utf-8"))
    if not isinstance(obj, dict):
        raise RuntimeError(" Gamma 接口返回格式异常")
    return obj


def _api_all(cfg, path, params=None, log=None):
    """翻页取全部行。返回 (rows, total_items)。"""
    rows = []
    page = 1
    total = 0
    while page <= _MAX_PAGES:
        p = dict(params or {})
        p["page"] = page
        p["limit"] = _PAGE
        obj = _api_get(cfg, path, p, log)
        data = obj.get("data") or []
        rows.extend(data)
        pag = obj.get("pagination") or {}
        try:
            total = int(pag.get("total_items") or len(rows))
        except (TypeError, ValueError):
            total = len(rows)
        nxt = pag.get("next_page")
        if not nxt or not data:
            break
        try:
            page = int(nxt)
        except (TypeError, ValueError):
            break
    return rows, total


def fetch_subnets(cfg, log=None):
    """全部子网的 Gamma 总览（含开通标志、收支、emission 分配字段）。"""
    rows, total = _api_all(cfg, "/v1/gamma/subnets", log=log)
    _log(log, "Gamma 取数：子网 %d 行（总 %d）" % (len(rows), total))
    return rows


def fetch_activity(cfg, kind=None, ts_start=None, ts_end=None, log=None):
    """支付流水。kind=mint/spend；ts 为 Unix 秒（北京时间自然周期由调用方算）。"""
    params = {}
    if kind:
        params["type"] = kind
    if ts_start is not None:
        params["timestamp_start"] = int(ts_start)
    if ts_end is not None:
        params["timestamp_end"] = int(ts_end)
    rows, total = _api_all(cfg, "/v1/gamma/activity", params, log)
    _log(log, "Gamma 取数：流水 %s %d 行（总 %d）" % (kind or "全部", len(rows), total))
    return rows


def fetch_deposits(cfg, log=None):
    rows, total = _api_all(cfg, "/v1/gamma/deposits", log=log)
    _log(log, "Gamma 取数：deposit %d 行（总 %d）" % (len(rows), total))
    return rows


# ---------------------------------------------------------------------------
# 换算与格式化
# ---------------------------------------------------------------------------
def _num(x):
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def g_rao(x):
    """rao → Gamma。"""
    return _num(x) / 1e9


def fmt_g(x, dec=2):
    return format(g_rao(x), ",.%df" % dec)


def fmt_usd(x):
    return "%.2f" % _num(x)


def share_pct(share):
    return _num(share) / float(_U64MAX) * 100.0


def short_addr(a):
    a = str(a or "")
    if not a:
        return "未知"
    return (a[:8] + "…" + a[-6:]) if len(a) > 16 else a


def _kb():
    try:
        return P._subnet_kb()
    except Exception:
        return {}


def sn_name(sn, kb=None):
    kb = kb if kb is not None else _kb()
    subs = (kb or {}).get("subnets") or {}
    d = subs.get(str(sn)) or {}
    name = str(d.get("name") or "").strip()
    return ("SN%d %s" % (sn, name)) if name else ("SN%d" % sn)


def enabled_subnets(rows):
    """按 gamma_enabled 过滤并按花费降序。"""
    out = [d for d in rows if d.get("gamma_enabled")]
    out.sort(key=lambda d: g_rao(d.get("gamma_spent")), reverse=True)
    return out


def period_bounds(scope, now=None):
    """返回 (ts_start, ts_end, 中文标签)。now 为北京时间 datetime。"""
    now = now or datetime.datetime.now(_TZ8)
    if scope == "week":
        start = (now - datetime.timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        label = "本周（%s 周一 00:00 → 现在）" % start.strftime("%Y-%m-%d")
    elif scope == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        label = "本月（%s 00:00 → 现在）" % start.strftime("%Y-%m-%d")
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "今日（%s 00:00 → 现在）" % start.strftime("%Y-%m-%d")
    return int(start.timestamp()), int(now.timestamp()), label


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------
def _table_header():
    return "子网 | 铸造Γ | 花费Γ | 未花Γ | 烧alpha | 笔数(铸/花)"


def _table_row(sn, minted, spent, outstanding, burned, mc, sc, kb=None):
    return "%s | %s | %s | %s | %s | %d/%d" % (
        sn_name(sn, kb), fmt_g(minted), fmt_g(spent), fmt_g(outstanding),
        fmt_g(burned), int(mc or 0), int(sc or 0))


def report_list(cfg, log=None, now=None):
    """开通 Gamma 支付的子网列表（用户要的专门入口）。"""
    rows = fetch_subnets(cfg, log)
    enabled = enabled_subnets(rows)
    now = now or datetime.datetime.now(_TZ8)
    lines = ["📋 已开通 Gamma 支付的子网（共 %d 个，SN编号+名称）" % len(enabled), ""]
    lines.append("截至：%s（北京时间，链上最新区块数据）" % now.strftime("%Y-%m-%d %H:%M"))
    lines.append("")
    if not enabled:
        lines.append("当前未查到开通 Gamma 的子网。")
    else:
        for i, d in enumerate(enabled, 1):
            shares = d.get("gamma_emission_shares") or []
            extra = ""
            if shares:
                parts = ["SN%s %s" % (s.get("target_netuid"), format(share_pct(s.get("share")), ".1f") + "%")
                         for s in shares]
                extra = "；emission 分配→ " + "、".join(parts)
            lines.append("%d. %s ｜ 花费 %s Γ ｜ 未花 %s Γ%s" % (
                i, sn_name(int(d["netuid"])), fmt_g(d.get("gamma_spent")),
                fmt_g(d.get("gamma_outstanding")), extra))
    lines.append("")
    lines.append("说明：开通=子网已打开 Gamma 开关；「花费」是客户用掉的服务信用（1Γ≈1美元），")
    lines.append("      不是子网收入，也不代表外部客户真实付费（可能含内部测试）。")
    title = "📋 Gamma 开通子网列表 · %s" % now.strftime("%Y-%m-%d %H:%M")
    return {"ok": True, "title": title, "digest": "\n".join(lines),
            "kind": "list", "count": len(enabled)}


def _agg_by_sn(acts):
    """按 netuid 聚合 gamma(rao) 与笔数，返回 {sn: [sum, count]}。"""
    out = {}
    for d in acts:
        try:
            sn = int(d.get("netuid"))
        except (TypeError, ValueError):
            continue
        cell = out.setdefault(sn, [0.0, 0])
        cell[0] += g_rao(d.get("gamma"))
        cell[1] += 1
    return out


def _top_payers(acts, limit=5):
    """按付款方聚合 spend，返回 [(地址, 金额Γ, 主要子网)]。"""
    from collections import defaultdict
    by = defaultdict(lambda: [0.0, 0, {}])
    for d in acts:
        who = d.get("hotkey") or d.get("coldkey") or d.get("service") or ""
        if not who:
            continue
        try:
            sn = int(d.get("netuid"))
        except (TypeError, ValueError):
            sn = 0
        cell = by[who]
        cell[0] += g_rao(d.get("gamma"))
        cell[1] += 1
        cell[2][sn] = cell[2].get(sn, 0.0) + g_rao(d.get("gamma"))
    out = sorted(by.items(), key=lambda kv: kv[1][0], reverse=True)
    res = []
    for who, cell in out[:limit]:
        main_sn = max(cell[2].items(), key=lambda kv: kv[1])[0] if cell[2] else 0
        res.append((who, cell[0], main_sn, cell[1]))
    return res


def report_snapshot(cfg, log=None, now=None):
    """当前收支总览（累计，全网关+开通子网表 + 最近支付）。"""
    rows = fetch_subnets(cfg, log)
    enabled = enabled_subnets(rows)
    acts = fetch_activity(cfg, "spend", log=log)
    now = now or datetime.datetime.now(_TZ8)
    tot_mint = sum(g_rao(d.get("gamma_minted")) for d in enabled)
    tot_spent = sum(g_rao(d.get("gamma_spent")) for d in enabled)
    tot_out = sum(g_rao(d.get("gamma_outstanding")) for d in enabled)
    tot_burn = sum(g_rao(d.get("alpha_burned_for_gamma")) for d in enabled)

    lines = ["💰 Gamma 收支总览（累计，链上数据）", ""]
    lines.append("截至：%s（北京时间）｜口径：客户烧 alpha 铸造 Gamma 买子网服务（机制1）" %
                 now.strftime("%Y-%m-%d %H:%M"))
    lines.append("")
    lines.append("全网汇总：铸造 %s Γ ｜ 花费 %s Γ ｜ 未花 %s Γ ｜ 累计烧 alpha %s" % (
        format(tot_mint, ",.2f"), format(tot_spent, ",.2f"),
        format(tot_out, ",.2f"), format(tot_burn, ",.2f")))
    if tot_mint > 0:
        lines.append("整体花费/铸造 = %.1f%%（花费掉的信用占比）" % (tot_spent / tot_mint * 100))
    lines.append("")
    lines.append("【已开通子网收支表】（按花费降序，最多列 15 个）")
    lines.append(_table_header())
    for d in enabled[:15]:
        lines.append(_table_row(int(d["netuid"]), d.get("gamma_minted"), d.get("gamma_spent"),
                                d.get("gamma_outstanding"), d.get("alpha_burned_for_gamma"),
                                d.get("mint_count"), d.get("spend_count")))
    lines.append("")
    lines.append("【最近付款流水】（钱包 → 子网，最多 5 条）")
    for d in acts[:5]:
        try:
            sn = int(d.get("netuid"))
        except (TypeError, ValueError):
            sn = 0
        lines.append("- %s → %s：%s Γ（%s）" % (
            short_addr(d.get("hotkey") or d.get("coldkey")), sn_name(sn),
            fmt_g(d.get("gamma")), (d.get("timestamp") or "")[:16].replace("T", " ")))
    lines.append("")
    lines.append("📝 解读：Gamma 是子网的美元计价服务信用，铸造=烧 alpha 换来的额度、花费=客户实际用掉、")
    lines.append("      未花=还挂在账户里的余额；三者分开看，不能相加抵消。花费最高的子网是 %s。" %
                 (sn_name(int(enabled[0]["netuid"])) if enabled else "无"))
    lines.append("      钱包是链上地址，不等于公司或子网；本报告不推断客户身份。")
    title = "💰 Gamma 收支总览 · %s" % now.strftime("%Y-%m-%d")
    return {"ok": True, "title": title, "digest": "\n".join(lines),
            "kind": "snapshot", "count": len(enabled)}


def report_period(cfg, scope, log=None, now=None):
    """自然周/月统计：时间段内铸造、花费、按子网聚合、主要付款方。"""
    now = now or datetime.datetime.now(_TZ8)
    ts0, ts1, label = period_bounds(scope, now)
    mints = fetch_activity(cfg, "mint", ts0, ts1, log)
    spends = fetch_activity(cfg, "spend", ts0, ts1, log)
    mint_by = _agg_by_sn(mints)
    spend_by = _agg_by_sn(spends)
    sns = sorted(set(mint_by) | set(spend_by),
                 key=lambda s: spend_by.get(s, [0])[0], reverse=True)
    tot_mint = sum(v[0] for v in mint_by.values())
    tot_spend = sum(v[0] for v in spend_by.values())
    title_scope = "周" if scope == "week" else "月"

    lines = ["📅 Gamma %s统计：%s" % (title_scope, label), ""]
    lines.append("统计口径：北京时间自然%s｜铸造 %d 笔、花费 %d 笔" %
                 ("周" if scope == "week" else "月", len(mints), len(spends)))
    lines.append("区间铸造 %s Γ ｜ 花费 %s Γ" % (format(tot_mint, ",.2f"), format(tot_spend, ",.2f")))
    lines.append("")
    lines.append("子网 | 本%s铸造Γ | 本%s花费Γ | 笔数(铸/花)" % (title_scope, title_scope))
    for sn in sns[:15]:
        m = mint_by.get(sn, [0.0, 0])
        s = spend_by.get(sn, [0.0, 0])
        lines.append("%s | %s | %s | %d/%d" % (
            sn_name(sn), format(m[0], ",.2f"), format(s[0], ",.2f"), m[1], s[1]))
    if not sns:
        lines.append("（本%s无 Gamma 铸造或花费记录）" % title_scope)
    lines.append("")
    payers = _top_payers(spends, 5)
    if payers:
        lines.append("【主要付款方】（钱包 → 主要子网，本%s花费降序）" % title_scope)
        for who, amt, sn, cnt in payers:
            lines.append("- %s → %s：%s Γ（%d 笔）" % (
                short_addr(who), sn_name(sn), format(amt, ",.2f"), cnt))
    lines.append("")
    lines.append("📝 解读：本期铸造 %s Γ、花费 %s Γ；花费集中在 %s。" % (
        format(tot_mint, ",.2f"), format(tot_spend, ",.2f"),
        sn_name(sns[0]) if sns else "无"))
    lines.append("      数字取自链上本期流水；付款方为链上地址，不推断真实身份。")
    title = "📅 Gamma %s统计 · %s" % (title_scope, now.strftime("%Y-%m-%d"))
    return {"ok": True, "title": title, "digest": "\n".join(lines),
            "kind": scope, "mint": tot_mint, "spend": tot_spend}


def report_detail(cfg, sn, log=None, now=None):
    """单子网明细：累计收支 + 本期流水 + 主要付款方。"""
    now = now or datetime.datetime.now(_TZ8)
    rows = fetch_subnets(cfg, log)
    d = next((x for x in rows if int(x.get("netuid") or -1) == int(sn)), None)
    acts = fetch_activity(cfg, "spend", log=log)
    mine = [a for a in acts if str(a.get("netuid")) == str(sn)]
    lines = ["🔍 %s · Gamma 明细" % sn_name(int(sn)), ""]
    lines.append("截至：%s（北京时间）" % now.strftime("%Y-%m-%d %H:%M"))
    lines.append("")
    if not d:
        lines.append("未查到该子网的 Gamma 链上状态。")
    else:
        lines.append("开通状态：%s ｜ Gamma 价格：%s" % (
            "✅ 已开通" if d.get("gamma_enabled") else "未开通", d.get("gamma_price")))
        lines.append("累计：铸造 %s Γ ｜ 花费 %s Γ ｜ 未花 %s Γ ｜ 烧 alpha %s" % (
            fmt_g(d.get("gamma_minted")), fmt_g(d.get("gamma_spent")),
            fmt_g(d.get("gamma_outstanding")), fmt_g(d.get("alpha_burned_for_gamma"))))
        lines.append("笔数：铸造 %s ｜ 花费 %s ｜ deposit %s 个" % (
            d.get("mint_count"), d.get("spend_count"), d.get("deposit_count")))
        shares = d.get("gamma_emission_shares") or []
        if shares:
            lines.append("emission 分配：")
            for s in shares:
                lines.append("- → SN%s %s（机制2，另算）" % (
                    s.get("target_netuid"), format(share_pct(s.get("share")), ".1f") + "%"))
    lines.append("")
    lines.append("【最近付款流水】（钱包 → 本子网，最多 10 条）")
    if not mine:
        lines.append("（近期无花费记录）")
    for a in mine[:10]:
        lines.append("- %s → %s Γ（%s）" % (
            short_addr(a.get("hotkey") or a.get("coldkey")), fmt_g(a.get("gamma")),
            (a.get("timestamp") or "")[:16].replace("T", " ")))
    lines.append("")
    lines.append("📝 解读：该子网累计花费 %s Γ、未花 %s Γ；付款方均为链上地址，不推断客户身份。" % (
        fmt_g(d.get("gamma_spent")) if d else "0", fmt_g(d.get("gamma_outstanding")) if d else "0"))
    title = "🔍 Gamma 明细 · %s · %s" % (sn_name(int(sn)), now.strftime("%Y-%m-%d"))
    return {"ok": True, "title": title, "digest": "\n".join(lines), "kind": "detail", "sn": int(sn)}


def run_gamma(cfg, action="list", sn=None, log=None, push=True, now=None):
    """主入口。action: list/snapshot/week/month/detail。返回报告 dict。"""
    P._CFG = cfg
    action = (action or "list").lower()
    try:
        if action == "list":
            res = report_list(cfg, log, now)
        elif action == "snapshot":
            res = report_snapshot(cfg, log, now)
        elif action in ("week", "month"):
            res = report_period(cfg, action, log, now)
        elif action == "detail":
            if sn is None:
                raise RuntimeError("单子网明细需要指定 SN 编号")
            res = report_detail(cfg, sn, log, now)
        else:
            raise RuntimeError("未知 Gamma 查询类型：%s" % action)
    except Exception as e:
        _log(log, "Gamma 情报查询失败：%s" % e)
        return {"ok": False, "error": str(e), "title": "⚠️ Gamma 情报查询失败",
                "digest": "Gamma 情报查询失败：%s\n请检查 taostats_api_key 与网络代理后重试。" % e,
                "kind": action}

    if push:
        try:
            P.push_feishu(cfg, res["title"], res["digest"])
        except Exception as e:
            _log(log, "Gamma 飞书推送失败（不影响 TG）：%s" % e)
        try:
            P.push_telegram(cfg, res["title"], res["digest"])
        except Exception as e:
            _log(log, "Gamma TG 推送失败：%s" % e)
    return res
