# -*- coding: utf-8 -*-
"""Gamma 机制2 跨子网支付回归测试（v1.29.0）。纯函数测试，不打网络。"""
import gamma_intel as G

PASS, FAIL = [], []


def ck(cond, name):
    (PASS if cond else FAIL).append(name)


# --- 单位换算与比例 ---
ck(abs(G.g_rao("34134389889") - 34.134389889) < 1e-6, "gamma rao→Γ")
ck(abs(G.g_rao("17999999640") - 17.99999964) < 1e-6, "alpha_sold rao→α")
ck(abs(G.share_pct("922337203685477580") - 5.0) < 0.01, "share 5%")
ck(abs(G.share_pct("1844674407370955161") - 10.0) < 0.01, "share 10%")

# --- 配置比例提取 ---
cfg_rows = [
    {"netuid": 120, "gamma_emission_shares": [{"target_netuid": 51, "share": "1844674407370955161"}]},
    {"netuid": 3, "gamma_emission_shares": [{"target_netuid": 51, "share": "922337203685477580"}]},
    {"netuid": 51, "gamma_emission_shares": []},
    {"netuid": None, "gamma_emission_shares": [{"target_netuid": 51, "share": 1}]},
    {"netuid": 99, "gamma_emission_shares": [{"target_netuid": None, "share": 1}]},
]
cfgs = G.emission_share_config(cfg_rows)
ck(len(cfgs) == 2, "配置比例过滤无效行后 2 条")
ck(cfgs[0] == (120, 51, 10.0) or abs(cfgs[0][2] - 10.0) < 0.01, "配置按百分比降序")
ck(all(c[1] == 51 for c in cfgs), "配置目标均为 SN51")

# --- 流水聚合 ---
acts = [
    {"source_netuid": 120, "netuid": 51, "gamma": "1000000000", "usd_value": "1.00",
     "alpha_sold": "2000000000", "timestamp": "2026-11-22T08:00:00Z"},
    {"source_netuid": 120, "netuid": 51, "gamma": "2000000000", "usd_value": "2.00",
     "alpha_sold": "3000000000", "timestamp": "2026-11-22T09:00:00Z"},
    {"source_netuid": 3, "netuid": 51, "gamma": "500000000", "usd_value": "0.50",
     "alpha_sold": "1000000000", "timestamp": "2026-11-22T07:00:00Z"},
    {"source_netuid": None, "netuid": 51, "gamma": "900000000", "usd_value": "9.00",
     "alpha_sold": "900000000", "timestamp": "2026-11-22T06:00:00Z"},  # 无效源，应丢弃
]
agg = G.agg_emission(acts)
ck(len(agg) == 2, "聚合丢弃无效源，剩 2 组")
ck(abs(agg[(120, 51)]["gamma"] - 3.0) < 1e-6, "源120铸造Γ累加=3")
ck(abs(agg[(120, 51)]["usd"] - 3.0) < 1e-6, "源120美元累加=3")
ck(abs(agg[(120, 51)]["alpha"] - 5.0) < 1e-6, "源120卖出α累加=5")
ck(agg[(120, 51)]["cnt"] == 2, "源120笔数=2")
ck(agg[(120, 51)]["last"].startswith("2026-11-22T09"), "最近时间取最大 timestamp")

# --- 流水表筛选口径 ---
lines_all, items_all = G._cross_flow_block(agg)
ck(any("SN120" in l for l in lines_all), "全量流水表含 SN120")
lines_src, items_src = G._cross_flow_block(agg, sn=120, sn_as="src")
ck(len(items_src) == 1 and items_src[0][0] == (120, 51), "按源筛选只剩 SN120")
ck(not any("SN3" in l for l in lines_src), "按源筛选表不含 SN3")
lines_tgt, items_tgt = G._cross_flow_block(agg, sn=51, sn_as="tgt")
ck(len(items_tgt) == 2, "按目标筛选 SN51 含 2 组")

# --- 空数据兜底 ---
empty_agg = G.agg_emission([])
le, ie = G._cross_flow_block(empty_agg)
ck(any("无跨子网支付流水" in l for l in le) and not ie, "空流水如实标注")

# --- 周月边界 ---
now = G.datetime.datetime(2026, 11, 22, 17, 0, tzinfo=G._TZ8)
_, _, wl = G.period_bounds("week", now)
ml = G.period_bounds("month", now)[2]
ck("2026-11-16" in wl and "周一" in wl, "本周起算日 11-16 周一")
ck("2026-11-01" in ml, "本月起算 11-01")

# --- 机制不混算：emission_mint 走独立端点，share 不当付款额 ---
src = open(G.__file__, encoding="utf-8").read()
ck('"emission_mint"' in src and "fetch_emission_mint" in src, "跨子网走 emission_mint 端点")
ck("crossweek" in src and "crossmonth" in src, "跨子网周月 action 存在")
ck("绝不混算" in src, "模块声明机制不混算")

print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAIL:", f)
    raise SystemExit(1)
