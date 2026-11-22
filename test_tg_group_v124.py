#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.24.0 回归：TG 群消息取数（游标翻页/毫秒边界）+ 提炼（严格筛选/作者解析/别名/关键人物本人）。

纯离线单测：不连 dtt、不调 LLM。用构造数据覆盖关键逻辑。
"""
import datetime
import sys

import tg_group_fetch as G
import tg_group_digest as D

OK = 0
FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  ✅", name)
    else:
        FAIL += 1
        print("  ❌", name, "::", str(detail)[:200])


def E(author, content, group="Hanlin.AI", ts=1795327347000):
    return {"author": author, "content": content, "groupName": group,
            "occurredAt": ts, "_time": "13:22"}


print("== 1) 毫秒边界 ==")
s, e = G._day_bounds("2026-11-22", until_now=False)
check("start 是毫秒", s > 1_000_000_000_000, s)
check("end-start=1天(ms)", e - s == 86400 * 1000, e - s)

print("== 2) 默认接入 4 群 ==")
check("4 个活跃群", len(G.DEFAULT_GROUP_IDS) == 4)
check("含 TAO-update-DC", "8116469191" in G.DEFAULT_GROUP_IDS)
check("config 覆盖", G._group_ids({"tg_group_ids": ["x", "y"]}) == ["x", "y"])

print("== 3) SN 提取：过滤无效/emoji/超范围 ==")
check("正常 SN", D._sn_tags("⛳sn:53 buyback") == [53])
check("sn:NN 形态", D._sn_tags("关于 SN99 的讨论") == [99])
check("过滤 SN0", D._sn_tags("⛳sn:0 占位") == [])
check("过滤 emoji SN", D._sn_tags("⛳sn:🪙｜price-τalk hello") == [])
check("过滤超范围", D._sn_tags("sn:9999 bad") == [])

print("== 4) 作者解析：群名@用户名 → 真人名 + 别名 ==")
check("群前缀去掉", D._display_author("Hanlin.AI@Paulius", {}) == "Paulius",
      D._display_author("Hanlin.AI@Paulius", {}))
check("括号昵称清理", D._display_author("Bittensor TAO Price Chat@Cicada(Never DM First)", {}) == "Cicada",
      D._display_author("Bittensor TAO Price Chat@Cicada(Never DM First)", {}))
cfg_alias = {"author_display_alias": {"5gurl": "iuac", "orange": "rrik", "orange1004": "rrik", "baobab": "iuac"}}
check("5gurl→iuac", D._display_author("Subnet Summer - τ@5gurl", cfg_alias) == "iuac",
      D._display_author("Subnet Summer - τ@5gurl", cfg_alias))
check("orange→rrik", D._display_author("X@orange1004", cfg_alias) == "rrik",
      D._display_author("X@orange1004", cfg_alias))

print("== 5) 严格筛选：水聊剔除 / 相关保留 ==")
noise = [
    E("X@a", "GM"), E("X@a", "👌"), E("X@a", "https://x.com/foo"),
    E("X@a", "/chart@tensorscope_bot"), E("X@a", "用户正在输入..."),
    E("TAO-update-DC@bot", "🔔 新事件通知 关键词: TW粉丝列表 ->用户 here4impact 关注了 Herm"),
]
for i, e in enumerate(noise):
    check("噪音%d剔除" % i, not D._strict_related(e, {}, set()))
kept = [
    E("X@a", "SN53 回购 125k 落地"), E("X@a", "buyback on sn99"),
    E("X@a", "const 表示回购利好"), E("X@a", "子网排放讨论"),
    E("X@a", "这周要上线新版本了"),
]
for i, e in enumerate(kept):
    check("相关%d保留" % i, D._strict_related(e, cfg_alias, set()))

print("== 6) 关键人物本人 vs 提及 ==")
check("本人命中", D._key_self(E("X@const", "hello"), cfg_alias))
check("提及不算本人", not D._key_self(E("X@Cicada", "Const is winning"), cfg_alias))
check("相关性含提及", D._key_hit(E("X@Cicada", "Const is winning"), cfg_alias))
check("别名 iuac 本人命中", D._key_self(E("X@5gurl", "hi"), cfg_alias))

print("== 7) 正文清洗 ==")
check("SN前缀→[SNnn]", "[SN53]" in D._body_clean(E("x", "⛳sn:53 buyback落地")))
check("emoji SN 去掉", "⛳" not in D._body_clean(E("x", "⛳sn:🪙｜price-τalk hello")))
check("Discord提及去掉", "<@&123>" not in D._body_clean(E("x", "⛳sn:31 <@&123> miner release v2")))

print("== 8) build_input 结构（构造 payload） ==")
payload = {
    "day": "2026-11-22",
    "bounds": {"start": int(datetime.datetime(2026, 11, 22, tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp() * 1000),
               "end": int(datetime.datetime(2026, 11, 22, 14, tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp() * 1000)},
    "events": [
        E("Hanlin.AI@Kobbo", "SN53 首次回购 125k 落地", "Hanlin.AI"),
        E("Hanlin.AI@a", "GM", "Hanlin.AI"),
        E("TAO-update-DC@TAO-update-DC", "⛳sn:31 miner release v2", "TAO-update-DC"),
        E("X@const", "const 表示 emission 利好", "Bittensor TAO Price Chat"),
    ],
}
text, st = D.build_input(payload, cfg_alias)
check("水聊被剔除", "GM" not in text)
check("按群分组标题", "Hanlin.AI" in text and "TAO-update-DC" in text)
check("关键人物本人段出现", "关键人物发言" in text and "const" in text)
check("SN 抽取", 53 in st["sns"] and 31 in st["sns"])
check("统计 related", st["related"] == 3, st)
check("无原始链接（本构造无链接）", "http" not in text)

print("\n==== 结果：%d 通过 / %d 失败 ====" % (OK, FAIL))
sys.exit(0 if FAIL == 0 else 1)
