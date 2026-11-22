# -*- coding: utf-8 -*-
"""子网重要事件投研（Subnet Research）

触发方式：TG `/投研 29` 或 CLI `python pipeline.py research 29`。

数据链路（三源交叉，缺一不编）：
  1. **本地档案**（subnet_archive/SNxx/archive.json）——群里真实讨论，按日分段；
  2. **链上/数据站**（taostats、bittensor.ai、subnetstats、dtaoscan）——价格、emission、
     TVL、注册状态、owner 等客观数据，尽量抓结构化字段；
  3. **公开信息**（官网、GitHub releases/commits、Google News、DuckDuckGo、
     X 账号经 r.jina.ai 镜像）——官方公告、版本发布、媒体报道与团队表态。

产出：
  subnet_archive/SNxx/research_YYYY-MM-DD.md
  分「事实基线 / 时间线 / 交叉验证矩阵 / 群内预期差 / 风险与未证实 / 跟踪清单」六段，
  每条事实必须带来源标签；三源不一致时**并列呈现**并标注冲突，绝不替读者下结论式合并。

红线（与日报同一套）：
  - 抓不到就写「未获取」，绝不虚构数字或公告；
  - 群内传闻 vs 官方信息必须分栏，不得混写；
  - 价格/emission 等数字只允许来自抓到的页面原文，模型不得改算。
"""

import datetime
import difflib
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import subnet_archive as A

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = A.TZ
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}


# ------------------------------------------------------------------ 抓取底层
def _cfg():
    try:
        import pipeline
        return pipeline._CFG or {}
    except Exception:
        return {}


def _opener(url):
    """兼容旧调用：只取代理通道；新代码请直接用 pipeline.net_open。"""
    try:
        import pipeline
        return pipeline._opener_for(url)
    except Exception:
        return urllib.request.build_opener()


def fetch(url, timeout=25, limit=3_000_000, retries=2):
    """抓取 URL，返回 (ok, text_or_err)。任何异常都不抛出，交给上层降级。

    v1.13.0：境外站点（taostats / GitHub / X / Google News 等）统一走
    pipeline.net_open 的「代理优先、直连兜底」，不再单通道硬撞。
    """
    last = ""
    for i in range(max(1, retries)):
        try:
            import pipeline
            req = urllib.request.Request(url, headers=UA)
            if pipeline._is_direct_host(pipeline._host_of(url)):
                with _opener(url).open(req, timeout=timeout) as r:
                    raw = r.read(limit)
            else:
                raw = pipeline.net_open(req, timeout=timeout, url=url, limit=limit)
            for enc in ("utf-8", "gb18030", "latin-1"):
                try:
                    return True, raw.decode(enc)
                except Exception:
                    continue
            return True, raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (401, 403, 404, 410):
                break
        except Exception as e:
            last = str(e)[:160]
    return False, last


def to_text(h):
    """HTML → 纯文本（丢 script/style/svg，压缩空白）。"""
    h = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", h or "")
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", html.unescape(h)).strip()


def _snip(text, pat, n=180, flags=re.I):
    m = re.search(pat, text or "", flags)
    if not m:
        return ""
    lo, hi = max(0, m.start() - n // 3), min(len(text), m.end() + n)
    return text[lo:hi].strip()


# ------------------------------------------------------------------ 数据源
def _subnet_meta(sn):
    kb = A.load_kb()
    v = (kb.get("subnets") or {}).get(str(sn)) or {}
    return {
        "sn": int(sn),
        "name": A.real_name(v),
        "desc": A.real_desc(v),
        "url": A.real_url(v.get("url")),
        "github": A.real_url(v.get("github")),
        "raw_desc": (v.get("desc") or "").strip(),
        # 知识库快照时间：subnets.json 是离线抓的，子网改名后不会自动更新。
        # 报告里要能说清「这个名字是哪天的快照」，否则错误名称会被当成事实引用。
        "kb_seen": (v.get("last_seen") or "").strip(),
        "aliases": list(v.get("aliases") or []),
    }


def chain_name(sn, log=print):
    """从 taostats 链上身份取子网**现名**。

    为什么必须单独取一次：subnets.json 是离线快照（靠 `python update_subnets.py`
    手动更新），子网一旦改名，知识库会一直留在旧名上。SN127 就是实例——
    链上早已是 Zils，知识库仍是 Astrid（last_seen 停在 11-02），
    于是投研报告整篇按 Astrid 写，用户看到的就是错名。
    链上身份是权威来源，优先于本地知识库。

    返回 {"name","desc","url","github"} 或 None（抓不到就退回知识库，不阻断投研）。
    """
    try:
        import update_subnets as U
        recs = U.fetch_identities(timeout=45)
    except Exception as e:
        log("链上身份核对跳过（%s）" % e)
        return None
    rec = recs.get(int(sn))
    if not rec:
        return None
    return {
        "name": (rec.get("name") or "").strip(),
        "desc": (rec.get("desc") or "").strip(),
        "url": (rec.get("url") or "").strip(),
        "github": (rec.get("github") or "").strip(),
    }


def _names_in_feedback(feedback):
    """从用户反馈里揪出「新名字」候选。

    典型句式：「127 已经改名为 Zils，怎么你总结出来的还是 Astrid」
    这里只做保守抽取 —— 抓「改名为 X」「叫 X」「是 X」后面的那个英文词，
    并排除用户自己提到的旧名（「还是 Astrid」里的 Astrid 不该被当成新名）。
    抓不准就不抓：宁可让模型去理解整句，也不要错误改名。
    """
    s = (feedback or "")
    cands = []
    for pat in (r"改名为\s*([A-Za-z][\w.\-]{1,30})",
                r"改叫\s*([A-Za-z][\w.\-]{1,30})",
                r"现名(?:是|叫)?\s*([A-Za-z][\w.\-]{1,30})",
                r"现在(?:叫|是)\s*([A-Za-z][\w.\-]{1,30})"):
        for m in re.finditer(pat, s, re.I):
            w = m.group(1).strip().rstrip("。，,.、")
            if w and w not in cands:
                cands.append(w)
    if not cands:
        return []
    # 排除「还是/仍然 + 旧名」这类句式里出现的词
    olds = set(w.lower() for w in re.findall(r"(?:还是|仍然是|依旧是)\s*([A-Za-z][\w.\-]{1,30})", s, re.I))
    out = [w for w in cands if w.lower() not in olds]
    return out or cands[:1]


def apply_chain_name(meta, chain, feedback=None, log=print):
    """用链上现名校正 meta；名字变了要说出来，不能默默替换。

    三种情况：
    1. 本地知识库和链上不一致 → 以链上为准，旧名进 aliases，并在 meta 里留
       `name_changed_from`，提示词会要求报告点明「改名」。
    2. 只靠用户反馈才知道新名（抓取失败时）→ 同样采信用户，标记来源为 feedback。
    3. 两边一致 → 什么都不做。
    """
    if not meta.get("name"):
        if chain and chain.get("name"):
            meta["name"] = chain["name"]
            log("   名称：知识库无记录，采用链上名「%s」" % chain["name"])
        return meta

    names_chain = (chain or {}).get("name") or ""
    if names_chain and A.ascii_name(names_chain).lower() != A.ascii_name(meta["name"]).lower():
        meta["name_changed_from"] = meta["name"]
        meta["name_change_source"] = "链上身份（taostats）"
        meta["aliases"] = sorted(set((meta.get("aliases") or []) + [meta["name"]]))
        log("   ⚠️ 名称不一致：知识库「%s」（快照 %s）→ 链上现名「%s」，报告以链上为准"
            % (meta["name"], meta.get("kb_seen") or "未知", names_chain))
        meta["name"] = names_chain
        # 改名往往连官网/GitHub/描述一起换（SN127：astrid.global → zils.ai）。
        # 不跟着换会抓错站：后续官网/GitHub/搜索/X 全按旧地址走，新项目的事实一条也抓不到。
        for k, label in (("url", "官网"), ("github", "仓库"), ("desc", "业务描述")):
            new = (chain or {}).get(k) or ""
            old = meta.get(k) or ""
            if new and A.real_url(new) != A.real_url(old):
                meta[k] = A.real_url(new) if k in ("url", "github") else new
                log("   ↻ %s 同步为链上值：%s → %s" % (label, old or "（空）", meta[k]))
    return meta



def enrich_meta(meta, sources, log=print):
    """知识库没收录 / 信息残缺时，从数据站反查补齐名称与官网。

    纯全网投研必须能处理「知识库没收录的编号」：先拿 bittensor.ai / dtaoscan 页面里的
    标题与链接补全 name / url / github，后面的搜索、X 定位、GitHub 抓取才有依据。
    只补空白字段，不覆盖知识库已有值。"""
    if meta.get("name") and meta.get("url"):
        return meta
    b = sources.get("bittensor.ai") or {}
    d = sources.get("dtaoscan") or {}
    for r in (b, d):
        ex = r.get("excerpt") or ""
        if not meta.get("name"):
            # 页面开头一般是「<名字> (SNxx) — ...」或「<名字> SN 29 ...」
            m = re.search(r"(?m)^([A-Za-z][\w.\-\u00c0-\u024f ]{2,40}?)\s*\(?\s*SN\s?%d\b"
                          % meta["sn"], ex)
            if not m:
                m = re.search(r"([A-Za-z][\w.\-\u00c0-\u024f]{2,30})\s+SN\s+%d" % meta["sn"], ex)
            if m:
                cand = m.group(1).strip()
                if cand.lower() not in ("home", "subnets", "overview", "trade", "price"):
                    meta["name"] = cand
                    log("投研：知识库无名称，从%s页面反查为「%s」"
                        % (r.get("src", "数据站"), cand))
        if not meta.get("url"):
            m = re.search(r"https?://(?:www\.)?([a-z0-9\-]{2,30}\.(?:ai|io|com|xyz|net|org|studio))",
                          ex, re.I)
            if m and "bittensor" not in m.group(1).lower() and "taostats" not in m.group(1).lower():
                meta["url"] = "https://" + m.group(1)
                log("投研：知识库无官网，从页面反查为 %s" % meta["url"])
    return meta


def src_taostats(sn):
    """taostats 子网页：TAO 价、子网价、注册与 emission。直连经常被 TLS 掐断，回退 jina 镜像。"""
    ok, h = fetch("https://taostats.io/subnets/%d" % sn)
    via = "直连"
    if not ok:
        ok, h = fetch("https://r.jina.ai/https://taostats.io/subnets/%d" % sn, timeout=40)
        via = "jina镜像"
    if not ok:
        return {"src": "taostats", "ok": False, "err": h}
    t = to_text(h)
    facts = {}
    for key, pat in (
        ("tao_price", r"\$\s*([0-9][0-9,]*\.?[0-9]*)\s*[-+][0-9.]+%"),
        ("alpha_price", r"([0-9.]+)\s*·\s*SN\s?\d+"),
        ("emission", r"(?:emission|Emission)[^0-9]{0,24}([0-9.,]+\s*τ?(?:\s*/\s*day)?)"),
        ("tvl", r"(?:TVL|Liquidity)[^0-9]{0,24}([0-9.,]+[KMB]?\s*τ?)"),
        ("reg_cost", r"[Rr]egistration[^0-9]{0,24}([0-9.]+\s*τ)"),
    ):
        m = re.search(pat, t)
        if m:
            facts[key] = m.group(1).strip()
    return {"src": "taostats", "ok": bool(facts), "via": via, "facts": facts,
            "excerpt": t[:1600], "err": "" if facts else "页面可访问但未解析到指标"}


def src_bittensor_ai(sn):
    """bittensor.ai 子网页（结构化度最高：价格/24h/7d/市值/脑神经元/注册状态）。"""
    ok, h = fetch("https://bittensor.ai/subnets/%d" % sn)
    if not ok:
        return {"src": "bittensor.ai", "ok": False, "err": h}
    t = to_text(h)
    facts = {}
    for key, pat in (
        ("price", r"Price\s*([0-9.]+)\s*τ"),
        ("chg24", r"([+\-][0-9.]+)%\s*24h"),
        ("chg7d", r"([+\-][0-9.]+)%\s*7d"),
        ("mcap", r"(?:Market cap|MCap)\s*([0-9.,]+[KMB]?)\s*τ"),
        ("volume", r"Volume\s*\(24h\)\s*([0-9.,]+[KMB]?)\s*τ"),
        ("tvl", r"TVL\s*([0-9.,]+[KMB]?)\s*τ"),
        ("neurons", r"Neurons\s*([0-9,]+)\s*/\s*([0-9,]+)"),
        ("reg", r"Registration\s*(Open|Closed)"),
    ):
        m = re.search(pat, t, re.I)
        if m:
            facts[key] = " / ".join(g for g in m.groups() if g)
    return {"src": "bittensor.ai", "ok": bool(facts), "facts": facts, "excerpt": t[:1600],
            "err": "" if facts else "未解析到指标"}


def _rsc_grab(html, key):
    """从 Next.js RSC payload 里抠出一个 JSON 值（对象/数组）。

    bittensor.ai 是 RSC 流式渲染，数据不在 DOM 里而是在
    `<script>self.__next_f.push([1,"..."])</script>` 的转义字符串里：
        \"ownerBuybacks\":{\"netuid\":51,...}
    所以既要还原 \" 转义，又要让 JSON 解析器自己判断边界
    （手动括号配平会被外层 [1,"..."] 的方括号带偏，实测会多吞一大段）。
    """
    if not html:
        return None
    for k in ('\\"%s\\":' % key, '"%s":' % key):
        i = html.find(k)
        if i < 0:
            continue
        raw = html[i + len(k):].replace('\\"', '"').replace('\\\\', '\\')
        try:
            return json.JSONDecoder().raw_decode(raw)[0]
        except Exception:
            continue
    return None


def _rao(v, scale=1e9):
    """把 RSC 里的 rao 整数字符串换成可读数值（1 TAO = 1e9 rao）。"""
    try:
        return float(v) / scale
    except Exception:
        return 0.0


# bittensor.ai 的「Revenue / Recycle」定义（页面原文，原文照抄便于溯源）：
#   The owner's emission cut at today's price, over the TAO recycled for
#   registrations that day. Excludes the owner's validator dividends.
# 也就是说：收入看 owner cut 抽到的 alpha，回购看 ownerBuybacks。


def _income_from_page(html):
    """从 bittensor.ai 页面正文里取「收入/分配」相关数字。

    页面结构（实测 SN64）：
        Owner Cut 18.00%
        Daily distribution ... 7,200.0000 ش /day ≈ 489.9801 τ
        Miners 43.84% 3,156.1963 ش ≈ 214.7880 τ /day
        Validators 38.16% 2,747.8367 ش ≈ 186.9980 τ /day
        Owner 18.00% 1,295.9670 ش ≈ 88.1942 τ /day
        Revenue / Recycle   No recycle last day
    """
    t = to_text(html)
    out = {}

    m = re.search(r"Owner Cut\s*([0-9.]+)%", t)
    if m:
        out["owner_cut_pct"] = m.group(1)

    m = re.search(r"Daily distribution[^0-9]{0,80}([0-9.,]+)\s*\S+\s*/day\s*≈\s*([0-9.,]+)\s*τ", t)
    if m:
        out["daily_alpha"] = m.group(1).replace(",", "")
        out["daily_tao"] = m.group(2).replace(",", "")

    # 三个分配方（矿工/验证人/所有者），取各自 alpha 与 TAO
    for label, key in (("Miners", "miner"), ("Validators", "validator"), ("Owner", "owner")):
        m = re.search(r"%s\s+([0-9.]+)%%\s*([0-9.,]+)\s*\S+\s*≈\s*([0-9.,]+)\s*τ" % label, t)
        if m:
            out[key + "_pct"] = m.group(1)
            out[key + "_alpha"] = m.group(2).replace(",", "")
            out[key + "_tao"] = m.group(3).replace(",", "")

    # Revenue / Recycle 一句话结论（"No recycle last day" / "1.23×"）
    m = re.search(r"Revenue\s*/\s*Recycle\s*(.{0,60}?)(?=\s*Emissions|\s*$)", t, re.S)
    if m:
        out["rev_recycle"] = " ".join(m.group(1).split())[:60]

    return out


def src_income(sn):
    """子网收入 + 回购（这是「子网重大事件总结」的收入模块数据源）。

    为什么要单独一个源：dtaoscan/bittensor.ai 的常规 facts 里**没有**收入与回购，
    它们藏在 bittensor.ai 的 RSC payload（ownerBuybacks / netEmission）和
    资金流图（buckets[].flows.protocolBuy）里，必须单独解析。

    返回结构：
        owner_cut_pct   所有者抽成比例（收入分配基准）
        owner_tao_day   所有者每日抽成（TAO，页面当日价折算）—— 收入口径
        daily_tao       子网每日总分配（TAO）
        burn/pool       所有者回购：30 天 + 终身（alpha 与 TAO 两套口径）
        protocol_buy    协议回购：逐日 flows.protocolBuy（TAO）与累计
        burned_pct      累计销毁占供应比
        trend           收入/回购的逐日序列（供模型判断趋势）
    """
    ok, h = fetch("https://bittensor.ai/subnets/%d" % sn, timeout=30)
    if not ok:
        return {"src": "收入/回购", "ok": False, "err": h}
    out = {"src": "收入/回购", "ok": False, "sn": sn, "err": ""}

    out.update(_income_from_page(h))

    ob = _rsc_grab(h, "ownerBuybacks")
    if isinstance(ob, dict):
        burn = ob.get("burn") or {}
        pool = ob.get("poolBuys") or {}
        out["ob"] = {
            "burn_alpha_30d": _rao(burn.get("alpha30dRao")),
            "burn_tao_30d": _rao(burn.get("tao30dRao")),
            "burn_alpha_life": _rao(burn.get("lifetimeAlphaRao")),
            "burn_tao_life": _rao(burn.get("lifetimeTaoRao")),
            "burn_count_life": burn.get("lifetimeCount") or 0,
            "pool_alpha_30d": _rao(pool.get("alpha30dRao")),
            "pool_tao_30d": _rao(pool.get("tao30dRao")),
            "pool_alpha_life": _rao(pool.get("lifetimeAlphaRao")),
            "pool_tao_life": _rao(pool.get("lifetimeTaoRao")),
            "pool_share_30d": pool.get("shareOfOwnerCut30d") or 0,
        }

    ne = _rsc_grab(h, "netEmission")
    if isinstance(ne, dict):
        w = ne.get("windows") or {}
        out["emission"] = {}
        for k in ("24h", "7d", "30d"):
            x = w.get(k) or {}
            out["emission"][k] = {
                "net_alpha": _rao(x.get("net_alpha")),
                "burned_alpha": _rao(x.get("burned_alpha")),
                "recycled_alpha": _rao(x.get("recycled_alpha")),
            }
        out["burned_pct"] = ne.get("cumulative_burned_pct_of_supply")

    # 协议回购：资金流图逐日数据（30 天）
    buckets = _rsc_grab(h, "buckets")
    if isinstance(buckets, list) and buckets:
        series = []
        for b in buckets:
            if not isinstance(b, dict):
                continue
            fl = b.get("flows") or {}
            # 注意：flows 里的金额**已经是 TAO/alpha 浮点数**，不能再除 1e9。
            # （只有 ownerBuybacks / netEmission 里的 *Rao 字段才是 rao 整数。
            #   这里早期版本误用了 _rao()，261.29 τ 被压成 0.00000026 → round 成 0，
            #   表现是「有回购的子网也显示 30 天全零」。）
            def _f(x):
                try:
                    return round(float(x), 2)
                except Exception:
                    return 0.0
            series.append({
                "d": (b.get("start") or "")[:10],
                "pb": _f(fl.get("protocolBuy")),      # 协议回购 TAO
                "ps": _f(fl.get("protocolSell")),
                "ub": _f(fl.get("userBuy")),
                "us": _f(fl.get("userSell")),
                "px": b.get("priceClose"),
            })
        out["series"] = series
        days_with_buy = [s for s in series if s["pb"] > 0]
        out["protocol_buy"] = {
            "days": len(series),
            "sum": round(sum(s["pb"] for s in series), 2),
            "avg": round(sum(s["pb"] for s in series) / max(1, len(series)), 2),
            "active_days": len(days_with_buy),
            "zero_days": len(series) - len(days_with_buy),
            "first": series[0]["d"], "last": series[-1]["d"],
            # 前后对比：把窗口切成两半看趋势（前 15 天 vs 后 15 天）
            "half_early": round(sum(s["pb"] for s in series[:len(series)//2]), 2),
            "half_late": round(sum(s["pb"] for s in series[len(series)//2:]), 2),
        }

    out["ok"] = bool(out.get("owner_tao_day") or out.get("ob") or out.get("protocol_buy"))
    if not out["ok"]:
        out["err"] = "页面可访问但未解析到收入/回购字段"
    return out


def src_dtaoscan(sn):
    """dtaoscan：这个站信息最全 —— alpha 价格、池子深度、emission、
    神经元占用、owner Conviction 锁仓（官方动作的强证据）。"""
    ok, h = fetch("https://dtaoscan.io/subnet/%d" % sn)
    if not ok:
        return {"src": "dtaoscan", "ok": False, "err": h}
    t = to_text(h)
    facts = {}
    pats = (
        ("price", r"pool-implied price\s*([0-9.]+)\s*TAO"),
        ("price_usd", r"\$([0-9.,]+)\s*\(TAO/USD"),
        ("emission", r"emissions\s*~?([0-9.,]+\s*alpha/day)"),
        ("emission_usd", r"\(~\$([0-9.,]+)/day"),
        ("pool_tao", r"TAO in pool\s*([0-9,]+)\s*\$?[0-9,]*(?!\d)"),
        ("pool_usd", r"TAO in pool\s*[0-9,]+\s*\$([0-9,]+)"),
        ("alpha_staked", r"alpha out \(staked\)\s*([0-9,]+)"),
        ("neurons", r"neurons\s*([0-9,]+\s*/\s*[0-9,]+)"),
        ("tempo", r"tempo\s*([0-9,]+ blocks?)"),
        ("owner_locked", r"owner locked\s*([0-9,]+ alpha)"),
        ("unwinding", r"unwinding\s*([0-9,]+\s*alpha(?:\s*in a decaying lock)?)"),
        ("unwind_pct", r"\(([0-9.]+)%\s*of position\)"),
        # 注意：不能用 [^.] 收尾 —— "39.9%" 里的小数点会把状态串截断。
        # 也不能让 lookahead 只写 "\s+Decaying"：正则开了 re.I，正文里的
        # "no decaying lock"（正常状态）会被当成段尾，把值截成 "owner locked in · no"。
        ("owner_status", r"status\s*(owner (?:locked in|unwinding)\b[^\n]{0,110}?)(?=\s+read from|\s+Decaying\s*lock\s*=|$)"),
    )
    for key, pat in pats:
        m = re.search(pat, t, re.I)
        if m:
            facts[key] = " ".join(g for g in m.groups() if g)
    # 一句话业务摘要（站点自己写的，属于外部观点，报告里要标来源）
    blurb = _snip(t, r"is a (?:smaller|larger|competitive)?[^.]{20,200}\.", n=40)
    return {"src": "dtaoscan", "ok": bool(facts), "facts": facts,
            "blurb": blurb, "excerpt": t[:2200],
            "err": "" if facts else "未解析到指标"}


def src_website(meta):
    """官网首页：标题 + 摘要 + 是否有更新公告。"""
    url = meta.get("url") or ""
    if not url:
        return {"src": "官网", "ok": False, "err": "知识库无官网地址"}
    if not url.startswith("http"):
        url = "https://" + url
    ok, h = fetch(url)
    if not ok:
        return {"src": "官网", "ok": False, "err": h, "url": url}
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", h)
    if m:
        title = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
    desc = ""
    m = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', h)
    if m:
        desc = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
    body = to_text(h)[:1200]
    return {"src": "官网", "ok": True, "url": url, "title": title,
            "desc": desc, "excerpt": body,
            # 原始 HTML 留着，供 src_x 从页面里捞官方 X 账号链接
            "html": h[:400000]}


# ---------------------------------------------------------------- GitHub 摘要
# 用户要求：GitHub 那条必须说清「提交几次 + 主要更新了什么」，而且**不要英文**。
# 模型只要看得到英文 commit 标题就一定会抄（实测输出过
# 「最近提交集中在当日（arena observation / owner provision 相关 fix）」
# 以及整屏的 `Merge pull request #1177 from…`）。所以在**素材层**就把提交压成中文，
# 英文原文根本不进入模型的输入——这比在提示词里劝阻有效得多。
_GH_MERGE_RE = re.compile(r"^\s*merge\b", re.I)

# 提交意图分类（按动词/关键词判定，取命中最多的两类写成「以…为主」）
_GH_KINDS = (
    ("修复", r"\b(fix|fixes|fixed|fixing|bug|bugs|hotfix|patch|patches|resolve|resolves|resolved|correct|corrects)\b"),
    ("新增", r"\b(add|adds|added|adding|implement|implements|implemented|introduce|introduces|support|supports|new|create|creates|enable|enables)\b"),
    ("优化", r"\b(update|updates|updated|improve|improves|improved|enhance|enhances|refine|refines|adjust|adjusts|tune|tunes|tweak|tweaks|polish)\b"),
    ("重构整理", r"\b(refactor|refactors|refactoring|rename|renames|move|moves|clean|cleanup|reorganize|simplify|simplifies|extract|extracts|split)\b"),
    ("测试", r"\b(test|tests|testing|spec|specs|coverage|fixture|fixtures)\b"),
    ("文档", r"\b(docs?|readme|comment|comments|changelog|docstring)\b"),
    ("性能", r"\b(perf|performance|optimi[sz]e|optimi[sz]ation|speed|faster|cache|caching)\b"),
    ("依赖与配置", r"\b(bump|bumps|deps?|dependenc\w*|workflow|workflows|ci|lint|linter|chore|config|release|releases|version|versions)\b"),
)

# 业务主题（命中即计入；一个标题可命中多个，按频次取前三个）
_GH_TOPICS = (
    ("竞技场", r"\barena\b"),
    ("观测数据回执", r"\bobservation|acknowledg\w*|\back\b"),
    ("评审判定", r"\bjudge|judging|judgement\b"),
    ("引用校验", r"\bcitation|citations|cite|cites\b"),
    ("证据与溯源", r"\bevidence|grounded|excerpt|excerpts|provenance\b"),
    ("验证器", r"\bverif\w*|validator|validators\b"),
    ("供应方", r"\bprovider|providers\b"),
    ("租约与到期回收", r"\blease|leases|expire|expires|expired|expiry|recover|recovery\b"),
    ("所有者", r"\bowner|owners\b"),
    ("预算与额度", r"\bbudget|budgets|spend|spending|quota|cost|costs\b"),
    ("意图识别", r"\bintent|intents|icp\b"),
    ("矿池与槽位", r"\bpool|pools|slot|slots\b"),
    ("做市与费率", r"\bmarket making|maker|fee|fees|spread|quote|quotes\b"),
    ("模拟与回测", r"\bsimulat\w*|backtest|backtesting\b"),
    ("注册与质押", r"\bregist\w*|stake|stakes|staking|burn\b"),
    ("接口与网关", r"\bapi|apis|mcp|endpoint|endpoints|client|clients|gateway\b"),
    ("前端界面", r"\bui|frontend|dashboard|page|pages|component|components\b"),
    ("数据库", r"\bdb|database|migration|migrations|schema|query|queries\b"),
    ("模型推理", r"\bllm|llms|model|models|inference|prompt|prompts|embedding|embeddings\b"),
)


_GH_EMBED = re.compile(
    r'<script type="application/json" data-target="react-app\.embeddedData">(.*?)</script>', re.S)


def _gh_embedded(html_text):
    """从 commits 页面 HTML 里取出内嵌 JSON 的 commits 路由对象。

    为什么不用 commits.atom（2026-11-10 实测）：
    atom 一页**固定 20 条**，SN71 这种一天 35 次提交的仓库，一页只覆盖大半天，
    会把「近 14 天提交次数」低报成 20；而 HTML 页面内嵌的 JSON 一页给 35 条，
    还带 `hasNextPage` 与 `endCursor`，能真正翻到窗口起点。

    两个 URL 的内嵌 key 不一样，必须都认（一开始只认后者，导致第 2 页起全空）：
      · `/owner/repo/commits`        → payload.commitsRoute
      · `/owner/repo/commits/main`   → payload.commitsRefRoute
    """
    m = _GH_EMBED.search(html_text or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except Exception:
        return None
    p = d.get("payload") or {}
    for k in ("commitsRefRoute", "commitsRoute", "commits"):
        r = p.get(k)
        if isinstance(r, dict):
            return r
    return None


def _gh_parse_route(route):
    """commitsRefRoute → [{title, at}]，按 committedDate 取日期。"""
    out = []
    for g in route.get("commitGroups") or []:
        for c in g.get("commits") or []:
            at = (c.get("committedDate") or c.get("authoredDate") or "")[:10]
            title = (c.get("shortMessage") or "").strip()
            if title:
                out.append({"title": title, "at": at})
    return out


def _gh_get(url, tries=3, sleep=2):
    """取 GitHub 页面，容忍 502/代理抖动。

    实测（2026-11-10）：本机直连 github.com 会间歇性 502 Bad Gateway 或
    IncompleteRead，同一个 URL 隔几秒重试就能过。单次失败就放弃会把
    「提交次数」统计成 0 或严重低估，所以这里单独给一条短重试。
    """
    last = ""
    for i in range(max(1, tries)):
        ok, x = fetch(url, timeout=20, retries=1)
        if ok and len(x) > 20000 and _GH_EMBED.search(x):
            return True, x
        last = x if not ok else "页面无内嵌数据"
        if i + 1 < tries:
            time.sleep(sleep * (i + 1))
    return False, last


def _gh_pages(owner, repo, pages=3, today=None, days=14):
    """抓 commits 列表，翻到时间窗起点就停。

    两条路径互补：① HTML 内嵌 JSON（一页 35 条 + cursor，首选）；
    ② commits.atom（一页 20 条，HTML 拿不到时兜底）。
    合并去重后返回，下游按日期过滤，所以多抓不会污染结果。

    页数上限 3：实测 4 页要 98 秒（每页 20-25 秒），而 3 页已覆盖
    SN71 近 14 天全部提交（129 次）。再多翻只是把旧提交也拉进来。
    """
    base = "https://github.com/%s/%s/commits" % (owner, repo)
    out, seen = [], set()
    cut = ""
    if today:
        cut = (today - datetime.timedelta(days=int(days or 14) + 7)).strftime("%Y-%m-%d")
    url = base
    more = False        # 还有下一页没翻完（说明统计值只是下限）
    for _p in range(max(1, int(pages))):
        ok, x = _gh_get(url, tries=2)
        if not ok:
            break
        route = _gh_embedded(x)
        rows = _gh_parse_route(route) if route else []
        if not rows:
            break
        new = 0
        for c in rows:
            k = (c["at"], c["title"])
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
            new += 1
        pg = ((route.get("filters") or {}).get("pagination") or {})
        cur = pg.get("endCursor") or ""
        more = bool(pg.get("hasNextPage") and cur)
        if not more or new == 0:
            break
        if cut:
            # 本页已翻到窗口之前 → 没必要再翻（省时间，也少触发 GitHub 限流）
            got = [c["at"] for c in rows if c["at"]]
            if got and min(got) < cut:
                more = False        # 是主动停的，不是没翻完
                break
        url = base + "?after=" + urllib.parse.quote(cur, safe="")
    if out:
        return out, more
    # 兜底一：atom（一页 20 条）。翻页全失败时，这 20 条里最新的那条
    # 就是「最后一次更新」，哪怕一个月内的次数统计不出来，这一条也必须在。
    for uu in ("https://github.com/%s/%s/commits.atom" % (owner, repo),
               "https://github.com/%s/%s/commits/main.atom" % (owner, repo)):
        ok, x = fetch(uu, timeout=15, retries=2)
        if not ok:
            continue
        for it in re.findall(r"<entry>(.*?)</entry>", x, re.S):
            ti = re.search(r"<title>(.*?)</title>", it, re.S)
            up = re.search(r"<updated>(.*?)</updated>", it)
            title = html.unescape(re.sub(r"<.*?>", "", ti.group(1))).strip() if ti else ""
            at = (up.group(1) if up else "")[:10]
            if title and (at, title) not in seen:
                seen.add((at, title))
                out.append({"title": title, "at": at})
        if out:
            return out, True        # atom 只有 20 条，一定是被截断的
    # 兜底二：连 atom 都不通时，如果手里有 release，就把发版日当最后一次更新。
    # 这样「最后一次更新」永远不会因为翻页异常而整个消失。
    return out, False


def gh_summary(commits, releases=None, days=30, today=None, truncated=False, shown=None):
    """把提交列表压成「最后一次更新什么时候 / 近一个月几次 / 最后一次改了什么」。

    用户明确指定的三条口径（2026-11-10）：
      「翻页有限制，就统计最后一次更新什么时候，近一个月更新几次，最后一次更新什么内容」
    所以摘要的主语是**最后一次提交**，不是「提交集中在哪天」——
    翻页拿到多少算多少，拿到的是下限，就老实写「至少 N 次」，绝不假装是全量。

    返回 dict，含 sentence（可直接写进报告的一句中文）。
    """
    if today is None:
        today = datetime.datetime.now(TZ).date()
    if not isinstance(today, datetime.date):
        today = datetime.datetime.now(TZ).date()
    days = int(days or 30)
    lo = today - datetime.timedelta(days=days)
    rel = releases or []
    sm = {"ok": True, "days": days, "total": 0, "merge": 0, "slim": 0, "shown": shown or 0,
          "per_day": {}, "first": "", "last": "", "peak_day": "", "peak_n": 0,
          "kinds": [], "topics": [], "last_commit": "", "truncated": bool(truncated),
          "last_detail": "", "active_days": 0, "releases": [], "sentence": "", "line": ""}
    rows = []
    for c in commits or []:
        at = (c.get("at") or "")[:10]
        try:
            d = datetime.datetime.strptime(at, "%Y-%m-%d").date()
        except Exception:
            continue
        rows.append((d, c.get("title") or ""))
    rows.sort(key=lambda r: r[0])
    if rows:
        sm["last_commit"] = rows[-1][0].strftime("%m-%d")
    inwin = [(d, t) for d, t in rows if lo <= d <= today]
    sm["total"] = len(inwin)
    merge_n = sum(1 for _d, t in inwin if _GH_MERGE_RE.match(t))
    sm["merge"] = merge_n
    sm["slim"] = len(inwin) - merge_n
    per = {}
    for d, _t in inwin:
        per[d] = per.get(d, 0) + 1
    sm["per_day"] = {k.strftime("%m-%d"): v for k, v in sorted(per.items())}
    sm["active_days"] = len(per)
    if per:
        pk = max(per.items(), key=lambda kv: (kv[1], kv[0]))
        sm["peak_day"], sm["peak_n"] = pk[0].strftime("%m-%d"), pk[1]
        sm["first"] = min(per).strftime("%m-%d")
        sm["last"] = max(per).strftime("%m-%d")
    # 最后一次真实改动（跳过纯合并提交，它们没有独立语义）
    real_all = [(d, t) for d, t in rows if not _GH_MERGE_RE.match(t)]
    if real_all:
        ld, lt = real_all[-1]
        ph = _gh_topic_phrase([lt])
        sm["last_detail_at"] = ld.strftime("%m-%d")
        if ph:
            sm["last_detail"] = "、".join(k for k, _n in ph[:2]) + "相关改动"
        else:
            kp = _gh_kind_phrase([lt])
            sm["last_detail"] = ("%s类改动" % kp[0][0]) if kp else ""
    # 统计口径用窗口内的真实改动
    real = [t for _d, t in inwin if not _GH_MERGE_RE.match(t)]
    sm["kinds"] = _gh_kind_phrase(real)
    sm["topics"] = _gh_topic_phrase(real)
    rl = []
    for r in rel[:6]:
        at = (r.get("at") or "")[:10]
        try:
            d = datetime.datetime.strptime(at, "%Y-%m-%d").date()
        except Exception:
            continue
        if d < lo:
            continue
        ver = re.search(r"v?\d+\.\d+(?:\.\d+)?", r.get("title") or "")
        rl.append({"at": d.strftime("%m-%d"),
                   "ver": ver.group(0) if ver else "",
                   "is_date_tag": bool(re.match(r"^\d{8}\b", (r.get("title") or "").strip()))})
    sm["releases"] = rl
    sm["sentence"] = gh_sentence(sm)
    return sm


def _gh_topic_phrase(real):
    """从提交标题里抽出业务主题（返回「涉及A、B、C」式的中文短语，或列表）。"""
    if not real:
        return []
    n = len(real)
    got = []
    for label, pat in _GH_TOPICS:
        c = sum(1 for t in real if re.search(pat, t, re.I))
        if c:
            got.append((label, c))
    got = [kv for kv in sorted(got, key=lambda kv: -kv[1]) if kv[1] >= max(1, n // 10)]
    return got[:3]


def _gh_kind_phrase(real):
    """改动类型（修复/新增/优化…）。要求过 1/3 才算「为主」，否则宁可不说。

    80 次改动里 6 次测试也会被写成「以测试为主」，读起来像这期只在写测试，
    与事实相反，所以设了占比门槛。
    """
    if not real:
        return []
    n = max(1, len(real))
    got = []
    for label, pat in _GH_KINDS:
        c = sum(1 for t in real if re.search(pat, t, re.I))
        if c and c / n >= (1 / 3.0):
            got.append((label, c))
    return sorted(got, key=lambda kv: -kv[1])[:2]


def gh_sentence(sm):
    """一句直接进报告的中文：最后一次什么时候 + 近一个月几次 + 最后一次改了什么。"""
    if not sm:
        return ""
    days = sm.get("days") or 30
    total = sm.get("total") or 0
    last = sm.get("last_commit") or ""
    detail = sm.get("last_detail") or ""
    topics = [k for k, _n in (sm.get("topics") or [])]
    kinds = [k for k, _n in (sm.get("kinds") or [])]
    if not total:
        if last:
            return "近 %d 天无提交，最后一次更新在 %s" % (days, last)
        return "近 %d 天无提交记录" % days
    # ① 最后一次更新什么时候 + 改了什么（这是用户要的主句）
    out = "最后一次更新：%s" % (sm.get("last_detail_at") or last)
    if detail:
        out += "，改的是" + detail
    elif topics:
        out += "，涉及" + "、".join(topics)
    # ② 近一个月几次（翻页受限时明确写「至少」）
    cnt = "近 %d 天%s %d 次提交" % (days, "至少" if sm.get("truncated") else "共", total)
    if sm.get("merge"):
        cnt += "（含合并 %d 次，实际改动 %d 次）" % (sm["merge"], sm["slim"])
    out += "；" + cnt
    # ③ 节奏（只在真正有参考价值时给：活跃天数少、或某天特别集中）
    # 翻页受限时样本只覆盖最近几天，说「只有 N 天有提交」会被读成「这项目一个月没动静」，
    # 属于把抓取局限误写成事实 —— 这种情况下一个字节的节奏描述都不给。
    if not sm.get("truncated"):
        if sm.get("active_days") and sm["active_days"] <= 5 and sm.get("first") and sm.get("last") \
                and sm["first"] != sm["last"]:
            out += "，只有 %d 天有提交（%s 至 %s）" % (sm["active_days"], sm["first"], sm["last"])
        elif sm.get("peak_n") and total and sm["peak_n"] * 2 >= total and sm.get("active_days", 0) > 1:
            out += "，其中 %s 当天 %d 次" % (sm["peak_day"], sm["peak_n"])
    # ④ 主体改动方向（占比达标才写；主题已在「最后一次」那句出现过就不再重复）
    if kinds:
        out += "，以%s为主" % "、".join(kinds)
    rest = [t for t in topics if t not in (detail or "")]
    if rest and not detail:
        out += "，涉及" + "、".join(rest)
    elif rest and detail:
        out += "，整体涉及" + "、".join(rest)
    # ⑤ 发版
    if sm.get("releases"):
        r = sm["releases"][0]
        ver = (" " + r["ver"]) if r.get("ver") else ""
        out += "；近 %d 天发版 %d 次（最新%s，%s）" % (days, len(sm["releases"]), ver, r["at"])
    else:
        out += "；本期无新版本"
    if sm.get("truncated"):
        out += "（翻页受限，实际次数更多）"
    return out


def src_github(meta, days=14):
    """GitHub releases + commits（Atom 源，无需 token）。"""
    gh = meta.get("github") or ""
    m = re.search(r"github\.com/([^/]+)/([^/?#]+)", gh)
    if not m:
        return {"src": "GitHub", "ok": False, "err": "知识库无仓库地址"}
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
    out = {"src": "GitHub", "ok": True, "repo": "%s/%s" % (owner, repo),
           "releases": [], "commits": [], "summary": None, "err": ""}
    ok, x = fetch("https://github.com/%s/%s/releases.atom" % (owner, repo),
                  timeout=15, retries=1)
    if ok:
        for it in re.findall(r"<entry>(.*?)</entry>", x, re.S)[:8]:
            ti = re.search(r"<title>(.*?)</title>", it, re.S)
            up = re.search(r"<updated>(.*?)</updated>", it)
            out["releases"].append({
                "title": html.unescape(re.sub(r"<.*?>", "", ti.group(1))).strip() if ti else "",
                "at": (up.group(1) if up else "")[:10]})
    # 统计窗口固定看近一个月（用户口径：「近一个月更新几次」），
    # 与报告时间线的 14 天窗口解耦 —— 开发节奏按月看才有意义。
    commits, more = _gh_pages(owner, repo, pages=3,
                              today=datetime.datetime.now(TZ).date(), days=30)
    out["commits"] = commits
    sm = None
    if commits or out["releases"]:
        sm = gh_summary(commits, out["releases"], days=30,
                        truncated=more, shown=len(commits))
    # 兜底：提交列表全空、或最后一次全是合并提交（没有内容可说）时，
    # 从**仓库主页**取最后一次提交的时间与中文内容摘要。
    # 用户明确要求：「兜底时仓库主页提取最后一次提交时间和内容摘要」。
    need_fb = (not commits) or (sm is not None and not sm.get("last_detail"))
    fb = _gh_repo_last_push(owner, repo) if need_fb else None
    if fb:
        # 只留中文字段进 sources，英文标题/路径不外泄到素材或档案
        out["homepage"] = {"at": fb["at"], "detail": fb["detail"],
                           "source": fb["source"], "oid": fb["oid"]}
    if not commits and fb:
        at = fb["at"] or ""
        short = at[5:] if len(at) == 10 else at
        sm = {"ok": True, "fallback": True, "homepage": True, "days": 30,
              "total": 0, "truncated": False, "last_commit": short,
              "last_detail_at": short, "last_detail": fb["detail"],
              "kinds": [], "topics": [], "releases": [], "sentence": ""}
        sm["sentence"] = (
            "最后一次更新：%s，改的是%s；近 30 天提交次数未取到（翻页异常，已由%s兜底）"
            % (short or "日期未取到", fb["detail"] or "内容未取到", fb["source"]))
    elif not commits and out["releases"]:
        # 连仓库主页都不通时，至少还有发版日期（内容摘要确实拿不到，如实写明）
        r = out["releases"][0]
        ver = re.search(r"v?\d+\.\d+(?:\.\d+)?", r.get("title") or "")
        sm = gh_summary([], out["releases"], days=30)
        sm["fallback"] = True
        sm["sentence"] = ("提交列表与仓库主页均未取到（翻页异常），仅知最后一次发版在 %s%s，内容未取到"
                          % ((r.get("at") or "日期未取到")[:10],
                             ("（版本 " + ver.group(0) + "）") if ver else ""))
    elif sm is not None and fb and not sm.get("last_detail"):
        # 正常抓到了提交列表，但最后一次是纯合并提交 → 用主页内容补「改了什么」
        at = fb["at"] or ""
        sm["last_detail"] = fb["detail"]
        sm["last_detail_at"] = (at[5:] if len(at) == 10 else at) or sm.get("last_commit") or ""
        sm["homepage"] = True
        sm["sentence"] = gh_sentence(sm)
    out["summary"] = sm
    if not out["releases"] and not commits and not fb:
        out["ok"] = False
        out["err"] = "仓库不存在、无公开动态，且仓库主页兜底失败"
    return out


def _gh_norm(s):
    """把提交标题/路径里的 _ - / . 换成空格，让词表的 \\b 边界能命中。

    实测 `training_observation_ack_window_test.py` 直接用 \\bobservation\\b
    是匹配不到的（下划线属于单词字符），必须先把分隔符打散。
    """
    return re.sub(r"[^A-Za-z0-9]+", " ", s or "")


# 路径专用主题词表：提交标题常常只有「Merge pull request #…」，
# 但改动文件列表能说明改的是哪个模块，这是兜底时最主要的内容线索。
_GH_PATH_TOPICS = (
    ("测试", r"(?:^|/)(?:tests?|specs?)\b|\btest_|_test\b|\.test\b"),
    ("文档", r"(?:^|/)docs?\b|readme|changelog|\.md\b"),
    ("接口与网关", r"gateway|proxy|endpoint|\bapis?\b"),
    ("数据库", r"postgres|\bsql\b|migration|\bschema\b|\bdb\b"),
    ("前端界面", r"frontend|dashboard|\bui\b|components?"),
    ("依赖与配置", r"\.github|workflow|requirements|pyproject|docker|\.ya?ml\b|\.toml\b"),
    ("模型推理", r"\bllm\b|inference|prompt|embedding"),
    ("验证器", r"validator|miner|neuron|weight|score"),
    ("竞技场", r"arena"),
)


def _gh_zh_detail(titles, paths=None):
    """英文提交标题 + 变更文件 → 一句中文内容摘要（英文绝不外泄）。

    兜底场景样本量只有 1~3 条，所以不套 gh_summary 的「占比过 1/3」门槛，
    命中了就说命中了；命中不到也要给一句中文，不能把英文标题丢给模型。
    """
    titles = [t for t in (titles or []) if t]
    paths = [p for p in (paths or []) if p]
    if not titles and not paths:
        return ""
    blob = " ".join(_gh_norm(t) for t in titles)
    alltext = blob + " " + " ".join(_gh_norm(p) for p in paths)
    praw = " ".join(paths)
    topics, seen = [], set()
    for label, pat in _GH_TOPICS:
        if label not in seen and re.search(pat, alltext, re.I):
            seen.add(label)
            topics.append(label)
    for label, pat in _GH_PATH_TOPICS:
        # 「测试」由下面的 kinds 表达，不占主题位，否则会写出
        # 「测试相关的测试改动」这种重复。
        if label != "测试" and label not in seen and re.search(pat, praw, re.I):
            seen.add(label)
            topics.append(label)
    topics = topics[:3]
    kinds = []
    for label, pat in _GH_KINDS:
        n = len(re.findall(pat, blob, re.I))
        if n:
            kinds.append((label, n))
    kinds = [k for k, _n in sorted(kinds, key=lambda kv: -kv[1])[:2]]
    # 同一个词既当主题又当意图时只留主题（例：「文档相关的优化改动」）
    kinds = [k for k in kinds if k not in topics]
    # 标题没写「test」但改的全是 tests/ 下的文件时，也要说清是测试改动
    if re.search(r"(?:^|/)(?:tests?|specs?)\b|\btest_|_test\b|\.test\b", praw, re.I):
        if "测试" not in kinds:
            kinds.append("测试")
    kinds = kinds[:2]
    if topics and kinds:
        return "%s相关的%s改动" % ("、".join(topics), "与".join(kinds))
    if topics:
        return "%s相关改动" % "、".join(topics)
    if kinds:
        return "%s类改动" % "与".join(kinds)
    return "代码逻辑调整"


def _gh_repo_last_push(owner, repo):
    """兜底：从仓库主页出发，取「最后一次提交时间 + 中文内容摘要」。

    用户口径（2026-11-10）：「兜底时仓库主页提取最后一次提交时间和内容摘要」。
    翻页异常时不能只回一句「抓取失败」，必须至少说清最后一次更新是什么时候、改了什么。

    路径：主页内嵌 JSON → currentOid / 默认分支 / 累计提交数
          → /commit/{oid}.patch（几十 KB，比近 1MB 的提交详情页稳得多）
          → Date + Subject + diff 文件列表 → 中文摘要。
    主页不通再退到 commits.atom 第一条，同样给时间 + 标题。
    """
    home = "https://github.com/%s/%s" % (owner, repo)
    oid, branch, at, total = "", "", "", ""
    ok, h = fetch(home, timeout=20, retries=2)
    if ok:
        m = _GH_EMBED.search(h)
        if m:
            try:
                p = json.loads(m.group(1)).get("payload") or {}
                cv = p.get("codeViewRepoRoute") or {}
                ref = cv.get("refInfo") or {}
                oid = ref.get("currentOid") or ""
                branch = ref.get("name") or ""
                total = str((cv.get("overview") or {}).get("commitCount") or "").replace(",", "")
                # listCacheKey 形如 v0:1794305385.0，是默认分支最后推送的 epoch 秒
                mt = re.search(r"(\d{10})(?:\.\d+)?", str(ref.get("listCacheKey") or ""))
                if mt:
                    at = datetime.datetime.fromtimestamp(
                        int(mt.group(1)), TZ).strftime("%Y-%m-%d")
            except Exception:
                pass
        rt = re.search(r'<relative-time[^>]*datetime="(\d{4}-\d{2}-\d{2})', h)
        if rt:
            at = rt.group(1)
    titles, paths = [], []
    if oid:
        ok2, patch = fetch("%s/commit/%s.patch" % (home, oid), timeout=20, retries=2)
        if ok2:
            for line in re.findall(r"(?m)^Subject:\s*([^\n]+)", patch):
                t = re.sub(r"^\[PATCH[^\]]*\]\s*", "", line).strip()
                t = re.sub(r"\s+", " ", t)
                if t:
                    titles.append(t)
            titles = titles[:3]
            paths = re.findall(r"(?m)^diff --git a/(\S+)", patch)[:20]
            dt = re.search(r"(?m)^Date:\s*([^\n]+)", patch)
            if dt:
                try:
                    from email.utils import parsedate_to_datetime
                    at = parsedate_to_datetime(dt.group(1).strip()).astimezone(TZ).strftime("%Y-%m-%d")
                except Exception:
                    pass
    if not titles:
        # 最后一道保险：atom 第一条即最新提交（标题 + 日期都有）
        for uu in ("%s/commits.atom" % home,
                   "%s/commits/%s.atom" % (home, branch or "main")):
            ok3, a = fetch(uu, timeout=15, retries=1)
            if not ok3:
                continue
            ent = re.search(r"<entry>(.*?)</entry>", a, re.S)
            if not ent:
                continue
            ti = re.search(r"<title>(.*?)</title>", ent.group(1), re.S)
            up = re.search(r"<updated>([^<]+)</updated>", ent.group(1))
            if ti:
                t = html.unescape(re.sub(r"<.*?>", "", ti.group(1))).strip()
                if t:
                    titles = [re.sub(r"\s+", " ", t)[:120]]
            if up and not at:
                at = up.group(1)[:10]
            if titles:
                break
    if not at and not titles:
        return None
    return {"at": at or "", "oid": oid[:8], "branch": branch, "total": total,
            "detail": _gh_zh_detail(titles, paths), "nfiles": len(paths),
            "source": "仓库主页" if oid else "提交订阅源"}


def _rss_date(s):
    """RSS 的 RFC822 日期 → ISO（`Mon, 07 Sep 2026 10:00:00 GMT` → `2026-09-07`）。

    早期实现直接 `[:16]` 截断，导致报告里出现「Mon, 07 Se」这种半截日期——
    模型照抄进正文，读者根本看不出是哪天，还浪费字数。"""
    s = (s or "").strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                "%d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", s)
    if m:
        try:
            return datetime.datetime.strptime(
                "%s %s %s" % (m.group(1), m.group(2), m.group(3)),
                "%d %b %Y").strftime("%Y-%m-%d")
        except Exception:
            pass
    return s[:10]


def src_news(meta, sn, days=21):
    """Google News RSS：按「子网名 / SNxx bittensor」检索，过滤时间窗。"""
    names = [n for n in {meta.get("name"), A.ascii_name(meta.get("name"))} if n and len(n) >= 3]
    queries = []
    for n in names[:2]:
        queries.append('"%s" bittensor' % n)
    queries.append('"subnet %d" bittensor' % sn)
    queries.append('bittensor subnet %d tao' % sn)
    items, seen = [], set()
    for q in queries[:3]:
        u = ("https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en"
             % urllib.parse.quote(q))
        ok, x = fetch(u)
        if not ok:
            continue
        for it in re.findall(r"<item>(.*?)</item>", x, re.S)[:12]:
            ti = re.search(r"<title>(.*?)</title>", it, re.S)
            pd = re.search(r"<pubDate>(.*?)</pubDate>", it)
            lk = re.search(r"<link>(.*?)</link>", it, re.S)
            so = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
            title = html.unescape(re.sub(r"<.*?>", "", ti.group(1))).strip() if ti else ""
            if not title or title in seen:
                continue
            seen.add(title)
            items.append({"title": title, "at": _rss_date(pd.group(1) if pd else ""),
                          "url": (lk.group(1) if lk else "").strip(),
                          "source": html.unescape(re.sub(r"<.*?>", "", so.group(1))).strip() if so else ""})
        if len(items) >= 12:
            break
    # 关键词相关度过滤：标题里必须出现子网名或 subnet N，否则是泛 TAO 文章
    keys = [k.lower() for k in names] + ["subnet %d" % sn, "sn%d" % sn, "sn %d" % sn]
    rel = [i for i in items if any(k in i["title"].lower() for k in keys)]
    return {"src": "Google News", "ok": bool(items), "items": rel[:10],
            "total": len(items), "note": "" if rel else "检索到新闻但无一与本品直接相关"}


def _ddg(query, limit=8):
    """DuckDuckGo 检索。直连会被人机验证拦截，因此优先走 r.jina.ai 镜像
    （返回标准 Markdown，标题+链接+摘要都在），失败再回退直连 HTML。"""
    q = urllib.parse.quote(query)
    out, seen = [], set()
    for url in ("https://r.jina.ai/https://duckduckgo.com/html/?q=%s" % q,
                "https://lite.duckduckgo.com/lite/?q=%s" % q):
        ok, h = fetch(url, timeout=40)
        if not ok:
            continue
        if "anomaly" in h.lower() or "challenge" in h.lower()[:4000]:
            continue
        if "Markdown Content" in h:      # jina 镜像：Markdown 结构
            body = h.split("Markdown Content", 1)[1]
            blocks = re.split(r"\n(?=## \[)", body)
            for b in blocks:
                m = re.search(r"## \[(.+?)\]\((https?://[^)]+)\)", b, re.S)
                if not m:
                    continue
                title = re.sub(r"\*\*|\s+", " ", m.group(1)).strip()
                link = m.group(2)
                mu = re.search(r"uddg=([^&]+)", link)
                if mu:
                    link = urllib.parse.unquote(mu.group(1))
                if link in seen:
                    continue
                seen.add(link)
                # 摘要 = 链接行之后第一段不带图片的正常文本
                snip = ""
                for ln in b.splitlines()[1:]:
                    s = re.sub(r"\[[^\]]*\]\((?:https?://)?[^)]*\)", " ", ln).strip()
                    s = re.sub(r"!\[[^\]]*\]", " ", s)
                    s = re.sub(r"\((?:https?://)?(?:duckduckgo|external-content)[^)]*\)", " ", s)
                    s = re.sub(r"[\](]+https?://\S+", " ", s)
                    s = re.sub(r"\s+", " ", s).strip(" ](")
                    if len(s) > 40:
                        snip = s[:220]
                        break
                out.append({"title": title[:150], "url": link, "snip": snip})
                if len(out) >= limit:
                    return {"src": "DuckDuckGo", "ok": True, "items": out}
        else:                             # 直连 HTML
            links = re.findall(r'<a rel="nofollow" href="([^"]+)"[^>]*class=.result-link.>(.*?)</a>',
                               h, re.S)
            snips = re.findall(r'class="result-snippet">(.*?)</td>', h, re.S)
            for i, (u, t) in enumerate(links[:limit]):
                mu = re.search(r"uddg=([^&]+)", u)
                real = urllib.parse.unquote(mu.group(1)) if mu else u
                if real.startswith("//"):
                    real = "https:" + real
                if real in seen:
                    continue
                seen.add(real)
                out.append({"title": html.unescape(re.sub(r"<.*?>", "", t)).strip()[:150],
                            "url": real,
                            "snip": html.unescape(re.sub(r"<.*?>", "", snips[i])).strip()[:220]
                            if i < len(snips) else ""})
            if out:
                return {"src": "DuckDuckGo", "ok": True, "items": out[:limit]}
    return {"src": "DuckDuckGo", "ok": False, "items": [], "err": "检索被拦截或无结果"}


def _bing(query, limit=8):
    """Bing 检索（兜底）。DDG 自 2026-11 起对无头请求常态返回「选鸭子」人机验证页，
    导致「搜索」源长期静默失效。Bing 不需要 cookie，能稳定返回 b_algo 结果块。
    注意：Bing 对长尾垂类词的召回很差，只当兜底，不要指望它替代 X/新闻源。"""
    u = ("https://www.bing.com/search?q=%s&count=%d&setlang=en&cc=US"
         % (urllib.parse.quote_plus(query), max(10, limit * 2)))
    ok, h = fetch(u, timeout=25)
    if not ok:
        return {"src": "DuckDuckGo", "ok": False, "items": [], "err": h}
    out, seen = [], set()
    for b in re.findall(r"<li class=\"b_algo\".*?</li>", h, re.S):
        m = re.search(r"<h2[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", b, re.S)
        if not m:
            continue
        real = html.unescape(m.group(1))
        if "bing.com/ck/a" in real:          # 跳转壳，还原不出真实地址就丢掉
            continue
        if real in seen:
            continue
        seen.add(real)
        p = re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
        snip = html.unescape(re.sub(r"<[^>]+>", " ", p.group(1) if p else ""))
        out.append({"title": html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()[:150],
                    "url": real,
                    "snip": re.sub(r"\s+", " ", snip).strip()[:220]})
        if len(out) >= limit:
            break
    return {"src": "DuckDuckGo", "ok": bool(out), "items": out[:limit],
            "err": "" if out else "Bing 无可用结果"}


def _web_search_items(query, limit=8):
    """统一检索入口：DDG 优先，被拦就落 Bing。返回 [{"title","url","snip"}]。"""
    r = _ddg(query, limit=limit)
    if r.get("items"):
        return r["items"]
    return _bing(query, limit=limit).get("items", [])


def src_web_search(meta, sn, limit=8):
    """搜索引擎：官网/文档/第三方分析/AMA 等公开页面（含摘要）。"""
    names = [n for n in {meta.get("name"), A.ascii_name(meta.get("name"))} if n and len(n) >= 3]
    qs = []
    for n in names[:1]:
        qs.append('"%s" bittensor subnet %d' % (n, sn))
    qs.append('bittensor "subnet %d"' % sn)
    out, seen = [], set()
    for q in qs[:2]:
        for it in _web_search_items(q, limit=limit):
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            out.append(it)
        if len(out) >= limit:
            break
    return {"src": "DuckDuckGo", "ok": bool(out), "items": out[:limit],
            "err": "" if out else "检索被拦截或无结果"}


# ------------------------------------------------------------------ X / 推特
# 通道演进史（别再改回 jina）：早期用 r.jina.ai 镜像读 x.com，2026-11 起该镜像
# 全线返回 401 Unauthorized，X 源实际已 100% 失效，而报告里仍标「X✓」——
# 这就是「明明有新资讯却什么都没提供」的直接原因。现改为两级：
#   ① api.fxtwitter.com v2（无需鉴权，结构化 JSON，带时间戳/作者/互动数，可翻页）
#   ② syndication / nitter 系保底（多数已 403/429，仅作兜底，不指望）
FX_PROFILE = "https://api.fxtwitter.com/2/profile/%s/statuses"
FX_TWEET = "https://api.fxtwitter.com/%s/status/%s"
FX_USER = "https://api.fxtwitter.com/2/profile/%s"
X_RESERVED = {"i", "intent", "share", "home", "search", "hashtag", "status",
              "explore", "settings", "login", "signup", "tos", "privacy", "compose",
              "messages", "notifications", "bookmarks", "lists", "topics", "article",
              "account", "download", "communities", "followers", "following", "likes",
              "media", "with_replies", "analytics", "jobs", "spaces"}
# 用户贴链接时可能出现的 host：官方站、移动站、以及常见的 fx/vx 镜像站。
# 一律归一化回 x.com，再交给 fxtwitter API 读取（x.com 本身有登录墙，正文抓不到）。
X_HOSTS = r"(?:x|twitter|mobile\.twitter|www\.twitter|fxtwitter|vxtwitter|fixupx|fixvx|nitter)\.com"


def _fx_json(url, timeout=25, tries=3):
    """取 fxtwitter JSON。它偶尔 404/SSL 抖动，重试并做短退避。"""
    last = ""
    for i in range(max(1, tries)):
        # 首次用短超时快速探一下：代理节点抖动时与其干等 16 秒，
        # 不如 8 秒内失败后立刻换一条连接重试（实测重试常在 1 秒内返回）。
        t = min(timeout, 8) if (i == 0 and tries > 1) else timeout
        ok, txt = fetch(url, timeout=t, retries=1)
        if ok and txt.strip().startswith("{"):
            try:
                return True, json.loads(txt)
            except Exception as e:
                last = "JSON 解析失败：%s" % str(e)[:80]
        else:
            last = (txt or "")[:100]
        if i < tries - 1:
            time.sleep(1.0 + i)
    return False, last


def _x_handles_from_html(h):
    """从官网/公告页 HTML 里捞出 X 账号（比搜索引擎猜名可靠得多）。

    实测 zils.ai → zils_ai、chutes.ai → chutes_ai、lium.io → lium_io 全部命中；
    比「子网名 + _ai」这种盲猜强。"""
    out = []
    for m in re.finditer(r"(?:https?:)?//(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{2,20})",
                         h or "", re.I):
        hd = m.group(1)
        if hd.lower() in X_RESERVED:
            continue
        out.append(hd)
    return list(dict.fromkeys(out))


def _x_candidates(meta, site_html=""):
    """定位官方 X 账号，来源按可信度排序：
    ① 官网页面里自己挂的 X 链接（最硬，项目方主动展示）
    ② 知识库/官网 URL 本身就是 x.com/xxx
    ③ 搜索引擎命中 site:x.com（DDG 常被反爬拦，Bing 兜底）
    ④ 子网名变体（最弱，只作最后尝试）
    GitHub owner 通常是人名，仅作低优先候选且必须过相关性校验。"""
    cands, low = [], []
    u = meta.get("url") or ""
    m = re.search(r"(?:twitter|x)\.com/([A-Za-z0-9_]{2,20})", u)
    if m and m.group(1).lower() not in X_RESERVED:
        cands.append(m.group(1))
    cands += _x_handles_from_html(site_html)
    base = A.ascii_name(meta.get("name"))
    # 只有官网没挂 X 链接时才去搜索引擎找（搜索通道慢且常被拦，别每次都用）
    if not _x_handles_from_html(site_html):
        for it in _web_search_items('site:x.com "%s" bittensor'
                                   % (base or "subnet %d" % meta.get("sn", 0)), limit=8):
            blob = " ".join([it.get("url") or "", it.get("snip") or "", it.get("title") or ""])
            for mm in re.finditer(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,20})", blob, re.I):
                hd = _x_clean_handle(mm.group(1))
                # 以前要求 URL 必须含 /status/ 才采纳，但 site: 搜索命中的大多是
                # 账号主页，官推因此被整批过滤掉。相关性交给后面的 X_TOPIC /
                # 名称校验把关，这里只排除保留字。
                if hd and hd not in cands:
                    cands.append(hd)
    if base and re.fullmatch(r"[A-Za-z0-9_]{3,20}", base):
        cands += [base, base + "_ai", base + "ai"]
    gh = meta.get("github") or ""
    mg = re.search(r"github\.com/([A-Za-z0-9_.\-]+)/", gh)
    if mg and mg.group(1).lower() not in ("bittensor", "opentensor", "orgs"):
        low.append(mg.group(1))
    return list(dict.fromkeys(cands))[:6], list(dict.fromkeys(low))[:2]


# 判断一个 X 号是否真的与本子网相关（避免把开发者个人碎碎念当官方表态）
X_TOPIC = re.compile(
    r"(?i)(bittensor|subnet|netuid|\btao\b|dtao|validator|miner|\bSN\s?\d|alpha|emission|"
    r"inference|mining|neuron)")


def _x_post_from_fx(it):
    """把 fxtwitter 的一条 status 规整成内部结构。"""
    a = it.get("author") or {}
    ts = it.get("created_timestamp") or 0
    try:
        dt = datetime.datetime.fromtimestamp(int(ts), TZ)
        at = dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        at = (it.get("created_at") or "")[:16]
    txt = re.sub(r"https?://t\.co/\S+", "", it.get("text") or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return {"id": str(it.get("id") or ""), "url": it.get("url") or "",
            "text": txt, "at": at, "ts": int(ts or 0),
            "author": a.get("screen_name") or "", "author_name": a.get("name") or "",
            "bio": (a.get("description") or "").strip(),
            "likes": int(it.get("likes") or 0), "replies": int(it.get("replies") or 0),
            "reposts": int(it.get("reposts") or 0), "views": int(it.get("views") or 0),
            "media": bool((it.get("media") or {}).get("all")) if isinstance(it.get("media"), dict)
                     else bool(it.get("media")),
            # reposted_by 非空 = 该账号转推了别人的贴（内容不是本人写的，但「转谁」本身是表态）
            "is_rt": bool(it.get("reposted_by")),
            "rt_by": (it.get("reposted_by") or {}).get("screen_name") or "",
            "lang": it.get("lang") or ""}


def x_user_posts(handle, pages=2, log=None, want_profile=True):
    """取某账号最近贴文（fxtwitter，可按 bottom 游标翻页）。
    返回 (profile_dict, [post...])；账号不存在/取不到时 profile 为 None。

    v1.14.0：时间线返回空数组**不等于账号不存在**。以前直接伪造一个
    `{"handle": h, "name": "", "bio": ""}` 的 prof，src_x 看到 posts 为空就
    记「无公开贴文」并把整条 X 源判成失败，用户于是看到「官推找不到」。
    现在零贴文时补读 /2/profile/{h} 拿真实资料，让报告能写「账号已核实、
    累计 0 条贴文」而不是「链接不可访问」。
    """
    hd = _x_clean_handle(handle)
    if not hd:
        return None, []
    prof0 = None
    if want_profile:
        prof0 = x_profile(hd)
        # 账号明确 0 条公开贴文时，时间线端点注定返回空数组，而它实测要
        # 14~28 秒才回（2026-11-11 ms_computer）。先问资料就能 1 秒结束。
        if prof0 and int(prof0.get("statuses") or 0) == 0:
            if log:
                log("    X @%s：账号累计 0 条公开贴文，跳过时间线" % hd)
            return prof0, []
    ok, d = _fx_json(FX_PROFILE % hd, timeout=18, tries=2)
    if not ok or not isinstance(d, dict):
        d = {}
    prof = None
    posts = []
    for it in d.get("results") or []:
        if it.get("type") not in (None, "status"):
            continue
        p = _x_post_from_fx(it)
        if not p["text"]:
            continue
        if prof is None and p["author"].lower() == hd.lower():
            # statuses=-1 =「时间线没给出累计贴文数」，不能被报告误写成 0 条
            prof = {"handle": p["author"], "name": p["author_name"], "bio": p["bio"],
                    "statuses": -1, "followers": 0, "following": 0,
                    "joined": "", "website": "", "url": "https://x.com/" + hd,
                    "via": "timeline"}
        posts.append(p)
    if prof is None:
        prof = prof0 or (x_profile(hd) if want_profile else None)
    if prof is None:
        return None, []
    cur = (d.get("cursor") or {}).get("bottom")
    for _ in range(max(0, pages - 1)):
        if not cur:
            break
        ok, d2 = _fx_json((FX_PROFILE % hd) + "?cursor=" + urllib.parse.quote(cur))
        if not ok or not isinstance(d2, dict) or not d2.get("results"):
            break
        for it in d2["results"]:
            if it.get("type") not in (None, "status"):
                continue
            p = _x_post_from_fx(it)
            if p["text"]:
                posts.append(p)
        nxt = (d2.get("cursor") or {}).get("bottom")
        if not nxt or nxt == cur:
            break
        cur = nxt
    if log:
        log("    X @%s：%d 条贴文（含转推），账号累计 %s 条"
            % (hd, len(posts), prof.get("statuses", "?")))
    return prof, posts


def _x_basic_post(tid, url, text, author, author_name, via):
    """兜底通道（oEmbed / 页面 meta）拿不到时间戳和互动数，字段补齐并如实标注。"""
    return {"id": tid, "url": url, "text": text, "at": "", "ts": 0,
            "author": author, "author_name": author_name, "bio": "",
            "likes": 0, "replies": 0, "reposts": 0, "views": 0,
            "media": False, "is_rt": False, "rt_by": "", "lang": "", "via": via}


def x_tweet(url_or_id, log=None):
    """按链接/ID 抓单条 X 帖：fxtwitter → oEmbed → x.com 页面 meta 三级兜底。

    x.com 本身有登录墙，直接打开只能拿到壳页面，所以先转成
    api.fxtwitter.com/status/{id}（实测 0.9~1.2 秒返回结构化 JSON）；
    该 API 抖动/删帖时再走 publish.twitter.com/oembed 和 x.com 的
    og/twitter description，两条都无需登录。
    """
    s = url_or_id or ""
    m2 = re.search(r"/status/(\d{15,25})", s) or re.search(r"\b(\d{15,25})\b", s)
    if not m2:
        return None
    tid = m2.group(1)
    hm = re.search(X_HOSTS + r"/([A-Za-z0-9_]{1,20})/status/", s, re.I)
    hd = _x_clean_handle(hm.group(1)) if hm else ""

    # ① fxtwitter 结构化 JSON（首选：带准确 UTC+8 时间与互动数）
    urls = ["https://api.fxtwitter.com/status/%s" % tid]
    if hd:
        urls.append(FX_TWEET % (hd, tid))
    for u in urls:
        ok, d = _fx_json(u, timeout=12, tries=2)
        if ok and isinstance(d, dict) and (d.get("tweet") or {}):
            t = d["tweet"]
            p = _x_post_from_fx(t)
            if not p["bio"]:
                p["bio"] = ((t.get("author") or {}).get("description") or "").strip()
            p["via"] = "fxtwitter"
            if log:
                log("    X 单帖已读取（fxtwitter）：@%s %s「%s…」"
                    % (p["author"], p["at"], p["text"][:40]))
            return p

    canon = "https://x.com/%s/status/%s" % (hd or "i", tid)
    # ② oEmbed：实测必须传**带账号名的完整 URL**，且不能把 url= 整体 percent-encode；
    #    `x.com/i/status/ID` 和编码后的参数都会被服务端挂到超时。
    #    该通道能拿正文和作者，但没有时间戳，必须如实标注。
    ok, txt = fetch("https://publish.twitter.com/oembed?url=" + canon,
                    timeout=16, retries=1)
    if ok and txt.strip().startswith("{"):
        try:
            od = json.loads(txt)
            body = re.search(r"<p[^>]*>(.*?)</p>", od.get("html") or "", re.S | re.I)
            if body:
                clean = re.sub(r"\s+", " ", html.unescape(
                    re.sub(r"<[^>]+>", "", body.group(1)))).strip()
                if clean:
                    au = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,20})/",
                                   od.get("html") or "")
                    p = _x_basic_post(tid, canon, clean,
                                      (au.group(1) if au else hd),
                                      od.get("author_name") or "", "oEmbed（无时间戳）")
                    if log:
                        log("    X 单帖已读取（oEmbed）：「%s…」" % clean[:40])
                    return p
        except Exception:
            pass

    # ③ x.com 页面 meta：登录墙不影响 og:description，可当最后一道兜底
    ok, h = fetch(canon, timeout=16, retries=1)
    if ok and h:
        b = re.search(r'<meta[^>]+(?:property|name)="(?:og:description|twitter:description)"'
                      r'[^>]+content="([^"]*)"', h, re.I)
        if b:
            clean = re.sub(r"\s+", " ", html.unescape(b.group(1))).strip()
            if clean and not re.match(r"(?i)^(x \(formerly|login|sign up|just a moment)", clean):
                p = _x_basic_post(tid, canon, clean, hd, "", "x.com 页面摘要（无时间戳）")
                if log:
                    log("    X 单帖已读取（页面摘要）：「%s…」" % clean[:40])
                return p
    return None


def _x_clean_handle(h):
    h = (h or "").lstrip("@").strip()
    if not h or h.lower() in X_RESERVED or not re.fullmatch(r"[A-Za-z0-9_]{1,20}", h):
        return ""
    return h


def x_targets_in(text):
    """把任意文本里的 X 链接归一化，分成「帖子」和「账号主页」两类。

    2026-11-11 事故：用户发 `/retry 24官推https://x.com/ms_computer`，
    旧 x_links_in 只认 `/status/数字`，账号主页链接被整条丢掉，
    报告于是写「未抓取到内容」——其实账号真实存在，只是 0 条贴文。
    现在兼容 twitter.com / 移动站 / x.com/i/status / fx、vx 镜像 / ?s=20 参数。
    """
    posts, handles = [], []
    # 只收 URL 合法字符：中文（如「官推」）紧贴链接时会被自然截断，不会吞进 path。
    pat = re.compile(r"(?:https?://)?(?:www\.)?" + X_HOSTS +
                     r"/[A-Za-z0-9_/%?=&#.~:+$,;!'\-]*", re.I)
    for m in pat.finditer(text or ""):
        raw = m.group(0).rstrip(".,;:!?/")
        pm = re.search(r"/(?:i/)?status/(\d{15,25})", raw)
        if pm:
            tid = pm.group(1)
            hm = re.search(X_HOSTS + r"/([A-Za-z0-9_]{1,20})/status/", raw, re.I)
            hd = _x_clean_handle(hm.group(1)) if hm else ""
            u = "https://x.com/%s/status/%s" % (hd or "i", tid)
            if u not in posts:
                posts.append(u)
            continue
        hm = re.search(X_HOSTS + r"/([A-Za-z0-9_]{1,20})(?:[/?#]|$)", raw, re.I)
        if hm:
            hd = _x_clean_handle(hm.group(1))
            if hd and hd not in handles:
                handles.append(hd)
    return {"posts": posts, "handles": handles}


def x_links_in(text):
    """兼容旧调用：只取帖子链接（已归一化到 x.com）。"""
    return x_targets_in(text)["posts"]


def x_official_from_archive(arc):
    """从历史投研结论里找回此前已确认的官方 X 账号。

    用户可能只在某一次 `/retry 24官推 https://x.com/ms_computer` 里给过链接；
    之后不带反馈再查同一子网时，不该又写成「官推找不到」。
    只认紧邻「官方 X / 官推 / 官方账号」的账号链接，避免把报告里引用的
    路人账号当权威。
    """
    if not arc:
        return []
    blob = "\n".join(str(r.get(k) or "")
                     for r in (arc.get("reports") or []) if isinstance(r, dict)
                     for k in ("events", "body", "title", "conclusion"))
    out = []
    for m in re.finditer(r"(?:官方\s*X|官推|官方账号)[^\n。；]{0,60}?"
                         r"(?:https?://)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,20})",
                         blob, re.I):
        hd = _x_clean_handle(m.group(1))
        if hd and hd not in out:
            out.append(hd)
    return out


def x_profile(handle, timeout=12):
    """读 X 账号资料（不依赖时间线）。

    fxtwitter 的 /2/profile/{h} 返回 name/bio/followers/statuses/joined/website，
    即使账号一条贴文都没发也能拿到 —— 这才是判断「官推到底存不存在」的依据。
    兜底读 x.com 主页的 og/twitter meta（登录墙不影响 meta）。
    """
    hd = _x_clean_handle(handle)
    if not hd:
        return None
    ok, d = _fx_json(FX_USER % hd, timeout=timeout, tries=2)
    if ok and isinstance(d, dict) and isinstance(d.get("user"), dict):
        u = d["user"]
        web = u.get("website") or {}
        ver = u.get("verification") or {}
        return {"handle": u.get("screen_name") or hd,
                "name": u.get("name") or "", "bio": (u.get("description") or "").strip(),
                "followers": int(u.get("followers") or 0),
                "following": int(u.get("following") or 0),
                "statuses": int(u.get("statuses") or 0),
                "joined": (u.get("joined") or "")[:16],
                "website": web.get("display_url") or web.get("url") or "",
                "avatar": u.get("avatar_url") or "", "protected": bool(u.get("protected")),
                "verified": bool(ver.get("verified")),
                "url": u.get("url") or ("https://x.com/" + hd), "via": "fxtwitter"}
    ok, h = fetch("https://x.com/" + hd, timeout=timeout, retries=1)
    if ok and h:
        t = re.search(r'<meta[^>]+(?:property|name)="(?:og:title|twitter:title)"[^>]+content="([^"]*)"', h, re.I)
        b = re.search(r'<meta[^>]+(?:property|name)="(?:og:description|twitter:description|description)"[^>]+content="([^"]*)"', h, re.I)
        if t or b:
            return {"handle": hd, "name": html.unescape(t.group(1)) if t else hd,
                    "bio": html.unescape(b.group(1)) if b else "",
                    "followers": 0, "following": 0, "statuses": -1,
                    "joined": "", "website": "", "avatar": "", "protected": False,
                    "verified": False, "url": "https://x.com/" + hd, "via": "x.com meta"}
    return None


# Bittensor 生态圈的几个「广播站」账号：它们发的是全生态资讯，
# 单个子网的官方号往往不转，但社区消息面主要来自这里（如 opentensor 报
# 「Chutes 与腾讯云合作」）。按需过滤出提到本子网的贴文。
X_ECO_ACCOUNTS = ("opentensor", "taostats", "bittensor")


def x_eco_posts(meta, days=14, log=None, skip_handles=None):
    """抓生态广播号的近期贴文，挑出点名本子网的（子网名 / SN 编号）。
    身份记为「生态账号提及」——比子网官方号弱，但比路人强，必须如实标注。

    skip_handles：本子网官方号已经在 src_x 里抓过了，这里跳过，避免重复请求。"""
    name = (A.ascii_name(meta.get("name")) or "").strip()
    sn = int(meta.get("sn") or 0)
    skip = {h.lower().lstrip("@") for h in (skip_handles or []) if h}
    pats = []
    if name and len(name) >= 3:
        pats.append(re.compile(r"\b%s\b" % re.escape(name), re.I))
    pats.append(re.compile(r"(?i)\bsubnet\s*%d\b|\bSN\s*%d\b|\bnetuid\s*%d\b" % (sn, sn, sn)))
    lo_ts = int((datetime.datetime.now(TZ) -
                 datetime.timedelta(days=max(3, int(days or 14)))).timestamp())
    hits, seen = [], set()
    # 三个生态号并发抓：串行时每个 ~15 秒，实测这一段白白花掉 45 秒，
    # 而且多数情况下一无所获（生态号本来就不会天天点名某个子网）。
    import concurrent.futures as cf
    targets = [h for h in X_ECO_ACCOUNTS if h.lower() not in skip]

    def _grab(h):
        try:
            return x_user_posts(h, pages=1, want_profile=False)[1]
        except Exception:
            return []

    posts_all = []
    pool = cf.ThreadPoolExecutor(max_workers=max(1, len(targets)))
    futs = [pool.submit(_grab, h) for h in targets]
    try:
        for fut in cf.as_completed(futs, timeout=25):
            try:
                posts_all.extend(fut.result() or [])
            except Exception:
                continue
    except cf.TimeoutError:
        pass        # 到点就走，拿到的先用；生态号提及本来只是补充信源
    pool.shutdown(wait=False)

    for p in posts_all:
        if p["id"] in seen or p["ts"] < lo_ts:
            continue
        hit = [x.pattern for x in pats if x.search(p["text"])]
        if not hit:
            continue
        # SN 编号正则太宽（"subnet 8" 会命中 subnet 80），要求同时出现名字或独立编号
        if name and len(name) >= 3 and not pats[0].search(p["text"]):
            m = re.search(r"(?i)\bsubnet\s*%d\b|\bSN\s*%d\b|\bnetuid\s*%d\b" % (sn, sn, sn),
                          p["text"])
            if not m:
                continue
        seen.add(p["id"])
        hits.append(p)
    hits.sort(key=lambda x: -x["ts"])
    if hits and log:
        log("    X 生态圈：%d 条提及 SN%d/%s" % (len(hits), sn, name or "-"))
    return hits


def src_x(meta, site_html="", extra_links=None, days=14, log=None,
          extra_handles=None, authority_handles=None):
    """X（推特）：读官方账号时间线 + 用户在群里/反馈里贴出的具体帖子。

    身份分级（报告里必须按级别标注，不能用 KOL 的话冒充官方表态）：
      official  = 用户直接提供的官推链接 / 官网自己挂的 X 链接 / 账号名或简介含子网名
      thirdparty= 账号本身不是项目方，但贴文点名了该子网（只能标「第三方提及」）
    其余一律丢弃，不把同名路人和泛 Bittensor 大 V 当官方。

    v1.14.0：extra_handles 接收用户贴的**账号主页链接**（例如
    「24官推https://x.com/ms_computer」）。这类链接以前被 x_links_in 整条丢掉，
    导致用户明明给了官推却看到「找不到」。现在它排在所有猜测候选之前，
    并且账号零贴文也算「已核实」，不再判成 X 源失败。
    """
    extra_links = list(extra_links or [])
    def _uniq(seq):
        out = []
        for h in (seq or []):
            hd = _x_clean_handle(h)
            if hd and hd not in out:
                out.append(hd)
        return out

    _uh = _uniq(extra_handles)
    # 只有用户**反馈里直接给出**的账号才算权威证据；群聊档案里翻出来的账号
    # 只是社区提到的候选，仍要过相关性校验，不能凭 is_user 直接定成官方号。
    _auth_src = extra_handles if authority_handles is None else authority_handles
    user_handles = {h.lower() for h in _uniq(_auth_src)}
    base = (A.ascii_name(meta.get("name")) or "").lower()
    sn = meta.get("sn", 0)
    hi, lo = _x_candidates(meta, site_html=site_html)
    hi = [h for h in _uh if h not in hi] + hi      # 用户指定 > 官网 > 搜索/猜测
    site_handles = {h.lower() for h in _x_handles_from_html(site_html)}
    authority = site_handles | user_handles
    tried, third, best = [], None, None
    all_posts, official = [], None
    # 用户指定 / 官网挂出的 X 号优先试：命中就不用再看后面的候选（实测每次翻页
    # 约 4 秒，逐个试完 5-6 个候选要 50 秒以上，投研整体会超时）。
    pri = sorted(hi, key=lambda h: 0 if h.lower() in authority else 1)
    for h in pri + [x for x in lo if x]:
        trusted = h not in lo
        prof, posts = x_user_posts(h, pages=2, log=log,
                                   want_profile=(h.lower() in authority))
        if not prof:
            tried.append("%s:不可达" % h)
            continue
        is_site = h.lower() in site_handles
        is_user = h.lower() in user_handles
        if not posts:
            # 账号真实存在但没有公开贴文 = 已核实的事实，不是「抓不到」。
            # 用户/官网指定的账号直接认定为官方，并停止再猜别的号。
            if trusted and (is_site or is_user or (base and base in h.lower())):
                st = int(prof.get("statuses") or 0)
                rec = {"src": "X", "ok": True, "handle": prof.get("handle") or h,
                       "bio": prof.get("bio") or "", "nposts": "0", "posts": [],
                       "items": [], "recent": [], "linked": [], "links": [],
                       "level": "official", "topic_hits": 0, "via": "fxtwitter",
                       "note": ("用户直接提供的官方账号链接" if is_user else
                                "官网挂出的 X 账号" if is_site else
                                "账号名含子网名，认定为项目方账号"),
                       "profile": prof,
                       "empty_reason": ("账号累计 %d 条公开贴文" % st if st >= 0
                                        else "页面可见资料，未取到累计贴文数")}
                if official is None:
                    official = rec
                if log:
                    log("    X @%s：账号已核实（%s），无公开贴文" % (h, rec["empty_reason"]))
                break
            tried.append("%s:无公开贴文" % h)
            continue
        own = [p for p in posts if p["author"].lower() == h.lower() and not p["is_rt"]]
        blob = " ".join([prof.get("bio") or ""] + [p["text"] for p in own]).lower()
        topic_hits = len(X_TOPIC.findall(blob))
        name_in_id = bool(base) and (base in h.lower() or base in (prof.get("bio") or "").lower())
        name_in_posts = bool(base) and base in " ".join(p["text"] for p in own).lower()
        is_authority = is_site or is_user
        rec = {"src": "X", "ok": True, "handle": prof.get("handle") or h,
               "bio": prof.get("bio") or "", "nposts": str(len(posts)),
               "posts": [p["text"] for p in own[:10]], "items": posts,
               "links": [p["url"] for p in own[:10]],
               "topic_hits": topic_hits, "via": "fxtwitter", "profile": prof}
        if trusted and (is_authority or name_in_id) and (topic_hits >= 1 or is_authority):
            rec["level"] = "official"
            rec["note"] = ("用户直接提供的官方账号链接" if is_user else
                           "官网挂出的 X 账号" if is_site else
                           "账号名/简介含子网名，认定为项目方账号")
            if official is None:
                official = rec
                all_posts += own
            break        # 官方号已定，不再试其它候选（省时间，也避免误配别人的号）
        if name_in_posts and topic_hits >= 1:
            rec["level"] = "thirdparty"
            rec["note"] = "非项目方账号，但贴文点名本子网 —— 只可作第三方提及"
            third = third or rec
            tried.append("%s:第三方提及" % h)
            continue
        tried.append("%s:与本子网无关（命中 %d）" % (h, topic_hits))
        if best is None and topic_hits >= 2:
            best = rec

    # 用户直接给的帖子链接：优先级最高，哪怕不是官方号也一定读出来。
    # 并发抓：单条帖子在代理抖动时要 10~15 秒，串行 6 条能拖到 90 秒，
    # 而投研整体有墙钟上限，超时会把 X 源整个丢掉。
    linked = []
    todo = extra_links[:6]
    if todo:
        import concurrent.futures as cf
        pool = cf.ThreadPoolExecutor(max_workers=min(3, len(todo)))
        futs = [pool.submit(x_tweet, u) for u in todo]
        try:
            for fut in cf.as_completed(futs, timeout=35):
                try:
                    p = fut.result()
                except Exception:
                    p = None
                if p:
                    p["from"] = "用户提供链接"
                    linked.append(p)
                    if log:
                        log("    X 单帖已读取：@%s「%s…」"
                            % (p.get("author") or "?", (p.get("text") or "")[:40]))
        except cf.TimeoutError:
            pass
        pool.shutdown(wait=False)
        linked.sort(key=lambda x: -int(x.get("ts") or 0))

    rec = official or third or best
    if rec is None and linked:
        # 官方号没找到，但用户给了链接：以链接为准建一条记录
        rec = {"src": "X", "ok": True, "handle": linked[0]["author"],
               "bio": "", "nposts": str(len(linked)), "posts": [p["text"] for p in linked],
               "items": linked, "links": [p["url"] for p in linked],
               "topic_hits": 0, "via": "fxtwitter",
               "level": "thirdparty",
               "note": "仅取到用户提供的具体帖子，未确认官方账号归属"}
    if rec:
        # 合并用户给的链接（按 id 去重），让模型一定看得到这几条
        items = {p["id"]: p for p in (rec.get("items") or [])}
        for p in linked:
            if p["id"] and p["id"] not in items:
                items[p["id"]] = p
        merged = sorted(items.values(), key=lambda x: -x["ts"])
        # 只留近 N 天（官方时间线可能含很久以前的贴），但用户给的链接不受时间窗限制
        lo_ts = int((datetime.datetime.now(TZ) -
                     datetime.timedelta(days=max(3, int(days or 14)))).timestamp())
        kept = [p for p in merged if p["ts"] >= lo_ts or p.get("from")]
        rec["items"] = merged
        rec["recent"] = kept
        rec["posts"] = [p["text"] for p in kept[:12]]
        rec["links"] = [p["url"] for p in kept[:12]]
        rec["linked"] = linked
        rec["nposts"] = str(len(merged))
        if log:
            log("    X 结论：@%s（%s），近 %d 天 %d 条，含用户提供 %d 条"
                % (rec.get("handle"), rec.get("level") or "未定级",
                   int(days or 14), len(kept), len(linked)))
        return rec
    return {"src": "X", "ok": False, "err": "；".join(tried[-3:]) or "账号不可达",
            "posts": [], "items": [], "recent": []}


def collect(meta, sn, log=print, extra_x_links=None, days=14,
            extra_x_handles=None, authority_x_handles=None):
    """并发抓取全网素材，整体设一个墙钟上限。

    为什么从串行改成并发（2026-11-10 实测）：9 个源串行跑，网络一抖动就累加成
    **213 秒**（GitHub 117s + 收入/回购 46s + Google News 55s + X 35s …），
    用户发个编号要等 10 分钟才拿到报告。这些源都是纯网络 I/O、彼此无依赖，
    并发后总耗时 ≈ 最慢的那一个，而不是全部之和。

    上限的意义：某个源卡住时不能让整份报告陪着等。到点还没回来的源按
    「超时跳过」处理，报告照出，并在头部如实标注哪个源没拿到。

    X 仍放在官网之后：官网页面里挂的 X 账号是最可靠的账号来源，
    所以要先拿到官网 HTML，再把其中的 X 链接喂给 src_x 定位。
    """
    import concurrent.futures as cf

    # 关键源（链上指标、收入/回购）值得多等一会儿；资讯类超时就直接放弃，
    # 缺一条新闻远不如「整份报告晚 2 分钟」代价大。
    jobs = [
        ("bittensor.ai", lambda: src_bittensor_ai(sn), 75),
        ("taostats", lambda: src_taostats(sn), 75),
        ("dtaoscan", lambda: src_dtaoscan(sn), 75),
        # 收入/回购单独抓一次 bittensor.ai：数据藏在 RSC payload 里，
        # 和上面的常规指标解析路径不同（不能合并，否则一边失败会拖累另一边）
        ("收入/回购", lambda: src_income(sn), 75),
        ("官网", lambda: src_website(meta), 45),
        ("GitHub", lambda: src_github(meta, days=days), 45),
        ("Google News", lambda: src_news(meta, sn), 45),
        ("DuckDuckGo", lambda: src_web_search(meta, sn), 45),
    ]

    res = {}
    t0 = time.time()
    # max_workers 取源数量：全是 I/O，开满才能真正「等最慢的那个」
    pool = cf.ThreadPoolExecutor(max_workers=len(jobs))
    futs = {}
    for name, fn, _lim in jobs:
        futs[pool.submit(fn)] = name
    try:
        for fut in cf.as_completed(list(futs.keys()), timeout=max(j[2] for j in jobs)):
            name = futs[fut]
            try:
                res[name] = fut.result()
            except Exception as e:
                res[name] = {"src": name, "ok": False, "err": str(e)[:160]}
            r = res[name]
            log("  投研抓取 %-12s %s%s" % (name, "OK" if r.get("ok") else "失败",
                                         "" if r.get("ok") else "（%s）" % r.get("err", "")[:80]))
    except cf.TimeoutError:
        log("  投研抓取：到 %.0f 秒上限，未返回的源按超时跳过" % (time.time() - t0))
    # 没回来的源补一条记录，报告头部才能如实反映「哪个源没拿到」
    for fut, name in futs.items():
        if name not in res:
            res[name] = {"src": name, "ok": False, "err": "超时跳过（>%ds）" % 75}
            log("  投研抓取 %-12s 超时跳过" % name)
    pool.shutdown(wait=False)   # 不等卡住的线程：它们都是 daemon，随进程结束

    # 官网原文（HTML）用于定位官方 X 账号
    site_html = (res.get("官网") or {}).get("html") or ""
    try:
        r = src_x(meta, site_html=site_html, extra_links=extra_x_links,
                  extra_handles=extra_x_handles,
                  authority_handles=authority_x_handles, days=days, log=log)
    except Exception as e:
        r = {"src": "X", "ok": False, "err": str(e)[:160], "posts": [], "recent": []}
    res["X"] = r
    log("  投研抓取 %-12s %s%s" % ("X", "OK" if r.get("ok") else "失败",
                                 "" if r.get("ok") else "（%s）" % r.get("err", "")[:80]))
    # 生态广播号里点名本子网的贴文（官方号没动静时，这里往往是唯一的外部消息源）
    try:
        # 官方号已经抓过（含两条转发），这里跳过它，只补其它生态广播号
        _skip = [res["X"].get("handle")] if (res.get("X") or {}).get("ok") else []
        eco = x_eco_posts(meta, days=days, log=log, skip_handles=_skip)
    except Exception as e:
        eco = []
        log("  投研抓取 %-12s 失败（%s）" % ("X 生态圈", str(e)[:80]))
    if eco:
        res["X 生态圈"] = {"src": "X 生态圈", "ok": True, "items": eco,
                          "handle": "、".join(sorted({p["author"] for p in eco}))}
    else:
        # 「生态账号近期没提到本子网」是**正常结果**，不是抓取失败：
        # 打成 ✗ 会让日志和报告头部显示成故障，误导后续排查。
        res["X 生态圈"] = {"src": "X 生态圈", "ok": True, "items": [], "skipped": True,
                          "err": "近 %d 天生态账号无点名本子网的贴文" % int(days or 14)}
    # 合并进 X 源，好让 _ext_block 一并输出（生态号必须标成「生态账号提及」）
    if eco and res.get("X"):
        res["X"]["eco"] = eco
    return res


# ------------------------------------------------------------------ 交叉验证
def _norm_num(s):
    s = (s or "").replace(",", "").replace("τ", "").strip()
    m = re.search(r"[0-9]+(?:\.[0-9]+)?", s)
    return m.group(0) if m else ""


def _same_num(a, b, rel=0.006):
    """两个数字是否「同一个数」：允许不同的显示精度。

    各数据站对 alpha 价格的小数位数不一样（bittensor.ai 给 0.06775、
    dtaoscan 给 0.067747），字面不等但其实是同一个值；
    早期实现按字符串比对，把这类全标成「数据冲突」，属于误报。"""
    if a == b:
        return True
    try:
        x, y = float(a), float(b)
    except Exception:
        return False
    if x == y:
        return True
    scale = max(abs(x), abs(y), 1e-12)
    return abs(x - y) / scale <= rel


def build_consensus(sources):
    """把各源抓到的同一指标并列，标注「一致 / 冲突 / 单一来源 / 未获取」。"""
    keys = [("price", "Alpha 价格(τ)"), ("price_usd", "Alpha 价格(USD)"),
            ("tao_price", "TAO 价(USD)"), ("chg24", "24h 涨跌"),
            ("chg7d", "7日 涨跌"), ("mcap", "市值(τ)"), ("tvl", "TVL(τ)"),
            ("pool_tao", "池内 TAO"), ("pool_usd", "池内 TAO 折 USD"),
            ("alpha_staked", "已质押 alpha"),
            ("volume", "24h 量(τ)"), ("emission", "Emission"),
            ("emission_usd", "Emission 折 USD/日"),
            ("neurons", "神经元占用"), ("reg", "注册状态"),
            ("owner_locked", "Owner 锁仓"), ("unwinding", "正在解锁的 alpha"),
            ("unwind_pct", "解锁占比"),
            ("tempo", "Tempo")]
    rows = []
    for k, label in keys:
        vals = []
        for sname, r in sources.items():
            f = (r or {}).get("facts") or {}
            if k in f and f[k]:
                vals.append((sname, f[k]))
        if not vals:
            continue
        nums = [(_norm_num(v), sn) for sn, v in vals]
        nums = [(n, s) for n, s in nums if n]
        if len(vals) == 1:
            status = "单一来源"
        elif len(nums) >= 2 and all(_same_num(nums[0][0], n) for n, _s in nums[1:]):
            # 精度不同但同值（0.06775 vs 0.067747）算一致，不是冲突
            status = "多源一致" if nums[0][0] == nums[-1][0] else "多源一致（精度不同）"
        elif not nums:
            status = "单一来源"
        else:
            status = "⚠️ 数据冲突"
        rows.append({"key": k, "label": label, "vals": vals, "status": status})
    return rows


RESEARCH_COMPRESS_PROMPT = (
    "你是投研报告的定稿编辑，本轮只做一件事：**删字数**，一个数字都不许改、不许换。\n"
    "\n【删什么（从高到低）】\n"
    "1. 时间线里纯社区闲聊、没有事件价值的那几条；\n"
    "2. 同一件事在时间线里写了两遍的，删掉重复的一条（红色资讯条目与群聊条目重复时，保留红色的那条）；\n"
    "3. 跟踪清单压到 4 条，每条只留「盯什么 + 判断阈值」；\n"
    "4. 各处铺垫词、形容词、重复解释。\n"
    "\n【必须原样保留】\n"
    "- 全部小节标题与顺序：时间线 / 当前数据 / 收入与回购 / 📢 当日社区动态（初稿有这节时必须有）/ 预期跟踪；"
    "**「📢 当日社区动态」这一节绝对不许删**，只许压短其中每条的表述，"
    "[社区] 标记、⚠️未验证、人名和具体消息内容都要留着；\n"
    "- 所有数字、单位、来源标记 `[群聊 MM-DD]` `[taostats]` `[dtaoscan]` `[bittensor.ai]` `[官网]` `[GitHub]` `[新闻]` `[X]` `[搜索]`；\n"
    "- 人名、子网名、SN 编号；\n"
    "- 时间线里带 `<font color=\"red\">📰 ...</font>` 的红色资讯条目，以及末尾那行红色图例注明；\n"
    "- 「⚠️ 数据冲突」「无法证实」这类判定词。\n"
    "\n只输出压缩后的完整报告，不写说明。"
)

# ------------------------------------------------------------------ 数字校验
# 报告里出现的「带单位的数字」必须在素材里逐字存在。
# 为什么只查带单位的：正文里「3 条」「5 天内」这类序数/天数是指代不是事实，
# 真编造一定是价格、数量、百分比、commit 数这类带单位的量。
_UNIT_NUM = re.compile(
    r"([0-9][0-9.,]*)\s*"
    r"(τ|TAO|alpha|USD|%|blocks?|commits?|/30d|/day|天|日|倍|万|亿|K\b|M\b|B\b)")
# 这些是模型写分析时的正常量词，不算事实数字
_SAFE_NUMS = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "30", "60", "90", "100"}


_GH_LINE_RE = re.compile(r"^\s*[-*•]\s*.*(GitHub|代码仓库|仓库提交)", re.I)
# 判断「只说了时间、没说内容」的半句话：出现这些词但没有任何内容关键词
_GH_VAGUE_ONLY = re.compile(r"提交集中|集中在(当日|今日|当天)|集中在\s*\d{1,2}-\d{1,2}")
# 英文残留：连续两个及以上英文单词、或 PR/branch 痕迹
_EN_CHUNK = re.compile(r"[A-Za-z][A-Za-z'\-]{1,}(?:\s+[A-Za-z][A-Za-z'\-]{1,}){1,}")
_EN_NOISE = re.compile(r"merge pull request|merge branch|merge remote|#\d{2,}|\b(?:fix|feat|chore|refactor)/", re.I)


def tidy_github_line(digest, gh_sum=None, log=print):
    """GitHub 那条事件的代码层兜底：保证「次数 + 中文内容」，清掉英文残留。

    为什么要代码层兜底：提示词里已经写得很死（不许只写「提交集中在当日」、
    不许出现英文分支名/PR 编号），但实测模型仍会照抄——因为它看得到英文原文。
    现在素材层已经不给英文了，这里再兜一道，防止模型自己编出英文词。
    """
    if not digest:
        return digest, 0
    gh_sum = gh_sum or {}
    fix_n = 0
    out = []
    for ln in digest.split("\n"):
        if not _GH_LINE_RE.search(ln):
            out.append(ln)
            continue
        s = ln
        # ① 删掉英文残留片段（保留仓库名 a/b 形式与子网名）
        def _kill_en(m):
            seg = m.group(0)
            if "/" in seg and len(seg.split()) == 1:
                return seg                       # owner/repo 形态，保留
            if re.match(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*$", seg) and len(seg.split()) <= 3:
                return seg                       # 人名形态，保留
            return ""
        if _EN_NOISE.search(s) or len(_EN_CHUNK.findall(s)) >= 1:
            s2 = _EN_NOISE.sub("", s)
            s2 = _EN_CHUNK.sub(_kill_en, s2)
            s2 = re.sub(r"[（(]\s*[)）]", "", s2)
            s2 = re.sub(r"\s{2,}", " ", s2).strip()
            s2 = re.sub(r"·\s*·", "·", s2)
            if s2.rstrip(" ·，,、") != s.rstrip(" ·，,、"):
                s = s2
                fix_n += 1
        # ② 没写「最后一次更新」或没给次数、没给内容 → 换成摘要完整句
        core = re.sub(r"[（(][^)）]*[)）]", "", s)
        has_num = bool(re.search(r"\d+\s*次", core))
        has_last = bool(re.search(r"最后一次|最近一次", core))
        has_content = bool(re.search(
            r"修复|新增|优化|重构|测试|文档|性能|涉及|模块|以.{1,6}为主|改的是", core))
        vague = bool(_GH_VAGUE_ONLY.search(core))
        fb = bool(gh_sum.get("fallback"))
        # 兜底场景：次数本来就取不到，正确写法是「次数未取到」；
        # 这时既不能因为缺 N 次而反复改写，更不能容忍模型编一个次数出来。
        has_missing = bool(re.search(r"未取到|取不到|无法统计", core))
        bad = (vague or (not has_content) or (not has_last)
               or (not (has_num or (fb and has_missing)))
               or (fb and has_num))
        if bad and gh_sum.get("sentence"):
            tail = ""
            mstat = re.search(r"[·|]\s*((?:✅|⚠️)[^·|]*)$", s)
            if mstat:
                tail = " · " + mstat.group(1).strip()
            date_m = re.match(r"^\s*([-*•]\s*)?((?:\d{4}-)?\d{2}-\d{2})\s*·\s*", s)
            head = (date_m.group(1) or "- ") + (date_m.group(2) + " · ") if date_m else "- "
            s = head + gh_sum["sentence"] + tail
            fix_n += 1
        out.append(s)
    if fix_n:
        log("GitHub 条目改写：%d 处（补「最后一次更新+次数+内容」、清英文残留）" % fix_n)
    return "\n".join(out), fix_n


def _take_section(digest, marker):
    """从报告里抠出一整节（标题行到下一个 **标题** 行前）。返回 (去掉该节的正文, 节文本)。"""
    lines = (digest or "").splitlines()
    head = re.compile(r"^\s*\*\*.+\*\*\s*$")
    start = next((i for i, ln in enumerate(lines)
                  if marker in ln and head.match(ln)), None)
    if start is None:
        return digest, ""
    end = next((j for j in range(start + 1, len(lines)) if head.match(lines[j])),
               len(lines))
    section = "\n".join(lines[start:end]).strip()
    rest = "\n".join(lines[:start] + lines[end:]).strip()
    return rest, section


def _insert_section(digest, section, before):
    """把保存的整节插回报告的指定标题之前（压缩/整理误删后的代码兜底）。"""
    lines = (digest or "").splitlines()
    pos = next((i for i, ln in enumerate(lines)
                if before in ln and ln.lstrip().startswith("**")), None)
    if pos is None:
        return (digest.rstrip() + "\n\n" + section).strip()
    return "\n".join(lines[:pos] + ["", section, ""] + lines[pos:]).strip()


def tidy_dates(digest, log=print):
    """时间线里年份只保留一次（用户要求：同一年份不要反复出现 2026 年）。

    提示词已经要求了，但模型经常照抄成每条都带年份；这里在代码层兜底：
    以「首条出现年份」为准，其后同年的 `2026-10-30` 一律缩成 `10-30`；跨年保留。
    """
    if not digest:
        return digest, 0
    lines = digest.split("\n")
    base = None
    n = 0
    out = []
    for ln in lines:
        m = re.match(r"^(\s*[-\d.、*]*\s*)(\d{4})-(\d{2}-\d{2})(.*)$", ln)
        if m and ("·" in ln or "已验证" in ln or "未验证" in ln):
            yr = m.group(2)
            if base is None:
                base = yr
                out.append(ln)          # 首条保留完整年份
                continue
            if yr == base:
                out.append("%s%s%s" % (m.group(1), m.group(3), m.group(4)))
                n += 1
                continue
        out.append(ln)
    if n:
        log("日期缩写：%d 处重复年份已省略（只保留首条年份）" % n)
    return "\n".join(out), n


def tidy_layout(digest, log=print):
    """手机端排版兜底：时间线每条一行、条间不留空行、统一 `- ` 前缀、
    删掉「与上次相比无变化」这类占位条目（用户要的是「重要事件」，不是元评估）。

    模型经常把条目写成段与段之间空一行，TG 里读起来像一屏一屏断开，很难扫。
    """
    if not digest:
        return digest, 0
    _EV = re.compile(r"^\s*(?:[-*•]\s*)?(\d{4}-\d{2}-\d{2}|\d{2}-\d{2})\s*[·•]")
    out, fixed, dropped = [], 0, 0
    lines = digest.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        # 空行：仅当下一行不是列表条目时保留（保证段与段之间还有一个空行）
        if not s:
            look = ""
            for j in range(i + 1, min(i + 3, len(lines))):
                if lines[j].strip():
                    look = lines[j].strip()
                    break
            if _EV.match(look):
                i += 1
                continue
            out.append(ln)
            i += 1
            continue
        if _EV.match(s):
            # 占位条目：只讲「无变化/延续上次判断」且没有实际事件的，直接删
            if re.search(r"无变化|无实质变化|延续上次判断", s) and not re.search(r"✅已验证|⚠️未验证", s[:12]):
                dropped += 1
                i += 1
                continue
            if not re.match(r"^\s*[-*•]\s", ln):
                out.append("- " + s)
                fixed += 1
            else:
                out.append(ln)
            i += 1
            continue
        out.append(ln)
        i += 1
    txt = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if fixed or dropped:
        log("排版整理：%d 条补 `- ` 前缀，%d 条占位条目已删" % (fixed, dropped))
    return txt, fixed + dropped


# ------------------------------------------------------------------ 最新资讯并入时间线
# v1.15.0：用户指出「📰 最新资讯」与「🗓 重要事件时间线」大面积重复。
# 提示词已改为四段（资讯并入时间线并标红）；本函数是代码层兜底，处理模型偶发的旧习惯：
#   1) 残留独立的「📰 最新资讯」段 → 逐条并入时间线（标红加粗），与原时间线条目去重；
#   2) 时间线内部同一件事写了两遍 → 保留信息更全的那条；
#   3) 无论哪条路径，时间线段末尾都补一行红色图例注明。
_RED_NEWS_RE = re.compile(r"<font\s+color=[\"']?red[\"']?\s*>.*?</font>", re.I | re.S)
_NEWS_SECTION_RE = re.compile(r"^\*\*📰\s*最新资讯\*\*", re.I | re.M)
_TIME_SECTION_RE = re.compile(r"^\*\*(?:🗓\s*)?重要事件时间线\*\*", re.I | re.M)
_LEGEND_LINE = "注：<font color=\"red\">**红色加粗**</font>为最新资讯"
_EXT_SOURCE_RE = re.compile(r"\[(?:X|新闻|官网|GitHub|搜索|X生态圈提及)\]")
_BIGRAM = lambda s: {s[i:i + 2] for i in range(len(s) - 1)}


def _line_sim(a, b):
    """两条文本是否讲同一件事：字符二元组 Jaccard + 覆盖度。"""
    a = re.sub(r"<[^>]+>", "", re.sub(r"^\s*[-*•]\s*", "", a or ""))
    b = re.sub(r"<[^>]+>", "", re.sub(r"^\s*[-*•]\s*", "", b or ""))
    ga, gb = _BIGRAM(a), _BIGRAM(b)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return max(inter / max(1, len(ga | gb)),
               (inter / max(1, min(len(ga), len(gb)))) * 0.9 if abs(len(ga) - len(gb)) <= 6 else 0.0)


def tidy_news_in_timeline(digest, log=print):
    """把最新资讯并入时间线、标红加粗，并在段尾加图例注明。返回 (文本, 改动数)。"""
    if not digest:
        return digest, 0
    changes = 0
    lines = digest.split("\n")

    # 段边界：标题以 ** 开头，到下一个 ** 标题前
    def section_bounds(pattern):
        for i, ln in enumerate(lines):
            if pattern.match(ln.strip()):
                j = i + 1
                while j < len(lines) and not re.match(r"^\*\*", lines[j].strip()):
                    j += 1
                return i, j
        return None

    tl = section_bounds(_TIME_SECTION_RE)
    if not tl:
        return digest, 0

    # 0) 先清掉文档里任意位置的旧图例行（模型有时把它写到别的段末尾），最后统一只留一条
    n0 = len(lines)
    lines = [ln for ln in lines
             if not ("为最新资讯" in ln and "红色" in ln and "<font" in ln)]
    if len(lines) != n0:
        tl = section_bounds(_TIME_SECTION_RE)  # 行号已变，重新定位
        changes += n0 - len(lines)

    # 1) 残留的独立资讯段：整段取出，逐条转成时间线格式，最后删除该段
    incoming = []
    news = section_bounds(_NEWS_SECTION_RE)
    if news:
        ns, ne = news
        tl_s0, tl_e0 = tl
        existing = [ln for ln in lines[tl_s0 + 1:tl_e0] if ln.strip()]
        for ln in lines[ns + 1:ne]:
            s = ln.strip()
            if not s.startswith(("-", "•")):
                continue
            body = re.sub(r"^\s*[-*•]\s*", "", s)
            m = re.match(r"^(\d{1,2}-\d{1,2})\s*[·•]\s*(.*)$", body)
            date, rest = (m.group(1), m.group(2)) if m else ("", body)
            rest = re.sub(r"\s*\[X\]\s*$", "", rest).strip()
            # 与时间线已有条目去重：同一件事不重复写
            if any(_line_sim(rest, t) >= 0.5 for t in existing):
                continue
            status = "⚠️未验证（外部资讯）"
            rec = ("- %s · <font color=\"red\">**📰 %s**</font> · %s" % (date, rest, status)
                   if date else "- <font color=\"red\">**📰 %s**</font> · %s" % (rest, status))
            incoming.append(rec)
        # 删除旧资讯段（含其后空行），行号整体前移
        k = ne
        while k < len(lines) and not lines[k].strip():
            k += 1
        del lines[ns:k]
        changes += 1
        tl = section_bounds(_TIME_SECTION_RE)
        log("资讯并入时间线：%d 条红色资讯，独立资讯段已删除" % len(incoming))

    # 2) 重写时间线段：合并 incoming、给带外部来源的条目补红字、内部去重、补图例
    tl_start, tl_end = tl
    seg = lines[tl_start + 1:tl_end]
    # 图例行单独摘出，最后重排到末尾
    legend = next((ln for ln in seg if "为最新资讯" in ln), None)
    items = [ln for ln in seg if ln.strip() and ln != legend]
    # 2a) 模型已写进时间线、但带外部来源标记又忘了标红的条目 → 补红字
    reded = 0
    for i, ln in enumerate(items):
        if "<font" in ln or "📰" in ln or not ln.strip().startswith(("-", "•")):
            continue
        if not _EXT_SOURCE_RE.search(ln):
            continue
        s = re.sub(r"^\s*[-*•]\s*", "", ln.strip())
        # 拆成 日期 · 正文 · 验证状态（验证状态固定留在红字外）
        vm = re.search(r"\s*·\s*((?:✅已验证|⚠️未验证)[^·]*)$", s)
        status = (" · " + vm.group(1).strip()) if vm else ""
        s = s[:vm.start()] if vm else s
        dm = re.match(r"^(\d{1,2}-\d{1,2})\s*[·•]\s*(.*)$", s)
        if dm:
            date, body = dm.group(1), dm.group(2).strip()
            items[i] = "- %s · <font color=\"red\">**📰 %s**</font>%s" % (date, body, status)
        else:
            items[i] = "- <font color=\"red\">**📰 %s**</font>%s" % (s.strip(), status)
        reded += 1
    if reded:
        log("时间线补标红：%d 条外部资讯" % reded)
        changes += reded

    # 2b) 并入 incoming（再次去重）
    for rec in incoming:
        if not any(_line_sim(rec, it) >= 0.5 for it in items):
            items.append(rec)
            changes += 1
    # 2c) 内部去重：保留信息更全 / 红色优先
    kept, drop = [], set()
    for a_i, i in enumerate(x for x in range(len(items)) if x not in drop):
        if i in drop:
            continue
        ia = items[i]
        for j in range(i + 1, len(items)):
            if j in drop or _line_sim(ia, items[j]) < 0.6:
                continue
            keep_i = (len(items[j]) > len(ia)) or ("📰" in ia and "📰" not in items[j])
            drop.add(j if keep_i else i)
            if not keep_i:
                break
            ia = items[j]
    if drop:
        items = [it for i, it in enumerate(items) if i not in drop]
        log("时间线去重：删除 %d 条重复事件" % len(drop))
        changes += len(drop)

    # 2d) 重建时间线段
    new_seg = items + ([legend] if legend else [_LEGEND_LINE])
    if not legend:
        changes += 1
    lines[tl_start + 1:tl_end] = new_seg

    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out, changes


# ------------------------------------------------------------------ 时间线：倒序 + 上限 + 超限跟踪回填
# v1.23.0 / v1.23.1 用户要求：
#   1) 时间线按真实日期倒序——最新事件排最前面（原来模型常按旧→新写，首条最旧）；
#   2) 每份报告时间线最多保留 TIMELINE_MAX_EVENTS 条，超出的条目不在时间线里显示；
#   3) 「📌 预期跟踪」里建一份**进展台账**：**所有有进展的事件都要体现**（不只是被截掉的），
#      每条写清「什么时间 + 什么事情 + 现在进展如何」；其中被时间线超限截断、仍有进展的事件，
#      额外标注「主线未显示」，保证截断不丢进度、且进度现状可读。
# 放在 tidy_news_in_timeline 之后调用：那一步会重建时间线段、去重、加图例。
TIMELINE_MAX_EVENTS = 10
_TL_ITEM_RE = re.compile(r"^\s*[-*•]\s*")
_TL_DATE_ANY_RE = re.compile(r"^\s*[-*•]?\s*(?:(\d{4})-)?(\d{1,2})-(\d{2})\s*[·•]")


def _tl_event_date(line, base_year):
    """从一条时间线事件解析 (year, month, day)；无日期返回 None。

    首条常写完整 `2026-11-13`，其余写 `11-14`。缺年份时用 base_year 补齐。
    对「跨年但只写了月日」的边界（如 12-30 与 01-02 同报告）：若月日明显大于
    带年份条目的月日，视为上一年——这里用简单的「12 月且.base 是 1 月」兜底。
    """
    m = _TL_DATE_ANY_RE.match(line or "")
    if not m:
        return None
    yr = int(m.group(1)) if m.group(1) else base_year
    mo, dy = int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= dy <= 31):
        return None
    return (yr, mo, dy)


_TL_PROGRESS_CUE_RE = re.compile(
    r"已兑现|已证伪|进行中|条件未触发|兑现|证伪|解仓|解锁|unwind|上线|开.*注册|"
    r"注册.*(关|开)|回购|销毁|支付|到账|跌破|突破|达标|未达标|恢复|重启|"
    r"达成|落地|通过|批准|关闭|开启|交割|更名|改名|结果|确认|首次|"
    r"发射|发布|推出|升级|完成|启动|签署|合作",
    re.I)
# 进展状态短语：把裸动词翻成一句可读的「现在进展」，弱词/泛词给更明确的措辞
_TL_STATE_PHRASE = {
    "发布": "新内容发布", "推出": "新内容推出", "上线": "已上线", "升级": "已升级",
    "完成": "已完成", "启动": "已启动", "开启": "已开启", "签署": "已签署",
    "合作": "达成合作", "落地": "已落地", "达成": "已达成", "通过": "已通过",
    "批准": "已批准", "确认": "已确认", "首次": "首次发生", "结果": "结果已出",
    "恢复": "已恢复", "重启": "已重启", "突破": "已突破", "达标": "已达标",
}


def _tl_progress_state(line, track_keywords):
    """判断一条事件是否「有进展」，并返回进展状态短语。

    返回 (has_progress, state)。state 形如「已落地 / 进行中 / 已兑现 / 数据更新」——
    供台账「现在进展如何」用。信号：进展动词/判定词，或命中预期跟踪段专名关键词。
    """
    s = _RED_STRIP_RE.sub(r"\1", line or "")
    s = re.sub(r"^\s*[-*•]\s*", "", s).strip()
    cues = [x.group(0) for x in _TL_PROGRESS_CUE_RE.finditer(s)]
    low = s.lower()
    kw_hit = any(k and len(k) >= 3 and k in low for k in track_keywords)
    if not cues and not kw_hit:
        return False, ""
    # 多个信号词时按「判定强度」取最高的那个，别被「首次」这类弱词抢先
    allcues = " ".join(cues)
    if any(w in allcues for w in ("已兑现", "证伪", "未达标", "跌破")):
        state = "已兑现/已证伪"
    elif any(w in allcues for w in ("解仓", "解锁", "回购", "销毁", "支付", "到账", "交割")):
        state = "已发生（链上/资金动作）"
    elif any(w in allcues for w in ("进行中", "未触发", "恢复", "重启")):
        state = "进行中/推进中"
    elif cues:
        # 取措辞最明确的一个信号词（跳过「首次」这类弱词）
        picked = next((c for c in cues if c in _TL_STATE_PHRASE), cues[0])
        state = _TL_STATE_PHRASE.get(picked, "新进展：%s" % picked)
    else:
        state = "相关进展（命中跟踪项）"
    return True, state


def _tl_ledger_line(line, state, base_year, truncated):
    """把一条时间线事件改写成台账条目：`时间 · 事情 · 现在进展`。

    truncated=True 时加「主线未显示」标注，告诉读者该事件因时间线限 10 条被截掉。
    """
    s = _RED_STRIP_RE.sub(r"\1", line or "")
    s = re.sub(r"^\s*[-*•]\s*", "", s).strip()
    d = _tl_event_date(line, base_year)
    if d:
        yr, mo, dy = d
        date_s = ("%04d-%02d-%02d" if yr != base_year else "%02d-%02d") % (
            (yr, mo, dy) if yr != base_year else (mo, dy))
    else:
        date_s = "日期未标注"
    # 去掉开头残留日期、markdown 加粗、**📰** 前缀；保留验证状态与来源标记 [X]/[群聊]
    body = re.sub(r"^(?:\d{4}-)?\d{1,2}-\d{2}\s*[·•]\s*", "", s).strip()
    body = body.replace("**", "").strip()
    body = re.sub(r"^📰\s*", "", body).strip()
    flag = " · ⚠️主线未显示（时间线超限移入）" if truncated else ""
    return "- %s · %s · 现在进展：%s%s" % (date_s, body, state, flag)


def _tl_track_keywords(digest):
    """从「📌 预期跟踪」段抽专名关键词（跳过判定词与虚词）。"""
    kws = set()
    m = re.search(r"\*\*(?:📌\s*)?预期跟踪\*\*\s*\n((?:-\s*.+\n?)+)", digest or "")
    if not m:
        return kws
    stop = {"进行中", "条件未触发", "已兑现", "已证伪", "本次", "上次", "新增", "跟踪",
            "兑现", "证伪", "判定", "输入", "数据", "观察", "关注", "继续", "即兑现", "即证伪"}
    for ln in m.group(1).split("\n"):
        s = _RED_STRIP_RE.sub(r"\1", ln.strip())
        s = re.sub(r"^[-•]\s*", "", s)
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{3,8}", s):
            t = tok.strip()
            if t and t not in stop and t.lower() not in _GENERIC_ANCHOR_STOP:
                kws.add(t.lower())
    return kws


def sort_cap_timeline(digest, log=print, max_events=TIMELINE_MAX_EVENTS):
    """时间线按日期倒序（最新在前）、最多留 max_events 条；超出的跟踪进度回填预期跟踪段。

    返回 (digest, stats)。图例行不参与排序/计数，始终留在时间线末尾。首条（最新）
    保留完整年份，其余同年条目缩写为月-日（tidy_dates 的既有口径）。
    """
    stats = {"total": 0, "kept": 0, "dropped": 0, "backfilled": 0}
    if not digest:
        return digest, stats
    lines = digest.split("\n")
    # 段边界：标题到下一个 ** 标题前
    ts = next((i for i, ln in enumerate(lines)
               if _TIME_SECTION_RE.match(ln.strip())), None)
    if ts is None:
        return digest, stats
    te = ts + 1
    while te < len(lines) and not re.match(r"^\*\*", lines[te].strip()):
        te += 1
    seg = lines[ts + 1:te]
    legend = next((ln for ln in seg if "为最新资讯" in ln), None)
    items = [ln for ln in seg if ln.strip() and ln != legend]
    if not items:
        return digest, stats

    # 基准年份：优先取段内出现的完整年份（通常是旧事件那条），否则用当年
    base_year = datetime.datetime.now(TZ).year
    for ln in items:
        mm = re.match(r"^\s*[-*•]?\s*(\d{4})-\d{2}-\d{2}\s*[·•]", ln)
        if mm:
            base_year = int(mm.group(1))
            break

    dated, undated = [], []
    for ln in items:
        d = _tl_event_date(ln, base_year)
        (dated if d else undated).append((d, ln))
    # 倒序：最新事件在最前；同日保持原相对顺序（Python sort 稳定）
    dated.sort(key=lambda x: x[0], reverse=True)
    # undated 已经是 (None, ln) 形式，不能再包一层元组
    ordered = dated + undated
    kept = [ln for _d, ln in ordered[:max_events]]
    dropped = [ln for _d, ln in ordered[max_events:]]
    stats.update(total=len(items), kept=len(kept), dropped=len(dropped))

    # 首条保留完整年份、其余同年缩写；倒序后首条是最新的，年份要同步到它
    rebuilt = []
    for i, ln in enumerate(kept):
        d = _tl_event_date(ln, base_year)
        if i == 0 and d:
            yr, mo, dy = d
            rebuilt.append(re.sub(r"^(\s*[-*•]?\s*)(?:\d{4}-)?(\d{1,2})-(\d{2})(\s*[·•])",
                                  lambda m: "%s%04d-%02d-%02d%s" % (m.group(1), yr, mo, dy, m.group(4)),
                                  ln, count=1))
        elif d and i > 0:
            yr, mo, dy = d
            if yr == base_year:  # 同年才缩写，跨年保留完整年份
                rebuilt.append(re.sub(r"^(\s*[-*•]?\s*)(?:\d{4}-)?(\d{1,2})-(\d{2})(\s*[·•])",
                                      lambda m: "%s%02d-%02d%s" % (m.group(1), mo, dy, m.group(4)),
                                      ln, count=1))
            else:
                rebuilt.append(ln)
        else:
            rebuilt.append(ln)
    new_seg = rebuilt + ([legend] if legend else [])
    lines[ts + 1:te] = new_seg
    out = "\n".join(lines)

    # 进展台账（v1.23.1）：「📌 预期跟踪」里体现**所有有进展的事件**（含时间线保留的与
    # 被截断的），每条写清「时间 + 事情 + 现在进展如何」；被截断的标注「主线未显示」。
    kws = _tl_track_keywords(out)
    dropped_set = set(dropped)
    ledger = []
    for _d, ln in ordered:  # ordered = 全部事件已按日期倒序（最新在前），与主线一致
        has, state = _tl_progress_state(ln, kws)
        if has:
            ledger.append(_tl_ledger_line(ln, state, base_year, ln in dropped_set))
    if ledger:
        note = ("**⏳ 进展台账**（所有有进展事件的现状；时间线仅显示最新 %d 条，"
                "标注「主线未显示」者因超限被截掉、但在此保留其进度）：\n%s"
                % (max_events, "\n".join(ledger)))
        out = _insert_section(out, note, "📌 预期跟踪")
        stats["backfilled"] = sum(1 for ln in dropped if _tl_progress_state(ln, kws)[0])
        log("时间线倒序/限条：保留最新 %d 条，截掉 %d 条；进展台账收录全部 %d 条有进展事件"
            "（其中被截断 %d 条已标注主线未显示）"
            % (len(kept), len(dropped), len(ledger), stats["backfilled"]))
    else:
        log("时间线倒序/限条：保留最新 %d 条，截掉 %d 条（无有进展事件）"
            % (len(kept), len(dropped)))
    return out, stats


def audit_numbers(digest, source_text, log=print):
    """检查报告里的带单位数字是否都在素材里。返回 (未命中列表, 命中率)。"""
    src = source_text or ""
    # 素材里的数字统一去掉千分位再比对，避免 "5,051,237" 与 "5051237" 误判
    src_nums = set()
    for m in re.finditer(r"[0-9][0-9.,]*", src):
        raw = m.group(0)
        src_nums.add(raw)
        src_nums.add(raw.replace(",", ""))
        src_nums.add(raw.rstrip(".").rstrip(","))
    miss, total = [], 0
    for m in _UNIT_NUM.finditer(digest or ""):
        num, unit = m.group(1), m.group(2)
        num = num.rstrip(".")
        if num in _SAFE_NUMS:
            continue
        total += 1
        cands = {num, num.replace(",", ""), num.replace(",", "").rstrip("0").rstrip(".")}
        # 允许「25.33K」这类单位缩写：数字部分对上即可
        if any(c in src_nums for c in cands if c):
            continue
        # 小数被四舍五入：容忍一位小数内的近似值
        hit = False
        try:
            v = float(num.replace(",", ""))
            for s in src_nums:
                try:
                    if abs(float(s.replace(",", "")) - v) <= max(0.01, abs(v) * 0.005):
                        hit = True
                        break
                except Exception:
                    continue
        except Exception:
            pass
        if not hit:
            miss.append("%s %s" % (num, unit))
    rate = (1 - len(miss) / total) if total else 1.0
    if miss:
        log("⚠️ 数字核对：%d/%d 个带单位数字在素材中找不到：%s"
            % (len(miss), total, "；".join(dict.fromkeys(miss))[:400]))
    else:
        log("✅ 数字核对通过：%d 个带单位数字均可在素材中定位" % total)
    return miss, rate


def strip_unknown_numbers(digest, miss):
    """对未命中的数字做降级：把数字替换成「（数值未核实）」并顺手修掉多余空格。

    比起删掉整句，降级更保留信息量；但绝不能放任可疑数字原样流出。"""
    if not miss:
        return digest, 0
    out, n = digest, 0
    for item in miss:
        num = item.split(" ")[0]
        pat = re.compile(r"(?<![\d.,])\s*" + re.escape(num) + r"\s*(?![\d.])")
        if pat.search(out):
            out = pat.sub("（数值未核实）", out, count=3)
            n += 1
    # 清掉替换后产生的怪空格：「锁仓 （数值未核实）%" → 「锁仓（数值未核实）%」
    out = re.sub(r"\s+（数值未核实）\s*", "（数值未核实）", out)
    out = re.sub(r"（数值未核实）\s+", "（数值未核实）", out)
    return out, n


# 报告里的来源标记 → collect() 里的源名
_MARK_OF = {"[bittensor.ai]": "bittensor.ai", "[taostats]": "taostats", "[dtaoscan]": "dtaoscan",
            "[官网]": "官网", "[GitHub]": "GitHub", "[新闻]": "Google News", "[X]": "X",
            "[搜索]": "DuckDuckGo"}
# [档案] 不在上面的映射里：它引用的是本地档案里的历史留档（群聊原话 / 此前投研），
# 不是本次抓取的外部源，所以**永远不该被判为「抓取失败的源被当引用」**。
_ARCHIVE_MARK = "[档案]"


def audit_sources(digest, sources, log=print):
    """抓取失败的源不许被当成引用。

    实测踩过：X 三个候选账号都不存在，报告却写「官方 X 账号（hotfloat_ai / hotfloatai）不存在 [X]」——
    这条结论本身没错，但从"没抓到"推到"账号不存在"属于过度推断，而且把程序内部探测用的账号名当成了
    公开事实。这里直接把这些行标出来，交给模型改写/删除，之后还会巡一遍兜底。

    「已明确写成 未获取/无法核实」的行是合规写法，不算违规引用 —— 那正是我们要求的降级结果。
    """
    if not digest:
        return digest, []
    ok_marks = {mk for mk, name in _MARK_OF.items() if (sources.get(name) or {}).get("ok")}
    bad = {mk for mk in _MARK_OF if mk not in ok_marks}
    if not bad:
        return digest, []
    ok_phrases = ("未获取", "无法核实", "抓取失败", "未抓到", "无数据")
    lines, hit = digest.split("\n"), []
    for i, ln in enumerate(lines):
        marks = {mk for mk in bad if mk in ln}
        if not marks:
            continue
        if any(p in ln for p in ok_phrases):
            continue          # 已降级成「未获取」，合规
        hit.append((i, ln, sorted(marks)))
    return digest, hit


def rewrite_bad_sources(digest, hits, sources, llm=None, log=print):
    """让模型把「引用了抓取失败源」的段落改写掉：要么删，要么降到「未获取」。"""
    if not hits:
        return digest
    bad = sorted({mk for _, _, mks in hits for mk in mks})
    failed = "、".join("%s（失败原因：%s）" % (mk, (sources.get(_MARK_OF[mk]) or {}).get("err", "未获取")[:80])
                       for mk in bad)
    listing = "\n".join("- " + ln for _, ln, _ in hits[:12])
    body = (
        "下面这份报告的若干行引用了**抓取失败的来源**：%s。\n"
        "抓取失败意味着「没拿到数据」，**不等于「该渠道不存在／没有动作」**——"
        "不能由「没抓到」推出「账号不存在」「没有公告」这类结论。\n\n"
        "【必须改写的行】\n%s\n\n【完整报告】\n%s\n\n"
        "改写规则：引用失败源的行，要么整条删除，要么把结论降级为"
        "「%s 未获取，无法核实」，并且**不得**出现在「事实基线」里当作已知事实。"
        "其余内容一字不动，小节标题与顺序保持原样，篇幅不得变长。只输出改写后的完整报告。"
        % (failed, listing, digest, "／".join(bad))
    )
    try:
        out = (llm(body) if llm else "").strip()
    except Exception as e:
        log("来源引用改写失败（忽略）：%s" % e)
        return digest
    if not out or len(out) < len(digest) * 0.5:
        return digest
    return out


# ------------------------------------------------------------------ 提示词
_RESEARCH_PROMPT_TMPL = (
    "你是 Bittensor 子网的买方研究员。输入会分成几块：【本地群聊档案】（Discord 真实讨论，带日期时间，"
    "**可能没有**）、【此前投研结论】（该子网档案里存的历史报告，只有做过投研才会有）、"
    "【链上/数据站事实】（抓取到的页面字段）、【官方与公开信息】（官网、GitHub、新闻、搜索、X）、"
    "【交叉验证矩阵】（同指标多源并列及其一致性判定）、"
    "【收入与回购】（owner cut 收入、协议回购逐日汇总、所有者回购 30 天/终身、净 emission）。"
    "另外会额外给出一段【代码预判的硬信号】——那是程序根据链上字段判定的客观状态"
    "（owner 解仓、神经元满员、注册关闭等），**必须写入报告**，不得忽略或弱化。\n"
    "\n【工作顺序（必须遵守）】\n"
    "先读【本地群聊档案】与【此前投研结论】，把「档案里已经确认过的事实」当成**起点**；"
    "再读外部源，专门找**上次报告之后的新增变化**（新公告、新解仓、新数据、群里新出现的说法），"
    "把这些变化写进时间线。**已经在上次报告里写过、这次没有新进展的事，不要重复罗列**。\n"
    "报告只回答四个问题：**什么时候发生了什么（时间线，外部最新资讯也写在这里并标红）· "
    "现在什么价（当前数据）· 收入多少、有没有回购（收入与回购）· 接下来盯什么（预期跟踪）**。"
    "除此之外的分析、感慨、免责声明、方法论说明，一律不要。\n"
    "\n【最新资讯并入时间线，不再单列（本条优先级极高）】\n"
    "群聊只是「社区在聊什么」，**不等于外部世界发生了什么**。用户明确要求："
    "不能出现「明明有新资讯，报告里却一条都没有」，也**不许把资讯单列成段**——"
    "独立的「📰 最新资讯」段与时间线大面积重复，已取消。所以：\n"
    "1. 输入里只要 [X] / [X 生态圈提及] / [新闻] / [官网] / [GitHub] 段里**有任何一条**"
    "近 %(days)d 天内的新内容，就**直接作为一条写进「🗓 重要事件时间线」**，按真实日期倒序归位（最新在前）；"
    "时间线最多 10 条，太多时优先保留最新的，超出的不必硬塞（代码层会截断并把其中的跟踪进度补进预期跟踪）；\n"
    "2. 特别是 [X] 段里标了「（用户提供）」的贴文——那是用户自己贴过来要你看的，"
    "**必须逐条写进去**，写清发布者、日期、原话要点；\n"
    "3. 每条来自外部源（X / 新闻 / 官网 / GitHub / 搜索）的事件，"
    "**事件正文必须用 `<font color=\"red\">**📰 正文**</font>` 红色加粗**："
    "日期写在红字外（`- 11-18 · `），验证状态写在红字后（如 ` · ✅已验证（官方X+官网）`）；"
    "同一条里红字只包事件正文，不要把日期、验证状态一起包进去；\n"
    "4. 群聊里的事件、纯群内传闻**不要标红**；只有外部世界的新消息才标红，"
    "让读者一眼分清「社区在聊」和「外面发生了什么」；\n"
    "5. 一件事同时在群聊和外部源出现的，只写**一条**，按外部源归位并标红，不要写两遍；\n"
    "6. 官方号时间线里看似日常的运营贴，只要含**新事实**（新合作、新上线、新功能、新数据、"
    "人事变动、被谁提到），就算资讯；纯表情、纯口号、纯抽奖不算。\n"
    "\n【事实纪律（违反即作废）】\n"
    "1. 每条事实后面必须带来源标记：`[群聊 MM-DD]`、`[档案]`（引用档案里的历史投研结论时用）、"
    "`[taostats]`、`[bittensor.ai]`、`[dtaoscan]`、`[官网]`、`[GitHub]`、`[新闻]`、`[X]`、`[搜索]`；"
    "没标记的句子只能是分析判断，且必须以「判断：」开头。\n"
    "2. 数字必须来自输入原文，**禁止换算、禁止四舍五入改写、禁止补全缺失位**；抓不到就写「未获取」。\n"
    "3. 群聊里的传闻/猜测/玩笑**不得**写成事实：写成「群内有人称（MM-DD，谁）：…」。"
    "玩笑、反讽、假设句（if/imagine/假如）不算证据。\n"
    "4. 共现≠因果。某公司/人物与某子网同日出现，不得写成「某公司支持该子网」。\n"
    "5. 人名、子网名、代币名、SN 编号一律照抄，不音译、不改写大小写。"
    "**子网改名时以输入里的现名为准**：若给了【⚠️ 改名提示】，通篇用现名，"
    "旧名只能写成「（原名 xxx）」；群聊或旧报告里的旧名不是当前名称。"
    "同理，若给了【用户反馈】且其中指出名称/身份有误，一律以用户反馈为准。\n"
    "6. 多源冲突时**在事件里直接说明冲突**（如「A 站 0.0025 / B 站 0.0027，未证实」），"
    "并标 `⚠️未验证`，不要替读者选一个，也不要为此单开一段。\n"
    "7. 输入里没有的公告、合作、融资、上所，一律不许出现。宁可报告短，不许编。\n"
    "8. **没有本地档案时**：绝对不许出现「群内有人说」「社区在传」「大家认为」这类表述，"
    "只依据外部源写时间线。\n"
    "9. 年份只在时间线首条出现一次，后面只写 `月-日`；价格只写当前价，不写历史价对比、不写涨跌幅。\n"
    "10. **X 贴文只能按输入里的原文引用**：不要把英文长段落直接照搬，翻成中文、保留关键英文专有名词；"
    "标了「生态账号提及」的是第三方转述，**不得**写成「官方宣布」；"
    "链接要不要给都可以，但作者名与日期必须准确。\n"
    "\n【输出结构（只有这四段，严格按顺序，不要加别的段）】\n"
    "\n**🗓 重要事件时间线**（报告主体，占 50%% 以上篇幅；外部资讯并入本段并标红）\n"
    "**每行一条，以 `- ` 开头，条与条之间不要空行**。格式固定为：`- 月-日 · 事件 · 验证状态`\n"
    "- **日期写法**：同一份报告里年份只出现一次。**首条**写 `2026-10-30`，"
    "其后各条一律只写 `10-31`、`11-06`，**不许再重复 2026**。跨年时才重新写年份。\n"
    "- **事件**：一句话讲清「谁做了什么 / 发生了什么」，带必要数字。官方动作与社区传闻都要，"
    "但传闻必须在事件里写明是「传言/群内称」，不许写成事实。纯闲聊不进时间线。\n"
    "- **验证状态**：每条结尾必须标，二选一（可带半句理由，不超过 25 字）：\n"
    "    · `✅已验证` —— 有两个及以上独立来源，或链上/官方一手数据直接支持；"
    "括注写清依据，如「✅已验证（官方X+官网）」\n"
    "    · `⚠️未验证` —— 只有单一来源、只有群内说法、或只是口头承诺；"
    "括注写清缺什么，如「⚠️未验证（仅群内称，无公告）」\n"
    "- **排序（本条优先级极高）**：严格按**真实日期倒序**——**最新的事件排最前面，最旧的排最后**，"
    "不许按重要性、来源或群聊顺序排。首条就是最近发生的那条。\n"
    "- **条数**：**最多 10 条**（代码层会严格执行，超出的旧事件会被截掉）；确实没有大事就写 1-3 条并说明「本期无重大事件」。"
    "近期资讯太多时优先保留最新、最重要的，旧的不必硬凑。\n"
    "- **只写真实发生的事**：不要写「与上次相比无变化」「延续上次判断」这种元评估条目——"
    "没有新事就少写一条，不要在时间线里塞占位行。\n"
    "- **GitHub 的逐条 commit 不算事件**——数据站「N commits/30d」与 GitHub「最近提交日期」"
    "是同一事实的两种表述，不得判为冲突；开发节奏**只写一条**。\n"
    "- **GitHub 这条必须回答三件事（用户明确要求）**："
    "① **最后一次更新是什么时候**（给日期，如「最后一次更新 11-10」）"
    "② **近一个月更新了几次**（照抄输入里的次数，翻页受限时写「至少 N 次」）"
    "③ **最后一次更新的是什么内容**（用中文说清改的是哪个模块、什么方向）。\n"
    "  写法示例：`- 11-10 · 代码仓库最后一次更新在 11-10，改的是竞技场观测数据回执；近一个月至少 65 次提交（含合并 21 次），以修复为主，涉及矿池与验证器 · ✅已验证（GitHub）`\n"
    "  **严禁**写成「提交集中在当日」「最近提交集中在 10-14~10-29」这种只有时间、没有内容、"
    "也没给次数的半句话——**时间和次数都是附带信息，最后一次改了什么才是正文**。\n"
    "  **抓取兜底例外**：若素材里写明「提交次数未取到（翻页异常）」，就照写这句，"
    "只给「最后一次更新在X日 + 改了什么」，**绝不许凭空补一个次数**；"
    "这条仍然要写成一条事件，不要因为缺次数就整条删掉。\n"
    "- **GitHub 这条通篇用中文**：不许出现提交标题原文、英文分支名、英文函数名、"
    "PR/issue 编号（如 `Merge pull request #1177 from…`）。只有仓库名、子网名、人名可保留英文。\n"
    "- **时间线不止群聊**：官方 X 帖、生态账号提及、新闻、官网更新、GitHub release 都要按真实日期进时间线，"
    "不要只写群里说过什么。\n"
    "- **外部资讯条目写成红色加粗**：来自 X / 新闻 / 官网 / GitHub / 搜索 的事件，正文用 "
    "`<font color=\"red\">**📰 事件正文**</font>` 标红加粗，日期和验证状态留在红字外；"
    "格式：`- 11-18 · <font color=\"red\">**📰 官方发帖宣布新合作 [X]**</font> · ✅已验证（官方X+官网）`。"
    "群聊内部消息、收入数字不要标红。\n"
    "- **所有外部源都没抓到**时，在时间线里写一条 `- 本期无外部资讯（X/新闻/官网 均未获取）· ⚠️未验证`，"
    "如实写清哪个源失败；**不许**在明明抓到内容的情况下留空或写「无」。\n"
    "- **时间线末尾固定写一行注明**（紧跟最后一条事件，独占一行）："
    "`<font color=\"red\">红色加粗</font>条目为最新资讯`。这行是图例，不参与事件计数。\n"
    "\n**📊 当前数据**（只保留**当前价格**，一行即可；不要市值/池子/emission/tempo/epoch 等任何其它指标。"
    "价格必须照抄抓取原文，不许换算；抓不到就写「当前价格：未获取」。格式：`当前价格：0.002504 τ [bittensor.ai]`）\n"
    "\n**💰 收入与回购**（用户明确要求必须有这一段；3-6 行，每行一条，格式 `- 项目：数值 · 变化说明`）\n"
    "- **必须回答三件事**：① 收入多少（所有者抽成比例 + 每日收入 τ）② 趋势如何（协议回购上升/下降/持平）"
    "③ 有没有回购、回购多少（协议回购 vs 所有者回购**分开写**，别混成一句）。\n"
    "- **数字全部照抄**上面【与上次投研的收入对比】与【子网收入与回购】段，**不要自己换算或再算百分比**。\n"
    "- **和上次比有变化的项，必须用红色标出来**，写法：`<font color=\"red\">新值（+/-X%%）</font>`。"
    "例如：`- 所有者收入：<font color=\"red\">160.59 τ/日（+10.8%%）</font>（上次 145.00）`。"
    "**没变化的项不要标红**，也不要为了标红而编造变化。\n"
    "- 若输入里**没有**【与上次投研的收入对比】段（说明是首次投研，或本地无历史快照），"
    "就只写本次绝对值，并在末尾加一句「首次记录，下次投研起可对比变化」。\n"
    "- 若所有者回购为**零**，必须明确写「所有者回购：零（团队未用 owner cut 回购）」，"
    "**不要**含糊带过——零回购本身是重要结论。\n"
    "- **零回购 + 档案有回购地址时必须并列写冲突**：若输入里有【档案登记的回购地址】段、"
    "而数据站所有者回购为零，不要二选一，两条都写——"
    "「数据站口径下所有者回购为零，但群聊/人工登记了回购钱包 <地址>（未链上核实），冲突待核」。"
    "**不许**因为数据站是零就删掉地址线索，也**不许**反过来把数据站改成非零。\n"
    "- 抓不到收入数据（输入里没有该段）就整段写「收入与回购：未获取（数据源未返回）」，不要留空、不要编数字。\n"
    "\n**📢 当日社区动态**（仅当输入里有【当日 dtt 社区消息】段时写这一节；没有就整段不写）\n"
    "- 这是 dtt 关键词监控当日抓到的公开讨论，**未经证实、不是证据**。必须单独成节、"
    "涵盖该子网当日的主要说法；同主题可合并，但每条都要标 `[社区]` 与 `⚠️未验证`，"
    "并写上平台与时间（如「[社区][Telegram 14:32]」）。\n"
    "- **必须写出具体内容**：把消息里的原话要点、数字、涉及对象，以及【X帖原文】里的正文"
    "一并写进这一节；只写「附了一个 X 链接」「有人提及」「重要人物发过言」"
    "而不写链接/发言的实际内容，**视为不合格**。输入里有【X帖原文】时，那就是链接的内容，"
    "必须据此写，不许编造、也不许略过。\n"
    "- **禁止**把社区说法写成已发生的事实、或与链上/数据站数字混成一句；"
    "与链上/数据站/官方结论冲突时，必须并列写「社区称X，但链上/数据站显示Y，⚠️待人工确认」，"
    "不得二选一、不得删掉任一方。\n"
    "- **关键人物（const）**：只写他当天与本子网明确相关的动态，每条提炼成一句短句"
    "「const 转推 @谁：内容要点」「const 回复 @谁：内容要点」「const 表示：内容要点」，"
    "写清对象与观点；人名用输入 `<>` 里的显示名（如 const），"
    "**禁止**写 @const_reborn / consttt / _数字 后缀、禁止贴原始链接或 t.co 链接、"
    "禁止照抄整段原文、禁止只写「重要人物发过言」；与本子网无关的发言不写。\n"
    "- 本节不计入时间线事件条数；若与档案里的历史社区线索重复，以本节的当日消息为准，"
    "历史线索只在有新增信息时补一句。\n"
    "\n**📌 预期跟踪**（先逐条结清上次的，再写本次新增的；合计 4-7 条）\n"
    "**若输入里有【上次跟踪事项（逐条回答）】段，必须先逐条给结论**，一条都不许漏：\n"
    "  格式：`- ✅已兑现 / ⏳进行中 / ❌已证伪 / ⚔️条件未触发：<上次那条的简述> → <这次的事实依据>`\n"
    "  · `✅已兑现` = 输入里有明确证据说明条件成立；\n"
    "  · `❌已证伪` = 输入里有明确证据说明条件被打破（如上次说「跌破 350 τ 视为走弱」，这次日均确实跌破）；\n"
    "  · `⏳进行中` = 有部分进展但还没到判定线；\n"
    "  · `⚔️条件未触发` = 这次输入里根本没有能判定它的数据（**这种情况要老实写，不许假装判定**）。\n"
    "  · 判定依据里的数字**必须来自本次输入**，不许拿上次报告里的数字充数。\n"
    "**结清之后**，再写本次新增的跟踪项，格式：`- 盯什么 → 什么情况算兑现 / 什么情况算证伪`。"
    "要具体到能拿数据核对（例如「owner 是否在 30 天内继续解仓 → 解仓比例再升 5%% 即证伪回购承诺」），"
    "不要写「关注官方动态」这类空话。\n"
    "- 变化了的判定结论（原来是「进行中」现在变「已兑现/已证伪」）**用红色标出结论词**，"
    "写法 `<font color=\"red\">✅已兑现</font>`。\n"
    "\n【禁止事项】\n"
    "不要写「一句话结论」段、不要写「事实基线」（只留当前价格那一行）、不要写「交叉验证矩阵」段、"
    "不要写「群内预期差」段、不要写「风险与未证实」段——"
    "**验证状态已经跟在每条事件后面了，不需要再单列一节复述**。"
    "也不要写开场白、不要写总结、不要写「综上所述」。"
    "**更不要在正文里写「（45-60字）」「（约 50 字）」这类字数标注**——那是给你自己看的约束，不是内容。\n"
    "\n【篇幅】**只有上限、没有下限**：全文不超过 **%(max_chars)s 字**。"
    "**不注水，但信息要全**：能一句话说清就别写两句，不要堆形容词、免责声明、重复表述；"
    "但社区动态、收入回购、关键事件、跟踪清单该写的必须写全，"
    "不要为了「简短」把关键信息砍掉。\n"
    "超过 %(max_chars)s 字才会被代码层压缩（只删不增）。\n"
    "\n【语言】中文；英文专有名词、代币名、代码库名保留原文。不用 # 标题、不用 > 引用，手机端可读。"
)              # 注意：这里不能就地 % 格式化，模板里含 %% 字面量，交给 research_prompt 统一处理


def research_prompt(days=14, max_chars=3000):
    """投研系统提示词（天数、篇幅上限可配：config.research_max_chars）。"""
    return _RESEARCH_PROMPT_TMPL % {"days": int(days or 14),
                                    "max_chars": int(max_chars or 3000)}


RESEARCH_SYSTEM_PROMPT = research_prompt(14)

RESEARCH_VERIFY_PROMPT = (
    "你是投研报告的合规校对员，不是作者。输入是【原始素材】+【报告初稿】。\n"
    "逐条核对，只做删改不做新增，输出修正后的完整报告：\n"
    "1. 每个数字、名称、日期必须能在【原始素材】里找到一模一样的字符串；找不到的删掉或改「未获取」；\n"
    "2. 检查来源标记：无标记的断言，补 `[群聊]` 或 `[社区]` 或改为「判断：」；标错来源的改正；\n"
    "3. 群聊传闻被写成事实的，降级为「群内有人称（日期，人物）：…」；"
    "社区讨论被写成事实的，降级为「社区称（日期，平台）：…」；\n"
    "4. 把「可能/或将/有望」后面没有证据支撑的推测删除，或明确标注为待验证假设；\n"
    "5. 冲突数据被合并成一个值的，改回并列呈现；\n"
    "6. 保留全部小节标题与顺序，**不得比初稿更长**。\n"
    "只输出修正后的报告正文。"
)


# ------------------------------------------------------------------ 素材拼装
def _arc_block(arc, days=14, cap_per_day=260):
    out, total = [], 0
    dd = arc.get("days") or {}
    keys = sorted(dd.keys())[-days:]
    for d in keys:
        ents = dd[d][:cap_per_day]
        total += len(ents)
        lines = []
        for e in ents:
            zh = ("  → " + e["zh"]) if e.get("zh") else ""
            lines.append("- [%s] <%s>%s%s" % (e["time"], e["author"],
                                              (" 🔥%d" % e["react"]) if e.get("react", 0) >= 3 else "",
                                              "：%s%s" % (e["text"].replace("\n", " ")[:400], zh)))
        out.append("【%s】%d 条\n%s" % (d, len(ents), "\n".join(lines)))
    return "\n\n".join(out), total, keys


def _ext_block(sources, meta):
    blocks = []
    for name in ("bittensor.ai", "taostats", "dtaoscan"):
        r = sources.get(name) or {}
        if r.get("ok") and r.get("facts"):
            kv = "；".join("%s=%s" % (k, v) for k, v in r["facts"].items())
            extra = ("\n  站点自述：%s" % r["blurb"]) if r.get("blurb") else ""
            blocks.append("[%s]%s %s%s\n  页面原文片段：%s"
                          % (name, ("（经%s）" % r["via"]) if r.get("via") else "",
                             kv, extra, (r.get("excerpt") or "")[:900]))
        else:
            blocks.append("[%s] 未获取（%s）" % (name, r.get("err", "无数据")))
    w = sources.get("官网") or {}
    if w.get("ok"):
        blocks.append("[官网] %s\n  标题：%s\n  描述：%s\n  正文片段：%s"
                      % (w.get("url", ""), w.get("title", "")[:160], w.get("desc", "")[:300],
                         (w.get("excerpt") or "")[:700]))
    else:
        blocks.append("[官网] 未获取（%s）" % w.get("err", ""))
    g = sources.get("GitHub") or {}
    if g.get("ok"):
        # 只给中文摘要，**不给英文 commit 原文**：给了就会被整段抄进报告。
        # 用户明确要求「说明提交次数 + 主要更新内容，英文能不要就不要」。
        sm = g.get("summary") or {}
        if sm:
            lines = [sm.get("sentence") or "未获取提交摘要"]
            if sm.get("last_detail_at"):
                lines.append("最后一次提交：%s，内容：%s"
                             % (sm["last_detail_at"], sm.get("last_detail") or "未获取"))
            if sm.get("kinds"):
                lines.append("改动类型分布：" + "、".join("%s %d 次" % (k, n) for k, n in sm["kinds"]))
            if sm.get("topics"):
                lines.append("涉及模块：" + "、".join("%s %d 次" % (k, n) for k, n in sm["topics"]))
            per = sm.get("per_day") or {}
            if 1 < len(per) <= 10:
                lines.append("逐日提交数：" + "、".join("%s %d 次" % (k, v) for k, v in per.items()))
            if sm.get("fallback"):
                note = ("（以上为**仓库主页兜底**的中文摘要：照抄「最后一次更新」的日期与内容；"
                        "**本次提交次数确实没取到，必须如实写「提交次数未取到」，不许编一个数字**；"
                        "不得写任何英文；仓库名可保留）")
            else:
                note = ("（以上已是**中文摘要**，"
                        "必须按「最后一次更新在X日、近一个月N次、最后一次改的是Y」来写；"
                        "不得改写次数；**不得写任何英文**：不写提交标题原文、英文分支名、"
                        "英文函数名、PR 编号；仓库名可保留）")
            blocks.append("[GitHub] 仓库 %s\n  %s\n  %s"
                          % (g.get("repo", ""), "\n  ".join(lines), note))
        else:
            blocks.append("[GitHub] %s\n  releases：%s\n  （commits 仅用于判断开发是否活跃，"
                          "**不得逐条列入事件时间线**）"
                          % (g.get("repo", ""),
                             "；".join("%s（%s）" % (x["at"], x["title"][:70])
                                       for x in g.get("releases", [])[:5]) or "无"))
    else:
        blocks.append("[GitHub] 未获取（%s）" % g.get("err", ""))
    n = sources.get("Google News") or {}
    if n.get("items"):
        blocks.append("[新闻] " + "\n  ".join("%s %s（%s）" % (i["at"][:10], i["title"][:90], i["source"])
                                              for i in n["items"][:8]))
    else:
        blocks.append("[新闻] 未获取（%s）" % (n.get("note") or n.get("err") or "无相关报道"))
    s = sources.get("DuckDuckGo") or {}
    if s.get("items"):
        blocks.append("[搜索] " + "\n  ".join(
            "%s ｜ %s ｜ %s" % (i["title"][:80], i["url"][:90], i["snip"][:120]) for i in s["items"][:6]))
    else:
        blocks.append("[搜索] 未获取")
    x = sources.get("X") or {}
    recent = x.get("recent") or []
    if recent:
        lvl = ("项目方官方账号（可作官方表态）" if x.get("level") == "official"
               else "第三方账号（**只能作第三方提及，不得写成官方公告**）")
        head = ("[X @%s] 身份：%s（%s）；简介：%s；本次抓到 %d 条贴文（按时间从新到旧，"
                "每条已带准确 UTC+8 时间与互动数）"
                % (x.get("handle", ""), lvl, x.get("note") or x.get("via") or "",
                   (x.get("bio") or "")[:200].replace("\n", " "),
                   len(recent)))
        lines = []
        # 只给最近 10 条：X 时间线动辄 20+ 条，全塞进去会把 Chat 素材挤到没边，
        # 模型也只会重复前几条。用户给的链接永远排最前（已按时间倒序，但单独保底）。
        _show = recent[:10]
        for p in _show:
            if p.get("from"):
                tagx = "（用户提供的链接）"
            elif p.get("is_rt"):
                tagx = "（@%s 转推 @%s 的贴）" % (p.get("rt_by") or x.get("handle") or "?",
                                              p.get("author") or "?")
            else:
                tagx = ""
            hot = ""
            if p.get("likes", 0) >= 100:
                hot = "，%d 赞" % p["likes"]
            elif p.get("views", 0) >= 5000:
                hot = "，%d 次浏览" % p["views"]
            lines.append("  - [%s] @%s%s%s：%s"
                         % (p.get("at") or "-", p.get("author") or "?", tagx, hot,
                            (p.get("text") or "")[:520].replace("\n", " ")))
            if p.get("url"):
                lines.append("    ↳ %s" % p["url"])
        blocks.append(head + "\n" + "\n".join(lines))
    elif x.get("ok"):
        prof = x.get("profile") or {}
        if prof:
            st = int(prof.get("statuses") or 0)
            lvl = ("项目方官方账号（可作官方表态）" if x.get("level") == "official"
                   else "第三方账号（**只能作第三方提及**）")
            blocks.append("\n  ".join([
                "[X @%s] 身份：%s（%s）" % (x.get("handle", ""), lvl,
                                        x.get("note") or x.get("via") or ""),
                "账号资料（已核实，来源 %s）：名称 %s；简介 %s；官网 %s；加入 %s；"
                "粉丝 %s；关注 %s；累计 %s 条公开贴文；链接 %s"
                % (prof.get("via") or "fxtwitter", prof.get("name") or "-",
                   (prof.get("bio") or "-")[:160].replace("\n", " "),
                   prof.get("website") or "-", prof.get("joined") or "-",
                   prof.get("followers", 0), prof.get("following", 0),
                   ("未知" if st < 0 else st), prof.get("url") or "-"),
                "%s；本次时间窗内 %d 条新贴文。"
                % (x.get("empty_reason") or "账号可达但时间窗内无新贴文", len(recent)),
                "✅ 这是**已核实的账号状态**，不是抓取失败：报告应写「官方账号已确认、"
                "目前未发布内容」，不得写「官推找不到 / 链接不可访问」，也不得编造推文。",
            ]))
        else:
            blocks.append("[X @%s] 账号可达但时间窗内无新贴文（简介：%s）"
                          % (x.get("handle", ""), (x.get("bio") or "")[:140]))
    else:
        blocks.append("[X] 未获取（%s）——不得据此推断官方态度" % x.get("err", ""))
    # 生态广播号（opentensor / taostats / bittensor）里点名本子网的贴文：
    # 这类是「第三方权威提及」，既不是子网官方公告，也不是路人闲聊，必须单独标注。
    eco = x.get("eco") or (sources.get("X 生态圈") or {}).get("items") or []
    if eco:
        blocks.append("[X 生态圈提及] 以下贴文来自生态广播账号（**不是本子网官方公告**，"
                      "引用时标「生态账号提及」）：\n"
                      + "\n".join("  - [%s] @%s（%d 赞）：%s"
                                  % (p.get("at") or "-", p.get("author") or "?",
                                     p.get("likes", 0),
                                     (p.get("text") or "")[:400].replace("\n", " "))
                                  for p in eco[:8]))
    return "\n".join(blocks)


def _tracking_items(digest):
    """从一份报告正文里抽出「📌 预期跟踪」的条目列表。

    这些条目是上次投研承诺「接下来盯什么 → 什么算兑现/证伪」的具体条件，
    本次必须逐条回答「兑现了没有」——这才是用户要的「跟踪进度对比」。
    """
    if not digest:
        return []
    m = re.search(r"\*\*(?:📌\s*)?预期跟踪\*\*\s*\n((?:-\s*.+\n?)+)", digest)
    if not m:
        return []
    out = []
    for ln in m.group(1).split("\n"):
        s = ln.strip()
        if not s.startswith(("-", "•")):
            continue
        s = re.sub(r"^[-•]\s*", "", s).strip()
        # 去掉红色标注，避免把 HTML 带进判定
        s = _RED_STRIP_RE.sub(r"\1", s)
        if len(s) >= 8:
            out.append(s[:200])
    return out


def tracking_block(prev_arc, today=None, arc_dir=None, limit=1, max_items=6):
    """生成「上次的跟踪事项 → 这次逐条判定」的输入段（用户明确要求）。

    为什么单独成段而不是塞进历史报告块：
    历史报告块是「上次说了什么」的**叙述**，模型容易泛泛地写「延续上次判断」；
    而用户要的是**逐条打钩**：上次说「盯 X → Y 算兑现」，这次必须回答 X 出现没有、算不算兑现。
    把条目拆成编号清单，模型才会照条回答。
    """
    if not prev_arc:
        return ""
    reps = [r for r in (prev_arc.get("reports") or []) if r.get("date")]
    # 找最近一份「不是今天」的报告（同日重跑不跟自己比）
    target = None
    for r in reps:
        if today and r.get("date") == today:
            continue
        target = r
        break
    if not target:
        return ""
    body = (target.get("body") or "").strip()
    if not body and arc_dir and target.get("file"):
        try:
            with open(os.path.join(arc_dir, "SN%d" % int(prev_arc["sn"]),
                                   target["file"]), encoding="utf-8") as f:
                body = f.read()
        except Exception:
            body = ""
    items = _tracking_items(body)
    if not items:
        return ""
    L = ["【上次投研（%s）留下的跟踪事项 —— 本次必须逐条回答】" % target.get("date")]
    for i, it in enumerate(items[:max_items], 1):
        L.append("%d. %s" % (i, it))
    L.append("**要求**：对上面每一条，在本次的「📌 预期跟踪」段里逐条给出结论，"
             "写法 `- ✅已兑现 / ⏳进行中 / ❌已证伪 / ⚔️条件未触发：<上次那条的简述> → <这次的事实依据>`。"
             "判定标准：\n"
             "  · 该条描述的现象在本次输入里**确实发生**了 → ✅已兑现；\n"
             "  · 该条描述的**反面**发生了（上次说「守住 438 τ」，这次跌破；上次说「转正」，这次仍是零）"
             "→ ❌已证伪。**注意：不能一律写「⏳进行中」蒙过去**，"
             "「仍是零」「跌破」本身就是明确结论；\n"
             "  · 只有「部分进展、还没到判定线」才是 ⏳进行中；\n"
             "  · 本次输入里**根本没有**能判定它的数据 → ⚔️条件未触发。\n"
             "判定依据里的数字必须是本次输入里的真值，**不许**为了凑结论编造进展。"
             "结清之后再写本次新增的跟踪项。")
    return "\n".join(L)


def _prev_reports_block(arc, limit=3, skip_date=None, body_chars=900, body_full_for=1, arc_dir=None):
    """把该子网**此前投研的结论**带给本次，实现「上次说了什么 → 这次怎么变了」。

    没有这一段时，写回档案的报告只是一份堆积的历史文件：下次投研完全不读它，
    两次报告之间无法体现「进展/反转/被证伪」，档案就失去闭环意义。

    body_chars：最近 body_full_for 份报告带**正文全文**（截断到 body_chars），
    更早的只带结论句 —— 既要能对比「上次判断的细节」，又不能把输入撑爆。

    旧档案（v1.6.0 之前落的）没有 body 字段，只有 file 指向 research_*.md；
    传 arc_dir 时按需回读磁盘，避免历史上限在摘要层面。
    """
    if not arc:
        return ""
    reps = [r for r in (arc.get("reports") or []) if r.get("date")][:limit]
    if not reps:
        return ""
    out = ["【此前 %d 次投研结论（本档案历史留档，供对比「变化」，不要直接当成今天的证据）】"
           % len(reps)]
    for i, r in enumerate(reps):
        toned = (r.get("conclusion") or r.get("title") or "").replace("\n", " ").strip()
        tag = "（同日上一版）" if (skip_date and r["date"] == skip_date) else ""
        out.append("- %s%s（证据强度 %s）：%s%s"
                   % (r["date"], tag, r.get("grade") or "未标注", toned[:320],
                      ("；当时关键事件：" + (r.get("events") or "")[:160]) if r.get("events") else ""))
        if i >= body_full_for:
            continue
        body = (r.get("body") or "").strip()
        if not body and arc_dir and r.get("file"):
            # 老档案没 body：回读同目录报告文件，保证「上次说了什么」不因版本升级而丢失
            try:
                p = os.path.join(arc_dir, "SN%d" % int(arc["sn"]), r["file"])
                with open(p, encoding="utf-8") as f:
                    body = f.read().strip()
                # 文件头是 `# 标题\n\n> 生成于…`，截掉，只留正文
                _m = re.search(r"\n\n([\s\S]+)$", body)
                if _m:
                    body = _m.group(1).strip()
            except Exception:
                body = ""
        if body:
            out.append("  ┌ 该次报告要点（供比对，勿当今天的证据）：")
            out.append("  " + body[:body_chars].replace("\n", "\n  "))
            out.append("  └ 完")
    out.append("用法：若本次外部证据与上述结论**不一致**（如上次说「无动作」这次出现官方公告、"
               "上次说「锁仓中」这次开始解仓），必须在「群内预期差」或「交叉验证矩阵」里明确点出"
               "「与 X 月 X 日结论相比发生了变化」；若一致，写「延续上次判断」。"
               "**不要把历史结论当作今天的证据引用**（来源标记用 [档案]，且必须注明是历史留档）。")
    return "\n".join(out)


def _consensus_block(rows):
    if not rows:
        return "（本次未从任何数据站抓到可比指标）"
    lines = []
    for r in rows:
        vals = "；".join("%s=%s" % (s, v) for s, v in r["vals"])
        lines.append("- %s：%s → %s" % (r["label"], vals, r["status"]))
    return "\n".join(lines)


# -*- coding: utf-8 -*-


# 最近一次 build_input 抽出的收入快照（run_research 落档时取用）
_LAST_INC_SNAP = {}


# ------------------------------------------------------------------ 收入与回购
# 设计要点（2026-11-09 用户需求）：
#   1. 子网重大事件总结里必须有「收入模块」：收入多少、趋势如何、有没有回购、回购多少；
#   2. 本地有历史投研时，要**对比上次**的同一组指标，把变化的地方标出来；
#   3. 变化用红色字体。飞书 lark_md 支持 <font color='red'>，TG 旧版 Markdown 不支持，
#      所以红色只加在飞书通道，TG 侧降级成「⬆/⬇ + 星标」。

# 参与「变化对比」的指标：(字段路径, 展示名, 单位, 小数位, 变化阈值%)
# 阈值含义：变动幅度小于它视为「基本持平」，不标红，避免噪声满屏飘红
# 从正文里剥离红色标注（判定跟踪事项时不该把 HTML 当成条件的一部分）
_RED_STRIP_RE = re.compile(r"<font[^>]*>(.*?)</font>", re.I | re.S)

_INCOME_METRICS = (
    (("owner_tao",), "所有者抽成收入", "τ/日", 2, 5.0),
    (("daily_tao",), "子网日分配总额", "τ/日", 2, 5.0),
    (("owner_cut_pct",), "所有者抽成比例", "%", 2, 0.0),
    (("protocol_buy", "sum"), "协议回购（30天累计）", "τ", 2, 10.0),
    (("protocol_buy", "avg"), "协议回购（日均）", "τ", 2, 10.0),
    (("protocol_buy", "active_days"), "有回购的天数", "天", 0, 0.0),
    (("ob", "pool_alpha_30d"), "所有者回购·池内（30天）", "α", 0, 0.0),
    (("ob", "burn_alpha_30d"), "所有者回购·销毁（30天）", "α", 0, 0.0),
    (("ob", "pool_alpha_life"), "所有者回购·池内（终身）", "α", 0, 1.0),
    (("burned_pct",), "累计销毁占供应", "%", 2, 1.0),
)


def _dig(d, path):
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def income_snapshot(inc):
    """从 src_income() 的结果里抽出**可对比的标量快照**（存进档案，下次用来比变化）。

    只存标量不存逐日序列：序列 30 条 × 每次投研会把档案撑大，
    而趋势结论（前半 vs 后半、有回购天数）已经在 protocol_buy 里算好了。
    """
    if not isinstance(inc, dict) or not inc.get("ok"):
        return {}
    snap = {"as_of": datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")}
    for path, _label, _unit, nd, _th in _INCOME_METRICS:
        v = _dig(inc, path)
        if v is None:
            continue
        try:
            snap[path[0] if len(path) == 1 else ".".join(path)] = round(float(v), nd)
        except Exception:
            pass
    pb = inc.get("protocol_buy") or {}
    for k in ("days", "zero_days", "half_early", "half_late", "first", "last"):
        if k in pb:
            snap["protocol_buy." + k] = pb[k]
    if inc.get("rev_recycle"):
        snap["rev_recycle"] = inc["rev_recycle"]
    return snap


def _fmt_num(v, nd):
    # None / 空串必须给占位符，不能直接 str() —— 否则报告输入里会漏出「约 None α/日」
    # 这种残渣，模型照抄进报告就是一条硬伤（数字硬校验也拦不住，因为它不是数字）。
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
    except Exception:
        return str(v)
    if nd == 0:
        return "{:,.0f}".format(f)
    return "{:,.{nd}f}".format(f, nd=nd)


def diff_income(prev, cur):
    """对比两次快照，返回 [(标签, 旧值, 新值, 变化率%, 是否显著)]。

    「显著」才标红：比例类指标只要变了就算变（阈值 0），
    金额类给 5%/10% 阈值，过滤掉日常波动噪声。
    """
    out = []
    if not prev or not cur:
        return out
    for path, label, unit, nd, th in _INCOME_METRICS:
        key = path[0] if len(path) == 1 else ".".join(path)
        if key not in prev or key not in cur:
            continue
        try:
            a, b = float(prev[key]), float(cur[key])
        except Exception:
            continue
        if a == b:
            pct, sig = 0.0, False
        else:
            pct = (b - a) / abs(a) * 100.0 if a else (100.0 if b else 0.0)
            sig = abs(pct) >= th if a else True
        out.append((label, a, b, pct, sig, unit, nd))
    return out


def _trend_word(pb):
    """把「前半 vs 后半」翻成一句话趋势判断。

    必须区分两种情况，否则会把「有回购但没算出前后半段」误报成「无回购」：
      · `half_early`/`half_late` 都缺 → 趋势算不出来，只能说「不可判」
      · 两者都在且都 ≤0 → 才是真的「窗口内无协议回购」
    """
    if not pb:
        return ""
    e, l = pb.get("half_early"), pb.get("half_late")
    if e is None and l is None:
        # 逐日序列不足（比如只拿到 1-2 天）时算不出前后半段。
        # 不带外层括号：调用处已经是「趋势%s」，再加括号会变成「趋势（…）」套括号。
        return "不可判（逐日数据不足）"
    e, l = e or 0, l or 0
    if e <= 0 and l <= 0:
        return "窗口内无协议回购，趋势不可判"
    if e <= 0 < l:
        return "由无到有（后半段开始回购）"
    d = (l - e) / abs(e) * 100.0
    if d >= 20:
        return "上升（后半段 +%.0f%%）" % d
    if d <= -20:
        return "下降（后半段 %.0f%%）" % d
    return "基本持平（后半段 %+.0f%%）" % d


def income_block(inc, prev_snap=None, source_tag="[bittensor.ai]"):
    """拼「收入模块」输入段：给模型的数据块（不含排版）。

    模型拿到的必须是**已经算好的数字与变化**，而不是让它自己从
    30 天序列里做加减法 —— 数字硬校验（audit_numbers）只认输入里逐字存在的值。
    """
    # 注意：这个函数恒返回二元组 (文本, 快照)，早退也必须给全 —— 调用方是
    # `inc_txt, inc_snap = income_block(...)` 直接解包，返回单个字符串会 ValueError。
    if not isinstance(inc, dict) or not inc.get("ok"):
        return "", {}
    L = ["【子网收入与回购（链上/官方数据站口径，可直接引用）】"]
    L.append("口径说明：收入 = 所有者按 Owner Cut 抽到的 emission（页面按当日价折算成 TAO）；"
             "回购分两种，**不要混为一谈**：①「协议回购」= 链上把多余 emission 的 TAO "
             "从池子买入 alpha（推价，全子网通用机制）；"
             "②「所有者回购」= 子网团队用自己的 owner cut 买回 alpha 或销毁（团队主动行为）。")

    cut = inc.get("owner_cut_pct")
    if cut:
        L.append("- 所有者抽成比例：%s%% %s" % (cut, source_tag))
    if inc.get("owner_tao"):
        # α 折算是可选的（页面偶尔不给 alpha 口径），没有就整段省略，不要留「约 — α/日」
        _oa = inc.get("owner_alpha")
        _line = "- 所有者收入（当前口径）：%s τ/日" % _fmt_num(inc["owner_tao"], 2)
        if _oa:
            _line += "（约 %s α/日）" % _fmt_num(_oa, 0)
        L.append(_line + " %s" % source_tag)
    if inc.get("daily_tao"):
        # 三方占比缺数据时只写总额，不要写「矿工 ?% / 验证人 ?% / 所有者 ?%」
        _split = [("矿工", inc.get("miner_pct")), ("验证人", inc.get("validator_pct")),
                  ("所有者", inc.get("owner_pct"))]
        _has = [(n, v) for n, v in _split if v not in (None, "", 0)]
        _line = "- 子网每日分配总额：%s τ/日" % _fmt_num(inc["daily_tao"], 2)
        if _has:
            _line += "（%s）" % " / ".join("%s %s%%" % (n, v) for n, v in _has)
        L.append(_line + " %s" % source_tag)
    if inc.get("rev_recycle"):
        L.append("- 站点「Revenue / Recycle」结论：%s（= 当日所有者 emission 抽成 ÷ 当日注册销毁 TAO）"
                 % inc["rev_recycle"])

    pb = inc.get("protocol_buy") or {}
    if pb.get("days"):
        # 日期区间有就写，没有就省略括号（不要留「近 30 天， ~ 」这种空区间）
        _span = ""
        if pb.get("first") and pb.get("last"):
            _span = "，%s ~ %s" % (pb["first"], pb["last"])
        L.append("- 协议回购（近 %d 天%s）：累计 %s τ，日均 %s τ，有回购 %d 天 / 零回购 %d 天；"
                 "趋势%s %s"
                 % (pb["days"], _span,
                    _fmt_num(pb.get("sum"), 2), _fmt_num(pb.get("avg"), 2),
                    pb.get("active_days", 0), pb.get("zero_days", 0),
                    _trend_word(pb), source_tag))
        if pb.get("half_early") is not None:
            L.append("  （前半段 %s τ → 后半段 %s τ）"
                     % (_fmt_num(pb["half_early"], 2), _fmt_num(pb["half_late"], 2)))

    ob = inc.get("ob") or {}
    if ob:
        b30, p30 = ob.get("burn_alpha_30d") or 0, ob.get("pool_alpha_30d") or 0
        if b30 or p30:
            L.append("- **所有者回购（近 30 天）：销毁型 %s α（%s τ）+ 池内买入 %s α（%s τ）；"
                     "占 owner cut 的 %.2f%%** %s"
                     % (_fmt_num(b30, 0), _fmt_num(ob.get("burn_tao_30d"), 2),
                        _fmt_num(p30, 0), _fmt_num(ob.get("pool_tao_30d"), 2),
                        (ob.get("pool_share_30d") or 0) * 100, source_tag))
        else:
            L.append("- 所有者回购（近 30 天）：**零**——团队没有用自己的 owner cut 回购 "
                     "（不代表没收入，只代表收入没拿来回购） %s" % source_tag)
        life = (ob.get("burn_alpha_life") or 0) + (ob.get("pool_alpha_life") or 0)
        if life:
            # 终身累计要拆开写：销毁型和池内买入是两种不同性质的行为，
            # 合成一个数会让模型误判成「团队一直在回购」。
            _b, _p = ob.get("burn_alpha_life") or 0, ob.get("pool_alpha_life") or 0
            _parts = []
            if _b:
                _parts.append("销毁型 %s α" % _fmt_num(_b, 0))
            if _p:
                _parts.append("池内买入 %s α" % _fmt_num(_p, 0))
            _tao = (ob.get("burn_tao_life") or 0) + (ob.get("pool_tao_life") or 0)
            _line = "- 所有者回购（终身累计）：%s" % " + ".join(_parts or ["%s α" % _fmt_num(life, 0)])
            if _tao:
                _line += "（约 %s τ）" % _fmt_num(_tao, 2)
            L.append(_line)
    if inc.get("burned_pct"):
        L.append("- 累计销毁占供应量：%s%%" % _fmt_num(inc["burned_pct"], 2))

    em = inc.get("emission") or {}
    if em.get("24h"):
        L.append("- 净 emission：24h %s α / 7d %s α / 30d %s α（其中销毁 %s α）"
                 % (_fmt_num(em["24h"].get("net_alpha"), 0),
                    _fmt_num((em.get("7d") or {}).get("net_alpha"), 0),
                    _fmt_num((em.get("30d") or {}).get("net_alpha"), 0),
                    _fmt_num((em.get("30d") or {}).get("burned_alpha"), 0)))

    # ---- 与上次投研的对比 ----
    if prev_snap:
        rows = [r for r in diff_income(prev_snap, income_snapshot(inc)) if r[4]]
        # 只有**真有变化**时才出对比段。一项变化都没有却顶着个「对比」标题，
        # 模型会硬凑一条「无变化」占位，读者看到的是空壳。
        if rows:
            L.append("\n【与上次投研的收入对比（上次快照 %s）】" % prev_snap.get("as_of", "?"))
            for label, a, b, pct, sig, unit, nd in rows:
                arrow = "↑" if b > a else ("↓" if b < a else "＝")
                # 百分号不加空格（中文排版），其余单位用空格隔开
                joiner = "" if unit == "%" else " "
                L.append("- %s：%s → %s%s%s（%+.1f%%）%s"
                         % (label, _fmt_num(a, nd), _fmt_num(b, nd), joiner, unit, pct, arrow))
            L.append("**以上变化项必须在报告的「收入与回购」段里逐条写出，并标注变化幅度**；"
                     "括号里的箭头与幅度照抄，不要自己再算一遍。")
        else:
            L.append("\n【与上次投研的收入对比】上次快照 %s：各项指标**均无显著变化**，"
                     "本次不必标红，也不要编造变化。" % prev_snap.get("as_of", "?"))
    return "\n".join(L), income_snapshot(inc)


def _wallet_block(arc):
    """把档案里登记的回购地址拼成提示词小段（仅线索，供冲突核对）。"""
    if not arc:
        return ""
    wallets = [w for w in (arc.get("buyback_wallets") or []) if isinstance(w, dict)]
    if not wallets:
        return ""
    L = ["档案登记回购地址 %d 个（群聊推断 / 人工补录，均**未经链上核实**）：" % len(wallets)]
    for w in wallets:
        src = "人工补录" if w.get("source") == "manual" else "群聊推断"
        note = (w.get("note") or "").strip() or "无备注"
        L.append("- `%s`（%s）：%s" % (w.get("address") or "", src, note))
    L.append("**使用规则**：这些数据站可能没归集。若「收入与回购」段里所有者回购为零、"
             "而这里登记了回购地址，不要二选一，必须**并列写出冲突**——"
             "例如「数据站口径下所有者回购为零，但群聊提及回购钱包 <地址>（未链上核实），待核」。"
             "禁止把地址当成已确认回购，也禁止因数据站为零就删掉这条线索。")
    return "\n".join(L)


def _community_block(arc, limit=4, chars=700):
    """把档案里的「社区消息日报线索」拼成提示词小段。

    这些是 dtt 关键词监控抓来的多平台公开讨论（传闻/爆料/喊单混杂），
    **不是证据、不是投研结论**。投研只能拿它当线索，必须与外部源交叉核对；
    若与链上/数据站结论冲突，须在报告里并列标「待人工确认」，不得静默采信。
    """
    if not arc:
        return ""
    cd = [c for c in (arc.get("community_digests") or []) if isinstance(c, dict)]
    if not cd:
        return ""
    L = ["档案里的社区消息线索 %d 条（dtt 关键词监控，来自 Twitter/X·Telegram·Discord；"
         "**未经证实，是线索不是证据**）：" % len(cd)]
    for c in cd[:limit]:
        plats = "、".join(c.get("platforms") or []) or "多平台"
        exc = re.sub(r"\s+", " ", (c.get("excerpt") or "")).strip()
        L.append("- 【%s · %s】%s" % (c.get("date") or "—", plats, exc[:chars]))
    L.append("**使用规则**：只能当线索。若上述社区说法与本次链上/数据站/官方源结论**矛盾**，"
             "必须在报告里并列写出「社区（日期）称X，但链上/数据站显示Y，待人工确认」，"
             "不得采信任何一方、也不得删掉线索；若无外部证据支持，须明确写「社区传闻，未获证实」。"
             "禁止把社区消息当今日确定的事实或当成已验证的事件。")
    return "\n".join(L)


def _enrich_community_events(events, cfg=None, log=print, max_links=8):
    """抓取社区消息里 X 帖子的正文，拼回 content。

    用户实测反馈：报告里只写「附第三方 X 链接」，链接里的实际内容完全没体现。
    原因就是 dtt 只给链接、不给正文，模型无从下笔。这里在拼提示词前先把正文读出来。
    t.co 短链无法直接解析，x_targets_in 只收 x.com/twitter.com 直链。
    """
    seen, n = set(), 0
    import community_digest as CD
    # 优先抓「已标 SN + 相关性高」的消息里的链接，保证当前子网的链接不被别的帖子挤掉
    ordered = sorted(events or [],
                     key=lambda e: (bool(e.get("sn")), CD._relevance(e)), reverse=True)
    for e in ordered:
        content = (e.get("content") or "").strip()
        extra = []
        for u in (x_targets_in(content).get("posts") or []):
            if u in seen or n >= max_links:
                continue
            seen.add(u)
            n += 1
            try:
                p = x_tweet(u)
                if p and (p.get("text") or "").strip():
                    extra.append("【X帖原文 @%s：%s】"
                                 % (p.get("author") or "?", p["text"].strip()[:400]))
                    log("   🧵 社区 X 链接已抓正文：@%s「%s…」"
                        % (p.get("author") or "?", p["text"][:40]))
                else:
                    extra.append("【X帖原文：抓取失败，仅有链接 %s】" % u)
            except Exception as ex:
                extra.append("【X帖原文：抓取失败（%s）】" % str(ex)[:60])
        if extra:
            e["content"] = content + " " + " ".join(extra)
    return events


def _format_community_today(payload, sn, limit=15, cfg=None):
    """从 dtt 当日取数结果里挑出该 SN 的消息，拼成投研输入段。

    返回 (block, kept)；无相关消息返回 ("", 0)。
    复用 community_digest 的相关性打分/清洗，保证社区日报与投研用同一套过滤口径。
    """
    import community_digest as CD
    events = [e for e in ((payload or {}).get("events") or [])
              if str(sn) in [str(x) for x in (e.get("sn") or [])]]
    if not events:
        return "", 0
    events = [e for e in events if CD._relevance(e, cfg) >= 0]
    seen, picked = set(), []
    for e in sorted(events, key=lambda x: x.get("occurredAt") or 0, reverse=True):
        key = ((e.get("author") or ""), (e.get("content") or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        picked.append(e)
        if len(picked) >= limit:
            break
    if not picked:
        return "", 0
    lines = []
    for e in picked:
        t = e.get("occurredAt")
        tstr = (datetime.datetime.fromtimestamp(t / 1000, TZ).strftime("%H:%M")
                if t else "??:??")
        plat = CD.PLAT_CN.get(e.get("platform"), e.get("platform") or "未知")
        author = CD._clean(e.get("author") or "?", 40)
        content = CD._clean(e.get("content"))[:320]
        lines.append("- [%s][%s] <%s> %s [社区]" % (tstr, plat, author, content))
    b = (payload or {}).get("bounds") or {}

    def _ts(v):
        return datetime.datetime.fromtimestamp(v, TZ).strftime("%m-%d %H:%M") if v else "?"

    rng = ("%s → %s" % (_ts(b.get("start")), _ts(b.get("end")))) if b else "当日 00:00 → 当前"
    more = ("（另有 %d 条同类消息已省略）" % (len(events) - len(picked))) if len(events) > len(picked) else ""
    block = (
        "SN%d 当日 dtt 社区消息 %d 条（北京时间 %s；Twitter/X·Telegram·Discord）%s：\n%s\n"
        "**使用规则**：这些是关键词监控抓来的公开讨论，**未经证实、是线索不是证据**。"
        "报告必须单独写「📢 当日社区动态」一节涵盖其中主要说法，每条标 `[社区]` 与 `⚠️未验证`；"
        "**必须写出消息正文/【X帖原文】的具体内容，禁止只写「附链接」「有人提及」**；"
        "与链上/数据站/官方结论冲突时并列写「社区称X，但链上/数据站显示Y，⚠️待人工确认」，"
        "不得采信任何一方、也不得删掉线索；禁止把这些消息写成今日已确定的事实。"
        % (sn, len(picked), rng, more, "\n".join(lines))
    )
    return block, len(picked)


def _key_author_block(payload, cfg=None):
    """关键人物（创始人 const 等）当日动态，供投研模型判断与本子网的相关性。

    只提供结构化线索（形态/对象/原文摘要/是否带 SN），相关性判断与提炼由模型做：
    - dtt 标了本 SN 的，优先相关；
    - 没标 SN 的（如 const 的「19 days is slow」），看内容是否指向本子网项目/争议。
    人名一律用显示名（备注名优先，如 const），不带 @handle / _辨识码。
    """
    import community_digest as CD
    names = CD._key_author_names(cfg)
    if not names:
        return ""
    lines, seen = [], set()
    events = sorted(((payload or {}).get("events") or []),
                    key=lambda x: x.get("occurredAt") or 0, reverse=True)
    for e in events:
        if not CD._key_author_hit(e, cfg):
            continue
        author = (e.get("author") or "")
        c = CD._clean(e.get("content"))[:500]
        key = (author, c[:80])
        if key in seen:
            continue
        seen.add(key)
        t = e.get("occurredAt")
        tstr = (datetime.datetime.fromtimestamp(t / 1000, TZ).strftime("%H:%M")
                if t else "??:??")
        plat = CD.PLAT_CN.get(e.get("platform"), e.get("platform") or "未知")
        lines.append("- [%s][%s] <%s>（%s） %s [社区·重要人物]"
                     % (tstr, plat, CD._clean(CD._display_author(author, cfg), 40),
                        CD._msg_kind(e, cfg), c))
        if len(lines) >= 30:
            break
    if not lines:
        return ""
    return ("【当日关键人物动态（%s；含 dtt 已归属与未归属 SN 的消息）】\n%s\n"
            "**使用规则**：以上是关键人物（如创始人 const）当日动态线索，人名已是显示名、"
            "直接用（禁止写回 @const_reborn 这类账号）。请判断每条与本子网的相关性："
            "dtt 标了本 SN 的，或内容点名本子网项目/事件/争议的（如训练天数、性能对比、合作、争议），"
            "才算相关；相关条目写进「📢 当日社区动态」节，每条**提炼成一句短句**："
            "「const 转推 @谁：内容要点」「const 回复 @谁：内容要点」「const 表示：内容要点」，"
            "写清动作对象与具体观点/数字，标 `[社区]` + `⚠️未验证`；"
            "**禁止贴原始链接、禁止照抄整段原文、禁止只写「重要人物发过言」**。"
            "与本子网无关的日常发言忽略。"
            % ("、".join("@" + x for x in names), "\n".join(lines)))


_PEOPLE_SUMMARY_PROMPT = (
    "你是投研助理。下面是一份「本子网当日社区消息」背景（已确认跟本子网相关），"
    "以及当日 dtt 关键词监控抓到的人物发言线索（已做初步筛选）。\n"
    "任务：**只挑出跟本子网明确相关**的发言，按人提炼成中文短句。\n"
    "\n"
    "【重要口径】这是「子网重要事件跟踪报告」里的社区动态，"
    "**只放跟本子网有关的消息**。把生态全量所有人物观点都写出来是「社区日报」的职责，"
    "不在这里做；哪怕发言人是创始人，只要与本子网无关也不写。\n"
    "\n"
    "【输出格式（每条一行，严格照此）】\n"
    "- 人名 转推 @对象：一句话内容要点\n"
    "- 人名 回复 @对象：一句话内容要点\n"
    "- 人名 表示：一句话内容要点\n"
    "（本人发言用「表示」；转推用「转推 @谁」；回复/提及用「回复 @谁」；"
    "对象若线索里写了 @某人 就带上，没写就不硬造。）\n"
    "\n"
    "【相关判定：满足至少一条才写】\n"
    "1. 线索的 SN 归属就是本子网；\n"
    "2. 内容点名本子网项目名、SN 编号或官方账号；\n"
    "3. 内容的议题与「本子网当日社区消息」背景里的事件是同一件"
    "（同一个性能对比、争议、训练进度、合作、回购等）。\n"
    "   **注意**：同一件事的评论常不重复项目名——例如背景在说「SN91 Cascade 用 19 天"
    "击败 Salesforce 达到 SOTA」，某人只说「19 days is slow, SOTA should take hours」"
    "也是在评论**同一件事**，必须算相关、按人名写出来；不能因为它没写项目名就丢掉。\n"
    "\n"
    "【必须剔除——哪怕发言人是创始人】\n"
    "- 明确属于别的子网的（线索标了别的 SN，或内容在谈别的项目/争议）；\n"
    "- 与本子网议题无关的泛泛发言（别的子网进展、隐私推理、日常寒暄、纯链接）。\n"
    "\n"
    "【其它硬规则】\n"
    "- **线索里 `[标本SN]` 与 `[★创始人]` 的消息必须逐条覆盖**：它们已由代码判定为"
    "本子网强相关（标了本 SN 或点名了项目名），哪怕内容只是一个 👌 加链接，"
    "也要写出「某人 转推/表示：链接谈及的具体内容要点」，**不得整条漏掉**；"
    "同一人多条同观点才合并。\n"
    "- 写内容要点、不是抄原文：抓结论/数字/态度，一句话讲清；"
    "**不许贴任何 URL、t.co 链接、x.com 链接**。同一人同一观点合并成一条。\n"
    "- 人名**直接写纯文本**（如 `const 表示：`、`cats 转推 @MSBIntel：`），"
    "**输出里绝对不要带尖括号 `<` `>`**——输入线索里的 `<cats>` 只是标记显示名的格式，"
    "不是让你把尖括号抄进结果。禁止 @handle 当人名、consttt、_数字后缀、备注标签；"
    "被转推/回复的对象若是关键人物用 @显示名（@const），其他对象保留其 @handle。\n"
    "- 只写消息里明确有的内容，**不许臆测技术细节**（如模型类型、架构、参数规模）；"
    "原文只说「击败 Salesforce」就只写到这，不要自行补成「多模态大模型」之类。\n"
    "- 相关发言人里 const（创始人）排最前，其余按发言份量排；"
    "每人 1-2 条、每条不超过 45 字；相关发言很少就少写，一条都没有就只输出「无」。\n"
    "- 只输出要点行，不要标题、解释、总数统计。\n"
)


def _sn_event_anchors(events, cfg=None, sn=None, max_terms=40):
    """从当日 dtt 消息里抽「本子网当日专属议题词组」，作为未标 SN 消息的捞回线索。

    为什么需要：const 的「19 days is actually quite slow. SOTA is achievable in hours」
    既没写 cascade、也没标 SN，但它紧跟 cats 那条 cascade/Gscade + X帖（Cascade 19 天
    击败 Salesforce 的 Moirai 2.0）之后，是**同一件事的评论**。用户 2026-11-22 明确：
    SN91 上一份报告里 const/cats 的动态就是和 91 有关的，不能因「只按编号+字面名」
    漏掉。这里把当日已明确归属本 SN 的消息里的专名/产品名/议题词抽出来，
    让「提到同一件事」的未标注发言能被捞回——但**只在已确认本 SN 有相关消息的
    前提下**启用，且命中必需是专名级（非通用词），SN25 那种无命中日的子网不会
    被凭空塞入 const 的无关动态。
    """
    import community_digest as CD
    terms = {}
    for e in events or []:
        if sn is not None and str(sn) not in [str(x) for x in (e.get("sn") or [])]:
            continue
        c = re.sub(r"https?://\S+", " ", e.get("content") or "")
        if not c:
            continue
        # 专名/产品名：首字母大写的词（单个或连续词组合）。const 的评论常只重复
        # 其中一个词（如「19 days … SOTA」只呼应 Aph 的「Beating SOTA …」），
        # 所以既收词组、也收其中的单个大写词，否则捞回规则形同虚设。
        for m in re.finditer(r"(?<![a-z0-9])([A-Z][A-Za-z0-9]{2,}(?:[\s\-][A-Z0-9][A-Za-z0-9.]*){0,2})", c):
            phrase = m.group(1).strip()
            for t in [phrase] + re.findall(r"[A-Z][A-Za-z0-9.]{2,}", phrase):
                if len(t) < 4 or t.lower() in _GENERIC_ANCHOR_STOP:
                    continue
                terms.setdefault(t.lower(), t)
        # 关键词监控命中的词条本身（dtt 按关键词抓，命中词就是议题词）
        kw = (e.get("keyword") or e.get("matchedKeyword") or "")
        if kw and str(kw).strip():
            terms.setdefault(str(kw).strip().lower(), str(kw).strip())
    return sorted(terms.values(), key=lambda s: -len(s))[:max_terms]


_GENERIC_ANCHOR_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "was",
    "are", "not", "but", "you", "your", "its", "our", "all", "can", "will", "just",
    "yes", "time", "day", "days", "into", "out", "now", "new", "one", "two", "how",
    "what", "why", "who", "rt", "subnet", "bittensor", "tao", "message", "very",
    "get", "got", "let", "use", "using", "make", "made", "top", "best", "more",
    "still", "even", "only", "over", "back", "they", "them", "their", "about",
    "than", "then", "when", "where", "which", "there", "here", "some", "much",
}


def _sn_related_events(events, sn, sn_name, cfg=None, context=""):
    """从当日 dtt 全部消息里，筛出与当前子网**强相关**的（供子网报告社区动态用）。

    口径（用户 2026-11-22 两次确认）：「明确归属 + 名称/专名/同议题强证据捞回」，
    绝不靠弱信号凑数。
      社区日报=生态全量所有人都列；这里=只留跟本 SN 明确有关的，避免 SN25 报告
      混入一整屏 SN8/SN64/SN120/Openrouter 等无关 const 动态。

    命中任一即保留（强证据）：
      1. dtt 的 sn 归属里明确含本 SN；
      2. 内容词边界点名本子网项目名（含大小写/拼写变体、官方 handle）；
      3. 内容命中 config.research_sn_anchors[SN] 登记的精确专名（如 moirai/gift-eval）；
      4. **同议题捞回**：未标 SN 的消息，命中本 SN 当日已确认相关消息的
         专名词组（如 const 的 "19 days SOTA" 呼应 cascade 击败 Salesforce 的
         当日事件）——仅在本 SN 当日确实有相关消息时启用，且只捞「专名级」命中。
    明确标了别的 SN 的消息一律排除，除非同时点名了本子网。
    """
    import community_digest as CD
    cfg = cfg or {}
    sn_s = str(sn)
    name = (sn_name or "").strip()

    # 本子网标识：编号子串 + 项目名词边界。项目名短（如 SN25 的 "UR"）仍用词边界，
    # 避免在 "your"/"architecture" 里误命中。名称同时生成「容忍首尾一个字符差」的
    # 变体（Gscade↔Cascade 这类手写/拼写变体），但只对够长的名字启用，短名不开。
    num_tokens = []
    for tok in ("sn%d" % sn, "#%d" % sn, "sn %d" % sn, "子网%d" % sn, "sn:%d" % sn):
        num_tokens.append(tok.lower())

    name_patterns = []
    if name and len(name) >= 2:
        name_patterns.append(re.compile(r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])"))

    # 精确专名锚点：config 显式登记的才算，避免泛化词误命中。
    anchors = []
    for a in (cfg.get("research_sn_anchors") or {}).get(sn_s, []) or []:
        a = str(a).strip().lower()
        if a and len(a) >= 3:
            anchors.append(re.compile(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])"))

    # 同议题词组：只从「明确标本 SN」的消息里抽，且需本 SN 当日确有命中才启用。
    # 这样 SN25 那种当日 0 命中的子网不会被凭空塞 const 动态。
    own_events = [e for e in events or []
                  if sn_s in [str(x) for x in (e.get("sn") or [])]]
    topic_terms = _sn_event_anchors(own_events, cfg, sn=sn) if own_events else []

    def _names_me(text):
        low = text.lower()
        if any(t in low for t in num_tokens):
            return True
        if any(p.search(low) for p in name_patterns):
            return True
        # 子网名称的手写/拼写变体（Gscade↔cascade）：只对够长的名字（≥5）启用，
        # 且按词逐个比对、相似度阈值 0.72。SN25 的 "UR" 这种短名绝不走模糊，
        # 否则会在 your/architecture 里到处误命中（v1.22.1 修过的坑）。
        if len(name) >= 5:
            nl = name.lower()
            for tok in re.findall(r"[a-z][a-z0-9]{3,}", low):
                if abs(len(tok) - len(nl)) > 2:
                    continue
                if difflib.SequenceMatcher(None, tok, nl).ratio() >= 0.72:
                    return True
        return False

    def _topic_hits(text):
        """返回命中的本 SN 当日专属议题词个数（专名级，词边界）。"""
        if not topic_terms:
            return 0
        low = text.lower()
        return sum(1 for t in topic_terms
                   if len(t) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(t.lower())
                                                + r"(?![a-z0-9])", low))

    hits = []
    for e in events or []:
        tags = [str(x) for x in (e.get("sn") or [])]
        content = (e.get("content") or "")
        low = content.lower()
        names_me = _names_me(content)

        # ① dtt 明确标了本 SN → 保留
        if sn_s in tags:
            hits.append(e)
            continue
        # ② 明确标了别的 SN → 一律排除（除非内容又点名本子网）
        if tags and not names_me:
            continue
        # ③ 内容点名本子网项目名/SN编号 → 保留
        if names_me:
            hits.append(e)
            continue
        # ④ 未标 SN：命中显式登记的精确专名锚点 → 保留
        if anchors and any(p.search(low) for p in anchors):
            hits.append(e)
            continue
        # ⑤ 未标 SN：命中本 SN 当日专属议题词组 → 保留（const「19 days/SOTA」这类）。
        #    防误捞：单个泛化议题词只有**关键人物**发言才算；非关键人物必须命中
        #    至少 2 个专属词（同一件事的多个专名），避免 SN25 式误命中。
        ntopic = _topic_hits(content)
        if ntopic and (CD._key_author_hit(e, cfg) or ntopic >= 2):
            hits.append(e)
    return hits


def _people_summary_block(payload, cfg, sn, meta, llm, log=print, max_chars=180,
                          context=""):
    """与当前 SN 相关的人物观点：按人提炼成短句（含 const 与其他相关发言人）。

    复用一次小 LLM 调用做「相关性判断 + 提炼」，不贴链接、不抄原文、不用账号名。
    const 等创始人排最前；无相关发言返回空，绝不退回「原文全塞」。
    context 是本子网当日社区动态正文（含已抓的 X 帖正文），作相关性判断背景。
    返回 (block, kept_lines)。
    """
    import community_digest as CD
    names = CD._key_author_names(cfg)
    events = list((payload or {}).get("events") or [])
    if not events:
        return "", 0
    # 子网重要事件报告的社区动态：只保留跟当前子网相关的消息。
    # 「所有人物都列」是社区日报的职责，不在这里做（用户 2026-11-22 明确）。
    sn_name = (meta or {}).get("name") or ""
    hits = _sn_related_events(events, sn, sn_name, cfg, context=context)
    if not hits:
        return "", 0
    # 去重：同一人同一内容只留一条
    seen, uniq = set(), []
    for e in hits:
        k = (CD._display_author(e.get("author"), cfg), (e.get("content") or "")[:80])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    # 排序：本 SN 标注的优先 → 关键人物 → 其余；同组按相关性降序。
    def _is_key(e):
        return any(n in (e.get("author") or "").lower() for n in names)

    def _sn_hit(e):
        return sn in [x for x in (e.get("sn") or [])]

    uniq.sort(key=lambda e: (0 if _sn_hit(e) else (1 if _is_key(e) else 2),
                             -CD._relevance(e, cfg)))
    mine = uniq[:40]  # 上限防上下文爆炸（已预筛为相关消息）
    if not mine:
        return "", 0
    # 链接要抓正文，否则无法「总结链接内容」（用户明确要求）；只抓相关的少量。
    try:
        _enrich_community_events(mine, cfg, log=log, max_links=6)
    except Exception as e:
        log("   ⚠️ 人物观点链接正文抓取跳过（%s）" % str(e)[:100])
    lines = []
    for e in mine[:70]:
        c = CD._clean(e.get("content"), max_chars)
        nm = CD._clean(CD._display_author(e.get("author"), cfg), 40)
        mark = "★创始人" if _is_key(e) else ("标本SN" if _sn_hit(e) else "关键词命中")
        lines.append("- <%s>[%s] 形态=%s；SN归属=%s；内容=%s"
                     % (nm, mark, CD._msg_kind(e, cfg),
                        ",".join(str(x) for x in (e.get("sn") or [])) or "未标注",
                        c))
    sn_name = (meta or {}).get("name") or "未收录"
    bg = (context or "").strip()
    body = _PEOPLE_SUMMARY_PROMPT + (
        "\n【本子网：SN%d %s】\n" % (sn, sn_name)
        + "【本子网当日社区消息（判断相关性的背景）】\n"
        + (CD._clean(bg, 1400) if bg else "（无）")
        + "\n\n【当日发言线索（`<>` 内是显示名，直接用）】\n" + "\n".join(lines) + "\n"
    )
    try:
        out = (llm(body) or "").strip()
    except Exception as e:
        log("   ⚠️ 人物观点提炼失败（%s），跳过该段" % str(e)[:120])
        return "", 0
    if not out or out.strip() in ("无", "无。", "无相关", "没有相关发言"):
        return "", 0
    kept = []
    # 兜底：把关键人物的原始账号名统一成显示名（被回复对象里模型可能写回 @const_reborn）
    key_alias = {}
    for n in names:
        key_alias[n + "_reborn"] = n
        key_alias[n + "tt"] = n
    for ln in out.splitlines():
        ln = re.sub(r"^\s*[-•*]\s*", "", ln).strip()
        ln = re.sub(r"https?://\S+", "", ln).strip()  # 兜底删链接
        # 兜底：模型可能把输入线索里的 `<cats>...</cats>` 当 XML 标签抄进来，
        # 先去掉闭合标签 `</x>`，再去掉裸尖括号。
        ln = re.sub(r"</[^>]+>", "", ln)
        ln = re.sub(r"[<>]", "", ln).strip()
        for alias, canon in key_alias.items():
            ln = re.sub(r"@" + re.escape(alias) + r"\b", "@" + canon, ln, flags=re.I)
        # 兜底：清掉行内残留的 emoji / 希腊字母装饰（τ、🌊、✠ 等），保留 ASCII handle
        ln = re.sub(r"[\u0370-\u03ff\u2190-\u2bff\U0001f000-\U0001faff\ufe0f\u200d]", "", ln)
        ln = re.sub(r"\s+", " ", ln).strip()
        # 行首「人名 动作」部分再做一次显示名简化（如 Rayah τensor → Rayah）
        m = re.match(r"^(.+?)\s+(转推|回复|表示)\b", ln)
        if m:
            head_plain = CD._simplify_name(m.group(1))
            if head_plain != m.group(1):
                ln = head_plain + ln[m.end(1):]
        if ln and ln not in ("无", "无。") and len(kept) < 45:
            kept.append(ln)
    if not kept:
        return "", 0
    block = "**📢 当日社区动态（按人提炼）**\n%s" % (
        "\n".join("- " + x + " ⚠️未验证" for x in kept))
    return block, len(kept)


def _today_community_block(cfg, sn, day_str=None, log=print):
    """取当日 dtt 社区消息并拼段；取数失败一律不阻断投研主流程。

    返回 (block, kept, state)，state ∈ hit / empty / error；
    run_research 会把 state 打成固定日志行，供 TG 回执显示「当日 dtt 消息统计」。
    """
    day_str = day_str or datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        import community_fetch as CF
    except Exception as e:
        log("   ⚠️ 当日社区消息模块不可用（%s），跳过" % str(e)[:120])
        return "", 0, "error"
    try:
        payload = CF.read_day(day_str, cfg)
        if payload is not None and CF.cache_covers_now(payload, cfg):
            log("   当日 dtt 社区消息：复用 %s 已取数结果（%d 条）"
                % (day_str, (payload.get("stats") or {}).get("total", 0)))
        else:
            if payload is not None:
                log("   当日 dtt 社区消息：旧快照未覆盖当前，重取中…")
            payload = CF.fetch_day(day_str, cfg, log=log, until_now=True)
    except Exception as e:
        log("   ⚠️ 当日 dtt 社区消息取数失败（%s），本次投研继续，不带社区段" % str(e)[:160])
        return "", 0, "error"
    # 先把社区消息里 X 帖子的正文读出来 —— 否则报告只会写「附了一个链接」，
    # 链接里的实际内容完全没进素材（SN91 实测问题）。失败不阻断，按原文处理。
    try:
        _enrich_community_events(payload.get("events") or [], cfg, log=log)
    except Exception as e:
        log("   ⚠️ 社区 X 链接正文抓取失败（%s），按原始消息处理" % str(e)[:120])
    try:
        block, n = _format_community_today(
            payload, sn, limit=int(cfg.get("research_community_max", 12) or 12), cfg=cfg)
    except Exception as e:
        log("   ⚠️ 当日社区消息整理失败（%s），跳过" % str(e)[:120])
        return "", 0, "error"
    # 关键人物（const 等）动态**不注入主模型素材**：
    #  1) 它们大多与当前子网无关（SN91 报告混入 SN8/SN64/SN120 就是这个原因）；
    #  2) 主模型照抄后，那段又被当成 context 去捞「相关」消息，形成自我印证死循环。
    # 现在严格相关消息由代码层 _sn_related_events() 用强证据筛选，直接从 payload 取，
    # 不再经过主模型。主模型素材里只有「明确标注本 SN」的消息，让它写事实基线即可。
    if n:
        log("   ✅ 当日 dtt 社区消息：SN%d 明确归属 %d 条，已加入投研输入"
            % (sn, n))
    else:
        log("   当日 dtt 社区消息：SN%d 无明确归属消息" % sn)
    return block, n, ("hit" if n else "empty")


def _signal_block(sources):
    """硬规则信号：由代码判定、直接写进提示词，不允许模型忽略。

    Owner Conviction 解仓、神经元满员、注册关闭这类是**链上客观状态**，
    比群里任何说法都硬；模型容易只盯着聊天内容，必须显式点名。"""
    d = (sources.get("dtaoscan") or {}).get("facts") or {}
    b = (sources.get("bittensor.ai") or {}).get("facts") or {}
    sig = []
    pct = d.get("unwind_pct")
    st = d.get("owner_status") or ""
    try:
        pv = float(re.sub(r"[^\d.]", "", pct)) if pct else 0.0
    except Exception:
        pv = 0.0
    if pv >= 1 or "unwinding" in st.lower():
        sig.append("Owner 正在从 Conviction 锁仓中解仓：锁定 %s，解锁中 %s（%s%% of position）%s "
                   "—— 这是 owner 开始兑现的链上信号，必须在「风险」和「跟踪清单」里点名，"
                   "不得仅作中性数据罗列。"
                   % (d.get("owner_locked", "未获取"), d.get("unwinding", "未获取"),
                      pct or "?", ("；状态原文：" + st) if st else ""))
    elif 0 < pv < 1:
        # 0.008% 这种微调解仓：链上原文自己就标了 negligible，不能当解仓告警，
        # 但也不能完全抹掉 —— 进事实基线，说明「有微量但可忽略」。
        sig.append("Owner 锁仓有微量解仓（锁定 %s，解锁中 %s，仅 %s%% of position，链上原文：%s）"
                   "—— 量级可忽略，写进事实基线即可，不要写成解仓风险。"
                   % (d.get("owner_locked", "未获取"), d.get("unwinding", "未获取"), pct, st or "-"))
    elif "owner locked in" in st.lower():
        sig.append("Owner 处于完全锁仓状态（%s），无解仓 —— 属正面信号，可写入事实基线。" % st)
    nr = b.get("neurons") or d.get("neurons") or ""
    m = re.match(r"\s*([0-9,]+)\s*/\s*([0-9,]+)", nr)
    if m and m.group(1).replace(",", "") == m.group(2).replace(",", ""):
        # 满员 ≠ 不能进：注册窗口可能仍开着，只是新矿工必须 burn 抢 slot。
        # 之前一律写「无法注册」，会和事实基线里的「注册状态 Open」直接打架。
        if (b.get("reg") or "").lower() == "closed":
            sig.append("神经元已满员（%s）且注册已关闭：新矿工当前完全无法入场。" % nr)
        elif (b.get("reg") or "").lower() == "open":
            sig.append("神经元已满员（%s）但注册窗口仍开放：新矿工只能靠 burn（销毁 TAO）抢 slot，"
                       "不能直接注册入场 —— 表述必须是「满员 + 需 burn」，"
                       "不得写成「注册已关闭」。" % nr)
        else:
            sig.append("神经元已满员（%s）：常规注册路径已堵，新矿工需 burn 抢 slot 或二级市场购买。"
                       % nr)
    if (b.get("reg") or "").lower() == "closed":
        sig.append("注册已关闭（Registration Closed）：新矿工当前无法入场。")
    em, eu = d.get("emission"), d.get("emission_usd")
    if em:
        sig.append("Emission 规模：%s（约 $%s/日）—— 与其他子网对比时用绝对值，"
                   "不得换算成百分比或年化。" % (em, eu or "未获取"))
    if not sig:
        return ""
    return ("【代码预判的硬信号（必须体现在报告中，不得忽略）】\n- " + "\n- ".join(sig))


def build_input(arc, sources, meta, days=14, cap_per_day=260, prev_arc=None, today=None,
                arc_dir=None, community_today=""):
    """拼装投研输入。arc 为 None 时跳过群聊段（纯全网投研）。

    community_today：当日 dtt 关键词监控里该子网的社区消息段（由 run_research 取数拼好），
    有则单独成段并强制模型在报告里写「📢 当日社区动态」。

    prev_arc：用于取「此前投研结论」的档案。arc 会被 run_research 置 None（有档案但无群聊条目），
    此时历史报告仍然存在，必须单独传进来，否则「上次说了什么」永远进不了输入。
    """
    arc_txt, n, keys = ("", 0, []) if not arc else _arc_block(arc, days=days, cap_per_day=cap_per_day)
    hist = _prev_reports_block(prev_arc if prev_arc is not None else arc,
                              skip_date=today, arc_dir=arc_dir)
    head = (
        "【目标子网】SN%d %s\n【知识库业务描述】%s\n【官网】%s\n【仓库】%s\n"
        "【今日】%s\n"
        "【本地档案】%s\n"
        % (meta["sn"], meta["name"] or "未收录", meta["desc"] or "未收录",
           meta["url"] or "无", meta["github"] or "无",
           datetime.datetime.now(TZ).strftime("%Y-%m-%d"),
           ("%s ~ %s（%d 个交易日，%d 条命中消息）" % (keys[0], keys[-1], len(keys), n))
           if keys else "**本次无本地档案**（不做群聊对照，全部结论只依赖外部源）"))
    if meta.get("name_changed_from"):
        # 改名必须显式告知模型：否则它会从旧群聊/旧报告里继续沿用旧名，
        # 或者把「新旧两个名字」当成两个不同的项目来写。
        head += ("【⚠️ 改名提示】本子网**已改名**：旧名「%s」→ 现名「%s」（依据：%s）。\n"
                 "报告中一律使用现名；旧名只在解释历史事件时以「（原名 %s）」形式出现。\n"
                 % (meta["name_changed_from"], meta["name"],
                    meta.get("name_change_source") or "链上身份",
                    meta["name_changed_from"]))
    sig = _signal_block(sources)
    # 收入模块：单独成段，且**必须**出现在报告里（用户明确要求）。
    # 上次快照从档案的 income_hist 取（每次投研都会 append），用来比「变化」。
    _inc = sources.get("收入/回购") or {}
    _prev_snap = {}
    _pa = prev_arc if prev_arc is not None else arc
    if _pa:
        _ih = _pa.get("income_hist") or []
        # 取「最近一次、且不是今天」的快照：同日重跑不该跟自己比
        for _x in reversed(_ih):
            if (_x.get("snapshot") or {}) and (_x.get("date") or "") != today:
                _prev_snap = _x["snapshot"]
                break
    inc_txt, inc_snap = income_block(_inc, prev_snap=_prev_snap)
    # 档案里登记的回购地址（群聊推断 / 人工补录）：仅作线索。数据站零回购但档案有地址时，
    # 必须并列呈现冲突，不能一边倒写「团队没回购」。
    _wallet_txt = _wallet_block(_pa)
    # 社区消息日报线索：多平台公开讨论，未经证实。单独成段，强制与链上/数据站交叉核对。
    _community_txt = _community_block(_pa)
    body = ("\n===== 一、链上/数据站事实（抓取原文）=====\n" + _ext_block(sources, meta)
            + "\n\n===== 二、交叉验证矩阵（同指标多源并列）=====\n" + _consensus_block(build_consensus(sources)))
    if n:
        body = ("\n===== 一、本地群聊档案（Discord #price-talk，按日分段、按时间升序）=====\n" + arc_txt
                + "\n\n===== 二、链上/数据站事实（抓取原文）=====\n" + _ext_block(sources, meta)
                + "\n\n===== 三、交叉验证矩阵（同指标多源并列）=====\n" + _consensus_block(build_consensus(sources)))
    if inc_txt:
        body += "\n\n===== 收入与回购（链上口径，报告必须单独成段）=====\n" + inc_txt
    if _wallet_txt:
        body += "\n\n===== 档案登记的回购地址（仅线索，不代表已链上确认）=====\n" + _wallet_txt
    if _community_txt:
        body += "\n\n===== 社区消息线索（多平台公开讨论，未经证实，须交叉核对）=====\n" + _community_txt
    if community_today:
        body += ("\n\n===== 当日 dtt 社区消息（SN%d，未经证实，报告必须单独成节）=====\n"
                 % meta["sn"]) + community_today
    if hist:
        body += "\n\n===== 此前投研结论（历史留档）=====\n" + hist
    # 上次的「预期跟踪」逐条清单：报告必须逐条打钩（兑现/证伪/未触发）
    _trk = tracking_block(_pa, today=today, arc_dir=arc_dir)
    if _trk:
        body += "\n\n===== 上次跟踪事项（逐条回答）=====\n" + _trk
    if sig:
        body += "\n\n===== 末、" + sig
    elif not n:
        # 没有群聊素材时，交叉验证只能发生在「外部源之间」，必须写明，避免模型编造群内说法
        body += ("\n\n【注意】本次**没有本地群聊档案**：不得出现「群内有人说」「群里传」之类表述，"
                 "也不得虚构社区情绪；交叉验证只在外部源（数据站/官网/GitHub/新闻/搜索/X）之间进行。")
    # 收入快照挂到模块级"最后一次构建"上：改返回值会破坏所有按字符串使用它的调用点，
    # 而快照只有 run_research 落档时用得到，走旁路最省事。
    global _LAST_INC_SNAP
    _LAST_INC_SNAP = inc_snap or {}
    return head + body


# ------------------------------------------------------------------ 主流程
def run_research(cfg, sn_arg, targets=None, days=14, log=print, llm=None, push=True,
                 feedback=""):
    """生成并推送子网重要事件跟踪报告。返回 (title, digest, path)。

    feedback：用户对上一份报告的修正意见（来自 TG `/retry <子网> <反馈>`）。
    这是**权威修正**，优先于本地知识库与自动抓取：用户明确指出「127 已改名为 Zils」，
    就不能再按知识库里的 Astrid 出稿。反馈会写进输入并单独成段，提示模型据此纠正。
    """
    arc_dir = A.get_arc_dir(cfg)
    sn = A.resolve_sn(sn_arg, cfg)
    if sn is None:
        raise RuntimeError("无法识别子网：%s（可用编号 29 / SN29，或知识库里的子网名）" % sn_arg)
    feedback = (feedback or "").strip()
    arc = A.load_archive(arc_dir, sn)
    # 落档要用「今天」的日期；同时用来在历史报告里排除当天（同日重跑不跟自己的旧版对比）
    today = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    # 历史投研结论要单独留一份引用：下面会把 arc 置 None（有档案但没群聊条目），
    # 但「上次投研说了什么」仍然必须进输入，否则写回档案的报告永远是死档。
    prev_arc = arc
    # 没有本地档案 / 档案里没消息：**直接走全网投研**，不读导出记录、更不触发任何导出。
    # 群聊档案在这里退化为「可选加强项」而不是前置条件 —— 用户要的是随时能出报告。
    if arc and not (arc.get("days")):
        arc = None
    if arc:
        log("① 调取本地档案：%d 条命中消息，覆盖 %d 天（最近：%s）"
            % (sum(len(v) for v in arc["days"].values()), len(arc["days"]),
               "、".join(sorted(arc["days"].keys())[-5:])))
    else:
        log("① 调取本地档案：SN%d 无群聊归档（不读导出记录、不做导出），本次以档案历史报告 + 全网为准" % sn)
    _nrep = len((prev_arc or {}).get("reports") or [])
    if _nrep:
        log("   档案内历史投研：%d 份（最近 %s），将作为「变化对比」基线一并送入"
            % (_nrep, ((prev_arc.get("reports") or [{}])[0].get("date") or "-")))
    else:
        log("   档案内暂无历史投研报告：本次是首份，出稿后会写入档案")
    meta = _subnet_meta(sn)
    if not meta["name"] and prev_arc:
        meta["name"] = prev_arc.get("name") or ""
    # 名称以链上身份为准：知识库是离线快照，子网改名后不会自动跟随（SN127 就是
    # 知识库 Astrid / 链上 Zils）。抓不到链上数据时保留知识库值，不阻断投研。
    log("   名称核对：链上身份（taostats）…")
    meta = apply_chain_name(meta, chain_name(sn, log=log), log=log)
    if feedback:
        # 用户反馈里的名称同样要能纠正错误：即使链上抓取失败，也要采信用户。
        _fb_names = _names_in_feedback(feedback)
        if _fb_names and meta.get("name") and \
                _fb_names[0].lower() != A.ascii_name(meta["name"]).lower():
            meta.setdefault("name_changed_from", meta["name"])
            meta.setdefault("name_change_source", "用户反馈")
            meta["aliases"] = sorted(set((meta.get("aliases") or []) + [meta["name"]]))
            log("   ⚠️ 按用户反馈改名：「%s」→「%s」" % (meta["name"], _fb_names[0]))
            meta["name"] = _fb_names[0]
    log("===== 子网投研启动：SN%d %s =====" % (sn, meta["name"] or "（名称待反查）"))
    if feedback:
        log("   本次带用户反馈（将作为权威修正写入输入）：%s" % feedback[:100])
    # 群里/用户反馈里贴出的 X 链接：用户既然给了链接，就必须真去读出来。
    # 2026-11-11：链接分两类 —— 帖子（/status/ID）和**账号主页**（/handle）。
    # 用户说「24官推 https://x.com/ms_computer」给的是后者，以前整条被丢掉。
    _tg = x_targets_in(feedback)
    # 必须复制：下面会把群聊档案里翻出来的账号 append 进 extra_handles，
    # 若直接引用 _tg["handles"]，群聊路人号会一起变成「用户指定的官推」权威证据。
    extra_links, extra_handles = list(_tg["posts"]), list(_tg["handles"])
    auth_handles = list(_tg["handles"])
    # 群聊档案里出现过的 X 链接同样是「社区看到的最新资讯」，一并读（近期的优先）
    if arc:
        _chat_txt = "\n".join(e.get("text") or ""
                              for ents in (arc.get("days") or {}).values() for e in ents)
        _ct = x_targets_in(_chat_txt)
        for u in _ct["posts"]:
            if u not in extra_links:
                extra_links.append(u)
        for h in _ct["handles"]:
            if h not in extra_handles:
                extra_handles.append(h)
    extra_links = extra_links[:8]
    extra_handles = extra_handles[:6]
    if arc:
        # 历史报告里已经写明的官推，下次不带链接也要能接着用
        for h in x_official_from_archive(arc):
            if h not in extra_handles:
                extra_handles.append(h)
            if h not in auth_handles:
                auth_handles.append(h)
    if extra_links:
        log("   待读取的 X 帖链接：%d 条（来自反馈/群聊）" % len(extra_links))
    if extra_handles:
        log("   待核实的 X 账号：%s（来自反馈/群聊，按官方账号候选处理）"
            % "、".join("@" + h for h in extra_handles))
    log("② 全网抓取增补最新资讯（链上数据站 / 官网 / GitHub / 新闻 / 搜索 / X）…")
    # auth_handles = 本次反馈给的官推 + 历史报告里已确认过的官推；
    # 群聊里翻出来的账号只作候选，不算权威（避免把路人号直接定成官方）。
    sources = collect(meta, sn, log=log, extra_x_links=extra_links,
                      extra_x_handles=extra_handles,
                      authority_x_handles=auth_handles, days=days)
    # 知识库缺名称/官网时，用数据站页面反查补齐，再补抓依赖这些字段的源（官网/GitHub/搜索/X）
    before = (meta.get("name"), meta.get("url"), meta.get("github"))
    meta = enrich_meta(meta, sources, log=log)
    if (meta.get("name"), meta.get("url"), meta.get("github")) != before:
        for name, fn in (("官网", lambda: src_website(meta)),
                         ("GitHub", lambda: src_github(meta, days=days)),
                         ("DuckDuckGo", lambda: src_web_search(meta, sn))):
            if sources.get(name, {}).get("ok"):
                continue
            try:
                r = fn()
            except Exception as e:
                r = {"src": name, "ok": False, "err": str(e)[:120]}
            sources[name] = r
            log("  投研补抓 %-12s %s" % (name, "OK" if r.get("ok") else "失败"))
        if not (sources.get("X") or {}).get("ok"):
            try:
                r = src_x(meta, site_html=(sources.get("官网") or {}).get("html") or "",
                          extra_links=extra_links, extra_handles=extra_handles,
                          authority_handles=auth_handles, days=days, log=log)
            except Exception as e:
                r = {"src": "X", "ok": False, "err": str(e)[:120], "recent": []}
            sources["X"] = r
            log("  投研补抓 %-12s %s" % ("X", "OK" if r.get("ok") else "失败"))
    # ③ 当日 dtt 社区消息（关键词监控）：有该 SN 的消息才注入；取数失败不阻断投研。
    #    放在全网抓取之后，复用已取数缓存（社区日报刚跑过就直接复用，不重复请求 dtt）。
    community_today, _nct, _dtt_state = "", 0, "off"
    if cfg.get("research_community_today", True):
        log("③ 当日 dtt 社区消息（关键词监控）…")
        community_today, _nct, _dtt_state = _today_community_block(cfg, sn, today, log=log)
    else:
        log("③ 当日 dtt 社区消息：配置 research_community_today=false，跳过")
    text_block = build_input(arc, sources, meta, days=days,
                             cap_per_day=int(cfg.get("research_max_items_per_day", 260) or 260),
                             prev_arc=prev_arc, today=today, arc_dir=arc_dir,
                             community_today=community_today)
    if feedback:
        # 用户反馈单独成段，且明确标「权威修正」：模型很吃这一套，
        # 只把反馈混在正文里它常常当成又一条群聊传闻，改不动已经写下的判断。
        _xsrc = sources.get("X") or {}
        _xr = _xsrc.get("linked") or []
        _xprof = _xsrc.get("profile") or {}
        _xnote = ""
        if extra_links and _xr:
            _xnote = ("\n【重要】反馈里的 X 链接已真实抓取到原文，正文在上面的 [X] 段里（标注了"
                      "「用户提供」）。**必须**把这些内容写进时间线并注明日期与发布者；"
                      "**禁止**再写「帖子内容未获取/待确认」这类话——原文就在输入里。\n")
            if _xprof:
                _xnote += ("\n【补充】同时核实了账号 @%s（%s），资料见 [X] 段。\n"
                           % (_xprof.get("handle") or "?", _xprof.get("name") or ""))
        elif (extra_links or extra_handles) and _xprof:
            _xnote = ("\n【重要】反馈里给出的 X 账号已**真实核实**：@%s（%s；%s），"
                      "完整资料在上面的 [X] 段。报告**必须**写成「已核实官方账号存在，"
                      "目前无公开贴文」，**禁止**再写「官推找不到 / 链接不可访问 / 待确认」，"
                      "也**禁止**编造任何推文内容。\n"
                      % (_xprof.get("handle") or "?", _xprof.get("name") or "",
                         _xsrc.get("empty_reason") or "本次时间窗内无新贴文"))
        elif extra_links and not _xr:
            _xnote = ("\n【说明】反馈里的 X 帖子链接本次抓取失败（可能已被删除或设为私密），"
                      "如需提及只能写「链接不可访问」，不得编造其内容。\n")
        elif extra_handles:
            _xnote = ("\n【说明】反馈里给出的 X 账号（%s）本次未能核实（可能不存在或接口暂不可用），"
                      "只能写「链接不可访问/未核实」，不得编造账号资料或推文。\n"
                      % "、".join("@" + h for h in extra_handles))
        text_block += (
            "\n\n===== 用户反馈（最高优先级，必须据此修正）=====\n"
            "%s\n"
            "【执行要求】\n"
            "1. 上面的反馈是**用户直接指出的错误/补充事实**，优先级高于本地知识库、"
            "高于此前报告、高于自动抓取到的旧页面文字。\n"
            "2. 若反馈指出名称/身份不对（例如编号已改名），本次报告**必须使用新名字**，"
            "并在时间线里补一条该改名的说明；不得再出现旧名作为当前名称使用。\n"
            "3. 反馈里提到但抓取素材中没有的事实，按「⚠️未验证（用户反馈，待链上/官方确认）」处理，"
            "不要伪造成已验证。\n"
            "4. 重新核对整篇报告里与反馈冲突的表述，逐条改掉；不要只改标题。%s" % (feedback, _xnote))
    _nhist = len((prev_arc or {}).get("reports") or [])
    log("投研素材：%d 字符（%s + 外部 %d 源，成功 %d 源%s）"
        % (len(text_block), "群聊" if arc else "无群聊", len(sources),
           sum(1 for v in sources.values() if v.get("ok")),
           ("；含 %d 份历史投研做对比" % _nhist) if _nhist else ""))
    import pipeline
    if llm is None:
        def llm(body, sp=None):
            return pipeline.call_deepseek(cfg, body, system_prompt=sp)

    max_chars = int(cfg.get("research_max_chars", 3000) or 3000)
    digest = llm(text_block, research_prompt(days, max_chars=max_chars))
    # 字数标注清除：模型会把「每条 45-60 字」这类配额直接抄进正文，
    # 变成「**kτwτ**（45-60字）：…」——读者看到的是提示词不是内容，必须先删掉。
    try:
        digest = pipeline.strip_length_tags(digest or "")
    except Exception:
        pass
    # 二次校对：核对数字/来源标记/传闻降级（失败不阻断，报告仍可用）
    # 注意 sanitize_fn 必须传恒等函数：投研报告要保留英文来源标签与人名，
    # 日报那套 sanitize_digest 会把英文全译成中文，反而破坏来源可追溯性。
    try:
        digest = pipeline.polish_digest(cfg, text_block, digest,
                                        verify_prompt=RESEARCH_VERIFY_PROMPT,
                                        sanitize_fn=lambda s: pipeline.strip_length_tags(s))
    except Exception as e:
        log("投研校对跳过（%s）" % e)
    digest = (digest or "").strip()
    # 字数闸门：只限上限（默认 3000 字，config.research_max_chars 可改），不设下限。
    # rounds=3：模型每轮只删一部分，两轮常常压不到位（实测 5061→3465），三轮才收得住。
    try:
        digest = pipeline.compress_digest(cfg, digest, 0, max_chars, rounds=3,
                                          system_prompt=RESEARCH_COMPRESS_PROMPT,
                                          sanitize_fn=lambda s: pipeline.strip_length_tags(s),
                                          cut_hint="报告有四段骨架（时间线 / 当前数据 / 收入与回购 / 预期跟踪），"
                                                   "有 dtt 社区素材时还有第五段「📢 当日社区动态」。"
                                                   "压缩顺序：① 先删时间线里最不重要的整条事件（合并同类、去掉铺垫词）；"
                                                   "② 每条事件压到一行、去掉形容词与重复解释，**但日期、事件核心、"
                                                   "✅已验证/⚠️未验证标记必须留着**；"
                                                   "③ 预期跟踪压到 4 条；"
                                                   "④ 「📢 当日社区动态」**只许压短表述、绝对不许整节删除**，"
                                                   "每条的 [社区] 标记、⚠️未验证、人名、消息具体内容必须保留；"
                                                   "⑤ 「当前价格」「💰 收入与回购」两段**永远不许删**"
                                                   "（收入段是用户点名要求的）；收入段最多把相近项合并成一行，"
                                                   "但收入额、回购金额、变化百分比一个都不许丢；"
                                                   "⑥ 时间线末尾的红色图例注明行、以及带 <font color=\"red\">📰</font> 的"
                                                   "红色资讯条目**不许删**。"
                                                   "全部数字、来源标记、人名、SN 编号、<font color=\"red\"> 标红必须原样保留。")
    except Exception as e:
        log("投研压缩跳过（%s）" % e)
    # 排版整理：时间线每行一条、条间不空行、补 `- `、删占位条目
    try:
        digest, _ = tidy_layout(digest, log=log)
    except Exception as e:
        log("排版整理跳过（%s）" % e)
    # 日期去重：同一年份只在首条出现一次（模型常忘，代码兜底）
    try:
        digest, _ = tidy_dates(digest, log=log)
    except Exception as e:
        log("日期整理跳过（%s）" % e)
    # GitHub 条目兜底：必须有「提交次数 + 中文更新内容」，不许只写「提交集中在当日」，清英文残留
    try:
        digest, _ = tidy_github_line(
            digest, gh_sum=(sources.get("GitHub") or {}).get("summary"), log=log)
    except Exception as e:
        log("GitHub 条目整理跳过（%s）" % e)
    # 数字硬校验：报告里每个带单位的数字都必须能在素材里逐字找到，
    # 找不到的降级为「（数值未核实）」—— 宁可难看，也不放编造数字流出
    try:
        miss, rate = audit_numbers(digest, text_block, log=log)
        if miss:
            digest, nfix = strip_unknown_numbers(digest, miss)
            log("数字降级：%d 处可疑数字已标注为「（数值未核实）」" % nfix)
    except Exception as e:
        log("数字校验跳过（%s）" % e)
    # 来源引用校验：抓取失败的源不许被当成引用（「没抓到」≠「官方没动作」）
    try:
        digest, bad_hits = audit_sources(digest, sources, log=log)
        if bad_hits:
            log("⚠️ 来源引用：%d 行引用了抓取失败的源（%s），正在改写"
                % (len(bad_hits), "、".join(sorted({m for _, _, mks in bad_hits for m in mks}))))
            digest = rewrite_bad_sources(digest, bad_hits, sources, llm=llm, log=log)
            digest, still = audit_sources(digest, sources, log=log)
            if still:
                log("⚠️ 改写后仍有 %d 行引用失败源（已保留，人工复核）" % len(still))
    except Exception as e:
        log("来源引用校验跳过（%s）" % e)
    # 最新资讯并入时间线（v1.15.0）：放在所有「会重写整篇」的步骤之后，
    # 否则 rewrite_bad_sources / polish 用模型重写时可能又吐回旧的五段格式，把标红结果覆盖掉。
    try:
        digest, _nfix = tidy_news_in_timeline(digest, log=log)
        if _nfix:
            log("资讯/时间线整理：%d 处调整" % _nfix)
    except Exception as e:
        log("资讯并入时间线跳过（%s）" % e)
    # ===== 📢 当日社区动态：完全由代码层接管，绝不保留主模型照抄的英文原节 =====
    # 主模型那节是英文原文照抄、人名是原始账号，且（旧逻辑下）可能塞满与子网无关的
    # const 生态动态。这里先整体删掉，再按「强证据筛选结果」重建：
    #   有相关消息 → 提炼中文短句；无相关消息 → 如实写一句说明；提炼失败 → 也写说明。
    if "📢 当日社区动态" in digest:
        digest, _ = _take_section(digest, "📢 当日社区动态")

    _comm_done = False
    if _dtt_state == "hit" and _nct:
        try:
            import community_fetch as CF2
            _p2 = CF2.read_day(today, cfg)
            _cs, _cn = _people_summary_block(_p2, cfg, sn, meta, llm, log=log)
            if _cs:
                digest = _insert_section(digest, _cs, "📌 预期跟踪")
                log("📢 当日社区动态：强证据筛出 %d 条相关消息，已提炼为中文短句" % _cn)
                _comm_done = True
        except Exception as e:
            log("📢 当日社区动态提炼跳过（%s）" % str(e)[:120])

    if not _comm_done and _dtt_state in ("hit", "empty"):
        # 无相关消息或提炼失败：写一句如实说明，绝不退回主模型的全量无关原节。
        if _dtt_state == "empty":
            _note = ("**📢 当日社区动态**\n"
                     "当日 dtt 关键词监控未抓到与 SN%d（%s）明确相关的社区消息，本节从略。⚠️未验证"
                     % (sn, meta.get("name") or "未收录"))
        else:
            _note = ("**📢 当日社区动态**\n"
                     "当日抓到 SN%d 相关消息，但自动提炼未成功，暂不列出（不影响报告其它章节）。" % sn)
        digest = _insert_section(digest, _note, "📌 预期跟踪")
        log("📢 当日社区动态：无相关消息/提炼失败，已写如实说明（不保留主模型原节）")
    # 时间线倒序 + 上限（v1.23.0）：放在社区动态之后，回填的「跟踪进度补充」节
    # 紧挨「📌 预期跟踪」之前。此步之后不再有重写整篇的步骤，排序结果不会被覆盖。
    try:
        digest, _tstat = sort_cap_timeline(digest, log=log)
    except Exception as e:
        log("时间线倒序/限条跳过（%s）" % e)
    title = "🔬 SN%d %s · 重要事件跟踪报告（%s）" % (
        sn, meta["name"] or "未收录", datetime.datetime.now(TZ).strftime("%m-%d"))
    d = A.sn_dir(arc_dir, sn)
    os.makedirs(d, exist_ok=True)
    today = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    fname = "research_%s.md" % today
    path = os.path.join(d, fname)
    ok_srcs = [k for k, v in sources.items() if v.get("ok")]

    def _src_state(k, v):
        if not v.get("ok"):
            return "%s✗" % k
        # 收入/回购源的结构跟其它源不同（没有 items/facts，用的是 owner_tao 等扁平字段），
        # 漏判会让它明明抓到数据却显示「本期无内容」——实测 SN51 就出现过这个假警报。
        if k == "收入/回购":
            return "%s✓" % k if (v.get("owner_tao") or v.get("ob")
                                 or v.get("protocol_buy")) else "%s✓（本期无内容）" % k
        # GitHub 翻页全挂但仓库主页兜底成功时，提交列表是空的；
        # 若按通用规则标「本期无内容」会误导读者以为什么都没抓到。
        if k == "GitHub" and (v.get("summary") or {}).get("fallback"):
            return "%s✓（翻页受限，已由仓库主页兜底）" % k
        if not (v.get("items") or v.get("recent") or v.get("facts") or v.get("posts")):
            return "%s✓（本期无内容）" % k
        return "%s✓" % k

    with open(path, "w", encoding="utf-8") as f:
        f.write("# %s\n\n> 生成于 %s ｜ 数据源：%s\n> 抓取状态：%s\n\n%s\n" % (
            title, datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
            ("本地档案 + " if arc else "（无本地档案）纯全网 ") + "、".join(ok_srcs),
            "；".join(_src_state(k, v) for k, v in sources.items()),
            digest))
    log("④ 报告已生成：%s（%d 字）" % (path, len(re.sub(r"\s", "", digest))))
    # 固定格式的一行：TG 投研回执从这里抓「当日 dtt 消息统计」（不重新取数、不调 AI）。
    # 文案按最终报告里有没有这一节写，不能素材注入了却说「报告内见」——实测压缩会删节。
    _in_report = "📢 当日社区动态" in digest
    log({
        "hit": ("📢 当日dtt社区消息：%d 条，已写入报告「📢 当日社区动态」节" % _nct)
               if _in_report else
               ("📢 当日dtt社区消息：%d 条，已注入素材但报告未生成该节（请检查）" % _nct),
        "empty": "📢 当日dtt社区消息：无命中，未写入报告",
        "error": "📢 当日dtt社区消息：取数失败，未写入报告（不影响投研）",
    }.get(_dtt_state, "📢 当日dtt社区消息：已关闭"))
    # 投研产出增量写回子网档案：报告正文 + 结论 + 时间线 + 数据源一起入库；
    # 已有档案只替换本日记录（幂等），没有档案就新建一个 —— 这是下次投研的输入来源。
    try:
        _arc = A.attach_research(cfg, sn, today, title, digest, sources=ok_srcs,
                                 meta={"name": meta.get("name"), "desc": meta.get("desc"),
                                       "url": meta.get("url"), "github": meta.get("github")},
                                 file_name=fname, log=log,
                                 income_snap=(_LAST_INC_SNAP or None))
        log("⑤ 档案增量更新完成：SN%d 现有投研 %d 份、群聊 %d 条 —— 下次投研会先读它"
            % (sn, len(_arc.get("reports") or []),
               sum(len(v) for v in (_arc.get("days") or {}).values())))
    except Exception as e:
        log("⑤ 投研落档失败（报告文件已留存，档案未更新）：%s" % e)
    if push and cfg.get("push_enabled", True):
        want = [x.lower() for x in (targets or ["feishu", "tg"])]
        if "feishu" in want:
            try:
                pipeline.push_feishu(cfg, title, digest)
            except Exception as e:
                log("飞书推送异常：%s" % e)
        if "tg" in want:
            try:
                pipeline.push_telegram(cfg, title, digest)
            except Exception as e:
                log("Telegram 推送异常：%s" % e)
    log("===== 子网投研结束 =====")
    return title, digest, path
