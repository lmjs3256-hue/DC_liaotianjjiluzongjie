# -*- coding: utf-8 -*-
"""子网档案（Subnet Archive）

把本地已导出的 Discord 聊天记录，按 Bittensor 子网编号 **单独建档**：
  - 按消息真实时间顺序排列（不是按热度，保住指代与因果链）
  - 保留英文原话 + 中文译文（译文可选、可缓存，绝不改写原话）
  - 额外产出一份「人物观点索引」（谁在什么时候说过什么）

设计原则（与日报同一套红线）：
  1. **只搬运原文，不概括、不推断**：档案里出现的每一句都是导出 json 里的原话，
     归属规则是确定性的（编号/名称命中），不经过任何模型判断；
  2. **两档命中**：`strong` = 显式 SNxx / subnet N / 子网名 / 别名；
     `weak` = 裸编号（如只写「29」），风险高，单独存放、投研默认不用；
  3. **幂等**：以消息 id 去重，重复归档不会重复计数；
  4. 不依赖 pipeline.py，可独立导入（避免循环引用），LLM 通过参数注入。

目录结构：
  subnet_archive/
    index.json                 全局索引（有哪些子网、各多少条）
    _trans_cache.json          译文缓存（msg_id -> 中文）
    SN29/
      archive.json             结构化档案（按日期分组 + 人物索引 + 统计）
      archive.md               人类可读档案（时间线 + 人物观点）
      research_YYYY-MM-DD.md   投研报告（由 subnet_research.py 写入）
"""

import datetime
import hashlib
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = datetime.timezone(datetime.timedelta(hours=8))

# ---------------------------------------------------------------- 命中规则
SN_STRONG = re.compile(r"(?i)\bSN\s?(\d{1,3})\b")
SN_SUBNET = re.compile(r"(?i)\bsubnet\s*#?\s*(\d{1,3})\b")
# 裸编号：排除价格(0.0025)/版本(1.5)/带单位(24h)/百分号；只认 1-256
SN_BARE = re.compile(r"(?<![\w.:/%$#])(\d{1,3})(?![\w.:%])")
TOKEN = re.compile(r"[A-Za-z\u00c0-\u024f][A-Za-z0-9\u00c0-\u024f._\-]{2,}")
# 裸编号只有在这些语境词附近才认（否则 "2 weeks" 会把 SN2 拉进来）
CTX_WORDS = re.compile(
    r"(?i)(alpha|tao|emission|validator|miner|owner|registration|dereg|subnet|"
    r"price|pump|dump|slot|immunity|liquidity|staking|stake|yield|apr|buyback|"
    r"pool|chain|token|tokenomics)")

# 通用词名（Score / Liquidity / Instant…）要更硬的语境才算命中：
# 只有「明确在说某个子网」的词才认，"Liquidity in tao is still thin" 这种泛泛而谈不算。
STRONG_CTX = re.compile(
    r"(?i)(\bSN\s?\d|\bsubnet\b|\bnetuid\b|\bemission|validator|\bminer|"
    r"registration|dereg|\bowner\b|buyback|immunity|\bslot\b|taostats|"
    r"\bpool\b|alpha\s*(?:token|price|pool)|\bapy\b|\bapr\b)")

NOISE = re.compile(
    r"^(\s*(https?://\S+\s*)+|(\:\w+:\s*)+|(\[(?:attachment|embed)\]\s*)+|<@[!&]?\d+>|@\S+)$", re.I)

# 常见希腊/拉丁同形字母折叠，让 hoτfloaτ 能用 hotfloat 命中
_FOLD = {"τ": "t", "ο": "o", "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v",
         "ρ": "p", "υ": "u", "χ": "x", "η": "n", "μ": "m", "σ": "s", "γ": "y",
         "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x"}

# 这些名字太通用，一律不做名称匹配（只认显式 SNxx / subnet N）
NAME_STOP = {"unknown", "deprecated", "none", "null", "root", "claims",
             "bittensor", "subnet", "subnets", "protocol", "network", "agents",
             "gpt", "llm", "apex", "lambda", "targon", "omega", "delta", "sigma",
             "team", "labs", "studio", "dao", "ai", "app", "test", "temp"}

# 既是子网名、又是日常英文单词：直接按名字匹配会大量误报
# （群里说 "extract more liquidity from you" 并不是在谈论 SN77）。
# 处理方式：必须**大小写完全一致**且同句出现子网语境词（alpha/emission/subnet…），
# 否则只归档到「疑似提及」，不进正式档案。
GENERIC_NAMES = {
    "liquidity", "investing", "score", "instant", "synth", "swarm", "carbon",
    "sandbox", "witness", "nova", "zeus", "noah", "ralph", "soma", "dojo",
    "cortex", "beam", "cascade", "enigma", "affine", "cathedral", "gradients",
    "attune", "hone", "instant", "ditto", "minors", "actual", "confidential",
}


def fold(s):
    """把非 ASCII 同形字母折叠成 ASCII。"""
    out = []
    for ch in s or "":
        out.append(_FOLD.get(ch, ch))
    return "".join(out)


def ascii_name(name):
    """子网名去域名后缀 + 折同形字母，得到可用于文本匹配的基名。"""
    s = fold(name or "").strip()
    s = re.sub(r"\.(io|ai|studio|com|net|org|xyz|app|finance|ltd)$", "", s, flags=re.I)
    return s.strip()


# 知识库里的占位值：taostats 抓不到真名时会给 "deprecated"/"Unknown"/"None"，
# 直接当子网名用会污染档案（例：SN39 显示成「deprecated」）。
_PLACEHOLDER_NAMES = {"deprecated", "unknown", "none", "null", "n/a", "na", "-", "tbd", ""}


def real_name(v):
    """取知识库里可信的子网名；占位值返回空串（由调用方显示「（知识库未收录）」）。"""
    nm = (v or {}).get("name") or ""
    if nm.strip().lower() in _PLACEHOLDER_NAMES:
        return ""
    return nm.strip()


def real_desc(v):
    d = ((v or {}).get("zh") or "").strip()
    if d and d != "未知":
        return d
    d = ((v or {}).get("desc") or "").strip()
    if d.lower() in _PLACEHOLDER_NAMES:
        return ""
    return d


def real_url(u):
    """知识库里的链接也要过滤：taostats 抓不到时会写 deprecated/deprecated 这种假地址，
    带着它去投研只会得到 404，还会让模型以为「有仓库但无动态」。"""
    u = (u or "").strip()
    low = u.lower()
    if not u or "deprecated/deprecated" in low or low in ("http://", "https://"):
        return ""
    return u


# ---------------------------------------------------------------- 知识库
def load_kb(path=None):
    path = path or os.path.join(HERE, "subnets.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"subnets": {}}


def name_index(kb):
    """{小写名字/别名: sn} —— 用于把「Engy」「hotfloat」这类称呼反查编号。

    只收「可安全匹配」的名字：长度 ≥4、不是纯数字、不是通用英文词。
    带空格/连字符的复合名（"endure network"）保留，按整串匹配，减少碎片误报。"""
    idx = {}
    for key, v in ((kb or {}).get("subnets") or {}).items():
        try:
            sn = int(key)
        except Exception:
            continue
        cands = []
        nm = ascii_name(v.get("name"))
        if nm:
            cands.append(nm)
        for a in (v.get("aliases") or []):
            aa = ascii_name(a)
            if aa:
                cands.append(aa)
        gh = v.get("github") or ""
        m = re.search(r"github\.com/[^/]+/([^/?#]+)", gh)
        if m:
            cands.append(m.group(1))
        for c in cands:
            c = c.strip()
            low = c.lower()
            if len(c) < 4 or low in NAME_STOP or low.isdigit():
                continue
            # "-subnet"/"_subnet" 这类后缀名是仓库名，重复且噪音大，不收
            if re.search(r"[-_ ](subnet|miner|validator|docs|sandbox)$", low):
                continue
            idx.setdefault(low, sn)
    return idx


def classify(text, nidx):
    """返回 {sn: (kind, via)}；kind ∈ {strong, weak}。"""
    hits = {}
    for m in SN_STRONG.finditer(text):
        n = int(m.group(1))
        if 1 <= n <= 256:
            hits.setdefault(n, ("strong", "SN%d" % n))
    for m in SN_SUBNET.finditer(text):
        n = int(m.group(1))
        if 1 <= n <= 256:
            hits.setdefault(n, ("strong", "subnet %d" % n))
    has_ctx = bool(STRONG_CTX.search(text))
    if nidx:
        for tok in TOKEN.findall(text):
            key = tok.lower()
            sn = nidx.get(key)
            if sn is None:
                continue
            if key in GENERIC_NAMES:
                # 通用词：必须原样大小写（首字母大写或全大写）+ 同句有「明确谈子网」的语境才认
                if not (tok[:1].isupper() or tok.isupper()) or not has_ctx:
                    hits.setdefault(sn, ("weak", "通用词「%s」" % tok))
                    continue
            hits.setdefault(sn, ("strong", "名称「%s」" % tok))
    if not hits:  # 已有强命中时不再猜裸数字，避免同一条消息重复归档
        for m in SN_BARE.finditer(text):
            n = int(m.group(1))
            if not (1 <= n <= 256):
                continue
            lo, hi = max(0, m.start() - 20), min(len(text), m.end() + 20)
            if CTX_WORDS.search(text[lo:hi]):
                hits.setdefault(n, ("weak", "裸编号 %d" % n))
    return hits


# ---------------------------------------------------------------- 导出读取
def _day_json_files(out_dir, day_str):
    files = []
    for seq in (1, 2, 3, 4, 5):
        p = os.path.join(out_dir, "dtao_%s.%d.json" % (day_str, seq))
        if os.path.exists(p):
            files.append(p)
    legacy = os.path.join(out_dir, "dtao_%s.json" % day_str)
    if os.path.exists(legacy):
        files.append(legacy)
    return files


def list_export_days(out_dir):
    days = []
    try:
        for fn in os.listdir(out_dir):
            m = re.match(r"^dtao_(\d{4}-\d{2}-\d{2})(?:\.\d+)?\.json$", fn)
            if m:
                days.append(m.group(1))
    except Exception:
        pass
    return sorted(set(days))


def _entry(m, src, kind, via):
    ts = m.get("timestamp") or ""
    dt = None
    try:
        dt = (datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
              .replace(tzinfo=datetime.timezone.utc).astimezone(TZ))
    except Exception:
        pass
    author = ((m.get("author") or {}).get("nickname")
              or (m.get("author") or {}).get("name") or "?")
    return {
        "id": str(m.get("id") or ""),
        "ts": dt.strftime("%Y-%m-%dT%H:%M:%S%z") if dt else ts,
        "date": dt.strftime("%Y-%m-%d") if dt else (ts[:10] or ""),
        "time": dt.strftime("%H:%M") if dt else (ts[11:16] or "--:--"),
        "author": author,
        "text": (m.get("content") or "").strip(),
        "zh": "",
        "react": sum(r.get("count", 0) for r in (m.get("reactions") or [])),
        "src": os.path.basename(src),
        "kind": kind,
        "via": via,
    }


def scan_files(json_paths, nidx, log=print):
    """扫描若干导出 json，返回 {sn: {"strong": [entry], "weak": [entry]}}（已按 id 去重）。"""
    bucket, seen = {}, set()
    for jp in json_paths:
        try:
            with open(jp, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log("档案：读取失败 %s（%s）" % (os.path.basename(jp), e))
            continue
        for m in (data.get("messages") or []):
            c = (m.get("content") or "").strip()
            if not c or len(c) < 3 or NOISE.match(c):
                continue
            hits = classify(c, nidx)
            if not hits:
                continue
            mid = str(m.get("id") or "")
            for sn, (kind, via) in hits.items():
                key = (sn, mid)
                if key in seen:
                    continue
                seen.add(key)
                b = bucket.setdefault(sn, {"strong": [], "weak": []})
                b[kind].append(_entry(m, jp, kind, via))
    return bucket


# ---------------------------------------------------------------- 译文
def _cache_path(arc_dir):
    return os.path.join(arc_dir, "_trans_cache.json")


def load_trans_cache(arc_dir):
    try:
        with open(_cache_path(arc_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_trans_cache(arc_dir, cache):
    try:
        with open(_cache_path(arc_dir), "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


TRANS_PROMPT = (
    "你是翻译，不是编辑。把给定的每条 Discord 英文发言译成简体中文。\n"
    "硬性要求：\n"
    "1. 只译字面意思，不解释、不补充背景、不评论、不给投资判断；\n"
    "2. 人名、子网名、SN 编号、代币名（TAO/dTAO/Alpha）、数字、比例原样保留，不翻译不改写；\n"
    "3. 玩笑、反讽、黑话按原语气直译，不得改写成事实陈述；\n"
    "4. 每条译文一行，严格格式：`编号|||译文`；必须输出全部条目，不得缺号、不得合并、不得加序号说明；\n"
    "5. 原文残缺、只剩符号或看不出来意思的，译文写 `（原文不完整）`。\n"
    "只输出译文行，不要任何前言后语。"
)


def translate_missing(arc_dir, entries, llm, log=print, batch=60, limit=1200, workers=4):
    """给没有译文的条目补译（结果进缓存，同一 msg 只翻一次）。

    为什么批量 + 并发：早期实现每条消息一次 API 调用（一次 200 tokens 起步），
    建档 60 多个子网会打出上千次请求、既慢又费。现在按 batch 打包，
    用线程池并发跑几个批次；同一 message id 只翻一次，重复建档零成本。"""
    if not llm:
        return 0
    cache = load_trans_cache(arc_dir)
    todo, seen = [], set()
    for e in entries:
        mid = e.get("id") or ""
        if not mid or mid in seen:
            continue
        if cache.get(mid):
            e["zh"] = cache[mid]
            continue
        if len(e.get("text") or "") < 12:
            continue
        seen.add(mid)
        todo.append(e)
    todo = todo[:limit]
    if not todo:
        return 0

    batches = [todo[i:i + batch] for i in range(0, len(todo), batch)]

    def work(chunk):
        body = "\n".join("%d. %s" % (k + 1, (e["text"] or "").replace("\n", " ")[:600])
                         for k, e in enumerate(chunk))
        try:
            out = llm(body, TRANS_PROMPT) or ""
        except Exception as e:
            log("档案：翻译批次失败（%s）" % e)
            return {}
        got = {}
        for line in out.splitlines():
            line = line.strip()
            if "|||" not in line:
                continue
            k, v = line.split("|||", 1)
            k = re.sub(r"[^\d]", "", k)
            v = v.strip().strip("`").strip()
            if k and v:
                got[int(k)] = v
        return got

    done = 0
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, int(workers or 1))) as ex:
            results = list(ex.map(work, batches))
    except Exception as e:
        log("档案：并发翻译降级为串行（%s）" % e)
        results = [work(b) for b in batches]
    for chunk, got in zip(batches, results):
        for k, e in enumerate(chunk, 1):
            v = got.get(k)
            if v:
                e["zh"] = v
                cache[e["id"]] = v
                done += 1
        save_trans_cache(arc_dir, cache)
    log("档案：翻译 %d/%d 条（%d 批）" % (done, len(todo), len(batches)))
    return done


# ---------------------------------------------------------------- 档案读写
def sn_dir(arc_dir, sn):
    return os.path.join(arc_dir, "SN%d" % int(sn))


def archive_path(arc_dir, sn):
    return os.path.join(sn_dir(arc_dir, sn), "archive.json")


def load_archive(arc_dir, sn):
    try:
        with open(archive_path(arc_dir, sn), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_all_index(arc_dir):
    p = os.path.join(arc_dir, "index.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"updated_at": "", "subnets": {}}


def _merge_days(arc, strong, weak):
    """把新扫描的条目合并进已有档案（按 id 去重，按时间排序）。"""
    days = arc.setdefault("days", {})
    wdays = arc.setdefault("weak_days", {})
    for kind, target in (("strong", days), ("weak", wdays)):
        for e in (strong if kind == "strong" else weak):
            d = e.get("date") or "----"
            lst = target.setdefault(d, [])
            if any(x.get("id") == e.get("id") for x in lst):
                continue
            lst.append(e)
    for target in (days, wdays):
        for d in list(target.keys()):
            target[d].sort(key=lambda x: (x.get("ts") or "", x.get("id") or ""))
            if not target[d]:
                del target[d]
    return arc


def save_archive(arc_dir, arc):
    d = sn_dir(arc_dir, arc["sn"])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "archive.json"), "w", encoding="utf-8") as f:
        json.dump(arc, f, ensure_ascii=False, indent=1)


def _people(arc):
    """按人物聚合（只用强命中），返回 [(author, [entries...])]，按条数降序。"""
    people = {}
    for d, entries in (arc.get("days") or {}).items():
        for e in entries:
            people.setdefault(e.get("author") or "?", []).append(e)
    for k in people:
        people[k].sort(key=lambda x: x.get("ts") or "")
    return sorted(people.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))


def stats_of(arc):
    days = arc.get("days") or {}
    n_strong = sum(len(v) for v in days.values())
    n_weak = sum(len((arc.get("weak_days") or {}).get(d) or []) for d in (arc.get("weak_days") or {}))
    ds = sorted(days.keys())
    return {
        "strong": n_strong,
        "weak": n_weak,
        "days": len(ds),
        "first": ds[0] if ds else "",
        "last": ds[-1] if ds else "",
        "people": len(_people(arc)),
    }


WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _wd(day):
    try:
        return WEEKDAY[datetime.datetime.strptime(day, "%Y-%m-%d").weekday()]
    except Exception:
        return ""


def _clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def build_md(arc, max_lines_per_day=400):
    sn = arc["sn"]
    st = stats_of(arc)
    name = arc.get("name") or "（知识库未收录）"
    lines = ["# SN%d %s · 子网档案" % (sn, name), ""]
    meta = []
    if arc.get("biz"):
        meta.append("**业务**：%s" % arc["biz"])
    if arc.get("url"):
        meta.append("**官网**：%s" % arc["url"])
    if arc.get("github"):
        meta.append("**代码库**：%s" % arc["github"])
    if meta:
        lines += meta + [""]
    span = ("%s → %s" % (st["first"], st["last"])) if st["first"] else "暂无"
    if st["strong"] or st["weak"]:
        head_line = ("> 建档时间：%s · 归档条目 **%d** 条（另有裸编号疑似提及 %d 条）"
                     "· 覆盖 **%s** · 涉及 **%d** 人"
                     % (arc.get("updated_at") or "—", st["strong"], st["weak"], span, st["people"]))
        src_line = ("> 数据来源：Discord #price-talk 本地导出（`exports/dtao_*.json`）。"
                    "档案**只搬运原话**、按消息真实时间排序，归属规则为编号/名称命中，未经模型判断或改写。")
    else:
        # 只有投研报告、还没有群聊归档的档案：别写成「0 条」让人以为档案是空的
        head_line = ("> 建档时间：%s · **本档案目前只有投研报告，尚无群聊归档**"
                     "（导出日报时会自动补进群聊原话）" % (arc.get("updated_at") or "—"))
        src_line = ("> 数据来源：全网投研（链上数据站 / 官网 / GitHub / 新闻 / 搜索 / X）。"
                    "群聊原话会在后续导出时自动追加，两段互不覆盖。")
    lines += [head_line, src_line, ""]
    days = arc.get("days") or {}
    for day in sorted(days.keys()):
        entries = days[day]
        lines.append("## 📅 %s（%s）· %d 条" % (day, _wd(day), len(entries)))
        lines.append("")
        for e in entries[:max_lines_per_day]:
            hot = (" 🔥%d" % e["react"]) if e.get("react", 0) >= 3 else ""
            via = e.get("via") or ""
            tag = "" if via.startswith(("SN", "subnet")) else " `%s`" % via
            zh = e.get("zh") or ""
            body = "「%s」" % _clip(e.get("text"), 600)
            if zh:
                body += " → %s" % _clip(zh, 300)
            lines.append("- **%s** **%s**%s%s：%s" % (e.get("time"), e.get("author"), hot, tag, body))
        if len(entries) > max_lines_per_day:
            lines.append("- …（本日另有 %d 条，见 archive.json）" % (len(entries) - max_lines_per_day))
        lines.append("")

    people = _people(arc)
    if people:
        lines += ["## 👥 人物观点索引", "",
                  "> 同一人关于 SN%d 的全部表态，按时间排列；只保留其本人原话要点，不合并不同日期、不代为归纳立场。" % sn,
                  ""]
        for author, items in people:
            lines.append("### %s · %d 条" % (author, len(items)))
            for e in items:
                zh = e.get("zh") or _clip(e.get("text"), 120)
                lines.append("- %s %s → %s" % (e.get("date")[5:], e.get("time"), _clip(zh, 200)))
            lines.append("")

    # 投研报告摘要区（投研生成后自动增量写入，最新在最前）
    reports = arc.get("reports") or []
    if reports:
        lines += ["## 🔬 重要事件跟踪报告（投研产出）", "",
                  "> 报告正文已随档案入库（`archive.json` 的 `reports[].body`），"
                  "此处按时间倒序展开全文；同目录 `research_*.md` 是同一内容的备份副本。", ""]
        for r in reports[:20]:
            lines.append("### %s · 证据强度 %s" % (r.get("date") or "—", r.get("grade") or "—"))
            # 老档案只有 title（存的是整段结论），新档案有 conclusion；两者都兼容
            concl = r.get("conclusion") or r.get("title") or ""
            if concl:
                lines.append("- 结论：%s" % _clip(concl, 300))
            if r.get("events"):
                lines.append("- 关键事件：%s" % _clip(r["events"], 400))
            srcs = r.get("sources") or []
            if srcs:
                lines.append("- 数据源：%s" % "、".join(srcs))
            if r.get("file"):
                lines.append("- 正文副本：`%s`" % r["file"])
            body = (r.get("body") or "").strip()
            if body:
                lines += ["", "<details><summary>展开报告全文</summary>", "", body, "", "</details>", ""]
            else:
                lines.append("")

    wdays = arc.get("weak_days") or {}
    if wdays:
        n_weak = sum(len(v) for v in wdays.values())
        lines += ["## 🔍 疑似提及（裸编号命中，仅供参考）", "",
                  "> 这些消息只出现了数字「%d」，没有写 SN 前缀或子网名，归属**未经验证**；"
                  "仅用于人工回查，不作为投研依据。" % sn, ""]
        shown = 0
        for day in sorted(wdays.keys()):
            for e in wdays[day]:
                if shown >= 60:
                    break
                lines.append("- %s %s **%s**：「%s」" % (day[5:], e["time"], e["author"], _clip(e.get("text"), 200)))
                shown += 1
        if n_weak > shown:
            lines.append("- …（共 %d 条，见 archive.json）" % n_weak)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_md(arc_dir, arc):
    d = sn_dir(arc_dir, arc["sn"])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "archive.md"), "w", encoding="utf-8") as f:
        f.write(build_md(arc))


# ---------------------------------------------------------------- 投研落档
def attach_research(cfg, sn, date_str, title, digest, sources=None, meta=None,
                    file_name="", log=print, income_snap=None):
    """把一次投研的产出增量写入子网档案。

    - 档案已存在：只追加/替换**同一天**的报告记录，原有群聊条目一条不动；
    - 档案不存在：新建一个（只有报告、没有群聊条目也是合法档案）。
    为什么只存摘要：正文是长文，放 archive.json 会把档案撑爆；正文留在 research_*.md，
    json 里只留「结论 + 关键事件 + 数据源 + 证据强度」，方便以后按子网回看历史判断。

    income_snap：本次的收入/回购指标快照（标量字典）。单独存进 `income_hist`，
    下次投研拿它做「跟上一次比，哪些数字变了」的对比基准。
    为什么不放进 reports[]：报告会被后续同日重跑覆盖（date 相同就替换），
    而快照是「时间序列」，覆盖了就永远比不出变化，必须追加保存。
    """
    arc_dir = _archive_dir_of(cfg)
    os.makedirs(arc_dir, exist_ok=True)
    now = datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    meta = meta or {}
    arc = load_archive(arc_dir, sn)
    # 必须在写盘前判断：load_archive 之后 save_archive 已经落盘，再 os.path.exists 永远为真，
    # 日志会一律打印「已更新」，新建档案也被说成更新（实测 SN888 就是如此）。
    existed = bool(arc)
    _nm = (meta.get("name") or "").strip()
    if _nm.lower() in _PLACEHOLDER_NAMES | {"未知", "（知识库未收录）", "（未收录）"}:
        _nm = ""
    if not arc:
        arc = {
            "sn": int(sn),
            "name": _nm or "（知识库未收录）",
            "biz": meta.get("desc") or "",
            "url": meta.get("url") or "",
            "github": meta.get("github") or "",
            "created_at": now,
            "days": {},
            "weak_days": {},
            "reports": [],
            "note": "本档案由投研报告创建（尚无群聊归档）",
        }
    else:
        # 只在档案本身缺名称时才用投研抓到的名字补；已有真名不覆盖。
        # 例外：投研已用链上身份确认改名（meta["name_changed_from"] 有值），
        # 这时必须覆盖 —— 否则档案永远留着旧名，下次投研又从旧名起步
        # （SN127 实测：报告已写 Zils，档案里仍是 Astrid）。
        _renamed = bool((meta.get("name_changed_from") or "").strip())
        if _nm and (_renamed or (arc.get("name") or "").strip()
                    in ("", "未知", "（知识库未收录）", "（未收录）")):
            if _renamed and (arc.get("name") or "").strip() != _nm:
                log("   档案名称按链上校正：%s → %s" % (arc.get("name") or "（空）", _nm))
                old = (arc.get("name") or "").strip()
                if old:
                    arc["aliases"] = sorted(set((arc.get("aliases") or []) + [old]))
            arc["name"] = _nm
        for k, src in (("biz", "desc"), ("url", "url"), ("github", "github")):
            # 改名时官网/仓库连带失效，必须同步覆盖，否则下次抓取打到旧站
            if meta.get(k) and (_renamed or not arc.get(src)):
                if _renamed and (arc.get(src) or "") != meta[k]:
                    log("   档案 %s 同步为链上值：%s" % (src, meta[k]))
                arc[src] = meta[k]

    # 从正文里抽：标题句、时间线首条、验证状态统计。
    # 新版报告只有三段（时间线/当前数据/预期跟踪），没有「一句话结论」了 ——
    # 标题取「时间线里第一条✅已验证事件」，比人工编一句结论更贴合「以事件为主」的定位。
    txt = digest or ""
    first = ""
    m = re.search(r"\*\*(?:🎯\s*)?一句话结论\*\*\s*\n+(.+)", txt)   # 兼容旧报告
    if m:
        first = m.group(1).strip()
    events = ""
    me = re.search(r"\*\*(?:🗓\s*)?重要事件时间线\*\*\s*\n+((?:-\s*.+\n?)+)", txt)
    if me:
        events = me.group(1).lstrip("- ").strip()
    # 没有结论段时，拿第一条事件当标题（去掉日期与验证标记，只留事件本身）
    if not first and events:
        e0 = events.split("\n")[0].lstrip("- ").strip()
        e0 = re.sub(r"^[\d]{4}-[\d]{2}-[\d]{2}\s*·\s*|^[\d]{2}-[\d]{2}\s*·\s*", "", e0)
        e0 = re.sub(r"✅已验证[^·]*|⚠️未验证[^·]*", "", e0)
        # 括号里的「（官方动作）」「（社区传闻）」是分类标签，当标题没意义
        e0 = re.sub(r"[（(]\s*(?:官方动作|社区传闻|官方承诺[^）)]*|链上[^）)]*)\s*[）)]", "", e0)
        first = e0.strip(" ·，,")
    # 证据强度改由「已验证/未验证」条数推算：全已验证=强，各半=中，多数未验证=弱
    _ok = len(re.findall(r"✅已验证", events or txt))
    _bad = len(re.findall(r"⚠️未验证", events or txt))
    if _ok + _bad:
        _r = _ok / float(_ok + _bad)
        grade = "强" if _r >= 0.75 else ("弱" if _r <= 0.35 else "中")
    else:
        grade = ""
    mg = re.search(r"证据强度[：:]\s*\**\s*(强|中|偏?强|弱|中偏强|中偏弱)", first or txt)
    if mg and not grade:
        grade = mg.group(1)
    srcs = sorted({s for s in (sources or []) if s})
    # 标题压成一行：去掉来源标记与括号补充，截到第一个句号 —— 档案列表和 md 小节标题要用它
    brief = re.sub(r"\[[^\]]{1,20}\]", "", first)
    brief = re.split(r"证据强度", brief)[0]
    brief = re.split(r"(?<=[。！？])", brief.strip())[0].strip(" -—·；;，,")
    # 去掉来源标记后会留下「2026-06-15 ；链上」这种悬空空格，中文里不该有
    brief = re.sub(r"\s*([，。；、：！？])\s*", r"\1", brief)
    brief = re.sub(r"\s{2,}", " ", brief).strip()[:120] or first[:120]

    rec = {"date": date_str, "title": brief, "conclusion": first, "events": events,
           "grade": grade, "sources": srcs, "file": file_name, "generated_at": now,
           # 报告全文入库：此前只把摘要写进 archive.json、正文留在 research_*.md，
           # 结果「档案」和「报告」是两套东西，下次投研根本拿不到上次的完整分析。
           # 现在正文直接进档案，报告文件只是同一内容的可单独阅读副本。
           "body": (digest or "").strip()}
    reports = [r for r in (arc.get("reports") or []) if (r.get("date") or "") != date_str]
    reports.insert(0, rec)
    arc["reports"] = reports[:60]
    # 收入/回购快照：按日期追加（同日重跑覆盖当天那条，不重复堆积）
    if income_snap:
        _ih = [x for x in (arc.get("income_hist") or [])
               if (x.get("date") or "") != date_str]
        _ih.append({"date": date_str, "snapshot": income_snap})
        _ih.sort(key=lambda x: x.get("date") or "")
        arc["income_hist"] = _ih[-40:]
    arc["updated_at"] = now
    save_archive(arc_dir, arc)
    save_md(arc_dir, arc)
    refresh_index(arc_dir)
    log("投研落档：SN%d 档案%s，已记录 %s 的报告（累计 %d 份）"
        % (int(sn), "已更新" if existed else "已新建",
           date_str, len(arc["reports"])))
    return arc


def refresh_index(arc_dir):
    idx = {"updated_at": datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"), "subnets": {}}
    try:
        for fn in sorted(os.listdir(arc_dir)):
            m = re.match(r"^SN(\d+)$", fn)
            if not m:
                continue
            arc = load_archive(arc_dir, int(m.group(1)))
            if not arc:
                continue
            st = stats_of(arc)
            idx["subnets"][str(arc["sn"])] = {
                "name": arc.get("name") or "（未收录）",
                "biz": arc.get("biz") or "",
                "strong": st["strong"], "weak": st["weak"], "people": st["people"],
                "first": st["first"], "last": st["last"],
                "days": st["days"],
                # 报告份数：只有投研报告、没有群聊归档的档案（strong=0）靠它区分排序
                "reports": len(arc.get("reports") or []),
            }
    except Exception:
        pass
    with open(os.path.join(arc_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    return idx


# ---------------------------------------------------------------- 主入口
def _archive_dir_of(cfg):
    d = cfg.get("archive_dir") or "subnet_archive"
    return d if os.path.isabs(d) else os.path.join(HERE, d)


def archive_files(cfg, json_paths, log=print, llm=None, translate=False):
    """归档**这批导出文件**里出现的子网（增量、幂等）。

    这是唯一的写入口，两个场景共用：
      - 导出流程里自动调用（每跑完一次 daily 就顺手归档，无需任何指令）；
      - 全量重建（archive_days 把所有历史导出文件列出来喂进来）。
    已建档的子网只补充新条目，未建档的自动建 —— 判断依据只是消息 id 是否已存在。
    """
    arc_dir = _archive_dir_of(cfg)
    os.makedirs(arc_dir, exist_ok=True)
    json_paths = [p for p in (json_paths or []) if p and os.path.exists(p)]
    if not json_paths:
        return {"subnets": 0, "new": 0, "index": load_all_index(arc_dir), "arc_dir": arc_dir}

    kb = load_kb()
    subs = kb.get("subnets") or {}
    nidx = name_index(kb)
    bucket = scan_files(json_paths, nidx, log=log)
    if not bucket:
        return {"subnets": 0, "new": 0, "index": load_all_index(arc_dir), "arc_dir": arc_dir}

    now = datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    total_new, touched = 0, []
    for sn in sorted(bucket.keys()):
        b = bucket[sn]
        meta = subs.get(str(sn)) or {}
        arc = load_archive(arc_dir, sn) or {
            "sn": sn,
            "name": real_name(meta) or "（知识库未收录）",
            "biz": real_desc(meta),
            "url": real_url(meta.get("url")),
            "github": real_url(meta.get("github")),
            "created_at": now,
            "days": {},
            "weak_days": {},
        }
        # 知识库更新后同步元信息（名称/业务可能被官方改过）；占位值不回写，避免覆盖已有的真名
        if real_name(meta):
            arc["name"] = real_name(meta)
        elif (arc.get("name") or "").strip().lower() in _PLACEHOLDER_NAMES:
            # 反向修正：早期版本把知识库的 "deprecated"/"Unknown" 当成了真名写进档案，
            # 现在清掉，交给显示层写「（知识库未收录）」
            arc["name"] = ""
        if real_desc(meta):
            arc["biz"] = real_desc(meta)
        elif (arc.get("biz") or "").strip().lower() in _PLACEHOLDER_NAMES | {"未知"}:
            arc["biz"] = ""
        if real_url(meta.get("url")):
            arc["url"] = real_url(meta.get("url"))
        elif "deprecated" in (arc.get("url") or "").lower():
            arc["url"] = ""
        if real_url(meta.get("github")):
            arc["github"] = real_url(meta.get("github"))
        elif "deprecated" in (arc.get("github") or "").lower():
            arc["github"] = ""
        arc["updated_at"] = now
        before = sum(len(v) for v in (arc.get("days") or {}).values())
        _merge_days(arc, b["strong"], b["weak"])
        after = sum(len(v) for v in (arc.get("days") or {}).values())
        total_new += max(0, after - before)
        if translate and llm:
            all_entries = [e for v in (arc.get("days") or {}).values() for e in v]
            translate_missing(arc_dir, all_entries, llm, log=log)
        save_archive(arc_dir, arc)
        save_md(arc_dir, arc)
        touched.append(arc)
    idx = refresh_index(arc_dir)
    return {"subnets": len(touched), "new": total_new, "index": idx, "arc_dir": arc_dir}


def archive_days(cfg, days, log=print, llm=None, translate=False, out_dir=None):
    """按日期归档（days=None 表示全部已有导出）。内部转成文件列表交给 archive_files。"""
    out_dir = out_dir or os.path.join(HERE, cfg.get("output_dir", "exports"))
    if days is None:
        days = list_export_days(out_dir)
    files = []
    for d in days:
        files.extend(_day_json_files(out_dir, d))
    if not files:
        raise RuntimeError("没有可用导出记录（exports/dtao_*.json），无法建档")
    log("子网建档：扫描 %d 个导出文件（%s ~ %s）" % (len(files), days[0], days[-1]))
    r = archive_files(cfg, files, log=log, llm=llm, translate=translate)
    if not r["subnets"]:
        raise RuntimeError("这批导出记录里没有任何子网编号/名称命中，无法建档")
    log("子网建档完成：%d 个子网有档案，本次新增 %d 条（累计 %d 条）"
        % (r["subnets"], r["new"], sum(v["strong"] for v in r["index"]["subnets"].values())))
    return r


def get_arc_dir(cfg):
    return _archive_dir_of(cfg)


def resolve_sn(arg, cfg=None):
    """把用户输入解析成 SN 编号：'29' / 'sn29' / 'SN 29' / 'hotfloat' 都可以。"""
    s = (arg or "").strip()
    m = re.match(r"(?i)^(?:sn|subnet)?\s*#?\s*(\d{1,3})$", s)
    if m:
        return int(m.group(1))
    kb = load_kb()
    nidx = name_index(kb)
    low = ascii_name(s).lower()
    if low in nidx:
        return nidx[low]
    for tok in TOKEN.findall(s):
        if tok.lower() in nidx:
            return nidx[tok.lower()]
    # 模糊：名字包含关系（用户可能只记得半截）
    for k, sn in nidx.items():
        if len(low) >= 4 and (low in k or k in low):
            return sn
    return None


def digest_fingerprint(arc_dir, sn):
    """档案指纹（条目数 + 最大 id），投研时用来判断是否有新素材。"""
    arc = load_archive(arc_dir, sn) or {}
    ids = [e.get("id") or "" for v in (arc.get("days") or {}).values() for e in v]
    raw = "%d|%s" % (len(ids), max(ids) if ids else "")
    return hashlib.md5(raw.encode()).hexdigest()[:10]
