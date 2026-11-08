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
    }


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
            "desc": desc, "excerpt": body}


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
            items.append({"title": title, "at": (pd.group(1) if pd else "")[:16],
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


def src_web_search(meta, sn, limit=8):
    """搜索引擎：官网/文档/第三方分析/AMA 等公开页面（含摘要）。"""
    names = [n for n in {meta.get("name"), A.ascii_name(meta.get("name"))} if n and len(n) >= 3]
    qs = []
    for n in names[:1]:
        qs.append('"%s" bittensor subnet %d' % (n, sn))
    qs.append('bittensor "subnet %d"' % sn)
    out, seen = [], set()
    for q in qs[:2]:
        r = _ddg(q, limit=limit)
        for it in r.get("items", []):
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            out.append(it)
        if len(out) >= limit:
            break
    return {"src": "DuckDuckGo", "ok": bool(out), "items": out[:limit],
            "err": "" if out else "检索被拦截或无结果"}


def _x_candidates(meta):
    """猜官方 X 账号：官网直链 > 搜索引擎命中 site:x.com > 子网名变体。
    注意 GitHub owner 往往是开发者个人号，只能作为低优先候选（且必须过相关性校验）。"""
    cands, low = [], []
    u = meta.get("url") or ""
    m = re.search(r"(?:twitter|x)\.com/([A-Za-z0-9_]{2,20})", u)
    if m:
        cands.append(m.group(1))
    base = A.ascii_name(meta.get("name"))
    q = 'site:x.com "%s" bittensor' % (base or "subnet %d" % meta.get("sn", 0))
    r = _ddg(q, limit=8)
    for it in r.get("items", []):
        mm = re.search(r"x\.com/([A-Za-z0-9_]{2,20})", it["url"])
        if mm:
            h = mm.group(1)
            if h.lower() not in ("i", "intent", "share", "home", "search", "hashtag", "status"):
                cands.append(h)
    if base and re.fullmatch(r"[A-Za-z0-9_]{3,20}", base):
        cands += [base, base + "_ai", base + "ai"]
    gh = meta.get("github") or ""
    mg = re.search(r"github\.com/([A-Za-z0-9_.\-]+)/", gh)
    if mg and mg.group(1).lower() not in ("bittensor", "opentensor", "orgs"):
        low.append(mg.group(1))
    return list(dict.fromkeys(cands))[:5], list(dict.fromkeys(low))[:2]


# 判断一个 X 号是否真的与本子网相关（避免把开发者个人碎碎念当官方表态）
X_TOPIC = re.compile(
    r"(?i)(bittensor|subnet|netuid|\btao\b|dtao|validator|miner|\bSN\s?\d|alpha|emission|"
    r"inference|mining|neuron)")


def _x_extract(t):
    """从 jina 还原的 X 页面里抽账号简介 + 贴文。"""
    head = t.split("Markdown Content", 1)[1] if "Markdown Content" in t else t
    bio = ""
    mb = re.search(r"@[A-Za-z0-9_]{2,20}\s*\n+\s*([^\n\[]{10,220})", head)
    if mb:
        bio = mb.group(1).strip()
    nposts = re.search(r"([0-9,]+)\s+posts", head)
    posts = []
    for line in head.splitlines():
        s = line.strip()
        if not s.startswith("*"):
            continue
        s = re.sub(r"\[!\[Image[^\]]*\]\([^)]*\)\]\([^)]*\)", " ", s)
        s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
        s = re.sub(r"\[!\[[^\]]*\]\]\([^)]*\)", " ", s)          # 残留的空图链
        s = re.sub(r"\[\]\([^)]*\)", " ", s)                     # 空链接
        s = re.sub(r"\[([^\]]{2,160})\]\((https?://[^)]+)\)", r"\1", s)
        s = re.sub(r"https?://\S+", " ", s)
        s = re.sub(r"^[\*\s\-–—•\[\]]+", "", s).strip()
        s = re.sub(r"\s+", " ", s)
        if len(s) < 30 or s.lower().startswith("pinned"):
            continue
        posts.append(s[:400])
        if len(posts) >= 8:
            break
    return bio, (nposts.group(1) if nposts else ""), posts


def src_x(meta):
    """X（推特）：定位官方账号 → 经 r.jina.ai 镜像读时间线 → **身份校验**。

    校验分三级，报告里必须按级别标注，不能用 KOL 的话冒充官方表态：
      official  = 账号名或简介里出现该子网名（基本可认定为项目方账号）
      thirdparty= 账号本身不是项目方，但贴文里点名了该子网（只能标「第三方提及」）
      其余       = 丢弃（同名路人和泛 Bittensor 大 V 一律不要）
    """
    hi, lo = _x_candidates(meta)
    if not hi and not lo:
        return {"src": "X", "ok": False, "err": "未找到官方 X 账号线索", "posts": []}
    base = (A.ascii_name(meta.get("name")) or "").lower()
    tried, third = [], None
    for h, trusted in [(x, True) for x in hi] + [(x, False) for x in lo]:
        ok, t = fetch("https://r.jina.ai/https://x.com/%s" % h, timeout=40)
        if not ok or "Markdown Content" not in t:
            tried.append("%s:不可达" % h)
            continue
        if "This account doesn't exist" in t or "Profile Not Found" in t:
            tried.append("%s:不存在" % h)
            continue
        bio, nposts, posts = _x_extract(t)
        if not posts:
            tried.append("%s:无公开贴文" % h)
            continue
        blob = " ".join([bio] + posts).lower()
        if any(k in blob for k in X_TOPIC.findall(blob)) and not X_TOPIC.search(blob):
            tried.append("%s:主题无关" % h)
            continue
        topic_hits = len(X_TOPIC.findall(blob))
        name_in_id = bool(base) and (base in h.lower() or base in (bio or "").lower())
        name_in_posts = bool(base) and base in " ".join(posts).lower()
        rec = {"src": "X", "ok": True, "handle": h, "bio": bio, "nposts": nposts,
               "posts": posts, "topic_hits": topic_hits}
        if name_in_id:
            rec["level"] = "official"
            rec["note"] = "账号名/简介含子网名，认定为项目方账号"
            return rec
        if name_in_posts and topic_hits >= 1:
            rec["level"] = "thirdparty"
            rec["note"] = "非项目方账号，但贴文点名本子网 —— 只可作第三方提及"
            third = third or rec
            tried.append("%s:第三方提及" % h)
            continue
        tried.append("%s:与本子网无关（命中 %d）" % (h, topic_hits))
    if third:
        return third
    return {"src": "X", "ok": False, "err": "；".join(tried[-3:]) or "账号不可达", "posts": []}


def collect(meta, sn, log=print):
    """并发度不做，串行抓取（源少、且要统一走代理，串行更稳）。"""
    jobs = [
        ("bittensor.ai", lambda: src_bittensor_ai(sn)),
        ("taostats", lambda: src_taostats(sn)),
        ("dtaoscan", lambda: src_dtaoscan(sn)),
        ("官网", lambda: src_website(meta)),
        ("GitHub", lambda: src_github(meta)),
        ("Google News", lambda: src_news(meta, sn)),
        ("DuckDuckGo", lambda: src_web_search(meta, sn)),
        ("X", lambda: src_x(meta)),
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
RESEARCH_SYSTEM_PROMPT = (
    "你是 Bittensor 子网的买方研究员。输入会分成几块：【本地群聊档案】（Discord 真实讨论，带日期时间，"
    "**可能没有**）、【链上/数据站事实】（抓取到的页面字段）、【官方与公开信息】（官网、GitHub、新闻、搜索、X）、"
    "【交叉验证矩阵】（同指标多源并列及其一致性判定）。"
    "另外会额外给出一段【代码预判的硬信号】——那是程序根据链上字段判定的客观状态"
    "（owner 解仓、神经元满员、注册关闭等），**必须写入报告**，不得忽略或弱化。\n"
    "你要产出一份**子网近期重要事件跟踪报告**。有群聊档案时，核心价值是「把群里的说法和外部证据对齐」；"
    "**没有群聊档案时，核心价值是「把多方外部源对齐，找出互相矛盾或只有单一来源的地方」**。\n"
    "\n【事实纪律（违反即作废）】\n"
    "1. 每条事实后面必须带来源标记：`[群聊 MM-DD]`、`[taostats]`、`[bittensor.ai]`、`[dtaoscan]`、"
    "`[官网]`、`[GitHub]`、`[新闻]`、`[X]`、`[搜索]`；没标记的句子只能是分析判断，且必须以「判断：」开头。\n"
    "2. 数字必须来自输入原文，**禁止换算、禁止四舍五入改写、禁止补全缺失位**；抓不到就写「未获取」。\n"
    "3. 群聊里的传闻/猜测/玩笑**不得**写成事实：写成「群内有人称（MM-DD，谁）：…」。"
    "玩笑、反讽、假设句（if/imagine/假如）不算证据。\n"
    "4. 共现≠因果。某公司/人物与某子网同日出现，不得写成「某公司支持该子网」。\n"
    "5. 人名、子网名、代币名、SN 编号一律照抄，不音译、不改写大小写。\n"
    "6. 多源冲突时**并列呈现并说明冲突点**，不要替读者选一个；无法确认就写「数据存在冲突，需人工核实」。\n"
    "7. 输入里没有的公告、合作、融资、上所，一律不许出现。宁可报告短，不许编。\n"
    "8. **没有本地档案时**：绝对不许出现「群内有人说」「社区在传」「大家认为」这类表述，"
    "也不得虚构社区情绪与预期差；相应小节改为「外部信息对照」与「市场信号解读」，只依据外部源。\n"
    "\n【输出结构（小节标题加粗，严格按顺序）】\n"
    "**🎯 一句话结论**（这段时间该子网最重要的一件事 + 证据强度：强/中/弱；3 句以内）\n"
    "**📊 事实基线**（列表：指标 → 值 → 来源。**最多 12 条**，只挑与结论相关的；"
    "Tempo / Epoch / Network share 这类无关指标不要列；缺失写「未获取」）\n"
    "**🗓 重要事件时间线**（按日期升序，**最多 7 条**；每条：日期 · 事件 · 来源；"
    "区分「官方动作」与「社区传闻」；纯闲聊不进时间线。"
    "**GitHub 的逐条 commit 不算事件**——数据站给出的「N commits/30d」与 GitHub 的「最近提交日期」"
    "是同一个事实的两种表述，**不得判为冲突**；开发节奏最多合并成一条概述）\n"
    "**🔀 交叉验证矩阵**（逐条对齐，**最多 5 条**：群内说法/单一来源说法 → 外部证据 → 判定："
    "证实/部分证实/无法证实/被否定）\n"
    "**💬 群内预期差**（有档案时：群里在想什么、和链上/官方事实差在哪，**2-3 条**，每条 60 字内；"
    "无档案时改为「**市场信号解读**」：价位/流动性/emission/开发节奏说明什么，仍须带来源标记）\n"
    "**⚠️ 风险与未证实**（单一来源、冲突数据、无法核实的指控，"
    "逐条列出并写明缺什么证据，**最多 5 条**）\n"
    "**📌 跟踪清单**（接下来该盯的具体信号，**3-4 条**，可验证、带判断阈值）\n"
    "\n【篇幅（硬性）】全文 **1300-1800 字**（不含标点也按字数算）。"
    "这是手机端阅读的上限：宁可有 5 条扎实内容，不要 20 条流水账。"
    "超字数会被截断重写，等于白写。\n"
    "\n【语言】中文；英文专有名词、代币名、代码库名保留原文。不用 # 标题、不用 > 引用，手机端可读。"
)

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
    if x.get("posts"):
        lvl = ("项目方账号（可作官方表态）" if x.get("level") == "official"
               else "第三方账号（**只能作第三方提及，不得写成官方公告**）")
        blocks.append("[X @%s] 身份：%s；粉丝贴文 %s 条；简介：%s\n  "
                      % (x.get("handle", ""), lvl, x.get("nposts", "?"), (x.get("bio") or "")[:140])
                      + "\n  ".join("- " + p[:240] for p in x["posts"][:6]))
    else:
        blocks.append("[X] 未获取（%s）——不得据此推断官方态度" % x.get("err", ""))
    return "\n".join(blocks)


def _consensus_block(rows):
    if not rows:
        return "（本次未从任何数据站抓到可比指标）"
    lines = []
    for r in rows:
        vals = "；".join("%s=%s" % (s, v) for s, v in r["vals"])
        lines.append("- %s：%s → %s" % (r["label"], vals, r["status"]))
    return "\n".join(lines)


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


def build_input(arc, sources, meta, days=14, cap_per_day=260):
    """拼装投研输入。arc 为 None 时跳过群聊段（纯全网投研）。"""
    arc_txt, n, keys = ("", 0, []) if not arc else _arc_block(arc, days=days, cap_per_day=cap_per_day)
    head = (
        "【目标子网】SN%d %s\n【知识库业务描述】%s\n【官网】%s\n【仓库】%s\n"
        "【今日】%s\n"
        "【本地档案】%s\n"
        % (meta["sn"], meta["name"] or "未收录", meta["desc"] or "未收录",
           meta["url"] or "无", meta["github"] or "无",
           datetime.datetime.now(TZ).strftime("%Y-%m-%d"),
           ("%s ~ %s（%d 个交易日，%d 条命中消息）" % (keys[0], keys[-1], len(keys), n))
           if keys else "**本次无本地档案**（不做群聊对照，全部结论只依赖外部源）"))
    sig = _signal_block(sources)
    body = ("\n===== 一、链上/数据站事实（抓取原文）=====\n" + _ext_block(sources, meta)
            + "\n\n===== 二、交叉验证矩阵（同指标多源并列）=====\n" + _consensus_block(build_consensus(sources)))
    if n:
        body = ("\n===== 一、本地群聊档案（Discord #price-talk，按日分段、按时间升序）=====\n" + arc_txt
                + "\n\n===== 二、链上/数据站事实（抓取原文）=====\n" + _ext_block(sources, meta)
                + "\n\n===== 三、交叉验证矩阵（同指标多源并列）=====\n" + _consensus_block(build_consensus(sources)))
    if sig:
        body += "\n\n===== 末、" + sig
    elif not n:
        # 没有群聊素材时，交叉验证只能发生在「外部源之间」，必须写明，避免模型编造群内说法
        body += ("\n\n【注意】本次**没有本地群聊档案**：不得出现「群内有人说」「群里传」之类表述，"
                 "也不得虚构社区情绪；交叉验证只在外部源（数据站/官网/GitHub/新闻/搜索/X）之间进行。")
    return head + body


# ------------------------------------------------------------------ 主流程
def run_research(cfg, sn_arg, targets=None, days=14, log=print, llm=None, push=True):
    """生成并推送子网重要事件跟踪报告。返回 (title, digest, path)。"""
    arc_dir = A.get_arc_dir(cfg)
    sn = A.resolve_sn(sn_arg, cfg)
    if sn is None:
        raise RuntimeError("无法识别子网：%s（可用编号 29 / SN29，或知识库里的子网名）" % sn_arg)
    arc = A.load_archive(arc_dir, sn)
    # 没有本地档案 / 档案里没消息：**直接走全网投研**，不读导出记录、更不触发任何导出。
    # 群聊档案在这里退化为「可选加强项」而不是前置条件 —— 用户要的是随时能出报告。
    if arc and not (arc.get("days")):
        arc = None
    if arc:
        log("档案：%d 条命中消息，覆盖 %d 天（最近：%s）"
            % (sum(len(v) for v in arc["days"].values()), len(arc["days"]),
               "、".join(sorted(arc["days"].keys())[-5:])))
    else:
        log("本地暂无 SN%d 档案：本次**纯全网投研**（不读导出记录、不做导出），"
            "只用外部源交叉验证" % sn)
    meta = _subnet_meta(sn)
    if not meta["name"] and arc:
        meta["name"] = arc.get("name") or ""
    log("===== 子网投研启动：SN%d %s =====" % (sn, meta["name"] or "（名称待反查）"))
    sources = collect(meta, sn, log=log)
    # 知识库缺名称/官网时，用数据站页面反查补齐，再补抓依赖这些字段的源（官网/GitHub/搜索/X）
    before = (meta.get("name"), meta.get("url"), meta.get("github"))
    meta = enrich_meta(meta, sources, log=log)
    if (meta.get("name"), meta.get("url"), meta.get("github")) != before:
        for name, fn in (("官网", lambda: src_website(meta)),
                         ("GitHub", lambda: src_github(meta)),
                         ("DuckDuckGo", lambda: src_web_search(meta, sn)),
                         ("X", lambda: src_x(meta))):
            if sources.get(name, {}).get("ok"):
                continue
            try:
                r = fn()
            except Exception as e:
                r = {"src": name, "ok": False, "err": str(e)[:120]}
            sources[name] = r
            log("  投研补抓 %-12s %s" % (name, "OK" if r.get("ok") else "失败"))
    text_block = build_input(arc, sources, meta, days=days,
                             cap_per_day=int(cfg.get("research_max_items_per_day", 260) or 260))
    log("投研素材：%d 字符（%s + 外部 %d 源，成功 %d 源）"
        % (len(text_block), "群聊" if arc else "无群聊", len(sources),
           sum(1 for v in sources.values() if v.get("ok"))))
    import pipeline
    if llm is None:
        def llm(body, sp=None):
            return pipeline.call_deepseek(cfg, body, system_prompt=sp)

    digest = llm(text_block, RESEARCH_SYSTEM_PROMPT)
    # 二次校对：核对数字/来源标记/传闻降级（失败不阻断，报告仍可用）
    # 注意 sanitize_fn 必须传恒等函数：投研报告要保留英文来源标签与人名，
    # 日报那套 sanitize_digest 会把英文全译成中文，反而破坏来源可追溯性。
    try:
        digest = pipeline.polish_digest(cfg, text_block, digest,
                                        verify_prompt=RESEARCH_VERIFY_PROMPT,
                                        sanitize_fn=lambda s: s)
    except Exception as e:
        log("投研校对跳过（%s）" % e)
    digest = (digest or "").strip()
    # 字数硬闸门：投研报告固定 1300-1900 字。
    # 不定死会出 4900 字的长文，TG 要拆四五条、手机端根本读不完；
    # 压缩器传恒等清洗函数，保证数字与来源标记不被改写。
    try:
        digest = pipeline.compress_digest(cfg, digest, 1300, 1900,
                                          system_prompt=RESEARCH_COMPRESS_PROMPT,
                                          sanitize_fn=lambda s: s,
                                          cut_hint="优先砍「事实基线」里对结论无影响的指标"
                                                   "（Tempo/Epoch/Network share）与重复的 GitHub commit 罗列，"
                                                   "**最后才动群聊观点与交叉验证矩阵**"
                                                   "（那是本报告的核心价值，砍掉就白做了）；"
                                                   "跟踪清单压到 3-5 条；"
                                                   "全部数字、来源标记、人名、SN 编号必须原样保留。")
    except Exception as e:
        log("投研压缩跳过（%s）" % e)
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
    with open(path, "w", encoding="utf-8") as f:
        f.write("# %s\n\n> 生成于 %s ｜ 数据源：%s\n> 抓取状态：%s\n\n%s\n" % (
            title, datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
            ("本地档案 + " if arc else "（无本地档案）纯全网 ") + "、".join(ok_srcs),
            "；".join("%s%s" % (k, "✓" if v.get("ok") else "✗") for k, v in sources.items()),
            digest))
    log("报告已存：%s（%d 字）" % (path, len(re.sub(r"\s", "", digest))))
    # 投研产出增量写回子网档案：已有档案只追加本日报告摘要，没有档案就新建一个
    try:
        A.attach_research(cfg, sn, today, title, digest, sources=ok_srcs,
                          meta={"name": meta.get("name"), "desc": meta.get("desc"),
                                "url": meta.get("url"), "github": meta.get("github")},
                          file_name=fname, log=log)
    except Exception as e:
        log("投研落档失败（忽略）：%s" % e)
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
