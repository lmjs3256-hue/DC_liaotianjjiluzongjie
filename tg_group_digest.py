#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TG 群消息日报（dtt 管理员群消息库）提炼层
==========================================================
与 DC 日报 / dtt 关键词社区日报**完全隔离**：独立数据源、提示词、落盘、命令，
只复用 pipeline 的 AI 调用 / 清洗 / 飞书·TG 推送层，以及 community_digest 的
辅助函数（别名映射 / 关键人物判定 / SN 名称对照）。

数据流：
    tg_group_fetch.fetch_day()  →  build_input()（严格相关筛选 + 按群分组）
    →  call_llm（独立提示词）  →  sanitize  →  push_feishu / push_telegram

对外接口：
    run_tg_group_day(cfg, day_str, log=print, push=True) -> dict
"""

import datetime
import json
import os
import re

import community_fetch as CF
import tg_group_fetch as TGF

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = datetime.timezone(datetime.timedelta(hours=8))

MAX_MSG_CHARS = 300       # 单条正文截断
MAX_PER_GROUP = 30        # 每群最多喂多少条（严格相关后）
MAX_KEY_PERSON = 12       # 关键人物最多保留多少条

# 复用 dtt 关键词社区日报的正则 / 关键人物 / 别名逻辑
import community_digest as CD


# 官方机器人转发号：TAO-update-DC 群的全部消息都是这个 bot 发的 ⛳sn:NN 动态，
# 属于"项目/机器人例行播报"，按用户口径不算「人」，但实质动态要归到子网动态。
_BOT_AUTHORS = {"tao-update-dc", "bot", "tensorscope_bot"}

# 噪音：机器人斜杠指令、纯链接、纯表情/GM、纯 @提及、"正在输入"占位
_NOISE_PAT = re.compile(
    r"(?i)^\s*(/(chart|openinterest|price|help|start)\b|https?://\S+\s*$|"
    r"gm\b|gn\b|收到|👌|👍|🙏|用户正在输入|typing\.\.\.)"
)
_LINK_ONLY = re.compile(r"^\s*(https?://\S+\s*)+$")


def _author_name(author):
    """群消息作者 `群名@用户名` → 取 @ 后的真实用户名，去装饰/辨识码，再走别名映射。

    群消息格式与关键词流不同：关键词流是 `人名,备注:频道`，群消息是 `群名@用户名`。
    community_digest._display_author 会取 @ 前的群名（错误），故这里自己解析。
    """
    a = (author or "").strip()
    if "@" in a:
        a = a.rsplit("@", 1)[-1].strip()   # 取 @ 后：Hanlin.AI@Paulius → Paulius
    # 去辨识码后缀、装饰（复用 CD 的简化 + 别名映射）
    a = re.sub(r"_\d{2,6}$", "", a).strip()
    a = re.sub(r"[（(].*?[）)]", "", a).strip()  # Cicada(Never DM First) → Cicada
    name = CD._simplify_name(a)
    return CD._display_alias(name, None)  # 别名映射不依赖 cfg（写死在 config 的映射由调用方再套）


def _display_author(author, cfg=None):
    """带 cfg 的作者显示名（应用 config.author_display_alias，如 5gurl/baobab→iuac）。"""
    a = (author or "").strip()
    if "@" in a:
        a = a.rsplit("@", 1)[-1].strip()
    a = re.sub(r"_\d{2,6}$", "", a).strip()
    a = re.sub(r"[（(].*?[）)]", "", a).strip()
    name = CD._simplify_name(a)
    return CD._display_alias(name, cfg)


def _is_bot_msg(e):
    raw = (e.get("author") or "").rsplit("@", 1)[-1].strip().lower()
    return raw in _BOT_AUTHORS


def _sn_tags(content):
    """抽取正文里的 SN 编号（含 ⛳sn:NN 形态），返回 1-512 的 int 列表。

    过滤 bot 把 SN 字段填成 emoji/文字的无效值（⛳sn:🪙｜price-τalk）与 SN0。
    """
    out = []
    for x in re.findall(r"(?i)\bsn[: ]?(\d{1,3})\b", content or ""):
        n = int(x)
        if 1 <= n <= 512:
            out.append(n)
    return out


def _anchors(cfg=None):
    """config.research_sn_anchors 里登记的专名（合集成集合，小写）。"""
    m = (cfg or {}).get("research_sn_anchors") or {}
    out = set()
    for v in m.values():
        if isinstance(v, list):
            out |= {str(x).strip().lower() for x in v if str(x).strip()}
    return out


def _key_hit(e, cfg=None):
    """相关性用：作者本人或正文提及关键人物，都算相关（const 被讨论也重要）。"""
    name = _author_name(e.get("author"))
    return CD._key_author_hit({"author": name, "content": e.get("content") or ""}, cfg)


def _key_self(e, cfg=None):
    """「关键人物观点」段用：仅作者本人是关键人物，不含「别人提到 const」。"""
    name = _author_name(e.get("author")).lower()
    for n in CD._key_author_names(cfg):
        pat = r"(?<![a-z0-9])" + re.escape(n) + r"(?:_[a-z0-9]+|tt|[0-9]{2,6})?(?![a-z])"
        if re.search(pat, name):
            return True
    return False


def _is_noise(e):
    c = (e.get("content") or "").strip()
    if not c or len(c) < 3:
        return True
    if _LINK_ONLY.match(c):
        return True
    if _NOISE_PAT.match(c):
        return True
    # TAO-update-DC bot 的 "⛳sn:NN  用户正在输入..." 占位
    if re.search(r"(?i)用户正在输入|typing\.\.\.", c) and len(c) < 40:
        return True
    # bot 转发的无关通知：TW粉丝列表 / 新事件通知里 NBA 八卦这类，与子网无关
    if re.search(r"(?i)新事件通知|关键词:\s*TW粉丝|粉丝列表.*关注了", c) \
            and not re.search(r"(?i)\bsn[: ]?\d{1,3}\b|subnet|buyback|回购|emission|排放", c):
        return True
    return False


def _strict_related(e, cfg=None, anchor_set=None):
    """严格相关筛选（用户 v1.24.0 定）：命中任一才保留。

    群消息是原始聊天，噪音大。保留：
      1. 含 SN 编号（⛳sn:NN / SN99 / sn:22）；
      2. 含 Bittensor 关键词（复用 CD._BT_KW：subnet/buyback/emission/tao/子网/回购…）；
      3. 含 config.research_sn_anchors 登记的专名；
      4. 关键人物（const/iuac/rrik）发言；
      5. 含中文（中文频道几乎都是圈内讨论）。
    纯水聊/表情/机器人指令/纯链接/Gm 一律剔除（_is_noise 前置）。
    """
    if _is_noise(e):
        return False
    c = e.get("content") or ""
    if _sn_tags(c):
        return True
    if CD._BT_KW.search(c):
        return True
    if CD._SN_TAG.search(c):
        return True
    if anchor_set:
        low = c.lower()
        if any(k and len(k) >= 3 and k in low for k in anchor_set):
            return True
    if _key_hit(e, cfg):
        return True
    if CD._CN_RE.search(c):
        return True
    return False


def _body_clean(e):
    """规整正文：bot 的 ⛳sn:NN 机器前缀 → [SNnn] 标记；emoji/无效 SN 前缀去掉。"""
    body = re.sub(r"\s+", " ", (e.get("content") or "")).strip()
    # 有效 SN 前缀 → [SNnn]（保留归属信息，去机器腔）
    def _sn_repl(m):
        n = int(m.group(1))
        return "[SN%d]" % n if 1 <= n <= 512 else ""
    body = re.sub(r"(?i)⛳\s*sn[: ]?(\d{1,3})\b", _sn_repl, body)
    # 无效 SN 前缀（⛳sn:🪙｜price-τalk 这种）去掉
    body = re.sub(r"(?i)⛳\s*sn[: ]?[^\s].{0,12}?price[-\s]?τalk\s*", "", body)
    body = re.sub(r"(?i)⛳\s*sn[: ]?\S{1,8}(?=\s|$)", "", body)
    # Discord 提及占位 <@&123> / <@123> 去掉
    body = re.sub(r"<@!?&?\d+>", "", body)
    return body.strip()


def build_input(payload, cfg=None):
    """严格相关筛选 + 按群分组 + 关键人物单列，返回给模型的输入文本。

    返回 (text_block, stats)：stats = {total, related, by_group, key_count, sns}
    """
    events = payload.get("events") or []
    anchor_set = _anchors(cfg)

    related = [e for e in events if _strict_related(e, cfg, anchor_set)]
    # 按群分桶
    by_group = {}
    for e in related:
        g = e.get("groupName") or e.get("groupId") or "未知群"
        by_group.setdefault(g, []).append(e)

    bounds = payload.get("bounds") or {}
    bs, be = bounds.get("start"), bounds.get("end")
    def _bstr(ms):
        return (datetime.datetime.fromtimestamp(ms / 1000, TZ).strftime("%m-%d %H:%M")
                if ms else "?")
    _range = ("%s → %s" % (_bstr(bs), _bstr(be))) if bs and be else "所选日 00:00 → 当前"

    lines = [
        "平台：dtt 管理员群消息库（Telegram 群原始聊天，与关键词事件流隔离）",
        "日期：%s（北京时间 %s）" % (payload.get("day", ""), _range),
        "当日群消息 %d 条，严格相关筛选后保留 %d 条（水聊/表情/机器人指令/纯链接已剔除）"
        % (len(events), len(related)),
        "",
        "【铁律】以下都是群聊原文，混有观点、传闻、喊单、玩笑。按「有人说」处理，"
        "不写成既定事实；不补原文没有的数字/人名/SN；不贴原始链接；中文短句、词必达意。",
        "",
        "===== 按群分组的当日相关消息（每条已标时间+发言人显示名）=====",
    ]
    all_sns = set()
    for g, es in by_group.items():
        # 官方 bot 群（TAO-update-DC）消息多，按 SN 聚合呈现；其余按时间
        es.sort(key=lambda x: x.get("occurredAt") or 0, reverse=True)
        lines.append("\n--- %s（%d 条）---" % (g, len(es)))
        seen, n = set(), 0
        for e in es:
            body = _body_clean(e)
            key = body[:70]
            if key in seen:
                continue
            seen.add(key)
            all_sns |= set(_sn_tags(e.get("content") or ""))
            author = _display_author(e.get("author"), cfg)
            t = e.get("_time") or "??:??"
            lines.append("- [%s][%s] %s" % (t, author, CD._clean(body, MAX_MSG_CHARS)))
            n += 1
            if n >= MAX_PER_GROUP:
                lines.append("  …（该群另有 %d 条已省略）" % (len(es) - n))
                break

    # 关键人物（const/iuac/rrik）**本人发言**单列，强制逐条提炼。
    # 「别人提到 const」不算 const 的观点，留在分群讨论里，避免张冠李戴。
    key_es = [e for e in related if _key_self(e, cfg)]
    key_es.sort(key=lambda x: x.get("occurredAt") or 0, reverse=True)
    if key_es:
        names = CD._key_author_names(cfg)
        lines.append("\n===== 关键人物发言（%s，必须逐条提炼真正观点，不得当普通群友过滤）====="
                     % "、".join("@" + CD._display_alias(x, cfg) for x in names))
        seen = set()
        for e in key_es[:MAX_KEY_PERSON]:
            body = _body_clean(e)
            if body[:70] in seen:
                continue
            seen.add(body[:70])
            lines.append("- [%s][%s][%s] %s" % (
                e.get("_time") or "??:??",
                e.get("groupName") or "",
                _display_author(e.get("author"), cfg),
                CD._clean(body, MAX_MSG_CHARS)))

    stats = {
        "total": len(events),
        "related": len(related),
        "by_group": {g: len(es) for g, es in by_group.items()},
        "key_count": len(key_es),
        "sns": sorted(all_sns),
    }
    return "\n".join(lines), stats


TG_GROUP_SYSTEM_PROMPT = (
    "你是一名 Bittensor 生态资深研究员，负责把「Telegram 群原始聊天」当日消息"
    "整理成一份中文 TG 群消息日报。数据来自 dtt 管理员群消息库，已做严格相关筛选"
    "（水聊/表情/机器人指令/纯链接已剔除）。\n"
    "\n"
    "【输出结构（严格按顺序，标题一律 **加粗**；无内容的小节整节省略）】\n"
    "**🔥 群内热点**（当天群里被反复提及、或明显重要的事件，3-6 条；"
    "每条一句说清「谁 + 什么事」，关键主体与 **SN编号** 加粗，标群名）\n"
    "**📢 子网动态**（官方 bot 转发的 ⛳sn:NN 动态，按 SN 分组：每个有实质的 SN 写一块，"
    "格式「**SN53**（N 条）」下接 1-2 条要点；同一 SN 的多条零散播报合并成一句，"
    "不逐条复述机器人腔。查不到子网名就只写编号，不要编名字）\n"
    "**💬 分群讨论**（按群名分组，如 Hanlin.AI / Subnet Summer - τ；"
    "每条写清「发言人：真正观点」，重点写清看多/看空、质疑、依据、指向哪个子网或机制；"
    "玩笑/调侃如实写，不硬拔高）\n"
    "**👀 关键人物观点**（输入末尾「关键人物发言」里的人必须单列、逐条提炼真正立场，"
    "严禁当普通群友合并或一句带过；人名用输入里的显示名，如 const、iuac、rrik）\n"
    "**💡 一句话总结**（当天群内情绪与主线，一句收束）\n"
    "\n"
    "【人名与写法（硬性）】\n"
    "- 人名照抄输入 `<>`/方括号里的显示名（已是规范名：备注名优先、映射后 iuac/rrik），"
    "禁止写 `群名@用户名`、`名字_数字`、`备注:` 这类账号辨识码后缀。\n"
    "- **const 必须单列**：只写他当天相关发言，每条提炼成一句「const 表示/质疑/附和：要点」，"
    "写清他说了什么，不贴原始链接、不照抄全文。\n"
    "- 关键人物观点「词必达意」：把零散中英夹杂的发言串成完整意思，"
    "利好讲清「谁把什么导向哪里、预期效果」，不要只写「已准备接入」这种没头没尾的表述。\n"
    "\n"
    "【铁律】\n"
    "- 不贴任何原始链接 / x.com / discord.gg 地址；链接内容有信息就提炼成一句，没有就略。\n"
    "- **禁止编造**：不补原文没有的数字、人名、SN 编号、交易细节；原文没有就说没有。\n"
    "- 官方 bot 的「⛳sn:NN 用户正在输入」「请打开 DC 查看」这类占位不算实质内容，不写进报告。\n"
    "- 与 Bittensor 子网、代币机制、团队动态无关的话题不列。\n"
    "\n"
    "【排版与篇幅（飞书/TG 双平台通用）】\n"
    "- 关键主体 **加粗**：人物、**SN编号/子网名**、代币、事件、关键数字。\n"
    "- emoji 标程度：🔥 热点、✅ 落地、⚠️ 风险、📌 关注、❌ 利空。\n"
    "- 每条单独成行用 - 项目符号，小节间空行，适合手机端；不用 # 标题、不用 > 引用。\n"
    "- **字数上限 2500 字**（含标点）；消息少就少写，简洁+信息全=合格。\n"
    "- 禁止废话：不写「此外」「总体来看」「可以看出」这类填充词；不复述已说过的事实；"
    "一条只说一件事。\n"
    "- 绝对不许在正文里写任何字数标注或元信息。\n"
)


def _out_dir(cfg=None):
    return TGF._out_dir(cfg)


def run_tg_group_day(cfg, day_str, log=print, push=True):
    """主流程：取数 -> 严格筛选构建 -> AI 生成 -> 清洗 -> 落档 -> 双推。"""
    import pipeline as P

    day_str = P._resolve_day_alias(day_str, TZ)
    today = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    yest = (datetime.datetime.now(TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    day_label = ("今日" if day_str == today else "昨日" if day_str == yest else day_str)

    # 1) 取数（窗口 00:00→当前，缓存不覆盖当下则重取）
    payload = TGF.read_day(day_str, cfg)
    if payload is not None and TGF.cache_covers_now(payload, cfg):
        log("TG群日报：复用已取数的 %s（%d 条，边界覆盖到当前）"
            % (day_str, payload.get("stats", {}).get("total", 0)))
    else:
        if payload is not None:
            log("TG群日报：%s 旧快照边界未覆盖当前时刻，重取中…" % day_str)
        payload = TGF.fetch_day(day_str, cfg, log=log, until_now=True)

    if not (payload.get("events") or []):
        digest = "📭 %s TG 群消息库当日无消息。" % day_str
        return {"ok": True, "day": day_str, "digest": digest, "empty": True}

    # 2) 严格筛选 + 构建输入
    text_block, stats = build_input(payload, cfg)
    log("TG群日报：严格筛选后 %d/%d 条相关（%s），关键人物 %d 条，覆盖 SN %s，调用 AI 生成…"
        % (stats["related"], stats["total"],
           " ".join("%s=%d" % (k, v) for k, v in stats["by_group"].items()),
           stats["key_count"], stats["sns"] or "无"))
    if stats["related"] == 0:
        digest = "📭 %s TG 群消息当日无与 Bittensor 子网相关的内容。" % day_str
        return {"ok": True, "day": day_str, "digest": digest, "empty": True,
                "stats": stats}

    gen_cfg = dict(cfg)
    gen_cfg["deepseek_max_tokens"] = max(int(cfg.get("deepseek_max_tokens", 2500) or 2500), 6000)
    digest = P.call_llm(gen_cfg, text_block, system_prompt=TG_GROUP_SYSTEM_PROMPT)
    digest = P.sanitize_digest(digest)
    # 兜底清理模型吐出的人名串（复用社区日报的清理）
    try:
        digest = CD.strip_name_dump(digest, log=log)
    except Exception:
        pass

    # 3) 落盘（独立文件）
    out_dir = _out_dir(cfg)
    os.makedirs(out_dir, exist_ok=True)
    dp = os.path.join(out_dir, "tg_group_%s.digest.txt" % day_str)
    with open(dp, "w", encoding="utf-8") as f:
        f.write(digest.strip())

    # 4) 按 SN 落进子网档案 community_digests[]（与关键词社区日报同字段，source 标 tg_group）
    archived = 0
    try:
        import subnet_archive as SA
        rel_dp = os.path.relpath(dp, HERE).replace("\\", "/")
        for sn in stats["sns"]:
            try:
                SA.attach_community_digest(
                    cfg, day_str, sn, "TG群消息 SN%d 相关动态（详见 %s）" % (sn, rel_dp),
                    platforms=["Telegram"], source_file=rel_dp, log=log)
                archived += 1
            except Exception as e:
                log("TG群日报落档 SN%d 失败（忽略）：%s" % (sn, e))
        log("TG群日报落档完成：%d 个子网写入社区线索" % archived)
    except Exception as e:
        log("TG群日报落档跳过（不影响推送）：%s" % e)

    # 5) 双推
    title = "💬 Bittensor TG 群消息日报 · %s（%s）" % (day_str, day_label)
    if push:
        log("TG群日报推送：飞书 + TG")
        try:
            P.push_feishu(cfg, title, digest)
        except Exception as e:
            log("TG群日报飞书推送失败（不影响 TG）：%s" % e)
        try:
            P.push_telegram(cfg, title, digest)
        except Exception as e:
            log("TG群日报 TG 推送失败：%s" % e)

    return {"ok": True, "day": day_str, "digest": digest, "stats": stats,
            "path": dp, "archived_sn": archived, "total": stats["total"]}


if __name__ == "__main__":
    import sys
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    r = run_tg_group_day(cfg, day, push=False)
    print("OK:", r.get("day"), r.get("stats"))
    print((r.get("digest") or "")[:1500])
