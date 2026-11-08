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
RESEARCH_SYSTEM_PROMPT = (
    "你是 Bittensor 子网的买方研究员。输入会分成几块：【本地群聊档案】（Discord 真实讨论，带日期时间，"
    "**可能没有**）、【此前投研结论】（该子网档案里存的历史报告，只有做过投研才会有）、"
    "【链上/数据站事实】（抓取到的页面字段）、【官方与公开信息】（官网、GitHub、新闻、搜索、X）、"
    "【交叉验证矩阵】（同指标多源并列及其一致性判定）。"
    "另外会额外给出一段【代码预判的硬信号】——那是程序根据链上字段判定的客观状态"
    "（owner 解仓、神经元满员、注册关闭等），**必须写入报告**，不得忽略或弱化。\n"
    "\n【工作顺序（必须遵守）】\n"
    "先读【本地群聊档案】与【此前投研结论】，把「档案里已经确认过的事实」当成**起点**；"
    "再读外部源，专门找**上次报告之后的新增变化**（新公告、新解仓、新数据、群里新出现的说法），"
    "把这些变化写进时间线。**已经在上次报告里写过、这次没有新进展的事，不要重复罗列**。\n"
    "报告只回答三个问题：**什么时候发生了什么（时间线）· 现在什么价（当前价格）· 接下来盯什么（预期跟踪）**。"
    "除此之外的分析、感慨、免责声明、方法论说明，一律不要。\n"
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
    "\n【输出结构（只有这三段，严格按顺序，不要加别的段）】\n"
    "\n**🗓 重要事件时间线**（这是报告主体，占 60% 以上篇幅）\n"
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
    "\n**📊 当前数据**（只保留**当前价格**，一行即可；不要市值/池子/emission/tempo/epoch 等任何其它指标。"
    "价格必须照抄抓取原文，不许换算；抓不到就写「当前价格：未获取」。格式：`当前价格：0.002504 τ [bittensor.ai]`）\n"
    "\n**📌 预期跟踪**（接下来该盯什么，4-6 条，每条必须可验证）\n"
    "格式：`- 盯什么 → 什么情况算兑现 / 什么情况算证伪`。"
    "要具体到能拿数据核对（例如「owner 是否在 30 天内继续解仓 → 解仓比例再升 5% 即证伪回购承诺」），"
    "不要写「关注官方动态」这类空话。\n"
    "\n【禁止事项】\n"
    "不要写「一句话结论」段、不要写「事实基线」（只留当前价格那一行）、不要写「交叉验证矩阵」段、"
    "不要写「群内预期差」段、不要写「风险与未证实」段——"
    "**验证状态已经跟在每条事件后面了，不需要再单列一节复述**。"
    "也不要写开场白、不要写总结、不要写「综上所述」。\n"
    "\n【篇幅】全文 **600-1600 字**。**简短是硬要求，不是建议**：能一句话说清就别写两句，"
    "不许为了显得详尽而堆形容词、堆免责声明、堆重复表述。宁可 600 字全是干货。\n"
    "超过 1600 字会被代码层强制压缩（只删不增），可能把你想保留的事件删掉。\n"
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
    body = ("\n===== 一、链上/数据站事实（抓取原文）=====\n" + _ext_block(sources, meta)
            + "\n\n===== 二、交叉验证矩阵（同指标多源并列）=====\n" + _consensus_block(build_consensus(sources)))
    if n:
        body = ("\n===== 一、本地群聊档案（Discord #price-talk，按日分段、按时间升序）=====\n" + arc_txt
                + "\n\n===== 二、链上/数据站事实（抓取原文）=====\n" + _ext_block(sources, meta)
                + "\n\n===== 三、交叉验证矩阵（同指标多源并列）=====\n" + _consensus_block(build_consensus(sources)))
    if hist:
        body += "\n\n===== 此前投研结论（历史留档）=====\n" + hist
    if sig:
        body += "\n\n===== 末、" + sig
    elif not n:
        # 没有群聊素材时，交叉验证只能发生在「外部源之间」，必须写明，避免模型编造群内说法
        body += ("\n\n【注意】本次**没有本地群聊档案**：不得出现「群内有人说」「群里传」之类表述，"
                 "也不得虚构社区情绪；交叉验证只在外部源（数据站/官网/GitHub/新闻/搜索/X）之间进行。")
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
    log("② 全网抓取增补最新资讯（链上数据站 / 官网 / GitHub / 新闻 / 搜索 / X）…")
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
                             cap_per_day=int(cfg.get("research_max_items_per_day", 260) or 260),
                             prev_arc=prev_arc, today=today, arc_dir=arc_dir)
    if feedback:
        # 用户反馈单独成段，且明确标「权威修正」：模型很吃这一套，
        # 只把反馈混在正文里它常常当成又一条群聊传闻，改不动已经写下的判断。
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
            "4. 重新核对整篇报告里与反馈冲突的表述，逐条改掉；不要只改标题。" % feedback)
    _nhist = len((prev_arc or {}).get("reports") or [])
    log("投研素材：%d 字符（%s + 外部 %d 源，成功 %d 源%s）"
        % (len(text_block), "群聊" if arc else "无群聊", len(sources),
           sum(1 for v in sources.values() if v.get("ok")),
           ("；含 %d 份历史投研做对比" % _nhist) if _nhist else ""))
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
    # 字数闸门：600-1600 字。用户要求「太多废话了，我只要时间、重要事件、预期跟踪」，
    # 所以这里是真的压短，上限就是手机端一屏半的阅读量。
    # rounds=3：模型每轮只删一部分，两轮常常压不到位（实测 5061→3465），三轮才收得住。
    try:
        digest = pipeline.compress_digest(cfg, digest, 500, 1600, rounds=3,
                                          system_prompt=RESEARCH_COMPRESS_PROMPT,
                                          sanitize_fn=lambda s: s,
                                          cut_hint="报告只有三段（时间线 / 当前数据 / 预期跟踪）。"
                                                   "压缩顺序：① 先删时间线里最不重要的整条事件（合并同类、去掉铺垫词）；"
                                                   "② 每条事件压到一行、去掉形容词与重复解释，**但日期、事件核心、"
                                                   "✅已验证/⚠️未验证标记必须留着**；"
                                                   "③ 预期跟踪压到 4 条；"
                                                   "④ 「当前价格」那一行永远不许删。"
                                                   "全部数字、来源标记、人名、SN 编号必须原样保留。")
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
    with open(path, "w", encoding="utf-8") as f:
        f.write("# %s\n\n> 生成于 %s ｜ 数据源：%s\n> 抓取状态：%s\n\n%s\n" % (
            title, datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
            ("本地档案 + " if arc else "（无本地档案）纯全网 ") + "、".join(ok_srcs),
            "；".join("%s%s" % (k, "✓" if v.get("ok") else "✗") for k, v in sources.items()),
            digest))
    log("③ 报告已生成：%s（%d 字）" % (path, len(re.sub(r"\s", "", digest))))
    # 投研产出增量写回子网档案：报告正文 + 结论 + 时间线 + 数据源一起入库；
    # 已有档案只替换本日记录（幂等），没有档案就新建一个 —— 这是下次投研的输入来源。
    try:
        _arc = A.attach_research(cfg, sn, today, title, digest, sources=ok_srcs,
                                 meta={"name": meta.get("name"), "desc": meta.get("desc"),
                                       "url": meta.get("url"), "github": meta.get("github")},
                                 file_name=fname, log=log)
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
