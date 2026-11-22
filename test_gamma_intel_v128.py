# -*- coding: utf-8 -*-
"""Gamma 情报汇总回归测试（v1.28.0）。不依赖真实 key，用构造数据验证换算/聚合/边界/兜底。"""
import datetime
import json
import os
import tempfile

import gamma_intel as G

PASS = []
FAIL = []


def ck(cond, name):
    (PASS if cond else FAIL).append(name)


def rao(x):
    return str(int(x * 1e9))


# 1) 单位换算
ck(abs(G.g_rao(rao(53.5)) - 53.5) < 1e-6, "g_rao rao->Gamma")
ck(G.fmt_g(rao(1234.5678)) == "1,234.57", "fmt_g 千分位+两位")
ck(G.fmt_usd("0.10") == "0.10", "fmt_usd 原值美元")
ck(abs(G.share_pct(922337203685477580) - 5.0) < 1e-6, "share u64 -> 5%")
ck(abs(G.share_pct(1844674407370955161) - 10.0) < 1e-6, "share u64 -> 10%")
ck(G.short_addr("5Cd9UfBzjWwQQke1sndyP6gpnxPWqUZFWvLdfDDN") == "5Cd9UfBz…LdfDDN", "地址缩写前8后6")
ck(G.short_addr(None) == "未知", "地址空值")

# 2) SN 名称映射（用本地 subnets.json）
ck(G.sn_name(51) == "SN51 lium.io", "SN名称-本地KB")
ck(G.sn_name(999).startswith("SN999"), "SN名称-未知回退")

# 2b) 蓝色链接名称（v1.29.2：开通列表名称统一蓝色链接）
lk = G.sn_name_link(51)
ck(lk == "SN51 [lium.io](https://taostats.io/subnets/51)", "名称链接格式SN51")
ck(lk.startswith("SN51 [") and lk.endswith(")"), "编号普通字+名称包链接")
ck(G.sn_name_link(117) == "SN117 [everyframe.studio](https://taostats.io/subnets/117)", "名称链接SN117域名")
ck(G.sn_name_link(999) == "SN999", "未知SN无名称不包链接")
# 链接语法在两通道转义后必须完好（否则蓝色链接会失效）
import pipeline as _P
ck(_P._tg_md(_P.mark_changes_tg(lk)) == lk, "TG转义不破坏链接")
ck(_P.mark_changes_feishu(lk) == lk, "飞书转义不破坏链接")

# 3) 开通子网过滤+排序
rows = [
    {"netuid": 51, "gamma_enabled": True, "gamma_spent": rao(100)},
    {"netuid": 80, "gamma_enabled": True, "gamma_spent": rao(500)},
    {"netuid": 7, "gamma_enabled": False, "gamma_spent": rao(999)},
]
en = G.enabled_subnets(rows)
ck(len(en) == 2 and en[0]["netuid"] == 80, "开通过滤+按花费降序")

# 4) 按子网聚合
acts = [
    {"netuid": 51, "gamma": rao(10)}, {"netuid": 51, "gamma": rao(5)},
    {"netuid": 80, "gamma": rao(3)}, {"netuid": None, "gamma": rao(7)},
]
agg = G._agg_by_sn(acts)
ck(abs(agg[51][0] - 15) < 1e-6 and agg[51][1] == 2, "按SN聚合求和+笔数")
ck(80 in agg and None not in agg, "聚合跳过非法netuid")

# 5) 付款方聚合
spends = [
    {"netuid": 80, "gamma": rao(100), "hotkey": "AAA"},
    {"netuid": 80, "gamma": rao(50), "hotkey": "AAA"},
    {"netuid": 51, "gamma": rao(20), "hotkey": "BBB"},
]
pay = G._top_payers(spends, 5)
ck(pay[0][0] == "AAA" and abs(pay[0][1] - 150) < 1e-6, "付款方聚合降序")
ck(pay[0][2] == 80, "付款方主要子网")

# 6) 自然周/月边界（北京时间）
now = datetime.datetime(2026, 11, 22, 17, 0, tzinfo=G._TZ8)
w0, w1, wl = G.period_bounds("week", now)
ck(datetime.datetime.fromtimestamp(w0, G._TZ8).strftime("%Y-%m-%d %H:%M") == "2026-11-16 00:00", "自然周起点=周一")
m0, m1, ml = G.period_bounds("month", now)
ck(datetime.datetime.fromtimestamp(m0, G._TZ8).strftime("%Y-%m-%d") == "2026-11-01", "自然月起点=1号")
ck(w1 == m1 == int(now.timestamp()), "周月终点一致=现在")

# 7) 修改时间不影响全局（纯函数）
ck(G.period_bounds("week", now)[0] != G.period_bounds("month", now)[0], "周月边界互不污染")

# 8) 汇总防双重换算：g_rao 后已是 Γ，直接用 format 展示，不能再过 fmt_g
#    回归 v1.28 实测 bug：tot_mint=g_rao(...) 后误传 fmt_g 导致显示 0.00
tot_mint = sum(G.g_rao(d.get("gamma_minted")) for d in [
    {"gamma_minted": rao(30000)}, {"gamma_minted": rao(29855.5)}])
shown = format(tot_mint, ",.2f")
ck(shown == "59,855.50", "汇总Γ值不双重换算")
ck(shown != G.fmt_g(tot_mint), "汇总值不得再传fmt_g")

# 9) 开通列表按编号升序 + 名称链接（v1.29.2），mock fetch_subnets 不依赖网络
_orig_fetch = G.fetch_subnets
try:
    G.fetch_subnets = lambda cfg, log=None: [
        {"netuid": 117, "gamma_enabled": True, "gamma_spent": rao(1)},
        {"netuid": 25, "gamma_enabled": True, "gamma_spent": rao(2)},
        {"netuid": 51, "gamma_enabled": True, "gamma_spent": rao(3)},
        {"netuid": 80, "gamma_enabled": True, "gamma_spent": rao(4)},
    ]
    rr = G.run_gamma({}, "list", push=False)
    body = rr["digest"]
    sn_order = [int(l.split(". SN")[1].split(" ")[0])
                for l in body.splitlines() if ". SN" in l]
    ck(sn_order == [25, 51, 80, 117], "开通列表按编号升序")
    ck("[lium.io](https://taostats.io/subnets/51)" in body, "列表名称蓝色链接")
finally:
    G.fetch_subnets = _orig_fetch

print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for n in FAIL:
    print("  FAIL:", n)
if FAIL:
    raise SystemExit(1)
