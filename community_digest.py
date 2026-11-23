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
# 来源简写（v1.27.3）：社区动态里来源直接写 (X)/(dc)/(tg)，不再写全称/中文平台名
PLAT_SHORT = {"twitter": "X", "x": "X", "telegram": "tg", "discord": "dc", "manual": "手动"}


def _plat_short(platform):
    """平台 → 简写 X / dc / tg；未知平台原样返回（小写）。"""
    return PLAT_SHORT.get(str(platform or "").lower(), str(platform or "?"))

# 单条消息正文截断（社区里长文很多，截断防止输入爆炸）
MAX_MSG_CHARS = 320
# 每个 SN 最多喂多少条（防止 24 号那种 119 条灌爆）
MAX_PER_SN = 12
# 最多覆盖多少个 SN（按消息数排序）
MAX_SN = 18
# 无 SN 归属但值得看的通用消息最多多少条
MAX_GENERIC = 12
# 创始人/核心交易者等关键人物：dtt 里的转推/推文/回复一律保留，不参与噪声剔除与条数截断
DEFAULT_KEY_AUTHORS = ["const", "5gurl", "fish", "orange1004"]

# dtt 把 Discord 消息抓成「人名,备注:频道名」的形态，如 5gurl,备注:dtao / orange1004,备注:rrik。
# 这些备注是**频道标签**不是人名，若按「备注优先」取会把多人合并成 dtao、丢失真实发言人。
# 备注命中本集合时退回取逗号前的真实人名。const 的备注（备注:const）是真人名，不在此列。
_CHANNEL_TAG_NOTES = {"dtao", "rrik", "price-talk", "price talk", "general", "chat"}

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
    author = (e.get("author") or "").lower()
    content = (e.get("content") or "").lower()
    for n in names:
        # 名字边界：const 可匹配 const / const_reborn / consttt，不匹配 constantly
        pat = r"(?<![a-z0-9])" + re.escape(n) + r"(?:_[a-z0-9]+|tt|[0-9]{2,6})?(?![a-z])"
        # 作者字段命中 → 一定是关键人物本人的发言，直接算
        if re.search(pat, author):
            return True
        # 正文提及：仅对「专名型」关键人物（const / 5gurl / orange1004）算，
        # 通用词 fish 不按正文匹配——否则「mango got a lil fish」「烤全鱼」都会被误当成 fish 的重要发言。
        if n not in ("fish",) and re.search(pat, content):
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


def _is_bt_related(e, cfg=None):
    """二值判断：这条消息是否「有动态且与 Bittensor 子网相关」（人物齐全的准入条件）。

    注意不能用 `_relevance() < 0` 代替——那是**排序分**，带「英文长文/RT 腔调」惩罚，
    实测会把 141 条带 SN 归属的明确相关消息（如「@NiomeAI is expanding sn55…」）误杀。
    这里的口径按用户 v1.22.8 定义，宽松得多，命中任一即算相关：
      1. dtt 已标注 SN 归属（关键词监控本身就命中了子网）；
      2. 内容含 Bittensor/子网 关键词或 SN 编号；
      3. 发言人是关键人物（const/fish/5gurl 等，其动态一律算相关）；
      4. 内容含中文（中文频道几乎都是 Bittensor 圈内讨论）。
    只有以上都不满足（既无 SN 归属、又无 BT 词、纯英文垃圾/无关推文）才剔除。
    """
    if e.get("sn"):
        return True
    c = (e.get("content") or "").strip()
    return bool(_BT_KW.search(c) or _SN_TAG.search(c) or _CN_RE.search(c)
                or _key_author_hit(e, cfg))


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


def strip_name_dump(digest, log=print):
    """清理「人名堆砌串」垃圾行。

    背景：提示词既要求「当日全部人物逐个点名」又限 3000 字，人一多（如 11-21 有
    268 个发言人）模型就会把写不下的名字用顿号硬串成一行：
        - **Bunty、Swif、Tungfa、…、T**
    里面混着项目官方账号名（Vanta/Chutes/Beam…）、末尾还被字数截断，
    既不是观点也不是子网动态，没有任何信息价值。这里在渲染前兜底删掉这类行。

    判定：一行以 `- **` 或 `- ` 开头，加粗段（或整行）被顿号切成 >=8 段，
    且这些段多是 2-16 字符的短名字（不含句子标点/动词），则认为是堆砌串而非正常观点。
    正常人物行形如 `- **const**：连发多推…`（加粗段只有一个人名，观点在冒号后），不会误伤。
    """
    if not digest:
        return digest
    kept, dropped, condensed = [], [], 0

    def _name_parts(text):
        return [p.strip() for p in re.split(r"[、,，]", text) if p.strip()]

    def _is_dump(parts):
        return len(parts) >= 8 and sum(1 for p in parts if len(p) <= 20) >= len(parts) * 0.8

    for ln in digest.splitlines():
        s = ln.strip()
        # 情形①：「- **A、B、C…**」 或 「- A、B、C…」——整行就是人名串，直接删。
        # 只看开头到第一个 **： 或闭合 ** 之间的名字段；垃圾串常被字数截断、末尾无闭合 **。
        if s.startswith("- **"):
            head = s[3:]
            ends = [p for p in (head.find("**："), head.find("**")) if p >= 0]
            name_field = head[:min(ends)] if ends else head
            parts = _name_parts(name_field)
            if _is_dump(parts):
                dropped.append((len(parts), len(s)))
                continue
        elif s.startswith("- "):
            parts = _name_parts(s[2:])
            if _is_dump(parts):
                dropped.append((len(parts), len(s)))
                continue
        # 情形②：「另有 **A、B、C…** 等：多为转推…」——归并说明里夹带超长人名列表。
        # 保留说明文字，把人名列表换成「数十位」这类数量表述。
        m = re.match(r"^(另有|其余)\s*\*\*(.+?)\*\*\s*(等.*)?$", s)
        if m and _is_dump(_name_parts(m.group(2))):
            n = len(_name_parts(m.group(2)))
            tail = (m.group(3) or "").strip()
            qty = "数十位" if n >= 40 else "十余位" if n >= 12 else "%d 位" % n
            kept.append("%s %s%s" % (m.group(1), qty, (" " + tail) if tail else ""))
            condensed += 1
            continue
        # 情形③：无加粗的「另有 A、B、C… 等：…」——同样压缩
        m2 = re.match(r"^(另有|其余)\s+(.+?)\s*等[：:，,]", s)
        if m2 and _is_dump(_name_parts(m2.group(2))):
            n = len(_name_parts(m2.group(2)))
            qty = "数十位" if n >= 40 else "十余位" if n >= 12 else "%d 位" % n
            rest = s[m2.end():]
            kept.append("%s %s等：%s" % (m2.group(1), qty, rest))
            condensed += 1
            continue
        kept.append(ln)
    if dropped or condensed:
        log("社区日报：清理 %d 处人名堆砌串（共 %d 个名字），压缩 %d 处归并说明"
            % (len(dropped), sum(n for n, _ in dropped), condensed))
        return "\n".join(kept)
    return digest


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
    n_done = n_confirmed + n_refuted
    if n_done and n_done >= len(conflicts):
        label = "✅ 冲突已人工确认"
        title_bits.append("核实 %d / 推翻 %d，以人工结论为准" % (n_confirmed, n_refuted))
    elif n_done:
        label = "⚠️ 部分待人工确认"
        title_bits.append("已确认 %d 处（核实 %d / 推翻 %d），其余请核对后再采信"
                          % (n_done, n_confirmed, n_refuted))
    else:
        label = "⚠️ 待人工确认"
        title_bits.append("请核对后再采信")
    header = "**%s（%s）**\n\n" % (label, "，".join(title_bits))
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


def conflicts_all_confirmed(flags, confirms):
    """冲突专区里点名的 SN 是否全部已有人工确认（决定标题是否还打「待核对」）。"""
    if not flags or not confirms:
        return False
    sns = []
    for c in flags:
        m = re.search(r"SN\s*(\d+)", c, re.I)
        if m:
            sns.append(int(m.group(1)))
    return bool(sns) and all(s in confirms for s in sns)


COMMUNITY_SYSTEM_PROMPT = (
    "你是一名 Bittensor 生态资深研究员，负责把「多平台社区关键词监控」抓到的当日消息，"
    "整理成一份**按子网分组**的中文社区日报。数据来源是 dtt 关键词监控工具，"
    "聚合了 Twitter/X、Telegram、Discord 上命中 Bittensor 关键词的公开消息。\n"
    "\n"
    "【社区情绪优先：关键人物的观点是核心信号】\n"
    "Bittensor 价格与预期高度依赖社区情绪，核心交易者/团队方（输入里标为关键人物，"
    "如 5GuRL、Fish、orange 等）的判断、质疑、看多看空理由是当天最有价值的信息，"
    "**必须逐条提炼出真正观点，严禁当普通群友过滤、合并成「dtao 群友」或一句带过**。\n"
    "- 词必达意：把零散的中英夹杂发言串成完整意思，写清他到底看多/看空、依据是什么、指向哪个子网或机制；"
    "关键利好（如排放导入 lium 换 TAO 回购销毁、每天买回销毁多少）必须讲清「谁把什么导向哪里、预期效果」，"
    "不要只写「已准备接入」这种没头没尾的表述；原文是玩笑/调侃就如实写，不要拔高成严肃观点。\n"
    "- 人名照抄输入 `<>` 里的显示名（已是规范名），不得把不同人合并、不得写频道名当人名。\n"
    "\n"
    "【铁律：不可全信，逐条标注来源】\n"
    "- 这些是**社区公开消息**，混有传闻、喊单、转发、观点、未证实爆料。一律按「有人说」处理，"
    "不要写成既定事实。每条必须标注来源平台（Twitter/X / Telegram / Discord）与发言人。\n"
    "- **禁止编造**：不补原文没有的数字、人名、SN 编号、交易细节；原文没有就说没有。\n"
    "- 只关注与 Bittensor 子网、代币机制、团队动态、上线/合作/催化相关的实质内容；"
    "纯水聊、无关转发、纯表情、与 Bittensor 无关的话题一般不列；"
    "**但关键人物（如创始人 const）的推文、转推、回复一律保留，不得当成无关转发删掉**。\n"
    "- **「齐全」的前提 = 当天有动态（发推/转推/评论/回复）且与 Bittensor 子网相关**。"
    "满足这个前提的发言人，输入末尾「当日全部人物与消息」已为你筛好并逐个列出，"
    "**必须全部点名**（有观点写观点，只有一句话就写「仅发言：…」），不许以「普通群友」为由省略。\n"
    "- **不满足前提的不算「人」，不进人物节**：纯表情、纯附和（GM/👌/收到）、"
    "纯转推无观点、与 Bittensor 无关的误抓内容（八卦/闲聊/ AI 泛谈）、项目官方账号与机器人的例行播报，"
    "这些已在输入里剔除。若确实要收尾说明，**只报数量、绝不列举人名**"
    "（正确：「另有数十位仅表情/附和/例行播报，不逐条列出」；错误：「另有 Infinite、Carbon、Almanac… 等」）。\n"
    "- **绝对禁止在人物节堆人名串**（「- **A、B、C…**」或「另有 A、B、C… 等」都算）："
    "无论加粗还是归并说明里，都不许把一串人名用顿号列出——那不是观点、没有信息量，"
    "还会混入项目名并被截断。归并说明只能写数量，一个具体人名都不列。\n"
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
    "与 subnet 无关的日常发言不列。其他满足「有动态且跟 Bittensor 相关」的人一律列人名+消息要点，"
    "不做重要性筛选；确实无人发言才写「无」）\n"
    "　用户名写法：输入里 `<>` 内已是规范显示名（备注名优先，如 `const`、`p383`），**直接用、不要改**；"
    "若遇到 `名字_四位数字`（如 `p383_54249`）去掉 `_数字` 后缀写成 `p383`，"
    "遇到 `名字@handle` 只取 `名字`，遇到 `备注:xxx` 只取 `xxx`；"
    "原文若有真实姓名/公开别名（如 Isabella Liu）则用真名。禁止出现 @handle/辨识码/备注标签。\n"
    "　**真人 vs 项目号**：Vanta/Chutes/Beam/Subnetradar/TAO.com 等是项目或媒体官方账号，"
    "其**实质动态归入「🧩 子网动态」**，人物节点名时按显示名写但不逐个凑数；"
    "纯表情/附和/例行播报按上面的归并说明处理，绝不堆成人名串。\n"
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
    "- **字数上限 3000 字**（含标点），优先保证「有动态且与 Bittensor 相关」的人物与 const 消息齐全；"
    "人太多写不下时用归并说明收尾，**不许为凑齐人名把名字串成一行、也不许截半个人名**。"
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


# 装饰字符：希腊字母(ταοεκ…)、emoji、方向/杂项符号(✠🌊→…)、变体选择符
_DECOR_RE = re.compile(r"[\u0370-\u03ff\u2190-\u2bff\U0001f000-\U0001faff\ufe0f\u200d]")


def _display_author(author, cfg=None):
    """取简洁显示名：备注名优先，去掉账号后缀/辨识码/emoji/希腊字母装饰。

    dtt 的 author 字段常见形态（实测 11-22）：
      consttt,备注:const      → const        （监控把人名塞进备注）
      const@const_reborn      → const        （显示名@账号，取 @ 前）
      canigonow,备注:17       → 17
      p383_54249              → p383         （显示名_辨识码）
      Aphτ 🌊α                → Aph          （emoji + 希腊字母装饰）
      Chairman τao            → Chairman
      Rayah τensor τ_τ        → Rayah
      ✠ PUNISHER ττ ✠         → PUNISHER
      Millie@AltcoinMillie    → Millie
    用户要求：人名要简单，直接用备注名/显示名，不要原始账号名和装饰符号。
    """
    a = (author or "").strip()
    m = re.search(r"备注[:：]\s*([^,，\s]+)", a)
    if m:
        note = m.group(1).strip()
        # 备注是频道名（dtao/rrik…）而非人名 → 取逗号前的真实人名，避免多人被合并成 dtao
        if note.lower() in _CHANNEL_TAG_NOTES:
            a = re.split(r"[,，]", a, 1)[0].strip()
        else:
            return _simplify_name(note)
    a = a.split("@")[0].strip()          # 显示名@账号 → 取显示名
    a = re.sub(r"_\d{2,6}$", "", a).strip()  # 名字_辨识码 → 取名字
    name = _simplify_name(a)
    return _display_alias(name, cfg)


def _display_alias(name, cfg=None):
    """账号 → 用户指定显示名（config.author_display_alias，如 5gurl/baobab→iuac、orange→rrik）。"""
    m = (cfg or {}).get("author_display_alias") or {}
    if not isinstance(m, dict) or not m:
        return name
    na = re.sub(r"_\d{2,6}$", "", (name or "").strip().lower())
    for k, v in m.items():
        if na == str(k).strip().lower():
            return str(v).strip()
    return name


def _simplify_name(a):
    """去掉 emoji / 希腊字母 / 装饰符号 / `_装饰` 尾巴；有装饰时取最长的干净词。"""
    a = (a or "").strip()
    clean = _DECOR_RE.sub(" ", a)
    clean = re.sub(r"[|｜]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        clean = a
    # 有装饰被去掉 → 名字里多个词时取最像名字的那个（通常第一个实质词）
    toks = [t for t in clean.split() if t]
    if not toks:
        return clean or "?"
    if len(toks) > 1:
        # "Chairman ao" / "Aph" / "PUNISHER TT"：取第一个词，避免 τao→ao 这种碎片
        return toks[0]
    return clean


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

    # 当日全部人物 + 消息：这里是「人物节必须齐全」的清单。用户口径（v1.22.8）：
    #   **齐全 = 当天有动态（发推/转推/评论/回复）且内容与 Bittensor 子网相关**。
    # 纯表情/附和、与 Bittensor 无关的误抓消息不算「动态」，不进这个清单——否则
    # 268 个发言人塞不下，模型就把名字串成一行凑数（v1.22.7 踩过的坑）。
    # 用 _relevance >= 0 做二次过滤（关键人物 const/fish 等豁免保留）。
    # 「齐全」是**人**齐全，不是每条消息都列：同一发言人多条时只留最有信息量的几条，
    # 否则 208 个作者 × 多条 = 733 条全塞进去，输入近万字、输出 5000 token 触顶被截断
    # （11-21 实测连「一句话总结」都丢了）。关键人物可多留，普通人每人至多 2 条。
    _related = [e for e in events if _is_bt_related(e, cfg)]
    _skipped = len(events) - len(_related)
    by_author = {}
    for e in _related:
        by_author.setdefault(_display_author(e.get("author"), cfg), []).append(e)
    all_lines, _n_people = [], 0
    for author, es in by_author.items():
        _n_people += 1
        # 每人按 _relevance 降序（信息量高优先）、同分按时间新优先，只留代表性发言。
        # 输入要紧凑：普通人 1 条、关键人物最多 3 条，每条压到 120 字符——
        # 208 人全出现即可（"齐全"=人齐），不必把每人所有发言堆进输入撑爆上下文。
        es.sort(key=lambda x: (-_relevance(x, cfg), -(x.get("occurredAt") or 0)))
        cap = 3 if any(_key_author_hit(x, cfg) for x in es) else 1
        for e in es[:cap]:
            all_lines.append("- <%s> %s" % (_clean(author, 30), _clean(e.get("content"), 120)))
    if all_lines:
        lines.append("\n===== 当日全部人物与消息（%d 位发言人，%d 条代表发言；"
                     "已剔除 %d 条与 Bittensor 无关的噪声）=====\n"
                     "以下每位都是「当天有动态且与 Bittensor 子网相关」的发言人，"
                     "每人已留其最有信息量的发言（关键人物多留），人物节须逐个点名这 %d 位；"
                     "禁止把人名串成一行。"
                     % (_n_people, len([l for l in all_lines if l.startswith("- <")]),
                        _skipped, _n_people))
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
    # 社区日报要逐个点名所有相关发言人，输出天然比 DC 日报长；用全局默认 max_tokens
    # （本地 5000）会被截断、连「一句话总结」都丢掉。这里只给社区日报临时提高上限，
    # 不动全局配置（用 cfg 副本，避免污染其它任务）。
    gen_cfg = dict(cfg)
    gen_cfg["deepseek_max_tokens"] = max(int(cfg.get("deepseek_max_tokens", 2500) or 2500), 8000)
    digest = P.call_llm(gen_cfg, text_block, system_prompt=COMMUNITY_SYSTEM_PROMPT)
    # 注意：这里**不调用** polish_digest / compress_digest —— 那两个函数内置 DC 日报的
    # 「必读重点/人物观点/其他细节」板块规范化，会把社区日报的「社区热点/子网动态」改写掉。
    # 社区日报用自己的提示词控结构，只做基础清洗。
    digest = P.sanitize_digest(digest)
    # 兜底：清理模型在字数压力下吐出的「人名堆砌串」（无观点的纯名字列表）
    digest = strip_name_dump(digest, log=log)

    # 3) 交叉核对：把散落在正文各处的「待人工确认」抽出来，置顶成独立专区。
    #    之前只在顶部写一句泛泛提示、冲突仍埋在长文条目里，群里根本看不见；
    #    现在正文里的冲突行被移走、集中进「⚠️ 待人工确认」专区，一眼可见。
    flags = [ln.strip() for ln in (digest or "").splitlines() if "待人工确认" in ln]
    # 关键：读档案里**此前已人工确认**的结论一并渲染。否则用户确认过的冲突，
    # 下次重跑社区日报又会从零标成「待人工确认」（2026-11-22 SN55 踩过这个坑）。
    confirms = {}
    try:
        import subnet_archive as SA
        confirms = SA.get_community_confirms(cfg, day_str) or {}
    except Exception as e:
        log("读取档案人工确认失败（不影响生成）：%s" % e)
    if flags:
        log("⚠️⚠️ 社区日报交叉核对：%d 处「待人工确认」（社区消息与历史结论冲突）：%s"
            % (len(flags), " | ".join(flags[:5])))
        if confirms:
            log("   其中 %d 个 SN 此前已人工确认，专区将直接标注核实/推翻结论" % len(confirms))
        body, _ = extract_conflicts(digest)
        digest = build_conflict_section(flags, confirms=confirms) + "\n\n" + body
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
                if SA.is_retired_sn(sn):
                    log("社区日报落档 SN%d 跳过（已注销归档）" % sn)
                    continue
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
    all_confirmed = conflicts_all_confirmed(flags, confirms)
    if flags:
        title += " ✅人工已确认" if all_confirmed else " ⚠️待人工核对"
    # 头部范围用本次 dtt 取数的真实边界（所选日 00:00 → 当前触发时刻），
    # 不能让推送层按标题日期去查 DC 日报的边界（v1.30.1 修复）。
    rng = P.bounds_range_text(payload.get("bounds"))
    if push:
        log("社区日报推送：飞书 + TG%s（覆盖 %s）"
            % ("（标题已打待核对标记）" if flags and not all_confirmed
               else "（标题标注人工已确认）" if all_confirmed else "", rng or "未记录"))
        try:
            P.push_feishu(cfg, title, digest, range_text=rng)
        except Exception as e:
            log("社区日报飞书推送失败（不影响 TG）：%s" % e)
        try:
            P.push_telegram(cfg, title, digest, range_text=rng)
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
    # 兼容三种专区标题（全部确认 / 部分确认 / 原先的待确认），避免重推时旧专区没被剥离而叠加
    m = re.search(r"^\*\*(?:✅ 冲突已人工确认|⚠️ 部分待人工确认|⚠️ 待人工确认)", body)
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
    # 确认版不重新取数，但仍要显示社区日报自身的取数边界（从当日缓存读），
    # 不能回落到 DC 日报的 range（v1.30.1）。
    cached = CF.read_day(day_str, cfg)
    rng = P.bounds_range_text((cached or {}).get("bounds"))
    if push:
        try:
            P.push_feishu(cfg, title, new_digest, range_text=rng)
        except Exception as e:
            log("确认版飞书推送失败（不影响 TG）：%s" % e)
        try:
            P.push_telegram(cfg, title, new_digest, range_text=rng)
        except Exception as e:
            log("确认版 TG 推送失败：%s" % e)
        log("社区日报确认版已推送：飞书 + TG（覆盖 %s）" % (rng or "未记录"))
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
