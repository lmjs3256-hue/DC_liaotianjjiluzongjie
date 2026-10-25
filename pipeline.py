#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DTAO price-talk 每日流水线：Discord 导出 -> DeepSeek 总结 -> 飞书 + Telegram 双推
=============================================================================
仅依赖 Python 标准库（无需 pip install）。半自动模式：由人工手动触发
（终端命令 / 在 WorkBuddy 对话里让我执行），也可接入 cron / 任务计划程序。
默认导出「前一天」指定频道的聊天记录，过滤闲聊，用 DeepSeek 生成中文结构化
日报，并推送到飞书群机器人和 Telegram Bot。支持指定日期查询、以及 repush 补推。

========= 首次使用 Setup =========
1) 复制 config.example.json -> config.json，填入你的密钥：
   - discord_token      : 你的 Discord user token（能读该频道）
   - channel_id         : 频道 ID（默认 DTAO price-talk = 1341812134807343114）
   - deepseek_api_key   : DeepSeek API key
   - feishu_webhook_url : 飞书自定义群机器人 webhook 地址
   - feishu_secret      : 飞书机器人签名密钥（没开签名则留空 "")
   - telegram_bot_token : @BotFather 拿到的 bot token
   - telegram_chat_id   : 目标聊天 ID（群组形如 -100xxxx）
2) 选择导出后端 dce_backend：
   - "docker" : 主机需装好 Docker（推荐，镜像自带 .NET，命令一致）
   - "local"  : 下载官方 DiscordChatExporter.Cli 的 exe，填入 dce_exe 绝对路径
3) 运行：  python pipeline.py
4) 排错看 run.log。

注意：使用 user token 自动化读取频道违反 Discord TOS，存在封号风险，风险自担。
=============================================================================
"""

import os
import sys
import json
import re
import glob
import subprocess
import datetime
import time
import hashlib
import base64
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "run.log")

# 全局配置（main 启动时填充），供网络函数读取代理设置
_CFG = None

def _opener():
    """按 config.proxy 构造 urllib opener。
    proxy=""            -> 系统默认（沿用环境变量）
    proxy="direct"      -> 完全不走代理（本机已开系统级 VPN 时用）
    proxy="http://h:p"  -> 强制走该代理（填自己的 VPN/Clash 代理地址）
    """
    if _CFG is None:
        return urllib.request.build_opener()
    p = _CFG.get("proxy", "")
    if p == "direct":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if p:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": p, "https": p}))
    # proxy="" 系统默认：若本机开了 Clash/V2Ray 系统代理，自动继承，免填端口
    sp = _windows_system_proxy()
    if sp:
        http, https = sp
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": http, "https": https}))
    return urllib.request.build_opener()

def _windows_system_proxy():
    """读取 Windows 系统代理（注册表 Internet Settings），返回 (http_proxy, https_proxy) 或 None。
    当 Clash/V2Ray 开启了「系统代理」时，DiscordChatExporter 子进程可借此自动翻墙。"""
    try:
        import winreg
    except ImportError:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if not enabled:
            return None
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if not server:
            return None
        # ProxyServer 可能是 "127.0.0.1:7890" 或 "http=127.0.0.1:7890;https=127.0.0.1:7890;..."
        if "=" in server:
            d = {}
            for part in server.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    d[k.strip().lower()] = v.strip()
            http = d.get("http") or d.get("https") or d.get("socks")
            https = d.get("https") or d.get("http") or http
        else:
            http = https = server
        if not http:
            return None
        return http, https
    except Exception:
        return None

# ----------------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------------
def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (ts, msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise RuntimeError("找不到 config.json，请先复制 config.example.json 并填写密钥")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

# ----------------------------------------------------------------------------
# 0) 选择 Discord token：支持单 token 兼容 + 多 token 按日期轮换
# ----------------------------------------------------------------------------
def pick_discord_token(cfg, day_str):
    """返回本次导出用的 token。
    - 配置了 discord_tokens（数组，且至少一个非空）-> 按日期确定性轮换，每天用不同 token，
      把单账号请求密度摊薄，降低 user token 被风控命中的概率。
    - 否则回退到单个 discord_token（向后兼容）。
    注意：轮换模式下，每个 token 对应的账号都必须是该服务器成员且能访问目标频道。
    """
    pool = [t for t in (cfg.get("discord_tokens") or []) if t]
    if pool:
        try:
            d = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
            idx = (d - datetime.date(2026, 1, 1)).days % len(pool)
        except Exception:
            idx = 0
        log("使用 discord_tokens 池中第 %d 个（共 %d 个），导出日 %s" % (idx + 1, len(pool), day_str))
        return pool[idx]
    single = cfg.get("discord_token")
    if not single:
        raise RuntimeError("config.json 缺少 discord_token，且 discord_tokens 为空")
    return single

# ----------------------------------------------------------------------------
# 1) 用 DiscordChatExporter 导出「前一天」频道记录为 JSON
# ----------------------------------------------------------------------------
def run_export(cfg, after_dt, before_dt, out_json):
    day_str = after_dt.strftime("%Y-%m-%d")
    token = pick_discord_token(cfg, day_str)
    channel = str(cfg["channel_id"])
    backend = cfg.get("dce_backend", "docker").lower()
    after = after_dt.strftime("%Y-%m-%d")
    before = before_dt.strftime("%Y-%m-%d")

    if backend == "docker":
        out_dir = os.path.dirname(out_json)
        out_name = os.path.basename(out_json)
        os.makedirs(out_dir, exist_ok=True)
        cmd = [
            "docker", "run", "--rm", "-v", "%s:/out" % out_dir,
            "tyrrrz/discordchatexporter:stable", "export",
            "-t", token, "-c", channel, "-f", "Json",
            "-o", "/out/%s" % out_name,
            "--after", after, "--before", before,
        ]
    else:
        exe = cfg.get("dce_exe")
        if not exe or not os.path.exists(exe):
            raise RuntimeError("dce_backend=local 但找不到 dce_exe：%s" % exe)
        os.makedirs(os.path.dirname(out_json), exist_ok=True)
        cmd = [exe, "export", "-t", token, "-c", channel, "-f", "Json",
               "-o", out_json, "--after", after, "--before", before]

    display = " ".join(cmd).replace(token, "***")
    log("导出命令：" + display)
    # 代理设置：让 DiscordChatExporter(.NET) 走指定出口
    env = os.environ.copy()
    p = cfg.get("proxy", "")
    if p == "direct":
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.pop(k, None)
    elif p:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[k] = p
    else:
        # proxy="" 系统默认：若本机开了 Clash/V2Ray 系统代理，自动继承，免填端口
        sp = _windows_system_proxy()
        if sp:
            http, https = sp
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env[k] = https if k.lower().endswith("https") else http
            log("检测到 Windows 系统代理，导出将走：%s" % http)
        else:
            log("未检测到 Windows 系统代理（proxy=空）。如需翻墙导出 Discord，请在 Clash/V2Ray 开启「系统代理」，或在 config.json 显式填写 proxy")
    # 超时保护：避免网络不通时无限卡死
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           env=env, timeout=600)
    except subprocess.TimeoutExpired:
        raise RuntimeError("DiscordChatExporter 超时（600s）：很可能网络无法连到 Discord，"
                           "请检查本机 VPN/代理，或在 config.json 设置 proxy")
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "")[:2000]
        raise RuntimeError("DiscordChatExporter 执行失败：\n%s" % err)
    if not os.path.exists(out_json):
        raise RuntimeError("导出完成但未生成文件：%s" % out_json)
    log("导出成功：%s (%d 字节)" % (out_json, os.path.getsize(out_json)))

# ----------------------------------------------------------------------------
# 2) 过滤闲聊，抽取与 Bittensor 子网 / 重要成员相关的实质内容
# ----------------------------------------------------------------------------
SUBSTANCE = re.compile(
    r"(sn\s?\d{1,3}|subnet\s?\d{1,3}|\bdtao\b|yuma|maciej|bittensor|buyback|buy back|"
    r"emission|dereg\w*|deregistration|coldkey|cold key|liquidity pool|\blp\b|\balpha\b|"
    r"\broot\b|taostats|tao\.app|\bvalidator\b|\bminer\b|\bshort\b|\bscam\b|\brug\b|"
    r"\bpool\b|\bsynth\b|chain buys|net buy|revenue|\bowner\b|proof of|consensus|"
    r"launch\w* subnet|new subnet|register\w* subnet|announce|deploy|migrat\w*|"
    r"listing|\bcex\b|\bdex\b|airdrop|whale|\bdump\b|\bpump\b|liquidit)", re.I)

NAMES = re.compile(
    r"(lium|leadpoe\w*|engy|score|babelbit|djinn|iota|herald|minotaur|niome|almanac|"
    r"redteam|bitsec|yanez|vanta|zils|synth|siam|\bfish\b|const|maciej|oscar|\bcow2|"
    r"\brhef|coconu|tfig|maclura|lychee|peaches|subconnect|jeetz|happy ai|haptic|"
    r"targil|staw|stability|score44|graphite|chutes|templar|goppy|milady|timewalker|"
    r"deity|kakashi|dr reed|moonlit|yuneric|baobab|\bcats\b|dougsillars|papaya|seb|"
    r"ben\.tao|bdhime\w*)", re.I)

MEMBERS = re.compile(
    r"(kakashi|dr reed|subconnect|peaches|moonlit|yuneric|\bfish\b|siam|baobab|\bcats\b|"
    r"dougsillars|papaya|bdhime\w*|ben\.tao|seb|niome|yuma|maciej|const|leadpoe\w*|"
    r"babelbit|djinn|iota|almanac|engy|herald|minotaur|oscar|\bcow2|\brhef|maclura|"
    r"tfig|coconu|guava|pineapple|lychee|banana|there is no spoon|geodurian|"
    r"passi0nfruit|roman|nemanjitap|kaimorris|fitz|galactic|sudachi|ayzndel|"
    r"namikaze|nobody|sigma)", re.I)

NOISE = re.compile(r"^(\s*(https?://\S+\s*)+|(\:\w+:\s*)+|(\[(?:attachment|embed)\]\s*)+)$", re.I)

def is_substantive(content):
    if not content:
        return False
    return bool(SUBSTANCE.search(content) or NAMES.search(content) or MEMBERS.search(content))

def extract_substantive(json_path):
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    msgs = data.get("messages", [])
    out = []
    for m in msgs:
        c = (m.get("content") or "").strip()
        if not c:
            continue
        if NOISE.match(c):
            continue
        if not is_substantive(c):
            continue
        author = (m.get("author") or {}).get("nickname") or (m.get("author") or {}).get("name") or "?"
        t = (m.get("timestamp") or "")[11:16]
        react = sum(r.get("count", 0) for r in m.get("reactions", []))
        out.append((react, t, author, c))
    # 按反应数降序，让高亮消息在前
    out.sort(key=lambda x: -x[0])
    return out

# ----------------------------------------------------------------------------
# 3) 调用 DeepSeek 生成中文结构化日报
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "你是一名 Bittensor 生态的资深研究员，负责把 Discord 频道「DTAO - price-talk」的每日聊天记录，"
    "整理成一份「重点突出、细节不漏、还能挖到布局机会」的中文日报。\n"
    "\n"
    "【内容边界】\n"
    "- 只关注：Bittensor 子网（subnet / SN 编号）、代币机制（DTAO/TAO、回购销毁、emission、注销、LP、Alpha、权重/流动性）、"
    "重要社区成员/核心开发者动态、交易所/合作/上线等催化事件。\n"
    "- 忽略：纯表情包、水聊、无信息量的喊单、与 Bittensor 无关的话题。\n"
    "\n"
    "【输出结构（严格按顺序；无内容的小节写「暂无」或省略；所有小节标题都用 **加粗**）】\n"
    "**🔥 必读重点**（2-4 条，当天最该知道的；每条一句说清「谁/什么 + 关键结论」，并把关键人物、**SN编号/子网名**、事件用 **加粗**）\n"
    "**🔢 重点子网动向**（有异动的子网：写 **SN编号 / 子网名** + 一句话状态；上线/rug/机制变更/算力异动务必标 ⚠️ 或 ✅）\n"
    "**👥 重要人物观点**（点名 **@人物/开发者**，他讲了什么、对什么有影响；普通群友无关发言不列）\n"
    "**📊 潜力观察 · 近期可布局**（核心新增：从讨论中挖掘值得提前关注的子网/标的/事件。每条含：标的/子网 → 预期(上所/升级/需求/合作/机制变更) → 催化剂与时间窗 → 逻辑 → 确定性(高/中/低)。最多 3-5 条，按临近度+确定性排序。仅基于讨论内容不臆造；仅暗示请标「推测」。当日无明显催化写「暂无明确催化」）\n"
    "**📎 其他细节**（补遗：前面没覆盖但有价值的细节，确保不错过）\n"
    "**💡 一句话总结**（当天全局判断，一句收束）\n"
    "\n"
    "【排版与强调规范（飞书/TG 双平台通用，务必遵守）】\n"
    "- 关键主体一律 **加粗**：人物名、**SN编号/子网名**、代币、事件、关键数字。\n"
    "- 用 emoji 标注程度：🔥 重大利好/热点、✅ 已落地、⚠️ 风险/异动、📌 值得关注、❌ 利空。\n"
    "- 每条要点单独成行，用 - 项目符号；小节之间空一行；避免横排长句，适合手机端。\n"
    "- 不要用 # 标题、不要用 > 引用（部分平台不渲染）。\n"
    "- 客观、不臆测、引用具体编号与名字；全文 900 字以内（潜力板块优先保住）。"
)

WEEKLY_SYSTEM_PROMPT = (
    "你是一名 Bittensor 生态的资深研究员，负责把 Discord 频道「DTAO - price-talk」过去 7 天的聊天记录，"
    "整理成中文周报，侧重「一周趋势、跨日升温信号、以及更确定的布局机会」。\n"
    "\n"
    "【内容边界】同日报：只关注子网、代币机制、核心成员动态、催化事件；忽略水聊与无关内容。\n"
    "\n"
    "【输出结构（严格按顺序；小节标题用 **加粗**）】\n"
    "**🔥 本周核心趋势**（3-5 条一周主线；标注哪些是「多次提及/持续升温」的）\n"
    "**🔢 重点子网动向**（本周有变化的子网：**SN编号/子网名** + 状态；上线/rug/机制变更标 ⚠️✅）\n"
    "**👥 重要人物观点**（**@人物** 本周关键表态及影响）\n"
    "**📊 潜力观察 · 近期可布局**（基于一周累积信号给更确定的布局线索：标的/子网 → 预期 → 催化剂与时间窗 → 逻辑 → 确定性(高/中/低)。最多 5 条，按确定性+临近度排序。仅基于讨论不臆造；推测项标「推测」）\n"
    "**⚠️ 风险汇总**（本周风险点/警示）\n"
    "**📎 其他细节**（补遗，不错过有价值信息）\n"
    "**💡 一句话总结**\n"
    "\n"
    "【排版规范】同日报：关键主体 **加粗**；🔥✅⚠️📌❌ 标注程度；- 项目符号、空行分段、手机端友好；"
    "不用 # 标题、不用 > 引用；客观不臆测，引用具体编号与名字；全文 1100 字以内。"
)

DEEPEN_HINT = (
    "\n\n【本次为重总结 / 合并总结】请在保持全部结构的前提下做更深入、更细致的分析："
    "重点深化「📊 潜力观察·近期可布局」——为每条补充逻辑链条、催化剂确定性(高/中/低)与更具体的时间窗推断；"
    "对「🔢 重点子网动向」补充更明确的判断与依据；避免流水账式复述，聚焦结论与逻辑。"
)

def call_deepseek(cfg, text_block, system_prompt=None):
    api_key = cfg["deepseek_api_key"]
    base = cfg.get("deepseek_base_url", "https://api.deepseek.com/v1").rstrip("/")
    model = cfg.get("deepseek_model", "deepseek-chat")
    url = base + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
            {"role": "user", "content": text_block},
        ],
        "temperature": 0.3,
        "max_tokens": 2500,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", "Bearer %s" % api_key)
    req.add_header("Content-Type", "application/json")
    try:
        with _opener().open(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError("DeepSeek HTTP 错误 %s: %s" % (e.code, e.read().decode("utf-8")[:500]))
    except Exception as e:
        raise RuntimeError("DeepSeek 调用失败：%s" % e)

def build_text_block(channel_name, day, items):
    lines = ["频道：%s    日期：%s    当日实质讨论 %d 条（已按热度排序，仅含子网/成员相关）\n"
             % (channel_name, day, len(items))]
    for i, (react, t, author, c) in enumerate(items, 1):
        c1 = c.replace("\n", " ").strip()
        if len(c1) > 600:
            c1 = c1[:600] + "…"
        lines.append("%d. [%s] <%s> %s" % (i, t, author, c1))
    return "\n".join(lines)

# ----------------------------------------------------------------------------
# 4) 推送：飞书（带签名）+ Telegram（分片）
# ----------------------------------------------------------------------------
def chunk_text(text, limit):
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n"):
        while len(para) > limit:
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(para[:limit]); para = para[limit:]
        if len(cur) + len(para) + 1 > limit:
            if cur:
                chunks.append(cur)
            cur = para
        else:
            cur = (cur + "\n" + para) if cur else para
    if cur:
        chunks.append(cur)
    return chunks

import hmac as _hmac

def feishu_sign(secret):
    # 飞书官方签名算法（与开放平台文档完全一致）：
    #   string_to_sign = timestamp + "\n" + secret
    #   hmac_code = HMAC_SHA256(key=string_to_sign, msg=空串)，再 base64
    timestamp = str(int(time.time()))
    string_to_sign = "%s\n%s" % (timestamp, secret)
    hmac_code = _hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    return timestamp, sign

# ---------------------------------------------------------------------------
# 排版增强：识别「EMOJI 标题行」并加粗（飞书 lark_md / TG Markdown 通用）
# ---------------------------------------------------------------------------
_SECTION_EMOJI = ("🔥", "🔢", "👥", "📊", "📎", "💡", "📌", "⚠️", "✅", "❌", "📝", "📈", "🧭")


def prettify_headers(text):
    """把以板块 emoji 开头的『标题行』加粗，让重点更突出。
    仅当该行较短（像标题而非正文）、且非列表项、且尚未加粗时才加粗，避免误伤。"""
    out = []
    for ln in text.split("\n"):
        s = ln.strip()
        if s.startswith(_SECTION_EMOJI) and not s.startswith(("-", "•", "*")) \
                and not s.startswith("**"):
            rest = s
            for e in _SECTION_EMOJI:
                if rest.startswith(e):
                    rest = rest[len(e):].strip()
                    break
            if 0 < len(rest) <= 30:
                out.append("**" + s + "**")
            else:
                out.append(ln)
        else:
            out.append(ln)
    return "\n".join(out)


def push_feishu(cfg, title, digest):
    url = cfg.get("feishu_webhook_url")
    if not url:
        log("未配置 feishu_webhook_url，跳过飞书推送")
        return
    secret = cfg.get("feishu_secret", "")
    # 手机端：wide_screen_mode 铺满屏宽；板块标题加粗，重点更突出
    m = re.search(r"(\d{4}-\d{2}-\d{2})", title)
    day = m.group(1) if m else ""
    body = prettify_headers(digest)
    # 正文顶部放「加粗标题 + 日期行」，方便在飞书里复制文案时直接选到标题与日期
    title_line = "**%s**" % title
    date_line = ("📅 %s　·　📱 手机端优化　·　🤖 DeepSeek 生成" % day) if day else "📱 手机端优化　·　🤖 DeepSeek 生成"
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": title_line}},
        {"tag": "div", "text": {"tag": "lark_md", "content": date_line}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": body}},
        {"tag": "hr"},
        {"tag": "note", "content": "DTAO price-talk 日报 · 由 DeepSeek 生成"},
    ]
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
            "elements": elements,
        },
    }
    if secret:
        ts, sign = feishu_sign(secret)
        card["timestamp"] = ts
        card["sign"] = sign
    _post_json(url, card)
    log("飞书推送完成")

def _tg_md(s):
    # 把飞书 lark_md 的 **加粗** 转成 Telegram 旧版 Markdown 的 *加粗*，
    # 并转义会破坏解析的字符（保留 * 用于加粗）
    s = re.sub(r"\*\*(.+?)\*\*", r"*\1*", s)
    s = s.replace("\\", "\\\\").replace("`", "\\`").replace("_", "\\_")
    return s

def push_telegram(cfg, title, digest):
    token = cfg.get("telegram_bot_token")
    chat_id = cfg.get("telegram_chat_id")
    if not token or not chat_id:
        log("未配置 telegram bot/chat_id，跳过 TG 推送")
        return
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    # TG 仅支持基础 Markdown：板块标题加粗 + 上下分隔线框，提升可读性
    body = _tg_md(prettify_headers(digest))
    sep = "────────────"
    t = _tg_md(title)
    full = "*%s*\n%s\n\n%s\n\n%s\n🤖 由 DeepSeek 生成 · DTAO price-talk 日报" % (t, sep, body, sep)
    for i, part in enumerate(chunk_text(full, 3900), 1):
        payload = {"chat_id": chat_id, "text": part,
                   "parse_mode": "Markdown", "disable_web_page_preview": True}
        try:
            _post_json(url, payload)
        except Exception as e:
            # Markdown 解析失败兜底：纯文本重发，保证不丢消息
            _post_json(url, {"chat_id": chat_id, "text": part, "disable_web_page_preview": True})
            log("Telegram 分片 %d Markdown 解析失败，已纯文本兜底：%s" % (i, e))
        log("Telegram 推送分片 %d 完成" % i)

def _post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with _opener().open(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    try:
        res = json.loads(raw)
    except Exception:
        res = raw
    if isinstance(res, dict):
        # 飞书用 code(0=成功)；Telegram 用 ok(True=成功)
        if res.get("code", 0) != 0:
            raise RuntimeError("推送返回错误：%s" % res)
        if res.get("ok") is False:
            raise RuntimeError("推送返回错误：%s" % res)
    return res

def retry(times, fn, *a, **k):
    """简单重试：最多 times 次，指数退避，全部失败抛出最后一次异常。"""
    last = None
    for i in range(times):
        try:
            return fn(*a, **k)
        except Exception as e:
            last = e
            log("  ↳ 第 %d/%d 次尝试失败：%s" % (i + 1, times, e))
            if i < times - 1:
                time.sleep(20 * (i + 1))
    raise last

# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# 3.5) 潜力股提取与跨日跟踪（watchlist）
# ----------------------------------------------------------------------------
def extract_watchlist(digest):
    """从日报 digest 中切出「📊 潜力观察」板块；无则返回空串。"""
    if not digest:
        return ""
    start = digest.find("📊")
    if start < 0:
        start = digest.find("潜力观察")
    if start < 0:
        return ""
    rest = digest[start:]
    # 截到下一个板块标题（📎 其他细节 / 💡 一句话总结 等）或文末
    m = re.search(r"\n[📎💡🔥🔢👥]\s", rest[1:])
    block = rest[:m.start() + 1] if m else rest
    # 清理模型偶发的孤立加粗标记（行首/行尾不成对的 **），避免落盘/显示出现星号；
    # 行内成对的 ** 保留，交由 TG 端的 _tg_bold 转成 *加粗*
    block = re.sub(r"\*\*+\s*$", "", block, flags=re.M)
    block = re.sub(r"^\*\*+\s*", "", block, flags=re.M)
    return block.strip()


def save_watchlist(day_label, digest):
    """把当日潜力观察落盘：单日文件 + 累积文件（跨日跟踪用）。"""
    out_dir = os.path.join(HERE, "exports")
    os.makedirs(out_dir, exist_ok=True)
    block = extract_watchlist(digest)
    safe = re.sub(r"[^0-9A-Za-z_~-]", "_", day_label)
    one = os.path.join(out_dir, "watchlist_%s.txt" % safe)
    with open(one, "w", encoding="utf-8") as f:
        if block:
            f.write("【%s 潜力观察 · 近期可布局】\n%s\n" % (day_label, block))
        else:
            f.write("【%s】未识别到潜力观察板块（可能当日无相关讨论）\n" % day_label)
    allp = os.path.join(out_dir, "watchlist_all.md")
    with open(allp, "a", encoding="utf-8") as f:
        f.write("\n---\n### %s\n%s\n" % (day_label, block if block else "（无）"))


def watch_summary(n_days=None):
    """跨日汇总潜力观察，返回 (热点统计头, 合并文本)。n_days=None 表示全部。"""
    files = sorted(glob.glob(os.path.join(HERE, "exports", "watchlist_*.txt")))
    if n_days:
        files = files[-n_days:]
    if not files:
        return "", "（还没有任何潜力观察记录，先发 /digest 跑几天日报）"
    counter = {}
    blocks = []
    for fp in files:
        m = re.search(r"watchlist_([0-9A-Za-z_~.-]+)\.txt$", fp)
        day = m.group(1) if m else os.path.basename(fp)
        txt = open(fp, encoding="utf-8").read()
        blocks.append(txt)
        toks = set(re.findall(r"SN\d+", txt))
        toks |= set(re.findall(r"@[A-Za-z0-9_]{2,}", txt))
        for t in toks:
            counter.setdefault(t, set()).add(day)
    merged = "\n\n".join(blocks)
    hot_lines = []
    for t, days in sorted(counter.items(), key=lambda kv: len(kv[1]), reverse=True)[:12]:
        hot_lines.append("• %s：%d 天被提及（%s）" % (t, len(days), ",".join(sorted(days)[-3:])))
    head = "📈 潜力标的跨日出现天数（近 %s 天，去重）：\n" % (n_days if n_days else "全部") + \
           ("\n".join(hot_lines) if hot_lines else "（未发现高频标的）")
    return head, merged


# ----------------------------------------------------------------------------
# 3.6) 每日导出节流（防止 Discord user token 被频繁滥用）
# ----------------------------------------------------------------------------
def _digest_state_path(cfg):
    return os.path.join(HERE, cfg.get("output_dir", "exports"), "_digest_state.json")

def _load_digest_state(cfg):
    try:
        with open(_digest_state_path(cfg), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_digest_state(cfg, state):
    try:
        d = os.path.dirname(_digest_state_path(cfg))
        os.makedirs(d, exist_ok=True)
        with open(_digest_state_path(cfg), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("导出状态保存失败（忽略）：%s" % e)

def _record_export(cfg, day_str, json_path, count):
    state = _load_digest_state(cfg)
    day = state.setdefault(day_str, {"exports": []})
    day.setdefault("exports", [])
    day["exports"].append({
        "ts": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
        "json": json_path,
        "count": count,
    })
    _save_digest_state(cfg, state)

def _day_exports(cfg, day_str):
    return _load_digest_state(cfg).get(day_str, {}).get("exports", [])

def _export_json_path(cfg, day_str, seq):
    return os.path.join(HERE, cfg.get("output_dir", "exports"), "dtao_%s.%d.json" % (day_str, seq))

def _throttle_cfg(cfg):
    """节流参数：每日最多导出次数 + 两次导出最小间隔（小时），均可在 config.json 覆盖。"""
    max_per_day = int(cfg.get("max_exports_per_day", 2))
    interval_h = float(cfg.get("min_export_interval_hours", 12))
    if max_per_day < 1:
        max_per_day = 1
    if interval_h < 0:
        interval_h = 0
    return max_per_day, interval_h

def _plan_digest(cfg, day_str, tz):
    """节流决策（按自然日 day_str 计）：
       exportN      -> 当天第 N 次，且距上次导出 ≥interval_h，正常导出
       resummarize  -> 当天不足上限但间隔 <interval_h：复用最近一次导出记录重总结，不重新导出
       merge        -> 当天已导出达上限：合并所有导出记录重总结，不再导出
       参数（每日上限 / 最小间隔）可在 config.json 用 max_exports_per_day / min_export_interval_hours 调整。
       返回 (action, source_jsons)
    """
    max_per_day, interval_h = _throttle_cfg(cfg)
    exps = _day_exports(cfg, day_str)
    now = datetime.datetime.now(tz)
    if len(exps) >= max_per_day:
        return "merge", [e["json"] for e in exps]
    if len(exps) >= 1:
        last = _parse_ts(exps[-1]["ts"], tz)
        if (now - last) >= datetime.timedelta(hours=interval_h):
            return "export%d" % (len(exps) + 1), []
        return "resummarize", [exps[-1]["json"]]
    return "export1", []

def _parse_ts(s, tz):
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=tz)
        except Exception:
            return datetime.datetime.now(tz)

def merge_substantive(json_paths):
    """合并多个导出 json 的实质消息，按 (作者, 内容) 去重，按热度降序。"""
    all_items, seen = [], set()
    for jp in json_paths:
        if not os.path.exists(jp):
            continue
        for it in extract_substantive(jp):
            key = (it[2], it[3])
            if key in seen:
                continue
            seen.add(key)
            all_items.append(it)
    all_items.sort(key=lambda x: -x[0])
    return all_items

def run_one_day(cfg, after_dt, before_dt):
    """导出「某一天」-> 过滤 -> 总结 -> 双推。
    内置每日导出节流（参数可在 config.json 配置）：
      max_exports_per_day        : 当天最多导出次数（默认 2）
      min_export_interval_hours : 两次导出最小间隔小时（默认 12）
    未达间隔或已达上限时，复用 / 合并已有导出记录重总结后推送，避免频繁调用 Discord user token。
    供 main() 在『前一天 / 指定单日 / 指定多日』三种情形复用。"""
    day_str = after_dt.strftime("%Y-%m-%d")
    out_dir = os.path.join(HERE, cfg.get("output_dir", "exports"))
    os.makedirs(out_dir, exist_ok=True)
    channel_name = cfg.get("channel_name", "price-talk")
    tz = datetime.timezone(datetime.timedelta(hours=8))

    # 1) 节流决策
    max_per_day, interval_h = _throttle_cfg(cfg)
    action, sources = _plan_digest(cfg, day_str, tz)
    if action.startswith("export"):
        seq = len(_day_exports(cfg, day_str)) + 1
        mode_label = "导出第%d次" % seq
    elif action == "resummarize":
        mode_label = "复用最近导出重总结（<%.0fh，未重新导出）" % interval_h
    else:  # merge
        mode_label = "合并%d次导出重总结（当日已达%d次上限，未重新导出）" % (len(sources), max_per_day)
    log("节流模式：%s（%s）" % (action, mode_label))

    if action.startswith("export"):
        seq = len(_day_exports(cfg, day_str)) + 1
        out_json = _export_json_path(cfg, day_str, seq)
        retry(3, run_export, cfg, after_dt, before_dt, out_json)
        items = extract_substantive(out_json)
        _record_export(cfg, day_str, out_json, len(items))
        log("过滤后保留实质消息 %d 条（%s，导出#%d）" % (len(items), day_str, seq))
    else:
        items = merge_substantive(sources)
        log("过滤后保留实质消息 %d 条（%s，%s）" % (len(items), day_str, mode_label))

    # 2) 总结
    if not items:
        digest = "📭 %s 频道 %s 当日无与 Bittensor 子网/重要成员相关的实质讨论。" % (channel_name, day_str)
    else:
        text_block = build_text_block(channel_name, day_str, items)
        sys_p = SYSTEM_PROMPT
        if action in ("resummarize", "merge"):
            sys_p = SYSTEM_PROMPT + DEEPEN_HINT
        digest = call_deepseek(cfg, text_block, system_prompt=sys_p)

    title = "🤖 DTAO price-talk 日报 · %s" % day_str

    # 3) 推送
    if cfg.get("push_enabled", True):
        try:
            push_feishu(cfg, title, digest)
        except Exception as e:
            log("飞书推送异常：%s" % e)
        try:
            push_telegram(cfg, title, digest)
        except Exception as e:
            log("Telegram 推送异常：%s" % e)
        # 落盘：当天 digest（供 repush 补推）+ 最新 digest（供预览）
        try:
            with open(os.path.join(out_dir, "dtao_%s.digest.txt" % day_str), "w", encoding="utf-8") as f:
                f.write(digest)
            with open(os.path.join(out_dir, "last_digest.txt"), "w", encoding="utf-8") as f:
                f.write(digest)
            # 潜力股沉淀（跨日跟踪）
            try:
                save_watchlist(day_str, digest)
            except Exception as e:
                log("watchlist 落盘异常（忽略）：%s" % e)
        except Exception:
            pass
    else:
        log("push_enabled=false，仅打印不推送：\n" + title + "\n" + digest)
    return digest


# 中文 / 英文「昨天 / 最新」等别名，统一解析成具体日期，方便 TG 按钮与手输
YESTERDAY_ALIASES = {"昨日", "昨天", "yesterday", "latest", "最新", "y"}


def _resolve_day_alias(ds, tz):
    """把「昨日 / 最新 / yesterday」等别名解析成 YYYY-MM-DD；不是别名则原样返回。"""
    if ds in YESTERDAY_ALIASES:
        yesterday = (datetime.datetime.now(tz) - datetime.timedelta(days=1)).date()
        return yesterday.strftime("%Y-%m-%d")
    return ds


def _latest_digest_date(cfg):
    """扫描 exports/dtao_YYYY-MM-DD.digest.txt，返回最新日期；无则返回 None。"""
    out_dir = os.path.join(HERE, cfg.get("output_dir", "exports"))
    files = glob.glob(os.path.join(out_dir, "dtao_*.digest.txt"))
    dates = []
    for f in files:
        m = re.search(r"dtao_(\d{4}-\d{2}-\d{2})\.digest\.txt$", f)
        if m:
            dates.append(m.group(1))
    return max(dates) if dates else None


def main(args):
    log("===== DTAO 流水线启动（半自动 / 手动触发）=====")
    cfg = load_config()
    global _CFG
    _CFG = cfg
    tz = datetime.timezone(datetime.timedelta(hours=8))  # 默认东八区
    if args.extra:
        # 指定日期（可多个，逐日导出并推送）
        for ds in args.extra:
            ds = _resolve_day_alias(ds, tz)
            try:
                d = datetime.datetime.strptime(ds, "%Y-%m-%d").date()
            except Exception:
                log("❌ 无法解析日期：%s（应为 YYYY-MM-DD 或 昨日/最新）" % ds)
                continue
            after_dt = datetime.datetime(d.year, d.month, d.day, tzinfo=tz)
            before_dt = after_dt + datetime.timedelta(days=1)
            run_one_day(cfg, after_dt, before_dt)
    else:
        # 默认：前一天
        now = datetime.datetime.now(tz)
        yesterday = (now - datetime.timedelta(days=1))
        after_dt = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        before_dt = after_dt + datetime.timedelta(days=1)
        run_one_day(cfg, after_dt, before_dt)
    log("===== DTAO 流水线结束 =====")


def run_repush(cfg, day_str, targets):
    """仅重新推送已生成的某日日报（不重新导出 / 不重新总结），用于补推失败的那一端。"""
    global _CFG
    _CFG = cfg
    out_dir = os.path.join(HERE, cfg.get("output_dir", "exports"))
    digest_path = os.path.join(out_dir, "dtao_%s.digest.txt" % day_str)
    if not os.path.exists(digest_path):
        raise RuntimeError("找不到 %s，无法补推。请先跑过该日的 daily 以生成 digest。" % digest_path)
    with open(digest_path, encoding="utf-8") as f:
        digest = f.read().strip()
    title = "🤖 DTAO price-talk 日报 · %s" % day_str
    if "feishu" in targets:
        try:
            push_feishu(cfg, title, digest)
            log("飞书补推完成：%s" % day_str)
        except Exception as e:
            log("飞书补推异常：%s" % e)
    if "tg" in targets:
        try:
            push_telegram(cfg, title, digest)
            log("Telegram 补推完成：%s" % day_str)
        except Exception as e:
            log("Telegram 补推异常：%s" % e)

# ----------------------------------------------------------------------------
# 周报模式：汇总 exports/ 下最近 7 天的日报原始文件，重新总结后推送
# ----------------------------------------------------------------------------
def run_weekly(cfg):
    global _CFG
    _CFG = cfg
    log("===== DTAO 周报流水线启动 =====")
    tz = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(tz).date()
    out_dir = os.path.join(HERE, cfg.get("output_dir", "exports"))
    files = []
    for d in range(1, 8):
        day = today - datetime.timedelta(days=d)
        p = os.path.join(out_dir, "dtao_%s.json" % day.strftime("%Y-%m-%d"))
        if os.path.exists(p):
            files.append(p)
    if not files:
        raise RuntimeError("exports/ 下没有可汇总的日报文件，请先跑几天 daily 再生成周报")
    log("汇总 %d 天的日报原始文件用于周报" % len(files))
    all_items = []
    for p in files:
        all_items += extract_substantive(p)
    all_items.sort(key=lambda x: -x[0])
    if len(all_items) > 400:
        all_items = all_items[:400]
    day_first = files[-1].split("dtao_")[1].split(".")[0]
    day_last = files[0].split("dtao_")[1].split(".")[0]
    text_block = build_text_block("price-talk(周报)", "%s~%s" % (day_first, day_last), all_items)
    digest = call_deepseek(cfg, text_block, WEEKLY_SYSTEM_PROMPT) if all_items else "本周无与 Bittensor 子网/重要成员相关的实质讨论。"
    title = "🤖 DTAO price-talk 周报 · %s~%s" % (day_first, day_last)
    if cfg.get("push_enabled", True):
        try:
            push_feishu(cfg, title, digest)
        except Exception as e:
            log("飞书推送异常：%s" % e)
        try:
            push_telegram(cfg, title, digest)
        except Exception as e:
            log("Telegram 推送异常：%s" % e)
        # 潜力股沉淀（周报也纳入跨日跟踪）
        try:
            save_watchlist("week_%s~%s" % (day_first, day_last), digest)
        except Exception as e:
            log("watchlist 落盘异常（忽略）：%s" % e)
    else:
        log("push_enabled=false，仅打印不推送：\n" + title + "\n" + digest)
    log("===== DTAO 周报流水线结束 =====")

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="DTAO price-talk 半自动流水线")
    ap.add_argument("mode", nargs="?", default="daily", choices=["daily", "weekly", "repush"],
                    help="daily=导出并推送（可跟日期，省略则前一天）；weekly=周报；"
                         "repush=仅补推已生成的日报（不重导/不重算）")
    ap.add_argument("extra", nargs="*",
                    help="daily: 一个或多个日期 YYYY-MM-DD；repush: 日期 [tg|feishu]（省略则双推）")
    args = ap.parse_args()
    try:
        if args.mode == "weekly":
            run_weekly(load_config())
        elif args.mode == "repush":
            if not args.extra:
                raise RuntimeError("repush 需要指定日期，例如：python pipeline.py repush 2026-10-23 tg")
            day = args.extra[0]
            if day in ("最新", "latest"):
                day = _latest_digest_date(load_config())
                if not day:
                    raise RuntimeError("没有可用的日报文件，请先跑 /digest 生成一次。")
            targets = [t for t in args.extra[1:] if t in ("feishu", "tg")] or ["feishu", "tg"]
            run_repush(load_config(), day, targets)
        else:
            main(args)
    except Exception as e:
        log("❌ 流水线失败：%s" % e)
        # 失败时也尝试推送告警
        try:
            cfg = load_config()
            alert = "❌ DTAO 流水线 %s 执行失败：\n%s" % (
                datetime.datetime.now().strftime("%Y-%m-%d"), e)
            if cfg.get("push_enabled", True):
                try:
                    push_feishu(cfg, "❌ DTAO 流水线失败", alert)
                except Exception:
                    pass
                try:
                    push_telegram(cfg, "❌ DTAO 流水线失败", alert)
                except Exception:
                    pass
        except Exception:
            pass
        sys.exit(1)
