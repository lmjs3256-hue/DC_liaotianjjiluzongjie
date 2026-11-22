#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推文链接正文核实（TG 群日报专用）
==========================================================
用户 v1.25.0 硬性要求：群里转发的 x.com / twitter.com 推文链接，
不能只写「某人转发了一条推特链接」就一带而过 —— 必须按推文 ID
抓取核实正文，把推文实际讲了什么提炼出来；抓取失败如实标注
「内容未能核实」，绝不编造。

实现：
    - fxtwitter / vxtwitter 公开镜像 API（无需登录），走 pipeline.net_open
      （代理优先、直连兜底），双镜像互为备份。
    - 按推文 ID 落持久缓存 exports/tg_group/tweet_cache.json，
      同一条推文只抓一次；失败短 TTL 后可重试（避免一次抖动永久失败）。

对外接口：
    extract_tweet_urls(text) -> [(url, tweet_id), ...]
    verify_links(events, cfg=None, log=print) -> dict
        给每条 event 附加 e["_tweets"] = [{url, id, ok, text, author, note}]
        返回统计 {checked, fetched, cached, failed}
"""

import datetime
import json
import os
import re
import time
import urllib.request

import pipeline as P

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = datetime.timezone(datetime.timedelta(hours=8))

# 单日最多核实多少条推文（防异常高频刷接口）
MAX_FETCH_PER_RUN = 40
# 单条推文正文喂给模型的上限（字符）
MAX_TWEET_CHARS = 700
# 失败结果多久后可重试（秒）
FAIL_TTL = 600

# x.com / twitter.com / fxtwitter.com / vxtwitter.com 的 /status/<id>
# 也兼容 /i/web/status/<id> 与带 ?s=xx 等查询串
_TWEET_RE = re.compile(
    r"(?<![a-z0-9])(?:https?://)?(?:www\.)?"
    r"(?:x\.com|twitter\.com|fxtwitter\.com|vxtwitter\.com)"
    r"/(?:[A-Za-z0-9_]{1,20}/status/|i/web/status/)(\d{10,25})",
    re.I)

# 镜像 API：主 + 备
_MIRRORS = [
    "https://api.fxtwitter.com",
    "https://api.vxtwitter.com",
]


def _cache_path(cfg=None):
    d = (cfg or {}).get("tg_group_output_dir") or "exports/tg_group"
    if not os.path.isabs(d):
        d = os.path.join(HERE, d)
    return os.path.join(d, "tweet_cache.json")


def _load_cache(cfg=None):
    p = _cache_path(cfg)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache, cfg=None):
    p = _cache_path(cfg)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception as e:
        # 缓存写失败不影响本次核实
        pass


def extract_tweet_urls(text):
    """从正文抽取推文链接，返回去重后的 [(url, tweet_id)]（按出现顺序）。"""
    out, seen = [], set()
    for m in _TWEET_RE.finditer(text or ""):
        url, tid = m.group(0), m.group(1)
        if tid in seen:
            continue
        seen.add(tid)
        out.append((url, tid))
    return out


def _clean_text(s):
    """推文正文规整：去图片短链、压空白、截断。"""
    s = re.sub(r"https?://pbs\.twimg\.com/\S+", "", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:MAX_TWEET_CHARS]


def _fetch_one(tweet_id, log=print):
    """抓单条推文正文。成功返回 {ok,text,author}；失败返回 {ok:False,note}。"""
    last_err = ""
    for base in _MIRRORS:
        # fxtwitter 镜像支持 /status/<id> 无用户名路径（实测可用，最通用）
        url = "%s/status/%s" % (base, tweet_id)
        try:
            raw = P.net_open(urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}),
                timeout=12, limit=200000)
            d = json.loads(raw.decode("utf-8", "replace"))
            t = d.get("tweet") or {}
            author_obj = t.get("author") or {}
            author = author_obj.get("name") or author_obj.get("screen_name") or ""
            screen = author_obj.get("screen_name") or ""
            text = _clean_text(t.get("text") or t.get("full_text") or "")
            if text:
                return {"ok": True, "text": text,
                        "author": ("%s (@%s)" % (author, screen) if screen else author).strip()}
            last_err = "empty text"
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, str(e)[:80])
    return {"ok": False, "note": "内容未能核实（%s）" % last_err}


def verify_links(events, cfg=None, log=print):
    """核实 events 里每条消息的推文链接，附加 e["_tweets"]。

    只处理带链接的消息；缓存命中直接用，未命中抓镜像 API。
    返回统计 dict：checked（含链接消息数）/ tweets / cached / fetched / failed。
    """
    cache = _load_cache(cfg)
    now = time.time()
    stats = {"checked": 0, "tweets": 0, "cached": 0, "fetched": 0, "failed": 0}
    budget = MAX_FETCH_PER_RUN
    cache_dirty = False

    for e in events:
        pairs = extract_tweet_urls(e.get("content") or "")
        if not pairs:
            continue
        stats["checked"] += 1
        tweets = []
        for url, tid in pairs:
            stats["tweets"] += 1
            hit = cache.get(tid)
            if hit and (hit.get("ok") or now - hit.get("ts", 0) < FAIL_TTL):
                info = {"url": url, "id": tid, "ok": bool(hit.get("ok")),
                        "text": hit.get("text", ""), "author": hit.get("author", ""),
                        "note": hit.get("note", "")}
                stats["cached"] += 1
            elif budget > 0:
                budget -= 1
                got = _fetch_one(tid, log=log)
                if got.get("ok"):
                    log("推文核实：%s → %s" % (tid, got["text"][:60]))
                else:
                    log("推文核实失败：%s（%s）" % (tid, got.get("note", "")))
                cache[tid] = {"ok": got.get("ok", False), "text": got.get("text", ""),
                              "author": got.get("author", ""), "note": got.get("note", ""),
                              "ts": now}
                cache_dirty = True
                info = {"url": url, "id": tid, "ok": got.get("ok", False),
                        "text": got.get("text", ""), "author": got.get("author", ""),
                        "note": got.get("note", "")}
                stats["fetched"] += 1
                if not got.get("ok"):
                    stats["failed"] += 1
            else:
                info = {"url": url, "id": tid, "ok": False,
                        "note": "内容未能核实（超出本次核实上限）"}
                stats["failed"] += 1
            tweets.append(info)
        e["_tweets"] = tweets

    if cache_dirty:
        _save_cache(cache, cfg)
    return stats


if __name__ == "__main__":
    import sys
    cfg = None
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    urls = sys.argv[1:] or ["https://x.com/bitcoinnewscom/status/2124111559332856196"]
    print("抽链接：", extract_tweet_urls(" ".join(urls)))
    print("核实：", json.dumps(verify_links([{"content": " ".join(urls)}], cfg), ensure_ascii=False))
