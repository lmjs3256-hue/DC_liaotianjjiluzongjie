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
import random
import hashlib
import base64

# ----------------------------------------------------------------------------
# 输出编码保护（重要）
# 中文 Windows 下，进程 stdout 被重定向为管道时默认用 GBK(cp936) 编码。
# 日志里一旦出现 ↳ / ❌ / ✅ / 🔥 等 GBK 无法编码的符号，print() 会抛
# UnicodeEncodeError；若此时异常正向上冒泡，就会连带把整个进程干掉且不留日志。
# 这里统一改成 UTF-8 输出（并容错替换），同时消除监听侧的乱码问题。
# ----------------------------------------------------------------------------
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    try:
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ----------------------------------------------------------------------------
# 版本号：遵循语义化版本（MAJOR.MINOR.PATCH）
#   修 bug / 内部优化      -> PATCH +1
#   新增功能（向后兼容）    -> MINOR +1
#   不兼容的配置/命令变更   -> MAJOR +1
# 每次改动请同步更新 README 顶部版本行与 CHANGELOG.md
# ----------------------------------------------------------------------------
__version__ = "1.0.1"
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
    try:
        print(line, flush=True)
    except Exception:
        # 任何编码/管道异常都不能让日志把进程带走
        try:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)
        except Exception:
            pass
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
# 记录本次实际使用的 token 编号，供日报末尾展示「令牌：n/m」
# 未发生真实导出（复用/合并重总结）时保持 None，日报里显示为「-」
_TOKEN_INFO = {"index": None, "total": None}


def pick_discord_token(cfg):
    """返回本次导出用的 token。
    - 配置了 discord_tokens（数组，且至少一个非空）-> 每次随机抽一个，把请求摊到多个账号，
      降低单个 user token 被风控命中的概率（同一天也可命中不同号）。
    - 否则回退到单个 discord_token（向后兼容）。
    注意：随机模式下，每个 token 对应的账号都必须是该服务器成员且能访问目标频道。
    """
    raw = cfg.get("discord_tokens") or []
    pool = [t for t in raw if t]
    # 防御：discord_tokens 数组中若混入空串/空值，一律忽略，绝不会随机抽到空 token 导致导出失败
    if len(raw) != len(pool):
        log("discord_tokens 共 %d 项，已忽略 %d 个空/无效项，实际可用 %d 个"
            % (len(raw), len(raw) - len(pool), len(pool)))
    if pool:
        idx = random.randrange(len(pool))
        log("使用 discord_tokens 池中第 %d 个（共 %d 个，随机）" % (idx + 1, len(pool)))
        _TOKEN_INFO["index"] = idx + 1
        _TOKEN_INFO["total"] = len(pool)
        return pool[idx]
    single = cfg.get("discord_token")
    if not single:
        raise RuntimeError("config.json 缺少 discord_token，且 discord_tokens 为空")
    _TOKEN_INFO["index"] = 1
    _TOKEN_INFO["total"] = 1
    return single

# ----------------------------------------------------------------------------
# 1) 用 DiscordChatExporter 导出「前一天」频道记录为 JSON
# ----------------------------------------------------------------------------
DISCORD_EPOCH_MS = 1420070400000

def _snow_to_ms(sid):
    """Discord snowflake ID -> Unix 毫秒"""
    return (int(sid) >> 22) + DISCORD_EPOCH_MS

def _ms_to_snow(ms):
    """Unix 毫秒 -> Discord snowflake ID"""
    return (int(ms) - DISCORD_EPOCH_MS) << 22

def _api_fetch(url, token, tries=5):
    """调用 Discord REST API（代理由 _opener() 按 config.proxy 决定）。
    429 限速自动等待重试；401/403 直接判定为 token 无效或无权访问。"""
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, headers={
            "Authorization": token,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DTAO-Pipeline/1.0",
            "Accept": "application/json",
        })
        try:
            with _opener().open(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            if e.code == 429:
                wait = 2.0
                try:
                    wait = float(json.loads(body).get("retry_after", 2))
                except Exception:
                    pass
                wait = min(max(wait, 1.0), 30.0)
                log("Discord API 429 限速，等待 %.1fs 后重试（第 %d/%d 次）" % (wait, i + 1, tries))
                time.sleep(wait)
                last = "HTTP 429: %s" % body[:200]
                continue
            if e.code in (401, 403):
                raise RuntimeError("Discord API 拒绝访问（HTTP %s）：token 无效、过期，或该账号无权访问此频道" % e.code)
            last = "HTTP %s: %s" % (e.code, body[:200])
            time.sleep(2)
        except Exception as e:
            last = str(e)
            time.sleep(2)
    raise RuntimeError("Discord API 请求失败（已重试 %d 次）：%s" % (tries, last))

def export_via_api(cfg, token, channel, after_date, before_date, out_json):
    """内置 API 后端：不用 DiscordChatExporter，直接调 Discord REST API 拉指定日期的消息。
    好处：不走 .NET 的代理解析逻辑（DCE 常在此卡死），代理完全由 config.proxy / 系统代理控制。
    产出 JSON 与 DCE 兼容：messages[] 含 author.name / content / timestamp / reactions[].count
    日期语义与 DCE 一致：按 UTC 解释，[after 00:00, before 00:00) 左闭右开。
    """
    import datetime as _dt
    start_ms = int(_dt.datetime.strptime(after_date, "%Y-%m-%d")
                   .replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
    end_ms = int(_dt.datetime.strptime(before_date, "%Y-%m-%d")
                 .replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)

    base = "https://discord.com/api/v9/channels/%s/messages" % channel
    url = "%s?limit=100&before=%d" % (base, _ms_to_snow(end_ms))
    collected, seen, pages = [], set(), 0

    while url and pages < 300:
        pages += 1
        data = _api_fetch(url, token)
        if not isinstance(data, list) or not data:
            break
        oldest = None
        for m in data:
            mid = str(m.get("id") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            ms = _snow_to_ms(mid)
            if start_ms <= ms < end_ms:
                collected.append(m)
            if oldest is None or int(mid) < int(oldest):
                oldest = mid
        if len(data) < 100 or not oldest:
            break
        if _snow_to_ms(oldest) < start_ms:
            break
        url = "%s?limit=100&before=%s" % (base, oldest)

    collected.sort(key=lambda m: int(m["id"]))
    out = []
    for m in collected:
        a = m.get("author") or {}
        name = a.get("global_name") or a.get("username") or "?"
        nick = ((m.get("member") or {}).get("nick")) or name
        out.append({
            "id": m.get("id"),
            "timestamp": m.get("timestamp"),
            "content": m.get("content") or "",
            "author": {"id": a.get("id"), "name": name, "nickname": nick},
            "reactions": [{"count": r.get("count", 0),
                           "emoji": {"name": (r.get("emoji") or {}).get("name", "")}}
                          for r in (m.get("reactions") or [])],
            "attachments": [{"url": x.get("url")} for x in (m.get("attachments") or [])],
        })

    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"channel": {"id": channel}, "messages": out}, f, ensure_ascii=False, indent=1)
    log("API 后端导出成功：%s（%d 条消息，%d 字节，翻页 %d 次）"
        % (out_json, len(out), os.path.getsize(out_json), pages))
    if not out:
        log("提示：该区间 API 返回 0 条消息，请确认日期与频道是否有发言")

def run_export(cfg, after_dt, before_dt, out_json):
    day_str = after_dt.strftime("%Y-%m-%d")
    token = pick_discord_token(cfg)
    channel = str(cfg["channel_id"])
    backend = cfg.get("dce_backend", "docker").lower()
    after = after_dt.strftime("%Y-%m-%d")
    before = before_dt.strftime("%Y-%m-%d")

    # 内置 API 后端：完全绕开 DiscordChatExporter（.NET），代理可控、速度秒级
    if backend == "api":
        export_via_api(cfg, token, channel, after, before, out_json)
        return

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
    # 超时保护：避免网络不通时无限卡死（默认 120s，正常导出只需 30s 左右）
    dce_timeout = int(cfg.get("dce_timeout", 120) or 120)
    fallback = bool(cfg.get("api_fallback", True))
    failed = None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           env=env, timeout=dce_timeout)
        if r.returncode != 0:
            failed = "DiscordChatExporter 退出码 %s：%s" % (r.returncode, ((r.stderr or r.stdout or "")[:300]))
        elif not os.path.exists(out_json):
            failed = "DiscordChatExporter 执行完成但未生成文件"
    except subprocess.TimeoutExpired:
        failed = "DiscordChatExporter 超时（%ss），多半卡在代理/网络连接" % dce_timeout
    except Exception as e:
        failed = "DiscordChatExporter 调用异常：%s" % e

    if failed:
        if fallback:
            log("⚠ %s —— 自动回退到内置 API 后端导出" % failed)
            export_via_api(cfg, token, channel, after, before, out_json)
            return
        raise RuntimeError(failed + "\n（如需自动改用内置 API 后端，请在 config.json 设置 \"api_fallback\": true）")

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

# 裸子网编号：Discord 里大家习惯直接写 "117"、"61"、"79"，不带 sn/subnet 前缀。
# 只认 1-256（Bittensor 子网号范围），并排除版本号/价格/时间等（前后接字母、点、%、冒号的不算）。
BARE_SN = re.compile(r"(?<![\w.:/%])([1-9]|[1-9]\d|1\d\d|2[0-4]\d|25[0-6])(?![\w.:%])")

def is_substantive(content):
    if not content:
        return False
    if SUBSTANCE.search(content) or NAMES.search(content) or MEMBERS.search(content):
        return True
    # 纯数字子网号：只在不像链接的短句里认，避免 URL 里的数字引入噪音
    if "http" in content.lower() or "@" in content:
        return False
    return bool(BARE_SN.search(content))

def extract_substantive(json_path, ctx=2):
    """抽出实质消息，并**带上前后各 ctx 条上下文**。
    为什么需要上下文：群聊里大量指代（「2t daily buyback 不值一提」）要结合上一句
    （「61 是不是出事了」）才能判断 2t 指哪个子网；只挑关键词句会把指代关系切断。"""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    msgs = data.get("messages", [])
    n = len(msgs)

    def clean(m):
        return (m.get("content") or "").strip()

    # 1) 先标记「实质消息」锚点
    anchor = [False] * n
    for i, m in enumerate(msgs):
        c = clean(m)
        if not c or NOISE.match(c):
            continue
        if is_substantive(c):
            anchor[i] = True

    if not any(anchor):
        return []

    # 2) 上下文扩展：把锚点前后 ctx 条也带上（条数过多时收紧到前后 1 条）
    if sum(anchor) * (2 * ctx + 1) > 160:
        ctx = 1
    keep = [False] * n
    for i in range(n):
        if anchor[i]:
            for j in range(max(0, i - ctx), min(n, i + ctx + 1)):
                keep[j] = True

    out = []
    for i, m in enumerate(msgs):
        if not keep[i]:
            continue
        c = clean(m)
        if not c or NOISE.match(c):
            continue
        author = (m.get("author") or {}).get("nickname") or (m.get("author") or {}).get("name") or "?"
        t = (m.get("timestamp") or "")[11:16]
        react = sum(r.get("count", 0) for r in m.get("reactions", []))
        out.append((react, t, author, c))
    # 按时间升序输出：模型需要顺着对话流理解指代与因果，按热度排序会把上下文打散
    out.sort(key=lambda x: x[1])
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
    "**🔥 必读重点**（3-7 条，当天最该知道的全部内容；每条一句说清「谁/什么 + 关键结论」，"
    "并把关键人物、**SN编号/子网名**、事件用 **加粗**；子网异动、上线、rug、机制变更、算力变化都写在这里，"
    "用 ⚠️ 或 ✅ 标注。**本节即子网动向的唯一出口，不要再单列子网汇总小节，避免重复**）\n"
    "**👥 重要人物观点**（点名 **@人物/开发者**，他讲了什么、对什么有影响；与上面重复的事实不用复述，只补充观点与依据；普通群友无关发言不列）\n"
    "**📎 其他细节**（补遗：前面没覆盖但有价值的细节，确保不错过）\n"
    "**💡 一句话总结**（当天全局判断，一句收束）\n"
    "\n"
    "【不要输出潜力观察】「潜力观察/布局建议」由另一个专门流程单独生成，日报正文中**不要**出现该板块，"
    "也不要在其他小节里写布局建议、目标价、催化剂清单。\n"
    "\n"
    "【排版与强调规范（飞书/TG 双平台通用，务必遵守）】\n"
    "- 关键主体一律 **加粗**：人物名、**SN编号/子网名**、代币、事件、关键数字。\n"
    "- 用 emoji 标注程度：🔥 重大利好/热点、✅ 已落地、⚠️ 风险/异动、📌 值得关注、❌ 利空。\n"
    "- 每条要点单独成行，用 - 项目符号；小节之间空一行；避免横排长句，适合手机端。\n"
    "- 不要用 # 标题、不要用 > 引用（部分平台不渲染）。\n"
    "- 客观、不臆测、引用具体编号与名字；全文 1000 字以内。\n"
    "\n"
    "【事实核查与验证规则（最高优先级，违反任何一条即为不合格日报）】\n"
    "1. **定性必须有依据**：对「跑路/弃盘/rug/转型/换方向/清仓」等重大定性，只能依据聊天记录中的明确表述或官方公告原文。"
    "若频道内出现官方澄清、更正或后续进展（如团队宣布转型、发布说明），**必须以最新的澄清为准**重写结论，"
    "并明确写出「官方澄清为转型/换方向」等表述；严禁沿用此前群友的猜测作为最终结论。\n"
    "2. **区分事实与猜测**：机器人/转发播报的官方公告是「事实」，群友的解读是「观点」，两者不得混写。"
    "群友的负面猜测若与官方公告矛盾，一律以公告为准；无公告、无明确说法时只能写「原因不明，社区有XX猜测」。\n"
    "3. **排名与最高级必须落地**：凡出现「第一/最大/最高/涨幅冠军」等表述，必须写明**具体的 SN 编号/子网名与数据**（如「SN27 当日 alpha 涨幅第一」）。"
    "若原消息本身没有指明是哪个子网，只能引用原话并标注「（原文未指明具体子网）」，或直接舍弃该条，**严禁脑补编号**。\n"
    "4. **数字/时间/代号照抄**：SN 编号、代币名、价格、时间等必须照抄原文，禁止改写、补全或张冠李戴；不确定的内容宁可不写。\n"
    "5. **重要事件多来源交叉**：单一消息来源的重大利好/利空，需标注「（单一消息源，待确认）」；"
    "频道内有多人讨论或有链接佐证的才可直接陈述。\n"
    "\n"
    "【溯源与禁止臆造（最高优先级，逐条自检）】\n"
    "6. **每条结论必须可溯源**：写进日报的每个事实，都必须能在聊天记录中找到对应原文；必要时用「某某称/据某某」标明说话人。"
    "找不到原文支撑的，一律删除，不要用常识补全。\n"
    "7. **禁止推断手段、动机与因果**：原文只说「有人卖了某标的」，就只能写「有群友称有人卖出」；"
    "不得自行添加「通过质押卖出」「本人承认卖出」「为了砸盘」「团队授意」等原文没有的手段、动机或因果链。"
    "原文没说清楚的地方，就写「具体方式/来源原文未说明」。\n"
    "8. **禁止拼接与移花接木**：不得把 A 的说法与 B 的说法合成一条因果事实，"
    "也不得把某人的观点安到另一个人头上；不同人、不同时间的表述要分别归属。\n"
    "9. **全文表述一致**：同一事实在「必读重点」「子网动向」「人物观点」等各小节中的定性必须一致，不得一处说「疑似跑路」、另一处说「已确认转型」。\n"
    "10. **宁可少写，不许补全**：信息不足时写「原文未提及」，绝不用推测填补空白；推测内容必须显式标注「（推测）」，且不得出现在「必读重点」里。\n"
    "11. **输出前自检**：逐条回看自己写的每一句，删除所有无法在原文找到依据的句子；"
    "重点检查数字、买卖方向、主体归属、时间先后这四类最容易出错的信息。\n"
    "\n"
    "【编号与指代（高频易错，必须遵守）】\n"
    "12. **分清「子网编号」和「数量单位」**：单独出现的 1-3 位数字（「117」「61」「79」）按子网编号理解；"
    "但带单位后缀的是数量，不是编号——「2t」= 2 个 TAO、「100k」= 10 万、「0.8B」= 8 亿参数。"
    "判断依据是上文：若前几句在讨论某个子网的回购，那么「2t daily buyback」指的就是**该子网每天回购 2 个 TAO**，"
    "必须写成「SN61 每天回购约 2 个 TAO」这类明确表述，**绝不能写成「SN2」**。\n"
    "13. **指代必须还原主语**：遇到「他们 / 这个团队 / it / 2t」这类指代，必须回溯前后几条消息确定说的是哪个子网，"
    "再在日报里写成明确的 **SN编号**；确实无法确定的，写「（原文未指明具体子网）」，不许瞎猜。\n"
    "14. **未解答的疑问也是信息**：群内对某子网的**点名、质疑、提问**（如「117 团队是谁」「61 是不是出事了」），"
    "即使当天没有人给出答案，也必须作为「待解疑问」写进日报，不能被省略——当天的疑点本身就是有价值的信号。\n"
    "15. **同一子网的连续对话要合并成一条完整叙事**：若某子网在一段时间内被连续讨论（质疑→回应→补充），"
    "应合成一条包含来龙去脉的要点，而不是拆成互不相关的孤立句子。\n"
    "16. **人名只能从「当日发言者名单」里取**：名字必须与名单**逐字一致**（含大小写与特殊字符，"
    "如 τFIG、kτwτ、Squirτle Baggins），不得音译、缩写、改写，更不能凭空造名；"
    "名单里没有的人名不得作为「某人说」出现。转述某人的观点时，务必确认那句话确实出自他本人。\n"
    "\n"
    "【稳定性要求】相同输入应给出相同结论：优先复述原文事实，不做发散联想；"
    "对同一事件的定性只有一种写法，不要在不同表述之间摇摆。\n"
    "\n"
    "【语言风格（面向普通读者）】\n"
    "- **用大白话写**，像给朋友讲今天发生了什么；少用行话，避免「SOTA」「BD」「NDAs」这类缩写直接甩出来，"
    "必须用时就写成「中文（英文缩写）」的形式。\n"
    "- **除人名、子网名/项目名、代币名（如 TAO、dTao）保留原文外，其余尽量用中文**；"
    "不要整句英文照搬，也不要在中文句子里夹杂无必要的英文单词。\n"
    "- 中文读者不熟悉的英文原话，翻译成中文并在括号里保留关键词即可，不必逐字直译。"
)

# 独立的「潜力观察」生成（只在 /watch 类场景需要，不随日报推送）
POTENTIAL_PROMPT = (
    "你是一名 Bittensor 生态的资深研究员，任务是从当日 Discord 频道「DTAO - price-talk」的聊天记录中，"
    "挖掘值得提前关注的布局线索，输出「潜力观察 · 近期可布局」板块，供人工参考。\n"
    "\n"
    "【输出格式（只输出这一块，不要任何其他小节）】\n"
    "**📊 潜力观察 · 近期可布局**\n"
    "- **SN编号/子网名或标的** → 预期：… → 催化：…（时间窗） → 逻辑：… → 确定性：**高/中/低**\n"
    "（最多 3-5 条，按临近度+确定性排序；当日确实没有可写的催化，就只输出「**📊 潜力观察 · 近期可布局**\n暂无明显催化」）\n"
    "\n"
    "【硬性规则（违反即不合格）】\n"
    "1. 每条必须能追溯到聊天记录中的具体讨论；凭空推断的市场预期一律不写。\n"
    "2. 预期/催化剂必须是记录里出现过的说法（如团队称上线时间、某人称将回购），并标明是谁说的；"
    "原文没给时间就写「时间未定」，不要编造「1-2 个月」「下周」这类窗口。\n"
    "3. 确定性只能按证据强度判断：多人讨论+官方公告=高；单一成员表态=中；纯个人感觉=低并标「（推测）」。\n"
    "4. 禁止把别人的观点安错人；禁止补充原文没有的数字、价格、市值。\n"
    "5. 关键主体用 **加粗**，数字与 SN 编号照抄原文。\n"
)

WEEKLY_SYSTEM_PROMPT = (
    "你是一名 Bittensor 生态的资深研究员，负责把 Discord 频道「DTAO - price-talk」过去 7 天的聊天记录，"
    "整理成中文周报，侧重「一周趋势、跨日升温信号、以及更确定的布局机会」。\n"
    "\n"
    "【内容边界】同日报：只关注子网、代币机制、核心成员动态、催化事件；忽略水聊与无关内容。\n"
    "\n"
    "【输出结构（严格按顺序；小节标题用 **加粗**）】\n"
    "**🔥 本周核心趋势**（4-6 条一周主线，趋势与子网变化合并写在这里；标注哪些是「多次提及/持续升温」的，"
    "子网写成 **SN编号/子网名** + 状态，上线/rug/机制变更标 ⚠️✅。**本节即子网动向的唯一出口，不另设子网汇总小节**）\n"
    "**👥 重要人物观点**（**@人物** 本周关键表态及影响，不重复上面的事实）\n"
    "**⚠️ 风险汇总**（本周风险点/警示）\n"
    "**📎 其他细节**（补遗，不错过有价值信息）\n"
    "**💡 一句话总结**\n"
    "\n"
    "【不要输出潜力观察】布局建议由独立流程生成，周报正文中**不要**出现潜力观察/布局/目标价板块。\n"
    "\n"
    "【排版规范】同日报：关键主体 **加粗**；🔥✅⚠️📌❌ 标注程度；- 项目符号、空行分段、手机端友好；"
    "不用 # 标题、不用 > 引用；客观不臆测，引用具体编号与名字；全文 1000 字以内。\n"
    "【事实核查规则】同日报【事实核查与验证规则】：重大定性以最新官方澄清为准；"
    "排名/最高级必须落到具体 SN 编号与数据，原文未指明就标「（原文未指明）」或舍弃；数字/代号照抄，严禁脑补。"
)

DEEPEN_HINT = (
    "\n\n【本次为重总结 / 合并总结】请在保持全部结构的前提下做更深入、更细致的分析："
    "对「🔥 必读重点」补充更明确的判断与依据（谁说的、原文如何表述），"
    "把散落在各条消息里的同一事件合并成一条完整脉络；"
    "避免流水账式复述，聚焦结论与逻辑。注意：仍然不要输出潜力观察/布局建议板块。"
)

FEEDBACK_HINT = (
    "\n\n【本次为人工反馈重总结】用户对上一版日报提出了具体意见/事实补充，"
    "请在严守【事实核查与验证规则】【溯源与禁止臆造】全部条款的前提下，据此修正并重新输出完整日报：\n"
    "——用户反馈开始——\n%s\n——用户反馈结束——\n"
    "执行要求：\n"
    "1. 与反馈直接冲突的旧结论必须删除或改写，不得原样保留；\n"
    "2. 反馈中提供的事实如果在聊天记录里没有直接对应原文，必须写成「据用户补充」，"
    "不得表述为群内共识或既定事实；\n"
    "3. 反馈只用于纠错与补充，不得借机新增无依据的推测内容；\n"
    "4. 未涉及的部分保持原有结构与依据不变；输出完整日报（不是修改说明）。"
)

def call_deepseek(cfg, text_block, system_prompt=None, tries=3):
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
        "temperature": float(cfg.get("deepseek_temperature", 0)),
        "max_tokens": 2500,
    }).encode("utf-8")
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", "Bearer %s" % api_key)
        req.add_header("Content-Type", "application/json")
        try:
            with _opener().open(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last = "DeepSeek HTTP 错误 %s: %s" % (e.code, detail)
            # 4xx（key 错误/参数错误）重试无意义，直接抛出
            if 400 <= e.code < 500 and e.code != 429:
                raise RuntimeError(last)
        except Exception as e:
            last = "DeepSeek 调用失败：%s" % e
        if i < tries - 1:
            log("  ↳ DeepSeek 第 %d/%d 次尝试失败（%s），%ds 后重试"
                % (i + 1, tries, last, 10 * (i + 1)))
            time.sleep(10 * (i + 1))
    raise RuntimeError(last)

def build_text_block(channel_name, day, items):
    # 发言者名单：给模型一份白名单，防止人名被音译、缩写或凭空编造
    authors, _seen = [], set()
    for _r, _t, _a, _c in items:
        if _a and _a not in _seen:
            _seen.add(_a)
            authors.append(_a)
    lines = ["频道：%s    日期：%s    当日消息 %d 条（**按时间顺序**，含少量上下文消息；"
             "🔥n 表示该条被表情回应 n 次，热度参考）"
             % (channel_name, day, len(items)),
             "当日发言者名单（共 %d 人；引用人名时必须原样照抄，不得音译/缩写/改写大小写，"
             "名单外的名字不得作为发言者出现）：%s"
             % (len(authors), "、".join(authors))]
    for i, (react, t, author, c) in enumerate(items, 1):
        c1 = c.replace("\n", " ").strip()
        if len(c1) > 600:
            c1 = c1[:600] + "…"
        hot = " 🔥%d" % react if react >= 2 else ""
        lines.append("%d. [%s] <%s>%s %s" % (i, t, author, hot, c1))
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
    # 正文顶部放「加粗标题 + 日期行」，与正文合并进同一个文本块（不加分隔线），
    # 这样在飞书手机端长按复制时，标题+日期+正文会被一并选中，日报才有完整意义
    title_line = "**%s**" % title
    date_line = ("📅 %s　·　📱 手机端优化　·　🤖 DeepSeek 生成" % day) if day else "📱 手机端优化　·　🤖 DeepSeek 生成"
    # 标题、日期、正文合为一个 lark_md 块（以空行分隔，不用 hr），保证整段可复制
    combined = "%s\n%s\n\n%s" % (title_line, date_line, body)
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": combined}},
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


def save_potential(day_label, block):
    """把潜力观察板块落盘（单日文件 + 累积文件），不推送。"""
    out_dir = os.path.join(HERE, "exports")
    os.makedirs(out_dir, exist_ok=True)
    block = (block or "").strip()
    safe = re.sub(r"[^0-9A-Za-z_~-]", "_", day_label)
    one = os.path.join(out_dir, "watchlist_%s.txt" % safe)
    with open(one, "w", encoding="utf-8") as f:
        if block:
            f.write("【%s 潜力观察 · 近期可布局】\n%s\n" % (day_label, block))
        else:
            f.write("【%s】当日无明显催化\n" % day_label)
    allp = os.path.join(out_dir, "watchlist_all.md")
    with open(allp, "a", encoding="utf-8") as f:
        f.write("\n---\n### %s\n%s\n" % (day_label, block if block else "（无）"))


def generate_potential(cfg, day_str, items):
    """单独生成潜力观察并落盘（不推送）。失败不影响日报主流程。"""
    if not items:
        save_potential(day_str, "")
        return ""
    try:
        text_block = build_text_block(cfg.get("channel_name", "price-talk"), day_str, items)
        block = call_deepseek(cfg, text_block, system_prompt=POTENTIAL_PROMPT)
        save_potential(day_str, block)
        log("潜力观察已单独生成并落盘（%s，不随日报推送）" % day_str)
        return block
    except Exception as e:
        log("潜力观察生成失败（不影响日报）：%s" % e)
        return ""


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
    """合并多个导出 json 的实质消息，按 (作者, 内容) 去重，**按时间升序**。
    （不再按热度排序：打乱时间线会让模型失去指代上下文）"""
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
    all_items.sort(key=lambda x: x[1])
    return all_items

def _audit_names(digest, items):
    """人名保真核对：把日报「人物观点」里出现的名字与当日真实发言者名单比对。
    不在名单里的只写日志提示（不阻断推送、不改内容），便于人工复核是否被模型改写或编造。"""
    authors = {a for _r, _t, a, _c in items if a}
    if not authors:
        return
    # 只匹配「- **名字**：」或「- **@名字**：」这种人物条目，避免把子网名/项目名当人名
    cands = re.findall(r"^\s*-\s*\*\*@?([^*\n]{2,30}?)\*\*\s*[：:]", digest or "", re.M)
    unknown = []
    for name in cands:
        n = name.strip()
        # 去掉「（SN23）」这类括注后再比对
        n = re.sub(r"[（(].*?[）)]", "", n).strip()
        if not n:
            continue
        if any(n == a or n.startswith(a) or a.startswith(n) for a in authors):
            continue
        unknown.append(n)
    if unknown:
        log("⚠️ 人名核对：以下名字不在当日发言者名单中，请人工确认是否被改写/编造：%s"
            % "、".join(sorted(set(unknown))))
    else:
        log("✅ 人名核对通过：日报中的人物均可对应到当日真实发言者")


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

    # 末尾标注：本次使用的 token 编号（第几个/共几个）+ 当日导出次数（已用/每日上限）
    # 令牌 n/m：本次随机抽中的是第 n 个，池中共 m 个；复用未导出时显示为「-」
    # 次数 x/y：该自然日已累计导出 x 次，每日上限 y 次
    used = len(_day_exports(cfg, day_str))
    t_idx = _TOKEN_INFO.get("index")
    t_total = len([t for t in (cfg.get("discord_tokens") or []) if t]) \
        or (1 if cfg.get("discord_token") else 0)
    idx_str = str(t_idx) if t_idx else "-"
    footer = "\n\n---\n令牌：%s/%d · 次数：%d/%d" % (idx_str, t_total, used, max_per_day)
    if not action.startswith("export"):
        footer += "（本次复用已有记录，未重新导出）"
    digest = (digest or "") + footer
    log("日报标注：令牌 %s/%d，当日导出次数 %d/%d%s"
        % (idx_str, t_total, used, max_per_day,
           "" if action.startswith("export") else "（复用，未重新导出）"))

    # 人名保真核对（仅写日志提示，不阻断推送）
    try:
        _audit_names(digest, items)
    except Exception as e:
        log("人名核对异常（忽略）：%s" % e)

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
        except Exception:
            pass
        # 潜力观察单独生成并落盘，只在 /watch 时推送，不随日报一起发
        try:
            generate_potential(cfg, day_str, items)
        except Exception as e:
            log("潜力观察流程异常（忽略）：%s" % e)
    else:
        log("push_enabled=false，仅打印不推送：\n" + title + "\n" + digest)
    return digest


def resummarize_with_feedback(cfg, day_str, feedback, targets=None):
    """按用户反馈重新总结某天日报（复用已有导出，不重新导出、不消耗 Discord 配额）。
    targets: None=双推；列表可含 "tg"/"feishu" 只推指定渠道。
    返回新 digest 文本。"""
    feedback = (feedback or "").strip()
    if not feedback:
        raise RuntimeError("反馈内容为空")
    out_dir = os.path.join(HERE, cfg.get("output_dir", "exports"))
    sources = [e["json"] for e in _day_exports(cfg, day_str)]
    if not sources:
        raise RuntimeError("%s 还没有导出记录，无法重总结（请先跑 /digest）" % day_str)
    items = merge_substantive(sources)
    if not items:
        raise RuntimeError("%s 的导出记录里没有实质消息" % day_str)

    channel_name = cfg.get("channel_name", "price-talk")
    text_block = build_text_block(channel_name, day_str, items)
    sys_p = SYSTEM_PROMPT + (FEEDBACK_HINT % feedback)
    log("按用户反馈重总结（%s，%d 条消息）：%s" % (day_str, len(items), feedback[:80]))
    digest = call_deepseek(cfg, text_block, system_prompt=sys_p)

    max_per_day, _ = _throttle_cfg(cfg)
    used = len(_day_exports(cfg, day_str))
    t_total = len([t for t in (cfg.get("discord_tokens") or []) if t]) \
        or (1 if cfg.get("discord_token") else 0)
    digest = (digest or "").rstrip() + \
        "\n\n---\n令牌：-/%d · 次数：%d/%d（人工反馈重总结，未重新导出）" % (t_total, used, max_per_day)
    title = "🤖 DTAO price-talk 日报 · %s（按反馈修订）" % day_str
    want = [x.lower() for x in (targets or ["feishu", "tg"])]
    if cfg.get("push_enabled", True):
        if "feishu" in want:
            try:
                push_feishu(cfg, title, digest)
            except Exception as e:
                log("飞书推送异常：%s" % e)
        if "tg" in want:
            try:
                push_telegram(cfg, title, digest)
            except Exception as e:
                log("Telegram 推送异常：%s" % e)
    else:
        log("push_enabled=false，仅打印：\n" + digest)

    try:
        with open(os.path.join(out_dir, "dtao_%s.digest.txt" % day_str), "w", encoding="utf-8") as f:
            f.write(digest)
        with open(os.path.join(out_dir, "last_digest.txt"), "w", encoding="utf-8") as f:
            f.write(digest)
    except Exception as e:
        log("日报落盘异常（忽略）：%s" % e)
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
    log("===== DTAO 流水线启动 v%s（半自动 / 手动触发）=====" % __version__)
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
        # 潜力观察单独生成（不随周报推送，只落盘供 /watch 读取）
        try:
            generate_potential(cfg, "week_%s~%s" % (day_first, day_last), all_items)
        except Exception as e:
            log("潜力观察流程异常（忽略）：%s" % e)
    else:
        log("push_enabled=false，仅打印不推送：\n" + title + "\n" + digest)
    log("===== DTAO 周报流水线结束 =====")

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="DTAO price-talk 半自动流水线")
    ap.add_argument("-V", "--version", action="version",
                    version="DTAO price-talk pipeline v%s" % __version__)
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
    except BaseException as e:
        # 连 KeyboardInterrupt / 编码错误等非 Exception 也一并记录，绝不再静默退出
        import traceback as _tb
        _stack = _tb.format_exc()
        log("❌ 流水线失败：%s\n%s" % (e, _stack))
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
