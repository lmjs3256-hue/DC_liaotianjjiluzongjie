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
import hashlib
import html
import json
import os
import re
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
    """复用 pipeline 的代理分流（国内直连、境外走系统代理），失败则用默认 opener。"""
    try:
        import pipeline
        return pipeline._opener_for(url)
    except Exception:
        return urllib.request.build_opener()


def fetch(url, timeout=25, limit=3_000_000, retries=2):
    """抓取 URL，返回 (ok, text_or_err)。任何异常都不抛出，交给上层降级。"""
    last = ""
    for i in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers=UA)
            with _opener(url).open(req, timeout=timeout) as r:
                raw = r.read(limit)
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


def src_github(meta, days=120):
    """GitHub releases + commits（Atom 源，无需 token）。"""
    gh = meta.get("github") or ""
    m = re.search(r"github\.com/([^/]+)/([^/?#]+)", gh)
    if not m:
        return {"src": "GitHub", "ok": False, "err": "知识库无仓库地址"}
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
    out = {"src": "GitHub", "ok": True, "repo": "%s/%s" % (owner, repo),
           "releases": [], "commits": [], "err": ""}
    ok, x = fetch("https://github.com/%s/%s/releases.atom" % (owner, repo))
    if ok:
        for it in re.findall(r"<entry>(.*?)</entry>", x, re.S)[:8]:
            ti = re.search(r"<title>(.*?)</title>", it, re.S)
            up = re.search(r"<updated>(.*?)</updated>", it)
            out["releases"].append({
                "title": html.unescape(re.sub(r"<.*?>", "", ti.group(1))).strip() if ti else "",
                "at": (up.group(1) if up else "")[:10]})
    ok, x = fetch("https://github.com/%s/%s/commits.atom" % (owner, repo))
    if not ok:
        ok, x = fetch("https://github.com/%s/%s/commits/main.atom" % (owner, repo))
    if ok:
        for it in re.findall(r"<entry>(.*?)</entry>", x, re.S)[:15]:
            ti = re.search(r"<title>(.*?)</title>", it, re.S)
            up = re.search(r"<updated>(.*?)</updated>", it)
            out["commits"].append({
                "title": html.unescape(re.sub(r"<.*?>", "", ti.group(1))).strip() if ti else "",
                "at": (up.group(1) if up else "")[:10]})
    if not out["releases"] and not out["commits"]:
        out["ok"] = False
        out["err"] = "仓库不存在或无公开动态"
    return out


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
X_RESERVED = {"i", "intent", "share", "home", "search", "hashtag", "status",
              "explore", "settings", "login", "signup", "tos", "privacy", "compose"}


def _fx_json(url, timeout=25):
    """取 fxtwitter JSON。它偶尔 404/SSL 抖动，固定重试 2 次（1.5s 退避）。"""
    import time as _t
    last = ""
    for i in range(3):
        ok, txt = fetch(url, timeout=timeout, retries=1)
        if ok and txt.strip().startswith("{"):
            try:
                return True, json.loads(txt)
            except Exception as e:
                last = "JSON 解析失败：%s" % str(e)[:80]
        else:
            last = (txt or "")[:100]
        if i < 2:
            _t.sleep(1.5 * (i + 1))
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
            mm = re.search(r"x\.com/([A-Za-z0-9_]{2,20})", it.get("url", ""))
            if mm and mm.group(1).lower() not in X_RESERVED and "/status/" in it.get("url", ""):
                cands.append(mm.group(1))
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


def x_user_posts(handle, pages=2, log=None):
    """取某账号最近贴文（fxtwitter，可按 bottom 游标翻页）。
    返回 (profile_dict, [post...])；账号不存在/取不到时 profile 为 None。"""
    hd = (handle or "").lstrip("@").strip()
    if not hd:
        return None, []
    ok, d = _fx_json(FX_PROFILE % hd)
    if not ok or not isinstance(d, dict):
        return None, []
    prof = None
    posts = []
    for it in d.get("results") or []:
        if it.get("type") not in (None, "status"):
            continue
        p = _x_post_from_fx(it)
        if not p["text"]:
            continue
        if prof is None and p["author"].lower() == hd.lower():
            prof = {"handle": p["author"], "name": p["author_name"], "bio": p["bio"]}
        posts.append(p)
    if prof is None:
        prof = {"handle": hd, "name": "", "bio": ""}
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
        log("    X @%s：%d 条贴文（含转推）" % (hd, len(posts)))
    return prof, posts


def x_tweet(url_or_id, log=None):
    """按链接/ID 抓单条 X 帖（用户在反馈里贴的链接必须能读出来，不能只写「未获取」）。"""
    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{2,20})/status/(\d+)", url_or_id or "")
    if m:
        hd, tid = m.group(1), m.group(2)
    else:
        m2 = re.search(r"\b(\d{15,25})\b", url_or_id or "")
        if not m2:
            return None
        hd, tid = "_", m2.group(1)
    ok, d = _fx_json(FX_TWEET % (hd, tid) if hd != "_" else
                     "https://api.fxtwitter.com/status/%s" % tid)
    if not ok or not isinstance(d, dict):
        return None
    t = d.get("tweet") or {}
    if not t:
        return None
    p = _x_post_from_fx(t)
    # 顶层 tweet 可能不含 author 详情，补一次
    if not p["bio"]:
        p["bio"] = ((t.get("author") or {}).get("description") or "").strip()
    if log:
        log("    X 单帖已读取：@%s %s「%s…」" % (p["author"], p["at"], p["text"][:40]))
    return p


def x_links_in(text):
    """从任意文本（群聊、用户反馈）里抽出 X 帖链接，去重保序。"""
    out = []
    for m in re.finditer(r"(?:https?://)?(?:www\.)?(?:x|twitter)\.com/[A-Za-z0-9_]{2,20}/status/\d+",
                         text or ""):
        u = m.group(0)
        if not u.startswith("http"):
            u = "https://" + u
        if u not in out:
            out.append(u)
    return out


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
    for h in X_ECO_ACCOUNTS:
        if h.lower() in skip:
            continue
        try:
            _p, posts = x_user_posts(h, pages=1)
        except Exception:
            continue
        for p in posts:
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


def src_x(meta, site_html="", extra_links=None, days=14, log=None):
    """X（推特）：读官方账号时间线 + 用户在群里/反馈里贴出的具体帖子。

    身份分级（报告里必须按级别标注，不能用 KOL 的话冒充官方表态）：
      official  = 账号名/简介里出现该子网名，或该号就是官网自己挂的 X 链接
      thirdparty= 账号本身不是项目方，但贴文点名了该子网（只能标「第三方提及」）
    其余一律丢弃，不把同名路人和泛 Bittensor 大 V 当官方。
    """
    extra_links = list(extra_links or [])
    base = (A.ascii_name(meta.get("name")) or "").lower()
    sn = meta.get("sn", 0)
    hi, lo = _x_candidates(meta, site_html=site_html)
    site_handles = {h.lower() for h in _x_handles_from_html(site_html)}
    tried, third, best = [], None, None
    all_posts, official = [], None
    # 官网挂出来的 X 号优先试：命中就不用再看后面的候选（实测每次翻页约 4 秒，
    # 逐个试完 5-6 个候选要 50 秒以上，投研整体会超时）。
    pri = sorted(hi, key=lambda h: 0 if h.lower() in site_handles else 1)
    for h in pri + [x for x in lo if x]:
        trusted = h not in lo
        prof, posts = x_user_posts(h, pages=2, log=log)
        if not prof:
            tried.append("%s:不可达" % h)
            continue
        if not posts:
            tried.append("%s:无公开贴文" % h)
            continue
        own = [p for p in posts if p["author"].lower() == h.lower() and not p["is_rt"]]
        blob = " ".join([prof.get("bio") or ""] + [p["text"] for p in own]).lower()
        topic_hits = len(X_TOPIC.findall(blob))
        name_in_id = bool(base) and (base in h.lower() or base in (prof.get("bio") or "").lower())
        name_in_posts = bool(base) and base in " ".join(p["text"] for p in own).lower()
        is_site = h.lower() in site_handles
        rec = {"src": "X", "ok": True, "handle": prof.get("handle") or h,
               "bio": prof.get("bio") or "", "nposts": str(len(posts)),
               "posts": [p["text"] for p in own[:10]], "items": posts,
               "links": [p["url"] for p in own[:10]],
               "topic_hits": topic_hits, "via": "fxtwitter"}
        if trusted and (is_site or name_in_id) and (topic_hits >= 1 or is_site):
            rec["level"] = "official"
            rec["note"] = ("官网挂出的 X 账号" if is_site
                           else "账号名/简介含子网名，认定为项目方账号")
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

    # 用户直接给的帖子链接：优先级最高，哪怕不是官方号也一定读出来（这是用户明确要看的）
    linked = []
    for u in extra_links[:6]:
        p = x_tweet(u, log=log)
        if p:
            p["from"] = "用户提供链接"
            linked.append(p)

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


def collect(meta, sn, log=print, extra_x_links=None, days=14):
    """并发度不做，串行抓取（源少、且要统一走代理，串行更稳）。

    X 放在官网之后：官网页面里挂的 X 账号是最可靠的账号来源，
    所以先把官网 HTML 拿到手，再把其中的 X 链接喂给 src_x 定位。"""
    jobs = [
        ("bittensor.ai", lambda: src_bittensor_ai(sn)),
        ("taostats", lambda: src_taostats(sn)),
        ("dtaoscan", lambda: src_dtaoscan(sn)),
        # 收入/回购单独抓一次 bittensor.ai：数据藏在 RSC payload 里，
        # 和上面的常规指标解析路径不同（不能合并，否则一边失败会拖累另一边）
        ("收入/回购", lambda: src_income(sn)),
        ("官网", lambda: src_website(meta)),
        ("GitHub", lambda: src_github(meta)),
        ("Google News", lambda: src_news(meta, sn)),
        ("DuckDuckGo", lambda: src_web_search(meta, sn)),
    ]
    res = {}
    for name, fn in jobs:
        try:
            r = fn()
        except Exception as e:
            r = {"src": name, "ok": False, "err": str(e)[:160]}
        res[name] = r
        log("  投研抓取 %-12s %s%s" % (name, "OK" if r.get("ok") else "失败",
                                     "" if r.get("ok") else "（%s）" % r.get("err", "")[:80]))
    # 官网原文（HTML）用于定位官方 X 账号
    site_html = (res.get("官网") or {}).get("html") or ""
    try:
        r = src_x(meta, site_html=site_html, extra_links=extra_x_links,
                  days=days, log=log)
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
    "1. 事实基线里对结论没影响的指标（Tempo、Epoch、Network share 这类）；\n"
    "2. 交叉验证矩阵里「无法证实」且与主线无关的条目；\n"
    "3. 时间线里纯社区闲聊、没有事件价值的那几条；\n"
    "4. 跟踪清单压到 3-5 条，每条只留「盯什么 + 判断阈值」；\n"
    "5. 各处重复表述（同一件事在结论和时间线各说一遍的，只留一处）。\n"
    "\n【必须原样保留】\n"
    "- 全部小节标题与顺序；\n"
    "- 所有数字、单位、来源标记 `[群聊 MM-DD]` `[taostats]` `[dtaoscan]` `[bittensor.ai]` `[官网]` `[GitHub]` `[新闻]` `[X]` `[搜索]`；\n"
    "- 人名、子网名、SN 编号；\n"
    "- 「⚠️ 数据冲突」「无法证实」这类判定词，以及风险与未证实里的每一条。\n"
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
    "报告只回答五个问题：**什么时候发生了什么（时间线）· 现在什么价（当前数据）· "
    "外面刚说了什么（最新资讯）· 收入多少、有没有回购（收入与回购）· 接下来盯什么（预期跟踪）**。"
    "除此之外的分析、感慨、免责声明、方法论说明，一律不要。\n"
    "\n【最新资讯是必答题，不是选答（本条优先级极高）】\n"
    "群聊只是「社区在聊什么」，**不等于外部世界发生了什么**。用户明确要求："
    "不能出现「明明有新资讯，报告里却一条都没有」。所以：\n"
    "1. 输入里只要 [X] / [X 生态圈提及] / [新闻] / [官网] / [GitHub] 段里**有任何一条**"
    "近 %(days)d 天内的新内容，就必须出现在「📰 最新资讯」段里，一条都不许漏；\n"
    "2. 特别是 [X] 段里标了「（用户提供）」的贴文——那是用户自己贴过来要你看的，"
    "**必须逐条写进去**，写清发布者、日期、原话要点；\n"
    "3. 只有某源**真的没抓到**（输入里明确写着「未获取」）时，才可以在该段写「X：未获取」；"
    "不许因为「内容太琐碎」「拿不准」就整段略过；\n"
    "4. 官方号时间线里看似日常的运营贴，只要含**新事实**（新合作、新上线、新功能、新数据、"
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
    "\n【输出结构（只有这五段，严格按顺序，不要加别的段）】\n"
    "\n**🗓 重要事件时间线**（报告主体，占 50%% 以上篇幅）\n"
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
    "- **条数**：按事情多少写，**最多 12 条**，重要的排前面；确实没有大事就写 1-3 条并说明「本期无重大事件」。\n"
    "- **只写真实发生的事**：不要写「与上次相比无变化」「延续上次判断」这种元评估条目——"
    "没有新事就少写一条，不要在时间线里塞占位行。\n"
    "- **GitHub 的逐条 commit 不算事件**——数据站「N commits/30d」与 GitHub「最近提交日期」"
    "是同一事实的两种表述，不得判为冲突；开发节奏最多合并成一条。\n"
    "- **时间线不止群聊**：官方 X 帖、生态账号提及、新闻、官网更新、GitHub release 都要按真实日期进时间线，"
    "不要只写群里说过什么。\n"
    "\n**📊 当前数据**（只保留**当前价格**，一行即可；不要市值/池子/emission/tempo/epoch 等任何其它指标。"
    "价格必须照抄抓取原文，不许换算；抓不到就写「当前价格：未获取」。格式：`当前价格：0.002504 τ [bittensor.ai]`）\n"
    "\n**📰 最新资讯**（外部世界最近说了什么；2-6 条，每条一行，格式 `- 月-日 · @账号/媒体 · 要点 [X]`）\n"
    "- 这一段的唯一职责是**补群聊看不到的信息**：官方发帖、合作、上线、媒体报道、生态账号点名、"
    "GitHub 发版。已经写进时间线的同一事件，这里只补一句「详见时间线」，或换个角度写影响；不要整段复述。\n"
    "- **所有源都没抓到**才写「本期无外部资讯（X/新闻/官网 均未获取）」，并如实写清哪个源失败；"
    "**不许**在明明抓到内容的情况下留空或写「无」。\n"
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
    "- 抓不到收入数据（输入里没有该段）就整段写「收入与回购：未获取（数据源未返回）」，不要留空、不要编数字。\n"
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
    "\n【篇幅】**只有上限、没有下限**：全文不超过 **1600 字**。"
    "**简短是硬要求，不是建议**：能一句话说清就别写两句，"
    "本期只有 3 件事，就写 3 条、四百字收工，不要为了显得详尽而堆形容词、堆免责声明、堆重复表述。\n"
    "超过 1600 字会被代码层强制压缩（只删不增），可能把你想保留的事件删掉。\n"
    "\n【语言】中文；英文专有名词、代币名、代码库名保留原文。不用 # 标题、不用 > 引用，手机端可读。"
)              # 注意：这里不能就地 % 格式化，模板里含 %% 字面量，交给 research_prompt 统一处理


def research_prompt(days=14):
    """投研系统提示词（天数可配，默认 14 天窗口）。"""
    return _RESEARCH_PROMPT_TMPL % {"days": int(days or 14)}


RESEARCH_SYSTEM_PROMPT = research_prompt(14)

RESEARCH_VERIFY_PROMPT = (
    "你是投研报告的合规校对员，不是作者。输入是【原始素材】+【报告初稿】。\n"
    "逐条核对，只做删改不做新增，输出修正后的完整报告：\n"
    "1. 每个数字、名称、日期必须能在【原始素材】里找到一模一样的字符串；找不到的删掉或改「未获取」；\n"
    "2. 检查来源标记：无标记的断言，补 `[群聊]` 或改为「判断：」；标错来源的改正；\n"
    "3. 群聊传闻被写成事实的，降级为「群内有人称（日期，人物）：…」；\n"
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
        ls = ["releases：" + ("；".join("%s（%s）" % (x["at"], x["title"][:70])
                                       for x in g.get("releases", [])[:5]) or "无")]
        ls.append("recent commits：" + ("；".join("%s（%s）" % (x["at"], x["title"][:60])
                                                  for x in g.get("commits", [])[:5]) or "无"))
        # commits 只是「技术是否在动」的证据，不是子网大事件。
        # 不点明的话，模型会把 10 条 commit 逐条搬进「重要事件时间线」，把真正的事件挤掉。
        blocks.append("[GitHub] %s\n  %s\n  （commits 仅用于判断开发是否活跃，"
                      "**不得逐条列入事件时间线**；最多合并成一句「最近提交集中在 X~Y」）"
                      % (g.get("repo", ""), "\n  ".join(ls)))
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


def build_input(arc, sources, meta, days=14, cap_per_day=260, prev_arc=None, today=None, arc_dir=None):
    """拼装投研输入。arc 为 None 时跳过群聊段（纯全网投研）。

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
    body = ("\n===== 一、链上/数据站事实（抓取原文）=====\n" + _ext_block(sources, meta)
            + "\n\n===== 二、交叉验证矩阵（同指标多源并列）=====\n" + _consensus_block(build_consensus(sources)))
    if n:
        body = ("\n===== 一、本地群聊档案（Discord #price-talk，按日分段、按时间升序）=====\n" + arc_txt
                + "\n\n===== 二、链上/数据站事实（抓取原文）=====\n" + _ext_block(sources, meta)
                + "\n\n===== 三、交叉验证矩阵（同指标多源并列）=====\n" + _consensus_block(build_consensus(sources)))
    if inc_txt:
        body += "\n\n===== 收入与回购（链上口径，报告必须单独成段）=====\n" + inc_txt
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
    # 这是「明明知道有新资讯却什么都没提供」的最直接来源。
    extra_links = x_links_in(feedback)
    # 群聊档案里出现过的 X 链接同样是「社区看到的最新资讯」，一并读（近期的优先）
    if arc:
        _chat_txt = "\n".join(e.get("text") or ""
                              for ents in (arc.get("days") or {}).values() for e in ents)
        for u in x_links_in(_chat_txt):
            if u not in extra_links:
                extra_links.append(u)
    extra_links = extra_links[:8]
    if extra_links:
        log("   待读取的 X 帖链接：%d 条（来自反馈/群聊）" % len(extra_links))
    log("② 全网抓取增补最新资讯（链上数据站 / 官网 / GitHub / 新闻 / 搜索 / X）…")
    sources = collect(meta, sn, log=log, extra_x_links=extra_links, days=days)
    # 知识库缺名称/官网时，用数据站页面反查补齐，再补抓依赖这些字段的源（官网/GitHub/搜索/X）
    before = (meta.get("name"), meta.get("url"), meta.get("github"))
    meta = enrich_meta(meta, sources, log=log)
    if (meta.get("name"), meta.get("url"), meta.get("github")) != before:
        for name, fn in (("官网", lambda: src_website(meta)),
                         ("GitHub", lambda: src_github(meta)),
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
                          extra_links=extra_links, days=days, log=log)
            except Exception as e:
                r = {"src": "X", "ok": False, "err": str(e)[:120], "recent": []}
            sources["X"] = r
            log("  投研补抓 %-12s %s" % ("X", "OK" if r.get("ok") else "失败"))
    text_block = build_input(arc, sources, meta, days=days,
                             cap_per_day=int(cfg.get("research_max_items_per_day", 260) or 260),
                             prev_arc=prev_arc, today=today, arc_dir=arc_dir)
    if feedback:
        # 用户反馈单独成段，且明确标「权威修正」：模型很吃这一套，
        # 只把反馈混在正文里它常常当成又一条群聊传闻，改不动已经写下的判断。
        _xr = (sources.get("X") or {}).get("linked") or []
        _xnote = ""
        if extra_links and _xr:
            _xnote = ("\n【重要】反馈里的 X 链接已真实抓取到原文，正文在上面的 [X] 段里（标注了"
                      "「用户提供」）。**必须**把这些内容写进时间线并注明日期与发布者；"
                      "**禁止**再写「帖子内容未获取/待确认」这类话——原文就在输入里。\n")
        elif extra_links and not _xr:
            _xnote = ("\n【说明】反馈里的 X 链接本次抓取失败（可能已被删除或设为私密），"
                      "如需提及只能写「链接不可访问」，不得编造其内容。\n")
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

    digest = llm(text_block, research_prompt(days))
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
    # 字数闸门：只限上限 1600 字（第四段「最新资讯」要留出空间），不设下限。
    # rounds=3：模型每轮只删一部分，两轮常常压不到位（实测 5061→3465），三轮才收得住。
    try:
        digest = pipeline.compress_digest(cfg, digest, 0, 1600, rounds=3,
                                          system_prompt=RESEARCH_COMPRESS_PROMPT,
                                          sanitize_fn=lambda s: pipeline.strip_length_tags(s),
                                          cut_hint="报告只有五段（时间线 / 当前数据 / 最新资讯 / 收入与回购 / 预期跟踪）。"
                                                   "压缩顺序：① 先删时间线里最不重要的整条事件（合并同类、去掉铺垫词）；"
                                                   "② 每条事件压到一行、去掉形容词与重复解释，**但日期、事件核心、"
                                                   "✅已验证/⚠️未验证标记必须留着**；"
                                                   "③ 预期跟踪压到 4 条；"
                                                   "④ 「当前价格」「最新资讯」「💰 收入与回购」三段**永远不许删**"
                                                   "（收入段是用户点名要求的）；收入段最多把相近项合并成一行，"
                                                   "但收入额、回购金额、变化百分比一个都不许丢；"
                                                   "⑤ 删掉时间线与最新资讯之间完全重复的表述，只留一处。"
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
        if not (v.get("items") or v.get("recent") or v.get("facts") or v.get("posts")):
            return "%s✓（本期无内容）" % k
        return "%s✓" % k

    with open(path, "w", encoding="utf-8") as f:
        f.write("# %s\n\n> 生成于 %s ｜ 数据源：%s\n> 抓取状态：%s\n\n%s\n" % (
            title, datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
            ("本地档案 + " if arc else "（无本地档案）纯全网 ") + "、".join(ok_srcs),
            "；".join(_src_state(k, v) for k, v in sources.items()),
            digest))
    log("③ 报告已生成：%s（%d 字）" % (path, len(re.sub(r"\s", "", digest))))
    # 投研产出增量写回子网档案：报告正文 + 结论 + 时间线 + 数据源一起入库；
    # 已有档案只替换本日记录（幂等），没有档案就新建一个 —— 这是下次投研的输入来源。
    try:
        _arc = A.attach_research(cfg, sn, today, title, digest, sources=ok_srcs,
                                 meta={"name": meta.get("name"), "desc": meta.get("desc"),
                                       "url": meta.get("url"), "github": meta.get("github")},
                                 file_name=fname, log=log,
                                 income_snap=(_LAST_INC_SNAP or None))
        log("④ 档案增量更新完成：SN%d 现有投研 %d 份、群聊 %d 条 —— 下次投研会先读它"
            % (sn, len(_arc.get("reports") or []),
               sum(len(v) for v in (_arc.get("days") or {}).values())))
    except Exception as e:
        log("④ 投研落档失败（报告文件已留存，档案未更新）：%s" % e)
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
