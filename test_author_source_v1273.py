# -*- coding: utf-8 -*-
"""v1.27.3 自测：社区动态人名（备注优先/名称主体）+ 来源简写 (X)/(dc)/(tg)。"""
import community_digest as CD
import subnet_research as SR

PASS = FAIL = 0


def check(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s  %s" % (label, extra))


print("\n[1] 平台 → 来源简写")
for plat, want in [("twitter", "X"), ("x", "X"), ("discord", "dc"),
                   ("telegram", "tg"), ("manual", "手动")]:
    got = CD._plat_short(plat)
    check(got == want, "%s → %s" % (plat, want), "得到 %s" % got)

print("\n[2] 人名：备注优先 / 无备注取名称主体")
for raw, want in [
    ("consttt,备注:const", "const"),
    ("canigonow,备注:17", "17"),
    ("Millie@AltcoinMillie", "Millie"),
    ("const@const_reborn", "const"),
    ("p383_54249", "p383"),
    ("Aphτ 🌊α@tao_Alph", "Aph"),
    ("TheBittensorBureau@TheBittensorBureau", "TheBittensorBureau"),
]:
    got = CD._display_author(raw)
    check(got == want, "%r → %s" % (raw, want), "得到 %r" % got)

print("\n[3] 输入段 _format_community_today：人名清洗 + 来源简写")
events = [
    {"author": "Millie@AltcoinMillie", "platform": "twitter", "content": "SN93 营收不错",
     "occurredAt": 1763800000000, "sn": [93]},
    {"author": "canigonow,备注:92", "platform": "discord", "content": "SN93 回购正在推进",
     "occurredAt": 1763800100000, "sn": [93]},
    {"author": "TAO-update-DC@TAO-update-DC", "platform": "telegram", "content": "SN93 新闻",
     "occurredAt": 1763800200000, "sn": [93]},
]
payload = {"events": events, "bounds": {}}
block, n = SR._format_community_today(payload, 93, cfg={})
print(block[:600])
check("(X)" in block and "<Millie>" in block, "推特人名去@handle、来源标 (X)")
check("(dc)" in block and "<92>" in block, "DC 人名取备注、来源标 (dc)")
check("(tg)" in block and "<TAO-update-DC>" in block, "TG 来源标 (tg)")
check("[Twitter/X]" not in block and "[Telegram]" not in block and "[Discord]" not in block,
      "无平台全称残留")

print("\n[4] 输出兜底：假 LLM 输出的旧式人名也被重建为 人名(来源)")
# 构造最小事件集喂给 _people_summary_block，用假 llm 返回旧格式文本
mine_events = [
    {"author": "Millie@AltcoinMillie", "platform": "twitter", "content": "SN93 Bitcast 营收 49.3 万美元，75% 用于回购", "sn": [93]},
    {"author": "canigonow,备注:92", "platform": "discord", "content": "SN93 回购正在推进", "sn": [93]},
]
fake_payload = {"events": mine_events}
meta = {"sn": 93, "name": "Bitcast"}


def fake_llm(_body):
    # 故意用旧格式：人名带 @handle、来源写全称/漏写，检验兜底重建
    return ("Millie@AltcoinMillie 表示：SN93 营收 49.3 万美元，75% 回购\n"
            "canigonow,备注:92(Twitter/X) 表示：SN93 回购在推进")


cs, cn = SR._people_summary_block(fake_payload, {}, 93, meta, fake_llm, log=lambda *_: None)
print(cs)
check(cn == 2, "两条都保留")
check("Millie(X) 表示" in cs, "人名去 handle + 来源 (X) 补上")
check("92(dc) 表示" in cs, "人名取备注 92 + 全称来源被替换为 (dc)")
check("Twitter/X" not in cs and "AltcoinMillie" not in cs and "备注:" not in cs,
      "全称来源/@handle/备注标签均未在输出残留")

print("\n==== 结果：%d PASS / %d FAIL ====" % (PASS, FAIL))
if FAIL:
    raise SystemExit(1)
