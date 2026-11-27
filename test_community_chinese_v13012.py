"""v1.30.12 回归：社区日报禁止照抄整句英文（提示词硬规则 + 代码层兜底）。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import community_digest as CD
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

# 1) 提示词含中文硬规则
sp = CD.COMMUNITY_SYSTEM_PROMPT
check("提示词含语言铁律标题", "语言铁律" in sp and "禁止照抄英文原句" in sp)
check("提示词禁整句英文照搬", "禁止把整句英文照搬进中文句子" in sp)
check("提示词要求两个以上英文词改写", "出现两个以上英文单词" in sp)
check("提示词要求输出前自检英文", "输出前逐条自检" in sp)

# 2) 版本号
check("version >= 1.30.12", tuple(int(x) for x in P.__version__.split(".")) >= (1, 30, 12))

# 3) 英文检测函数
cases = [
    ("const 宣布接管 SN50，涨 76%", 0),
    ("- 官方称 Engy 营收增长（Discord · const）", 0),
    ("const 表示 I will be coming into help push Synth forward as the lead operator", 1),
    ("原文 Expect a rocky few days and maybe weeks 被多人转推", 1),
    ("- **SN50 Synth**（73 条）：团队确认 owner rewards 清零", 0),
]
for text, expect_min in cases:
    n = len(CD.english_runs(text))
    check("检测[%s] => %d" % (text[:24], n), (n > 0) if expect_min else (n == 0))

# 4) 实际重出的日报无整句英文
p = "exports/community/community_2026-11-26.digest.txt"
if os.path.exists(p):
    d = open(p, encoding="utf-8").read()
    check("11-26 社区日报无整句英文", len(CD.english_runs(d)) == 0)
    check("11-26 社区日报含社区热点节", "社区热点" in d)
    check("11-26 社区日报含子网动态节", "子网动态" in d)
else:
    print("SKIP 日报文件不存在")

print("\n%d passed, %d failed" % (ok, bad))
sys.exit(1 if bad else 0)
