"""v1.30.11 回归：群内传言/阴阳推论不得写成客观因果。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline as P

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

# 1) 提示词新增 8c 硬规则，且保留原 8b 身份同一性规则
check("prompt 8c 群内传言不得写成客观因果",
      "群内传言/阴阳推论不得写成客观因果" in P.SYSTEM_PROMPT)
check("prompt 8c 要求标注未经证实",
      "未经证实" in P.SYSTEM_PROMPT and "该因果仅为当事人说法" in P.SYSTEM_PROMPT)
check("prompt 8b 同一人不得拆两主体仍在",
      "禁止把同一个人拆成两个主体" in P.SYSTEM_PROMPT)

# 2) 版本号已升（后续版本也满足，不绑死具体补丁号）
_v = tuple(int(x) for x in P.__version__.split("."))
check("version >= 1.30.11", _v >= (1, 30, 11))

# 3) 11-26 日报第 2 行按如实口径：保留信息点、标注未经证实、不再写「印证提前获知」成立因果
p = "exports/dtao_2026-11-26.digest.txt"
if os.path.exists(p):
    d = open(p, encoding="utf-8").read()
    line = next((l for l in d.splitlines() if "5GuRL" in l), "")
    check("日报保留 SN50 内幕信息点", "SN50" in line and "内幕" in line)
    check("日报标注群内说法未经证实", "未经证实" in line and "群内说法" in line)
    check("日报分开写 orange 与 5GuRL 各自说法",
          "orange" in line and "5GuRL" in line and "自述" in line)
    check("日报不再写「印证提前获知」成立因果", "印证了此前关于他提前获知" not in d)
    check("日报提及第三种身份说法(即 Const 本人)", "Const 本人" in line)
else:
    print("SKIP 日报文件不存在（非交付环境）")

print("\n%d passed, %d failed" % (ok, bad))
sys.exit(1 if bad else 0)
