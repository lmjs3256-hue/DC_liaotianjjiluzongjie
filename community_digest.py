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


def _relevance(e):
    """给一条消息打相关性分：越高越该保留。低于 0 视为噪声剔除。"""
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
    return score


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
    "纯水聊、无关转发、纯表情、与 Bittensor 无关的话题直接忽略。\n"
    "\n"
    "【输出结构（严格按顺序，标题一律 **加粗**；无内容的小节整节省略，不硬凑）】\n"
    "**🔥 社区热点**（当天跨平台被反复提及、或明显重要的事件，3-6 条；"
    "每条一句说清「谁 + 什么事」，关键主体与 **SN编号** 加粗，标来源平台）\n"
    "**🧩 子网动态**（**按 SN 分组**：每个有实质消息的子网写一块，格式「**SN53 Engy**（N 条）」"
    "下接 1-3 条要点；子网名以输入里的名称对照为准，查不到就只写编号，不要编名字。"
    "按消息量与重要性排序，只写有价值的前若干个，不必全部列出）\n"
    "**👀 人物与观点**（点名开发者/核心成员的重要表态，标平台；普通群友无关发言不列；没有就省略）\n"
    "**💡 一句话总结**（当天社区情绪与主线，一句收束）\n"
    "\n"
    "【交叉核对：发现矛盾必须标出来，交人工确认】\n"
    "- 输入里若附了「历史档案 / 投研结论」参考块，那是**历史快照，可能过时或被推翻**。\n"
    "- 当今日社区消息与历史结论**明显矛盾**（例如历史写「未回购/未上线」，今日多人称已回购/已上线），"
    "必须在该条后写「⚠️ 待人工确认：历史（日期）称X，但今日社区称Y」——"
    "**把冲突并列写出，不要二选一、不要静默采信任何一边**。\n"
    "- 没有矛盾就不要硬标；正常的、无历史对照的消息不加此标记。\n"
    "\n"
    "【排版与篇幅（飞书/TG 双平台通用）】\n"
    "- 关键主体 **加粗**：人物、**SN编号/子网名**、代币、事件、关键数字。\n"
    "- emoji 标程度：🔥 热点、✅ 落地、⚠️ 风险/待确认、📌 关注、❌ 利空。\n"
    "- 每条单独成行用 - 项目符号，小节间空行，适合手机端；不用 # 标题、不用 > 引用。\n"
    "- **字数上限 1500 字**（含标点），只有上限没有下限：消息少就少写，简洁+信息全=合格。\n"
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
        scored = [(_relevance(e), e) for e in by_sn[sn]]
        scored = [(sc, e) for sc, e in scored if sc >= 0]
        scored.sort(key=lambda x: (-x[0], -(x[1].get("occurredAt") or 0)))
        by_sn[sn] = [e for _sc, e in scored]
    generic = [e for e in generic if _relevance(e) >= 0]

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
            author = _clean(e.get("author") or "?", 40)
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
            author = _clean(e.get("author") or "?", 40)
            lines.append("- [%s][%s] <%s> %s" % (tstr, plat, author, _clean(e.get("content"))))
            n += 1
            if n >= MAX_GENERIC:
                break

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

    # 3) 交叉核对：出现「待人工确认」的醒日志 + 正文顶部提示
    flags = [ln.strip() for ln in (digest or "").splitlines() if "待人工确认" in ln]
    if flags:
        log("⚠️⚠️ 社区日报交叉核对：%d 处「待人工确认」（社区消息与历史结论冲突）：%s"
            % (len(flags), " | ".join(flags[:5])))
        notice = ("⚠️ **本篇含 %d 处「待人工确认」：社区消息与历史档案/投研结论存在冲突，"
                  "已并列写出，请人工核对后再采信。**\n\n" % len(flags))
        digest = notice + digest
    else:
        log("✅ 社区日报交叉核对通过（未发现与历史结论的明显冲突）")

    # 4) 落盘（与 DC 日报分开命名）
    out_dir = _out_dir(cfg)
    os.makedirs(out_dir, exist_ok=True)
    dp = os.path.join(out_dir, "community_%s.digest.txt" % day_str)
    with open(dp, "w", encoding="utf-8") as f:
        f.write(digest.strip())

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
