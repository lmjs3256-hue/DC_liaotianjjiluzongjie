#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bittensor 社区消息日报（dtt版）（dtt 关键词监控，多平台）
=====================================================
与现有 DC 日报**完全隔离**：独立数据源（dtt）、独立提示词、独立文件，
只复用 pipeline 的 AI 调用 / 结构清洗 / 飞书·TG 推送层。

数据流：
    community_fetch.fetch_day()  →  build_input()（按 SN 分组、标平台）
    →  call_llm（独立提示词）  →  交叉核对档案/投研（冲突标⚠️待人工确认）
    →  push_feishu / push_telegram

对外接口：
    run_community_day(cfg, day_str, log=print, push=True) -> dict
"""

import datetime
import json
import os
import re

import community_fetch as CF

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = datetime.timezone(datetime.timedelta(hours=8))

# 平台显示名
PLAT_CN = {"twitter": "Twitter/X", "telegram": "Telegram",
           "discord": "Discord", "manual": "手动录入"}

# 单条消息正文截断（社区里长文很多，截断防止输入爆炸）
MAX_MSG_CHARS = 320
# 每个 SN 最多喂多少条（防止 24 号那种 119 条灌爆）
MAX_PER_SN = 12
# 最多覆盖多少个 SN（按消息数排序）
MAX_SN = 18
# 无 SN 归属但值得看的通用消息最多多少条
MAX_GENERIC = 12
# 创始人等关键人物：dtt 里的转推/推文/回复一律保留，不参与噪声剔除与条数截断
DEFAULT_KEY_AUTHORS = ["const"]

# 相关性过滤：dtt 靠关键词命中，像 "ms" "jason" "fish" 这类通用词会误抓大量
# 与 Bittensor 无关的推文（实测 SN24 混进黄仁勋八卦、黎曼猜想）。这里按内容
# 做二次相关性打分，把明显无关的压到后面/剔除，只把有信息量的喂给模型。
_BT_KW = re.compile(
    r"(?i)\b(bittensor|dtao|alpha|subnet|subnets|buyback|buybacks|emission|"
    r"emissions|staking|delegat\w*|validator\w*|miner\w*|liquidity|"
    r"deregist\w*|tao|回购|子网|销毁|质押|权重|排放)\b"
)
# 明确的子网编号（带 sn 前缀，避免裸数字误命中）
_SN_TAG = re.compile(r"(?i)\bsn[: ]?\d{1,3}\b")
_CN_RE = re.compile(r"[\u4e00-\u9fff]")
# 明显是通用新闻/八卦的开头（dtt 误抓的英文长推）
_NEWSY = re.compile(r"(?i)^(breaking|rt @|just in|update:|bipartisan|researcher|mathematician)")


def _key_author_names(cfg=None):
    """关键人物名单（创始人等）。dtt 里的const有 const@const_reborn、
    consttt,备注:const 等多种写法，统一按 handle 匹配。"""
    names = (cfg or {}).get("community_key_authors")
    if names is None:
        names = DEFAULT_KEY_AUTHORS
    if isinstance(names, str):
        names = [names]
    return [str(x).strip().lower() for x in names if str(x).strip()]


def _key_author_hit(e, cfg=None):
    """这条消息是否为关键人物本人发言，或转推/回复/提及关键人物。

    创始人（const）的转推、推文、回复都很重要，即使 dtt 没标 SN、
    即使内容像英文长文或 RT 腔调，也不能被噪声过滤删掉。
    """
    names = _key_author_names(cfg)
    if not names:
        return False
    blob = ((e.get("author") or "") + " " + (e.get("content") or "")).lower()
    for n in names:
        # const 可匹配 const / const_reborn / consttt；不匹配 constantly 之类的误命中
        pat = r"(?<![a-z0-9])" + re.escape(n) + r"(?:_[a-z0-9]+|tt)?(?![a-z])"
        if re.search(pat, blob):
            return True
    return False


def _msg_kind(e, cfg=None):
    """给关键人物消息标形态：转推 / 回复·提及 / 推文·发言。"""
    c = (e.get("content") or "").strip()
    if re.match(r"(?i)^rt @", c):
        return "转推"
    if re.match(r"(?i)^@[a-z0-9_]+", c):
        return "回复·提及"
    return "推文·发言"


def _relevance(e, cfg=None):
    """给一条消息打相关性分：越高越该保留。低于 0 视为噪声剔除。
    关键人物（const 等）直接给高档分，保证不被 RT 腔调/英文长文规则误杀。"""
    c = (e.get("content") or "").strip()
    score = 0
    has_kw = bool(_BT_KW.search(c))
    has_cn = bool(_CN_RE.search(c))
    if has_kw:
        score += 12
    if has_cn:
        score += 3
    if _SN_TAG.search(c):
        score += 3
    # 英文长文 + 无关键词 + 新闻腔 = 大概率误抓（黄仁勋八卦、黎曼猜想之类）
    if len(c) > 200 and not has_kw and not has_cn:
        score -= 20
    if _NEWSY.match(c) and not has_kw and not has_cn:
        score -= 15
    # 极短的纯英文涂抹（如 "gMs"）无归属价值
    if len(c) < 4 and not has_kw:
        score -= 5
    # 创始人的推文/转推/回复一律保留，且排在普通消息前面
    if _key_author_hit(e, cfg):
        return max(score, 20)
    return score


def extract_conflicts(digest):
    """把正文里散落的「待人工确认」条目抽出来，集中成独立板块。

    返回 (去掉冲突行的正文, 冲突行列表)。这样 AI 就地标注的冲突不会再被长文淹没，
    推送时置顶成一个「⚠️ 待人工确认」专区，用户一眼可见。
    """
    lines = (digest or "").splitlines()
    conflicts = [ln.strip() for ln in lines if "待人工确认" in ln]
    kept = [ln for ln in lines if "待人工确认" not in ln]
    # 抽走条目后可能留下连续空行，压成单个空行
    body = []
    for ln in kept:
        if not ln.strip() and body and not body[-1].strip():
            continue
        body.append(ln)
    return "\n".join(body).strip(), conflicts


def build_conflict_section(conflicts, confirms=None):
    """把冲突条目拼成置顶的醒目专区（飞书/TG 通用，标题加粗）。

    AI 按提示词输出结构化格式：
        ⚠️待人工确认｜SN编号 子网名｜社区：<...>｜档案：<...>｜冲突：<...>
    这里解析成逐字段的多行板块，让「哪个子网/社区说啥/档案记啥/矛盾在哪」一眼可见。
    旧格式（无 ｜ 分隔）降级为原样列出，不丢内容。

    confirms: 可选 {sn(int): {status, note, at}}，来自子网档案的人工确认。
    有确认的冲突在末尾追加「✅ 人工已核实 / ❌ 人工已推翻 + 批注」，供确认版重推。
    """
    confirms = confirms or {}
    n_confirmed = sum(1 for k, v in confirms.items() if v.get("status") == "confirmed")
    n_refuted = sum(1 for k, v in confirms.items() if v.get("status") == "refuted")
    title_bits = ["%d 处 · 社区消息与历史档案结论冲突" % len(conflicts)]
    if n_confirmed or n_refuted:
        title_bits.append("已人工确认 %d 处（核实 %d / 推翻 %d）" % (n_confirmed + n_refuted, n_confirmed, n_refuted))
    else:
        title_bits.append("请核对后再采信")
    header = "**⚠️ 待人工确认（%s）**\n\n" % "，".join(title_bits)
    blocks = []
    for i, c in enumerate(conflicts, 1):
        c = c.strip().lstrip("-").strip()
        # 结构化：按全角｜（或半角|）切段，去掉开头的 ⚠️待人工确认
        parts = [p.strip() for p in re.split(r"[｜|]", c) if p.strip()]
        if parts and "待人工确认" in parts[0]:
            parts = parts[1:]
        fields = {"sn": "", "社区": "", "档案": "", "冲突": ""}
        for p in parts:
            key = None
            for k in ("社区", "档案", "冲突"):
                if p.startswith(k + "："):
                    key = k
                    break
            if key:
                fields[key] = p[len(key) + 1:].strip()
            elif not fields["sn"] and re.search(r"\d", p):
                fields["sn"] = p.strip()
        # 从 SN 字段提取编号，查人工确认
        msn = re.search(r"(\d+)", fields["sn"])
        sn = int(msn.group(1)) if msn else None
        cf = confirms.get(sn) if sn is not None else None
        if fields["社区"] and fields["档案"]:
            # 结构化成功：逐字段分行展示
            lines = ["**%d）%s**" % (i, fields["sn"] or "SN？")]
            if fields["社区"]:
                lines.append("　社区消息：%s" % fields["社区"])
            if fields["档案"]:
                lines.append("　档案记录：%s" % fields["档案"])
            if fields["冲突"]:
                lines.append("　冲突点：%s" % fields["冲突"])
            if cf:
                tag = "✅ 人工已核实" if cf.get("status") == "confirmed" else "❌ 人工已推翻"
                lines.append("　%s（%s）：%s" % (tag, cf.get("at") or "—", cf.get("note") or "（无批注）"))
            blocks.append("\n".join(lines))
            continue
        # 降级：旧格式原样列出（若有确认也追加上）
        line = "**%d）** %s" % (i, c)
        if cf:
            tag = "✅ 人工已核实" if cf.get("status") == "confirmed" else "❌ 人工已推翻"
            line += "\n　%s（%s）：%s" % (tag, cf.get("at") or "—", cf.get("note") or "（无批注）")
        blocks.append(line)
    return header + "\n\n".join(blocks)


COMMUNITY_SYSTEM_PROMPT = (
    "你是一名 Bittensor 生态资深研究员，负责把「多平台社区关键词监控」抓到的当日消息，"
    "整理成一份**按子网分组**的中文社区日报。数据来源是 dtt 关键词监控工具，"
    "聚合了 Twitter/X、Telegram、Discord 上命中 Bittensor 关键词的公开消息。\n"
    "\n"
    "【铁律：不可全信，逐条标注来源】\n"
    "- 这些是**社区公开消息**，混有传闻、喊单、转发、观点、未证实爆料。一律按「有人说」处理，"
    "不要写成既定事实。每条必须标注来源平台（Twitter/X / Telegram / Discord）与发言人。\n"
    "- **禁止编造**：不补原文没有的数字、人名、SN 编号、交易细节；原文没有就说没有。\n"
    "- 只关注与 Bittensor 子网、代币机制、团队动态、上线/合作/催化相关的实质内容；"
    "纯水聊、无关转发、纯表情、与 Bittensor 无关的话题一般不列；"
    "**但关键人物（如创始人 const）的推文、转推、回复一律保留，不得当成无关转发删掉**。\n"
    "- **禁止删人**：输入末尾「当日全部人物与消息」里出现的每一位发言人，"
    "都要在「👀 人物与观点」节点名写出（内容太少就写「仅发言：…」），"
    "不许以「普通群友」「无关发言」为由省略；const 相关消息必须逐条体现。\n"
    "\n"
    "【输出结构（严格按顺序，标题一律 **加粗**；无内容的小节整节省略，不硬凑）】\n"
    "**🔥 社区热点**（当天跨平台被反复提及、或明显重要的事件，3-6 条；"
    "每条一句说清「谁 + 什么事」，关键主体与 **SN编号** 加粗，标来源平台）\n"
    "**🧩 子网动态**（**按 SN 分组**：每个有实质消息的子网写一块，格式「**SN53 Engy**（N 条）」"
    "下接 1-3 条要点；子网名以输入里的名称对照为准，查不到就只写编号，不要编名字。"
    "按消息量与重要性排序，只写有价值的前若干个，不必全部列出）\n"
    "**👀 人物与观点**（逐个点名当天所有发言人，格式「**人名**：做了什么/说了什么（平台）」；"
    "人名一律用输入里的显示名（如 `const`、`p383`），**禁止**写 `xxx@handle`、"
    "`名字_数字`、`备注:` 这类账号/辨识码后缀；"
    "**const 必须单列**：只写他当天与本子网相关的发言/转推/回复，"
    "每条提炼成一句「const 转推 @谁：内容要点」「const 回复 @谁：内容要点」"
    "「const 表示：内容要点」，写清对象与他说了什么，不贴原始链接、不照抄全文；"
    "与 subnet 无关的日常发言不列。其他人一律列人名+消息要点，不做重要性筛选；确实无人发言才写「无」）\n"
    "　用户名写法：输入里 `<>` 内已是规范显示名（备注名优先，如 `const`、`p383`），**直接用、不要改**；"
    "若遇到 `名字_四位数字`（如 `p383_54249`）去掉 `_数字` 后缀写成 `p383`，"
    "遇到 `名字@handle` 只取 `名字`，遇到 `备注:xxx` 只取 `xxx`；"
    "原文若有真实姓名/公开别名（如 Isabella Liu）则用真名。禁止出现 @handle/辨识码/备注标签。\n"
    "**💡 一句话总结**（当天社区情绪与主线，一句收束）\n"
    "\n"
    "【交叉核对：发现矛盾必须标出来，交人工确认（格式严格）】\n"
    "- 输入里若附了「历史档案 / 投研结论」参考块，那是**历史快照，可能过时或被推翻**。\n"
    "- 当今日社区消息与历史结论**明显矛盾**（例如历史写「未回购/未上线」，今日多人称已回购/已上线），"
    "必须在该条后写冲突标记，**严格按下面固定格式**（用全角｜分隔，四项一个都不能少、顺序不能变）：\n"
    "  ⚠️待人工确认｜SN编号 子网名｜社区：<今日社区到底说了什么，含关键数字/主体/来源>｜"
    "档案：<档案或投研的记录日期 + 原话结论，如「2026-11-19 投研：收入为团队自报、无链上证据」>｜"
    "冲突：<一句话点明两边到底哪里对不上>\n"
    "  示例：⚠️待人工确认｜SN55 NIOME｜社区：taoscale 称自 2 月累计收入 $120,000｜"
    "档案：2026-11-19 投研：该数字为团队自报、无第三方或链上证据｜"
    "冲突：社区引用的收入数字可信度与档案记录不一致，需核实是否有链上/审计支撑\n"
    "- **SN 编号和子网名必须写**（从输入对照取，查不到就只写编号）；社区/档案/冲突三项内容都要具体，"
    "禁止只写「存在冲突」这种空话。把冲突并列写出，不要二选一、不要静默采信任何一边。\n"
    "- 没有矛盾就不要硬标；正常的、无历史对照的消息不加此标记。\n"
    "\n"
    "【排版与篇幅（飞书/TG 双平台通用）】\n"
    "- 关键主体 **加粗**：人物、**SN编号/子网名**、代币、事件、关键数字。\n"
    "- emoji 标程度：🔥 热点、✅ 落地、⚠️ 风险/待确认、📌 关注、❌ 利空。\n"
    "- 每条单独成行用 - 项目符号，小节间空行，适合手机端；不用 # 标题、不用 > 引用。\n"
    "- **字数上限 3000 字**（含标点），人物节优先保证所有人和 const 消息齐全；"
    "只有上限没有下限：消息少就少写，简洁+信息全=合格。\n"
    "- 禁止废话：不写「此外」「总体来看」「可以看出」「值得一提的是」这类填充词；"
    "不复述已说过的事实；不解释自己为什么这么写；一条只说一件事。\n"
    "- 绝对不许在正文里写任何字数标注或「来源：dtt」之外的元信息。\n"
)


def _sn_names():
    """从知识库取 SN 编号 -> 名称，供提示词写「SN53 Engy」。"""
    try:
        import pipeline as P
        kb = P._subnet_kb() or {}
        out = {}
        for k, v in (kb.get("subnets") or {}).items():
            nm = (v.get("name") or "").strip()
            if nm:
                try:
                    out[int(k)] = nm
                except Exception:
                    pass
        return out
    except Exception:
        return {}


def _clean(s, n=MAX_MSG_CHARS):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[:n] + "…"


def _display_author(author, cfg=None):
    """取人物显示名：有「备注:X」就用 X，不做任何账号名拼接。

    dtt 的 author 字段常见形态（实测 11-22）：
      consttt,备注:const      → const        （监控把人名塞进备注）
      const@const_reborn      → const        （显示名@账号，取 @ 前）
      canigonow,备注:17       → 17
      p383_54249              → p383         （显示名_辨识码）
      Millie@AltcoinMillie    → Millie
    用户要求：关键人物名有备注的直接用备注名，不要展示 const_reborn /
    consttt 这类原始账号名。故备注优先级最高，其次 @ 前显示名，再退 _数字 后缀。
    """
    a = (author or "").strip()
    m = re.search(r"备注[:：]\s*([^,，\s]+)", a)
    if m:
        return m.group(1).strip()
    a = a.split("@")[0].strip()
    a = re.sub(r"_\d{2,6}$", "", a).strip()
    return a or "?"


def build_input(payload, cfg=None):
    """把取数结果按 SN 分组，组织成给模型的输入文本。

    返回 (text_block, sn_list, sn_excerpts)：
      sn_excerpts[sn] = {excerpt, platforms}，供社区日报落档到各子网档案。
    """
    events = payload.get("events") or []
    names = _sn_names()
    sn_excerpts = {}

    # 按 SN 分桶（一条消息可能归多个 SN，分别计入）
    by_sn = {}
    generic = []
    for e in events:
        sns = e.get("sn") or []
        if not sns:
            generic.append(e)
            continue
        for sn in sns:
            by_sn.setdefault(sn, []).append(e)

    # 相关性过滤 + 按分数排序（噪声消息剔除）
    for sn in by_sn:
        scored = [(_relevance(e, cfg), e) for e in by_sn[sn]]
        scored = [(sc, e) for sc, e in scored if sc >= 0]
        scored.sort(key=lambda x: (-x[0], -(x[1].get("occurredAt") or 0)))
        by_sn[sn] = [e for _sc, e in scored]
    generic = [e for e in generic if _relevance(e, cfg) >= 0]

    # 排序：有实质消息多的 SN 优先
    ordered = sorted(by_sn, key=lambda s: (-len(by_sn[s]), s))[:MAX_SN]
    st = payload.get("stats") or {}

    bounds = payload.get("bounds") or {}
    _bs, _be = bounds.get("start"), bounds.get("end")
    def _bstr(ts):
        return datetime.datetime.fromtimestamp(ts, TZ).strftime("%m-%d %H:%M") if ts else "?"
    _range = ("%s → %s" % (_bstr(_bs), _bstr(_be))) if _bs and _be else "所选日 00:00 → 当前"
    lines = [
        "平台：dtt 关键词监控（Twitter/X · Telegram · Discord）",
        "日期：%s（北京时间 %s）" % (payload.get("day", ""), _range),
        "当日命中事件 %d 条（命中 SN %d 条；Twitter %s / Telegram %s / Discord %s）"
        % (st.get("total", 0), st.get("with_sn", 0),
           (st.get("by_platform") or {}).get("twitter", 0),
           (st.get("by_platform") or {}).get("telegram", 0),
           (st.get("by_platform") or {}).get("discord", 0)),
        "",
        "【子网名称对照（写「编号+名字」时用，查不到就只写编号）】",
        "  ".join("SN%d %s" % (s, names[s]) for s in ordered if s in names) or "（暂无）",
        "",
        "===== 按子网分组的当日消息（每条已标平台+发言人；时间按北京时间）=====",
    ]
    for sn in ordered:
        nm = names.get(sn, "")
        head = "SN%d%s（%d 条）" % (sn, (" " + nm) if nm else "", len(by_sn[sn]))
        lines.append("\n--- %s ---" % head)
        seen = set()
        n = 0
        sn_lines, sn_plats = [], set()
        for e in sorted(by_sn[sn], key=lambda x: x.get("occurredAt") or 0, reverse=True):
            key = (e.get("author"), e.get("content", "")[:80])
            if key in seen:
                continue
            seen.add(key)
            t = e.get("occurredAt")
            tstr = datetime.datetime.fromtimestamp(t / 1000, TZ).strftime("%H:%M") if t else "??:??"
            plat = PLAT_CN.get(e.get("platform"), e.get("platform") or "未知")
            author = _clean(_display_author(e.get("author"), cfg), 40)
            content = _clean(e.get("content"))
            line = "- [%s][%s] <%s> %s" % (tstr, plat, author, content)
            lines.append(line)
            sn_lines.append(line)
            sn_plats.add(plat)
            n += 1
            if n >= MAX_PER_SN:
                lines.append("  …（该子网另有 %d 条同类消息已省略）" % (len(by_sn[sn]) - n))
                break
        # 落档摘录： subnet 名 + 原文摘录（本地档案供投研回看线索）
        sn_excerpts[sn] = {
            "excerpt": "SN%d%s（%d 条社区消息）\n%s" % (
                sn, (" " + nm) if nm else "", len(by_sn[sn]), "\n".join(sn_lines)),
            "platforms": sorted(sn_plats),
        }

    if generic:
        lines.append("\n--- 未归到具体 SN 的通用消息（取前 %d 条）---" % MAX_GENERIC)
        seen = set()
        n = 0
        for e in sorted(generic, key=lambda x: x.get("occurredAt") or 0, reverse=True):
            key = (e.get("author"), e.get("content", "")[:80])
            if key in seen:
                continue
            seen.add(key)
            t = e.get("occurredAt")
            tstr = datetime.datetime.fromtimestamp(t / 1000, TZ).strftime("%H:%M") if t else "??:??"
            plat = PLAT_CN.get(e.get("platform"), e.get("platform") or "未知")
            author = _clean(_display_author(e.get("author"), cfg), 40)
            lines.append("- [%s][%s] <%s> %s" % (tstr, plat, author, _clean(e.get("content"))))
            n += 1
            if n >= MAX_GENERIC:
                break

    # 关键人物（创始人等）的全部消息：转推/推文/回复逐条列出，不筛、不截断、不受
    # MAX_GENERIC / MAX_PER_SN 限制。之前 const 的推文被「英文长文无关键词」规则剔除，
    # 这里单列一段兜底，日报必须逐条体现。
    key_names = _key_author_names(cfg)
    key_lines, _seen_key = [], set()
    for e in sorted(events, key=lambda x: x.get("occurredAt") or 0, reverse=True):
        if not _key_author_hit(e, cfg):
            continue
        k = (e.get("author"), (e.get("content") or "")[:80])
        if k in _seen_key:
            continue
        _seen_key.add(k)
        t = e.get("occurredAt")
        tstr = datetime.datetime.fromtimestamp(t / 1000, TZ).strftime("%H:%M") if t else "??:??"
        plat = PLAT_CN.get(e.get("platform"), e.get("platform") or "未知")
        key_lines.append("- [%s][%s] <%s>（%s） %s"
                         % (tstr, plat, _clean(_display_author(e.get("author"), cfg), 40),
                            _msg_kind(e, cfg), _clean(e.get("content"))))
    if key_lines:
        lines.append("\n===== 关键人物全部消息（%s：推文/转推/回复逐条列出，日报必须全部体现）====="
                     % "、".join("@" + x for x in key_names))
        lines.extend(key_lines)

    # 当日全部人物 + 消息：不做重要性筛选，人名和原文都给模型，
    # 由日报「人物与观点」节逐个点名。用户后续可针对具体人物再要求优化。
    all_lines, _seen_all = [], set()
    for e in sorted(events, key=lambda x: x.get("occurredAt") or 0, reverse=True):
        k = (e.get("author"), (e.get("content") or "")[:80])
        if k in _seen_all:
            continue
        _seen_all.add(k)
        all_lines.append("- <%s> %s" % (_clean(e.get("author") or "?", 40),
                                         _clean(e.get("content"))))
    if all_lines:
        lines.append("\n===== 当日全部人物与消息（人名+原文，共 %d 条；日报人物节须逐个点名，"
                     "不得按「普通群友」删人）=====" % len(all_lines))
        lines.extend(all_lines)

    # 附历史档案/投研参考块（复用 v1.17.0 的交叉核对能力）
    try:
        import pipeline as P
        ref = P.build_research_ref_block(cfg or P._CFG or {}, [], day_str=payload.get("day"))
        # 上面的 items 为空时可能不产出；构造带 SN 的伪 item 触发
        if not ref:
            fake = [("", "", "", "SN%d" % s) for s in ordered[:8]]
            ref = P.build_research_ref_block(cfg or P._CFG or {}, fake, day_str=payload.get("day"))
        if ref:
            lines.append("\n===== 历史档案/投研参考（历史快照，可能过时；与今日矛盾要标⚠️待人工确认）=====\n" + ref)
    except Exception as e:
        pass

    return "\n".join(lines), ordered, sn_excerpts


def _out_dir(cfg=None):
    d = (cfg or {}).get("community_output_dir") or "exports/community"
    return d if os.path.isabs(d) else os.path.join(HERE, d)


def run_community_day(cfg, day_str, log=print, push=True):
    """主流程：取数 -> 生成 -> 交叉核对 -> 双推。返回 {ok, day, digest, ...}。"""
    import pipeline as P

    day_str = P._resolve_day_alias(day_str, TZ)
    day_label = ("昨日" if day_str == (datetime.datetime.now(TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                 else ("今日" if day_str == datetime.datetime.now(TZ).strftime("%Y-%m-%d") else day_str))
    # 1) 取数：窗口是「所选日 00:00 → 当前触发时刻」，缓存必须覆盖到当下才可复用，
    #    否则重取（命令每次触发的时间不同，旧快照会漏掉临近时刻的新消息）。
    payload = CF.read_day(day_str, cfg)
    if payload is not None and CF.cache_covers_now(payload, cfg):
        log("社区日报：复用已取数的 %s（%d 条，边界覆盖到当前），未重复请求 dtt"
            % (day_str, payload.get("stats", {}).get("total", 0)))
    else:
        if payload is not None:
            log("社区日报：%s 旧快照边界未覆盖当前时刻，重取中…" % day_str)
        payload = CF.fetch_day(day_str, cfg, log=log, until_now=True)

    if not (payload.get("events") or []):
        digest = "📭 %s 社区关键词监控当日无命中消息。" % day_str
        return {"ok": True, "day": day_str, "digest": digest, "empty": True}

    # 2) 构建输入并生成
    text_block, sns, sn_excerpts = build_input(payload, cfg)
    log("社区日报：按 SN 分组完成（覆盖 %d 个子网），调用 AI 生成…" % len(sns))
    digest = P.call_llm(cfg, text_block, system_prompt=COMMUNITY_SYSTEM_PROMPT)
    # 注意：这里**不调用** polish_digest / compress_digest —— 那两个函数内置 DC 日报的
    # 「必读重点/人物观点/其他细节」板块规范化，会把社区日报的「社区热点/子网动态」改写掉。
    # 社区日报用自己的提示词控结构，只做基础清洗。
    digest = P.sanitize_digest(digest)

    # 3) 交叉核对：把散落在正文各处的「待人工确认」抽出来，置顶成独立专区。
    #    之前只在顶部写一句泛泛提示、冲突仍埋在长文条目里，群里根本看不见；
    #    现在正文里的冲突行被移走、集中进「⚠️ 待人工确认」专区，一眼可见。
    flags = [ln.strip() for ln in (digest or "").splitlines() if "待人工确认" in ln]
    if flags:
        log("⚠️⚠️ 社区日报交叉核对：%d 处「待人工确认」（社区消息与历史结论冲突）：%s"
            % (len(flags), " | ".join(flags[:5])))
        body, _ = extract_conflicts(digest)
        digest = build_conflict_section(flags) + "\n\n" + body
    else:
        log("✅ 社区日报交叉核对通过（未发现与历史结论的明显冲突）")

    # 4) 落盘（与 DC 日报分开命名）
    out_dir = _out_dir(cfg)
    os.makedirs(out_dir, exist_ok=True)
    dp = os.path.join(out_dir, "community_%s.digest.txt" % day_str)
    with open(dp, "w", encoding="utf-8") as f:
        f.write(digest.strip())
    # 原始冲突行存 sidecar：确认版重推时据此重渲染专区（带人工确认状态），
    # 不必重新调 AI / 重新取数，避免确认内容对不上或被改写。
    cp = os.path.join(out_dir, "community_%s.conflicts.json" % day_str)
    try:
        with open(cp, "w", encoding="utf-8") as f:
            json.dump({"day": day_str, "conflicts": flags}, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("冲突 sidecar 写入失败（不影响推送）：%s" % e)

    # 4b) 按 SN 落进各子网档案的 community_digests[]（供投研回看社区线索）。
    #     与 reports[]（投研结论）分开存，投研侧用独立段落读取并要求交叉核对。
    archived = 0
    try:
        import subnet_archive as SA
        rel_dp = os.path.relpath(dp, HERE).replace("\\", "/")
        for sn in sns:
            ex = sn_excerpts.get(sn)
            if not ex:
                continue
            try:
                SA.attach_community_digest(
                    cfg, day_str, sn, ex["excerpt"],
                    platforms=ex.get("platforms"), source_file=rel_dp, log=log)
                archived += 1
            except Exception as e:
                log("社区日报落档 SN%d 失败（忽略）：%s" % (sn, e))
        log("社区日报落档完成：%d 个子网写入社区线索" % archived)
    except Exception as e:
        log("社区日报落档跳过（不影响推送）：%s" % e)

    # 5) 双推
    title = "🌐 Bittensor 社区消息日报（dtt版）· %s（%s）" % (day_str, day_label)
    if flags:
        title += " ⚠️待人工核对"
    if push:
        log("社区日报推送：飞书 + TG%s" % ("（标题已打待核对标记）" if flags else ""))
        try:
            P.push_feishu(cfg, title, digest)
        except Exception as e:
            log("社区日报飞书推送失败（不影响 TG）：%s" % e)
        try:
            P.push_telegram(cfg, title, digest)
        except Exception as e:
            log("社区日报 TG 推送失败：%s" % e)

    return {"ok": True, "day": day_str, "digest": digest, "sn": sns,
            "flags": len(flags), "path": dp, "archived_sn": archived,
            "total": payload.get("stats", {}).get("total", 0)}


def push_confirmed_version(cfg, day_str, log=print, push=True):
    """按档案里的人工确认，重建某日社区日报的专区并重推「确认版」。

    不重新调 AI、不重新取数——用 sidecar 里保存的原始冲突行 + 档案确认状态，
    重渲染专区替换掉落盘 digest 的旧专区，再双推。这样确认内容不会被 AI 改写。

    返回 {ok, day, confirmed_count, path, digest}；无冲突或无确认时 ok=False 并给 reason。
    """
    import pipeline as P
    import subnet_archive as SA

    day_str = P._resolve_day_alias(day_str, TZ)
    out_dir = _out_dir(cfg)
    dp = os.path.join(out_dir, "community_%s.digest.txt" % day_str)
    cp = os.path.join(out_dir, "community_%s.conflicts.json" % day_str)
    if not os.path.exists(dp):
        return {"ok": False, "reason": "找不到 %s，请先生成该日社区日报" % dp}
    with open(dp, encoding="utf-8") as f:
        digest = f.read().strip()
    conflicts = []
    if os.path.exists(cp):
        try:
            with open(cp, encoding="utf-8") as f:
                conflicts = json.load(f).get("conflicts") or []
        except Exception as e:
            log("读取冲突 sidecar 失败：%s" % e)
    if not conflicts:
        # 兼容旧日报（sidecar 之前生成的）：从落盘文本里反推冲突行
        conflicts = [ln.strip() for ln in digest.splitlines() if "待人工确认" in ln]
    if not conflicts:
        return {"ok": False, "reason": "%s 没有待人工确认的条目，无需确认版" % day_str}

    confirms = SA.get_community_confirms(cfg, day_str)
    confirmed_count = len(confirms)
    if not confirmed_count:
        return {"ok": False,
                "reason": "%s 还没有任何人工确认（发 /社区确认 SN编号 结论 后再重推）" % day_str,
                "conflicts": len(conflicts)}

    # 用原始冲突行 + 档案确认，重渲染专区
    new_section = build_conflict_section(conflicts, confirms)
    # 从落盘 digest 中剥离旧专区：旧专区从开头到第一个板块标题（**🔥 / **🧩 等）
    body = digest
    m = re.search(r"^\*\*⚠️ 待人工确认", body)
    if m:
        rest = body[m.end():]
        # 找下一个加粗板块标题行作为正文起点
        nm = re.search(r"(?m)^\**(?:🔥|🧩|👀|💡|📌)", rest)
        body = rest[nm.start():].strip() if nm else rest.strip()
    new_digest = new_section + "\n\n" + body

    with open(dp, "w", encoding="utf-8") as f:
        f.write(new_digest.strip())
    log("社区日报确认版重建：%s（%d 处冲突，%d 处已人工确认）" % (day_str, len(conflicts), confirmed_count))

    day_label = ("昨日" if day_str == (datetime.datetime.now(TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                 else ("今日" if day_str == datetime.datetime.now(TZ).strftime("%Y-%m-%d") else day_str))
    title = "🌐 Bittensor 社区消息日报（dtt版）· %s（%s · ✅人工确认版）" % (day_str, day_label)
    if push:
        try:
            P.push_feishu(cfg, title, new_digest)
        except Exception as e:
            log("确认版飞书推送失败（不影响 TG）：%s" % e)
        try:
            P.push_telegram(cfg, title, new_digest)
        except Exception as e:
            log("确认版 TG 推送失败：%s" % e)
        log("社区日报确认版已推送：飞书 + TG")
    return {"ok": True, "day": day_str, "confirmed_count": confirmed_count,
            "conflicts": len(conflicts), "path": dp, "digest": new_digest}


if __name__ == "__main__":
    import sys
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    day = sys.argv[1] if len(sys.argv) > 1 else (
        datetime.datetime.now(TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    push = "--no-push" not in sys.argv
    res = run_community_day(cfg, day, push=push)
    print("完成：", json.dumps({k: v for k, v in res.items() if k != "digest"},
                               ensure_ascii=False)[:400])
