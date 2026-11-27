"""v1.30.13 回归：DC 日报人名装饰简化（cicada | τ → cicada），别名映射与核心人物判定不变。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline as P
import json
_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CFG = json.load(open(_cfg_path, encoding="utf-8")) if os.path.exists(_cfg_path) else {}

ok = 0
bad = 0

def check(name, cond):
    global ok, bad
    if cond:
        ok += 1
        print("PASS", name)
    else:
        bad += 1
        print("FAIL", name)

check("version >= 1.30.13", tuple(int(x) for x in P.__version__.split(".")) >= (1, 30, 13))

# 1) 装饰清理：cicada | τ → cicada 等（与社区日报口径一致）
for raw, want in [
    ("cicada | τ", "cicada"),
    ("✠ PUNISHER ττ ✠", "PUNISHER"),
    ("Aphτ 🌊α", "Aph"),
    ("Chairman τao", "Chairman"),
]:
    check("装饰清理 %r -> %s" % (raw, want), P._display_name(raw) == want)

# 2) 别名映射仍优先（5gurl→iuac）
check("别名映射 5gurl->iuac", P._display_name("5gurl", CFG) == "iuac")
check("别名映射不受装饰影响", P._display_name("cicada | τ") == "cicada")

# 3) 辨识码清理
check("辨识码 p383_54249->p383", P._display_name("p383_54249") == "p383")

# 4) 普通名字/连写数字保留（lunaa1244、ha 是真人名）
check("lunaa1244 保留", P._display_name("lunaa1244") == "lunaa1244")
check("ha 保留(非截断)", P._display_name("ha") == "ha")

# 5) 核心人物判定走原始名，不受显示名影响
check("核心人物判定 5gurl 仍为 key", P._is_daily_key("5gurl") is True)

# 6) 实际日报：无装饰残留，SN129 归属正确
p = "exports/dtao_2026-11-26.digest.txt"
if os.path.exists(p):
    d = open(p, encoding="utf-8").read()
    check("日报无 cicada | τ 残留", "cicada | τ" in d and False or "cicada | τ" not in d)
    check("日报含 cicada 与 SN129", "cicada" in d and "SN129" in d)
    check("日报保留内幕信息点(不删)", "群内说法，未经证实" in d)
else:
    print("SKIP 日报文件不存在")

print("\n%d passed, %d failed" % (ok, bad))
sys.exit(1 if bad else 0)
