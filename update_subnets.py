#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
子网知识库更新器（每周跑一次）
=================================
职责：把「SN 编号 ↔ 子网名 ↔ 当前业务」整理成一份本地对照表，供日报生成时当背景知识用。

设计原则（对应用户要求）：
1. **只记最新业务**：数据源是链上子网身份（SubnetIdentitiesV2）的当前描述，
   即子网团队自己在链上登记的那句话；被注销/换赛道之前的旧业务不会再进表。
2. **不清楚就写未知**：链上没填描述、或没拿到中文标签时，写「未知」，绝不用旧资料补全，也不虚构。
3. **每周校对，只做变量更新**：跑一次 = 拉最新数据 + 和本地表做 diff，
   只改动「新增 / 注销 / 改名 / 业务描述变化」这四类变量，其余沿用旧记录。
4. **可追溯**：每次改动追加到 subnets_changelog.md，带日期和来源。

用法：
    python update_subnets.py            # 拉取并更新（会调一次大模型翻译新条目的中文标签）
    python update_subnets.py --dry-run  # 只看差异，不写文件
    python update_subnets.py --no-llm   # 不调模型，中文标签留空（显示英文原描述）
"""

import os
import re
import sys
import json
import datetime

import pipeline as P

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "subnets.json")
MD_PATH = os.path.join(HERE, "subnets.md")
LOG_PATH = os.path.join(HERE, "subnets_changelog.md")
SRC_URL = "https://taostats.io/subnets"


# ----------------------------------------------------------------------------
# 1) 抓取 + 解析链上子网身份
# ----------------------------------------------------------------------------
def _proxy_url(cfg):
    """把 config.proxy / Windows 系统代理 归一成带 scheme 的 URL（urllib 需要 http:// 前缀）。"""
    p = (cfg or {}).get("proxy", "") or ""
    if p == "direct":
        return None
    if p:
        return p if "://" in p else "http://" + p
    sp = P._windows_system_proxy()
    if sp and sp[0]:
        h = sp[0]
        return h if "://" in h else "http://" + h
    return None


def fetch_identities(cfg=None, timeout=90, retries=3, log=None):
    """从 taostats 的子网页抓取链上身份 JSON（页面内嵌了 SubnetIdentities 数据）。

    重试是必需的：国内直连 taostats 经常抛 `SSL: UNEXPECTED_EOF_WHILE_READING`，
    这是可自愈的抖动而不是页面改版。不重试的话，周更正好撞上一次抖动就白等一周。
    """
    log = log or P.log
    # v1.13.0：与主流程共用代理解析，境外抓取统一「代理优先、直连兜底」
    if cfg is not None and getattr(P, "_CFG", None) is None:
        P._CFG = cfg
    req = P.urllib.request.Request(
        SRC_URL, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DTAO-Pipeline/1.0"})

    html = ""
    last_err = None
    for i in range(max(1, retries)):
        try:
            html = P.net_open(req, timeout=timeout, url=SRC_URL).decode("utf-8", "replace")
            last_err = None
            break
        except Exception as e:
            last_err = e
            if i + 1 < max(1, retries):
                wait = 5 * (2 ** i)          # 5 / 10 / 20 秒
                log("  链上身份抓取失败（第 %d/%d 次）：%s，%d 秒后重试"
                    % (i + 1, retries, e, wait))
                import time as _t
                _t.sleep(wait)
    if last_err is not None:
        raise last_err

    # 页面里的 JSON 是转义后嵌进脚本的，先反转义再按对象切
    raw = html.replace('\\"', '"').replace("\\\\", "\\")
    recs = {}
    for obj in re.findall(r"\{[^{}]*\"netuid\":\d+[^{}]*\}", raw):
        try:
            d = json.loads(obj)
        except Exception:
            continue
        nid = d.get("netuid")
        if isinstance(nid, int) and d.get("subnet_name"):
            recs[nid] = {
                "name": (d.get("subnet_name") or "").strip(),
                "desc": (d.get("description") or "").strip(),
                "additional": (d.get("additional") or "").strip(),
                "url": (d.get("subnet_url") or "").strip(),
                "github": (d.get("github_repo") or "").strip(),
            }
    return recs


# ----------------------------------------------------------------------------
# 2) 中文短标签（只翻新增/变化条目，旧的复用缓存，省 token）
# ----------------------------------------------------------------------------
ZH_PROMPT = (
    "你在维护一份 Bittensor 子网对照表。用户会给出一批「编号|子网名|链上英文描述」，"
    "请为每个子网写一句**极简中文业务标签**（不超过 22 个字），只描述它当前做什么业务。\n"
    "规则：\n"
    "1. 只依据给出的英文描述；描述为空或看不出业务，就写「未知」，**不许凭编号记忆或猜测补全**。\n"
    "2. 不要写营销话术（「颠覆」「领先」），不要写「Bittensor 子网」这种废话前缀。\n"
    "3. 输出严格的 JSON 对象，键是编号字符串，值是中文标签，例如 {\"64\": \"面向 AI 的无服务器算力平台\"}。\n"
    "4. 不要输出任何解释、不要加代码块标记。"
)


def translate_batch(cfg, rows, batch=40):
    """rows: [(netuid, name, desc)] -> {netuid: zh}"""
    out = {}
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        body = "\n".join("%d|%s|%s" % (n, nm, (d or "")[:220]) for n, nm, d in chunk)
        try:
            txt = P.call_llm(cfg, body, system_prompt=ZH_PROMPT)
            txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
            m = re.search(r"\{.*\}", txt, re.S)
            data = json.loads(m.group(0) if m else txt)
            for k, v in (data or {}).items():
                try:
                    out[int(k)] = str(v).strip()
                except Exception:
                    continue
        except Exception as e:
            P.log("  ↳ 中文标签翻译失败（这批保留英文原描述）：%s" % e)
    return out


# ----------------------------------------------------------------------------
# 3) 与本地表做 diff，只改变量
# ----------------------------------------------------------------------------
def load_data():
    if not os.path.exists(DATA_PATH):
        return {"updated_at": "", "source": SRC_URL, "subnets": {}, "deregistered": {}}
    with open(DATA_PATH, encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("subnets", {})
    d.setdefault("deregistered", {})
    return d


def apply_update(old, fresh, zh_map, today):
    """把 fresh 合并进 old，返回 (新数据, 变更列表)。只动四类变量。"""
    new = {"updated_at": today, "source": SRC_URL,
           "subnets": dict(old.get("subnets") or {}),
           "deregistered": dict(old.get("deregistered") or {})}
    changes = []

    for nid, rec in fresh.items():
        key = str(nid)
        prev = new["subnets"].get(key)
        _old_name = ""
        if key in new["deregistered"]:            # 重新注册回来（同一编号换了项目）
            _old = new["deregistered"].pop(key, None) or {}
            _old_name = _old.get("name", "")
            changes.append(("重新注册", nid, rec["name"], _old_name))
            prev = None

        if prev is None:
            aliases = []
            if rec.get("name") and _old_name:      # 编号被重新注册：记下旧项目名，便于识别「换了赛道」
                aliases.append(_old_name)
            new["subnets"][key] = {
                "name": rec["name"], "desc": rec["desc"],
                "zh": zh_map.get(nid, ""), "url": rec["url"], "github": rec["github"],
                "status": "active", "first_seen": today, "last_seen": today, "aliases": aliases,
            }
            if not changes or changes[-1][1] != nid:
                changes.append(("新增", nid, rec["name"], ""))
            continue

        prev["last_seen"] = today
        prev["status"] = "active"
        if prev.get("name") != rec["name"]:
            old_name = prev.get("name")
            prev.setdefault("aliases", []).append(old_name)
            prev["name"] = rec["name"]
            changes.append(("改名", nid, rec["name"], old_name or ""))
        if (prev.get("desc") or "") != rec["desc"]:
            prev["desc"] = rec["desc"]
            if nid in zh_map:
                prev["zh"] = zh_map[nid]
            changes.append(("业务变更", nid, rec["name"], ""))
        for k in ("url", "github"):
            if rec[k]:
                prev[k] = rec[k]
        prev["zh"] = prev.get("zh") or zh_map.get(nid, "")

    # 消失的编号 = 已注销：保留记录，供模型知道「这个编号没了」
    for key, rec in list(new["subnets"].items()):
        if int(key) not in fresh:
            new["subnets"].pop(key, None)
            rec["status"] = "deregistered"
            rec["deregistered_on"] = today
            new["deregistered"][key] = rec
            changes.append(("注销", int(key), rec.get("name", ""), ""))
    return new, changes


# ----------------------------------------------------------------------------
# 4) 落地文件
# ----------------------------------------------------------------------------
def write_json(data):
    # 编号按数字排序，方便人肉翻
    data["subnets"] = {k: data["subnets"][k] for k in sorted(data["subnets"], key=int)}
    data["deregistered"] = {k: data["deregistered"][k] for k in sorted(data["deregistered"], key=int)}
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def write_md(data):
    lines = ["# Bittensor 子网对照表（自动生成，勿手改）", "",
             "> 来源：%s 的链上子网身份（SubnetIdentitiesV2），最后更新：%s" % (SRC_URL, data["updated_at"]),
             "> 只记录**当前**业务；描述缺失一律写「未知」。被注销的编号单独列在文末。", "",
             "| SN | 子网名 | 业务 |", "|---|---|---|"]
    for k, v in data["subnets"].items():
        nm = v.get("name") or "未知"
        if v.get("aliases"):
            nm += "（旧名：%s）" % "、".join(v["aliases"])
        lines.append("| %s | %s | %s |" % (k, nm, v.get("zh") or v.get("desc") or "未知"))
    if data.get("deregistered"):
        lines += ["", "## 已注销（历史编号，仅用于识别旧名）", "", "| SN | 原名称 |", "|---|---|"]
        for k, v in data["deregistered"].items():
            lines.append("| %s | %s |" % (k, v.get("name") or "未知"))
    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def log_changes(changes, today):
    if not changes:
        return
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write("\n## %s\n" % today)
        for kind, nid, name, extra in changes:
            tail = "（旧名：%s）" % extra if extra else ""
            f.write("- %s：SN%d %s%s\n" % (kind, nid, name, tail))


# ----------------------------------------------------------------------------
# 5) 把知识库同步进已建档案
# ----------------------------------------------------------------------------
def sync_archives(cfg, kb=None, dry=False, log=None):
    """用知识库的当前「名称 / 业务 / 官网 / 仓库」校正已建子网档案。

    为什么必须做：subnets.json 是名字与业务的事实来源，档案只是它的副本。
    2026-11-09 就吃过这个亏 —— 链上和报告都改成 Zils 了，档案里还写着 Astrid，
    于是 `/档案` 显示旧名、`/retry` 的别名反查从旧名起步、下次投研又从旧名开写。

    只改有依据的新值：知识库给不出真名（占位值 deprecated/unknown）就不动档案，
    绝不拿空值覆盖已有内容。旧名进 aliases，保留可追溯。
    """
    import glob
    import subnet_archive as A

    log = log or P.log
    kb = kb if kb is not None else load_data()
    subs = kb.get("subnets") or {}
    arc_dir = A.get_arc_dir(cfg)
    changed = []
    for path in sorted(glob.glob(os.path.join(arc_dir, "SN*", "archive.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                arc = json.load(f)
        except Exception:
            continue
        rec = subs.get(str(arc.get("sn")))
        if not rec:
            continue
        nm = A.real_name(rec)
        biz = A.real_desc(rec)
        url = A.real_url(rec.get("url"))
        gh = A.real_url(rec.get("github"))

        old_name = (arc.get("name") or "").strip()
        dirty = []
        if nm and nm != old_name:
            if old_name and old_name not in ("未知", "（知识库未收录）", "（未收录）"):
                al = arc.setdefault("aliases", [])
                if old_name not in al:
                    al.append(old_name)
            arc["name"] = nm
            dirty.append("名称 %s → %s" % (old_name or "（空）", nm))
        for key, val, label in (("biz", biz, "业务"), ("url", url, "官网"), ("github", gh, "仓库")):
            if val and (arc.get(key) or "") != val:
                arc[key] = val
                dirty.append(label)
        if not dirty:
            continue

        if not dry:
            arc["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            A.save_archive(arc_dir, arc)
            try:
                A.save_md(arc_dir, arc)
            except Exception:
                pass
        changed.append((arc.get("sn"), old_name, arc.get("name") or "", dirty))
        log("  ↳ SN%s 档案同步：%s" % (arc.get("sn"), "；".join(dirty)))

    if changed and not dry:
        try:
            A.refresh_index(arc_dir)
        except Exception as e:
            log("档案索引刷新失败（忽略）：%s" % e)
    if changed:
        log("档案同步完成：%d 份子网档案已对齐知识库" % len(changed))
    return changed


# ----------------------------------------------------------------------------
# 6) 可编程入口（CLI 与 TG 周更共用，避免两套逻辑分叉）
# ----------------------------------------------------------------------------
def run(cfg=None, use_llm=True, dry=False, sync_archive=True, log=None):
    """执行一次校对，返回结构化结果。

    {"ok", "fetched", "changes": [(kind, sn, name, old_name)],
     "archive_synced": [(sn, old_name, new_name, [改动项])], "error"}

    ok=False 时**一个文件都不写**：抓不到链上数据就什么都不动，
    绝不让空数据把好不容易校好的知识库覆盖成空表。
    """
    log = log or P.log
    cfg = cfg if cfg is not None else P.load_config()
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    out = {"ok": False, "fetched": 0, "changes": [], "archive_synced": [],
           "error": "", "dry": dry}

    log("拉取链上子网身份：%s" % SRC_URL)
    try:
        fresh = fetch_identities(cfg, log=log)
    except Exception as e:
        out["error"] = "抓取异常：%s" % e
        log("❌ %s，本次不做任何修改" % out["error"])
        return out
    if not fresh:
        out["error"] = "没解析到任何子网数据（页面结构可能变了）"
        log("❌ %s，本次不做任何修改" % out["error"])
        return out
    out["fetched"] = len(fresh)
    log("解析到 %d 个子网" % len(fresh))

    old = load_data()
    old_sub = old.get("subnets") or {}

    # 找出需要翻译的：新条目 + 描述变化 + 中文标签为空
    need = []
    for nid, rec in fresh.items():
        prev = old_sub.get(str(nid))
        if prev is None or (prev.get("desc") or "") != rec["desc"] or not prev.get("zh"):
            need.append((nid, rec["name"], rec["desc"]))
    zh_map = {}
    if need and use_llm:
        log("需要生成中文业务标签 %d 条，调用模型中…" % len(need))
        zh_map = translate_batch(cfg, need)
        log("中文标签完成 %d/%d" % (len(zh_map), len(need)))

    new, changes = apply_update(old, fresh, zh_map, today)
    out["changes"] = changes
    if changes:
        log("变更 %d 项：" % len(changes))
        for kind, nid, name, extra in changes[:40]:
            log("  - %s SN%d %s%s" % (kind, nid, name, ("（旧名 %s）" % extra) if extra else ""))
        if len(changes) > 40:
            log("  …还有 %d 项（详见 %s）" % (len(changes) - 40, os.path.basename(LOG_PATH)))
    else:
        log("与上次一致，无变化")

    if dry:
        # dry-run 也把档案同步的差异试算出来（只打印不写盘），否则看不出全貌
        if sync_archive:
            try:
                out["archive_synced"] = sync_archives(cfg, kb=new, dry=True, log=log)
            except Exception as e:
                log("档案同步预演失败（忽略）：%s" % e)
        log("--dry-run：不写文件")
        out["ok"] = True
        return out

    write_json(new)
    write_md(new)
    log_changes(changes, today)
    log("已更新 %s / %s" % (os.path.basename(DATA_PATH), os.path.basename(MD_PATH)))
    out["ok"] = True

    if sync_archive:
        try:
            out["archive_synced"] = sync_archives(cfg, kb=new, log=log)
        except Exception as e:
            log("档案同步失败（知识库已更新，不影响本次结果）：%s" % e)
    return out


# ----------------------------------------------------------------------------
def main():
    dry = "--dry-run" in sys.argv
    use_llm = "--no-llm" not in sys.argv
    sync = "--no-sync-archive" not in sys.argv
    res = run(use_llm=use_llm, dry=dry, sync_archive=sync)
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
