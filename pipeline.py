#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DTAO price-talk 每日流水线：Discord 导出 -> LLM 总结 -> 飞书 + Telegram 双推
=============================================================================
仅依赖 Python 标准库（无需 pip install）。半自动模式：由人工手动触发
（终端命令 / 在 WorkBuddy 对话里让我执行），也可接入 cron / 任务计划程序。
默认导出「前一天」指定频道的聊天记录，过滤闲聊，用一个 OpenAI 兼容的大模型生成中文结构化
日报，并推送到飞书群机器人和 Telegram Bot。支持指定日期查询、以及 repush 补推。

========= 首次使用 Setup =========
1) 复制 config.example.json -> config.json，填入你的密钥：
   - discord_token      : 你的 Discord user token（能读该频道）
   - channel_id         : 频道 ID（默认 DTAO price-talk = 1341812134807343114）
   - deepseek_api_key   : 大模型 API key（字段名沿用 deepseek_*，可填任意 OpenAI 兼容服务）
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
__version__ = "1.10.1"
import urllib.request
import urllib.error
import urllib.parse

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


# 国内大模型 API 直连白名单：这些服务不需要翻墙，走 Clash 反而会被节点 502 掐断。
# 可用 config.json 的 no_proxy_hosts 追加（例如自建的 OpenAI 兼容网关）。
_DIRECT_HOSTS = (
    "api.deepseek.com",
    "api.moonshot.cn",
    "api.moonshot.ai",
    "dashscope.aliyuncs.com",
    "open.bigmodel.cn",
    "ark.cn-beijing.volces.com",
    "qianfan.baidubce.com",
)


def _host_of(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _is_direct_host(host):
    if not host:
        return False
    hosts = list(_DIRECT_HOSTS)
    if _CFG:
        hosts += [str(h).lower() for h in (_CFG.get("no_proxy_hosts") or []) if h]
    return any(host == h or host.endswith("." + h) for h in hosts)


def _opener_for(url):
    """按目标域名选 opener：国内 LLM 直连，其余（Discord/Telegram）走代理。

    代理抖动时，国内 API 请求被硬塞进 Clash 会出现 502，导致「总结失败」，
    而它其实根本不需要代理。这里按域名分流，能消掉一大半莫名失败。
    """
    host = _host_of(url)
    if _is_direct_host(host) and not (_CFG and _CFG.get("llm_direct") is False):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _opener()


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
    - 配置了 discord_tokens（数组，且至少一个非空）-> 顺序轮换：1 -> 2 -> 3 -> 1 …
      把请求均匀摊到多个账号，降低单个 user token 被风控命中的概率。
    - **顺序轮换（v1.4.2 起）**：上次用的编号记录在 exports/_digest_state.json 的
      _meta.last_token_idx，本次固定用它的下一个（环形）；首次/状态缺失时随机起点。
      （v1.3.2 曾用「排除上次后纯随机」，3 个号时某个号仍可能隔天又被抽中，
       用户要求分布均匀，故改为确定性轮换；只有 1 个号时永远用它。）
    - 否则回退到单个 discord_token（向后兼容）。
    注意：轮换模式下，每个 token 对应的账号都必须是该服务器成员且能访问目标频道。
    """
    raw = cfg.get("discord_tokens") or []
    pool = [t for t in raw if t]
    # 防御：discord_tokens 数组中若混入空串/空值，一律忽略，绝不会抽到空 token 导致导出失败
    if len(raw) != len(pool):
        log("discord_tokens 共 %d 项，已忽略 %d 个空/无效项，实际可用 %d 个"
            % (len(raw), len(raw) - len(pool), len(pool)))
    if pool:
        last_idx = None
        try:
            meta = _load_digest_state(cfg).get("_meta") or {}
            last_idx = int(meta.get("last_token_idx") or 0)
        except Exception:
            last_idx = 0
        if last_idx and 1 <= last_idx <= len(pool) and len(pool) > 1:
            idx = last_idx % len(pool)          # 上次第 N 个 -> 本次第 N+1 个（环形）
            log("令牌轮换：上次真实导出用的是第 %d 个，本次按顺序用第 %d 个（共 %d 个）"
                % (last_idx, idx + 1, len(pool)))
        else:
            idx = random.randrange(len(pool))   # 首次或状态缺失：随机起点，之后进入固定轮换
            log("令牌轮换：无上次记录，随机起点用第 %d 个（共 %d 个）" % (idx + 1, len(pool)))
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
    日期语义：[after, before) 左闭右开。参数两种格式都认：
      YYYY-MM-DD            -> 按 UTC 当日 00:00（兼容旧调用）
      YYYY-MM-DDTHH:MM:SS   -> 按 UTC 该时刻（run_export 会把北京时区日边界换算成 UTC 再传入）
    """
    import datetime as _dt
    def _to_utc_ms(s):
        if "T" in s:
            d = _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        else:
            d = _dt.datetime.strptime(s, "%Y-%m-%d")
        return int(d.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
    start_ms = _to_utc_ms(after_date)
    end_ms = _to_utc_ms(before_date)

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
    # after_dt/before_dt 是「北京时区」的自然日边界（00:00 -> 次日 00:00）。
    # Discord 按 UTC 计时，这里精确换算成 UTC 时刻再传给导出后端，
    # 保证导出范围就是北京时间自然日（v1.5.0 前 date 字符串被按 UTC 解释，实际范围是北京 08:00 -> 次日 08:00）。
    utc = datetime.timezone.utc
    after_utc = after_dt.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%S")
    before_utc = before_dt.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%S")
    # DCE/docker 后端按本机时区（北京）解释不带时区的日期时间，直接传北京本地时刻
    after_local = after_dt.strftime("%Y-%m-%dT%H:%M:%S")
    before_local = before_dt.strftime("%Y-%m-%dT%H:%M:%S")
    log("导出范围（北京时间）：%s -> %s"
        % (after_dt.strftime("%Y-%m-%d %H:%M"), before_dt.strftime("%Y-%m-%d %H:%M")))

    # 内置 API 后端：完全绕开 DiscordChatExporter（.NET），代理可控、速度秒级
    if backend == "api":
        export_via_api(cfg, token, channel, after_utc, before_utc, out_json)
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
            "--after", after_local, "--before", before_local,
        ]
    else:
        exe = cfg.get("dce_exe")
        if not exe or not os.path.exists(exe):
            raise RuntimeError("dce_backend=local 但找不到 dce_exe：%s" % exe)
        os.makedirs(os.path.dirname(out_json), exist_ok=True)
        cmd = [exe, "export", "-t", token, "-c", channel, "-f", "Json",
               "-o", out_json, "--after", after_local, "--before", before_local]

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
            export_via_api(cfg, token, channel, after_utc, before_utc, out_json)
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

def _msg_time(m, day_date=None):
    """Discord 时间戳是 UTC，转成北京时间显示；跨日补录的消息加「次日」前缀。
    抽出来共用，避免实质消息与水聊两条链路各写一遍、越改越不一致。"""
    ts = m.get("timestamp") or ""
    t = ts[11:16]
    try:
        bj = (datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
              + datetime.timedelta(hours=8))
        t = bj.strftime("%H:%M")
        if day_date and bj.date() != day_date:
            gap = (bj.date() - day_date).days
            t = ("次日 %s" % t) if gap == 1 else ("跨日%s %s" % (bj.strftime("%m-%d"), t))
    except Exception:
        pass
    return t


def extract_substantive(json_path, ctx=2, day_str=None):
    """抽出实质消息，并**带上前后各 ctx 条上下文**。
    为什么需要上下文：群聊里大量指代（「2t daily buyback 不值一提」）要结合上一句
    （「61 是不是出事了」）才能判断 2t 指哪个子网；只挑关键词句会把指代关系切断。
    day_str：该文件对应的「总结日」。导出范围可能跨到下一天（昨日 00:00 -> 现在的新规则），
    下一天的消息在时间前缀上加「次日 」，防止模型把今晨的事写成昨天的事。"""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    msgs = data.get("messages", [])
    n = len(msgs)
    day_date = None
    if day_str:
        try:
            day_date = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
        except Exception:
            day_date = None

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
        t = _msg_time(m, day_date)
        react = sum(r.get("count", 0) for r in m.get("reactions", []))
        out.append((react, t, author, c))
    # 按时间升序输出：模型需要顺着对话流理解指代与因果，按热度排序会把上下文打散
    out.sort(key=lambda x: x[1])
    return out


# ----------------------------------------------------------------------------
# 2b) 水聊抽取：日报里被过滤掉的闲聊（段子/梗/互怼/跑题），单独成一条链路
# ----------------------------------------------------------------------------
# 单独剔除的「无内容消息」：纯 @、纯表情、纯语气词。
# 水聊允许大量英文与梗，但完全不构成一句话的不值得送给模型。
_CHAT_DROP = re.compile(
    r"^(\s*(https?://\S+\s*)|(\:\w+:\s*)|(@\S+\s*)|(lol+|lmao+|xd+|ha+ha*|嗯+|哦+|啊+|好+|对+|1+)\s*)+$",
    re.I)

# 水聊侧记不碰的内容：政治站队、选举评论、脏话与歧视性辱骂。
# 提示词里写「不许复述」模型仍会漏（实测两次），所以直接在抽取阶段过滤，确定性更高。
_CHAT_SENSITIVE = re.compile(
    r"(?i)(trump|biden|harris|obama|democrat\w*|\bdems?\b|republican\w*|\bgop\b|election\w*|senate|"
    r"congress|president\w*|politic\w*|nazi|hitler|rape\w*|nigg\w*|retard\w*|faggot\w*|"
    r"cunt|bitch\w*|asshole|fuck\w*|shit\w*|porn\w*|kanker)"
    r"|选举|特朗普|拜登|民主党|共和党|纳粹")


def extract_chitchat(json_path, day_str=None, min_len=6, max_len=320):
    """抽出**水聊**消息：非空、能读成一句话、且不含任何实质信息（is_substantive 为 False）。
    这些正是日报里被丢掉的部分——群友的段子、梗、互怼、情绪、生活琐事与跑题讨论。
    min_len=6：太短的口水话（"vision"、"+1"、"same"）脱离上下文没有信息量，
    除非它被多人回应过（react>=2），否则丢掉。day_str 同上：跨零点的消息标「次日」。"""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    msgs = data.get("messages", [])
    day_date = None
    if day_str:
        try:
            day_date = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
        except Exception:
            day_date = None
    out = []
    dropped_sensitive = 0
    for m in msgs:
        c = (m.get("content") or "").strip()
        if not c or NOISE.match(c) or _CHAT_DROP.match(c):
            continue
        if is_substantive(c):
            continue
        if _CHAT_SENSITIVE.search(c):     # 政治/脏话/歧视：侧记里不出现，也不给模型看
            dropped_sensitive += 1
            continue
        react = sum(r.get("count", 0) for r in m.get("reactions", []))
        if len(c) < min_len and react < 2:
            continue
        author = (m.get("author") or {}).get("nickname") or (m.get("author") or {}).get("name") or "?"
        out.append((react, _msg_time(m, day_date), author, c[:max_len]))
    if dropped_sensitive:
        log("水聊过滤：跳过 %d 条涉政/脏话/歧视消息" % dropped_sensitive)
    out.sort(key=lambda x: x[1])
    return out


def merge_chitchat(json_paths, day_str=None):
    """合并同一天多次导出的水聊消息，按 (作者, 内容) 去重，按时间升序。"""
    all_items, seen = [], set()
    for jp in json_paths:
        if not os.path.exists(jp):
            continue
        for it in extract_chitchat(jp, day_str=day_str):
            key = (it[2], it[3])
            if key in seen:
                continue
            seen.add(key)
            all_items.append(it)
    all_items.sort(key=lambda x: x[1])
    return all_items


def _chat_stats(items):
    """给模型一段客观统计：谁说得最多、哪几条被回应最多。帮它抓重点，也避免它瞎猜热度。"""
    from collections import Counter
    cnt = Counter(a for _r, _t, a, _c in items)
    top = "、".join("%s %d 条" % (a, n) for a, n in cnt.most_common(8) if a and a != "?")
    hot = [it for it in sorted(items, key=lambda x: -x[0]) if it[0] >= 2][:5]
    lines = []
    if top:
        lines.append("发言最多：%s" % top)
    if hot:
        lines.append("被回应最多：" + "；".join(
            "%s「%s」🔥%d" % (a, (c or "").replace("\n", " ")[:24], r) for r, _t, a, c in hot))
    return lines


def build_chat_text_block(channel_name, days):
    """水聊输入：按日期分段，每条带「月-日 时:分」。
    days = [(day_str, items), ...]，已按时间正序。"""
    flat = [it for _ds, items in days for it in items]
    authors, _seen = [], set()
    for _r, _t, a, _c in flat:
        if a and a not in _seen:
            _seen.add(a)
            authors.append(a)
    span = days[0][0] if len(days) == 1 else "%s ~ %s" % (days[0][0], days[-1][0])
    lines = [
        "频道：%s    日期：%s    水聊消息 %d 条（按时间顺序；🔥n 表示被表情回应 n 次）"
        % (channel_name, span, len(flat)),
        "说明：这些是当天**被日报过滤掉的闲聊消息**，不含子网/行情实质讨论，"
        "整理目标是「群聊侧记」，不是投研。",
        "发言者名单（共 %d 人；引用人名必须原样照抄，名单外的名字不得作为发言者出现）：%s"
        % (len(authors), "、".join(authors)),
    ]
    for ln in _chat_stats(flat):
        lines.append(ln)
    multi = len(days) > 1
    for ds, items in days:
        if multi:
            lines.append("")
            lines.append("===== %s（%s）%d 条 =====" % (ds, _weekday_cn(ds), len(items)))
        for i, (react, t, author, c) in enumerate(items, 1):
            c1 = (c or "").replace("\n", " ").strip()
            hot = " 🔥%d" % react if react >= 2 else ""
            stamp = ("%s %s" % (ds[5:], t)) if multi else t
            lines.append("%d. [%s] <%s>%s %s" % (i, stamp, author, hot, c1))
    return "\n".join(lines)


_QUOTE_RE = re.compile(r"[「“\"]([^」”\"]{4,80})[」”\"]")


def _norm_text(s):
    """比较引语用：去掉空白/标点/加粗符号并转小写，避免格式差异导致匹配不上。"""
    return re.sub(r"[\s\*_`，。、；：！？,.!?;:'\"“”‘’「」（）()\-–—~…]", "", (s or "")).lower()


def quote_author_audit(digest, items):
    """确定性核对「引语 → 发言者」：引号里的话如果能在原文找到，就必须由该行点名的人说出。
    实测校对轮会漏（把 Nexras 的「France is dead」安到 Kakashi 头上），所以再加这道代码闸门。
    返回 (mismatches, not_found)：
      mismatches = [(行文本, 引语, 真实发言者列表, 行内点名的人)]，可直接用于让模型定点改；
      not_found  = 原文里找不到的引语（可能是翻译，也可能是编的，只记日志不自动改）。"""
    users = set(a for _r, _t, a, _c in items if a and a != "?")
    normed = [(_norm_text(c), a) for _r, _t, a, c in items]
    mismatches, not_found = [], []
    for line in digest.split("\n"):
        quotes = _QUOTE_RE.findall(line)
        if not quotes:
            continue
        mentioned = [n for n in users if n and n in line]
        for q in quotes:
            nq = _norm_text(q)
            if len(nq) < 6:
                continue
            authors = {a for nc, a in normed if a and nq in nc}
            if not authors:
                not_found.append((line.strip()[:60], q))
                continue
            if mentioned and not (authors & set(mentioned)):
                mismatches.append((line.strip()[:80], q, sorted(authors), mentioned))
    return mismatches, not_found


def fix_quote_authors(cfg, text_block, digest, mismatches):
    """把引语归属错误交给模型定点修正（只改这些句子的归属，其余一字不动），然后复检。"""
    if not mismatches:
        return digest
    hint = "\n".join("- 引语「%s」实际是 %s 说的，但初稿写成 %s"
                     % (q, "、".join(a), "、".join(m)) for _l, q, a, m in mismatches)
    try:
        fixed = call_llm(
            cfg,
            text_block + "\n\n【侧记初稿】\n" + digest
            + "\n\n【已核实的引语归属错误（只改这些，别的一字不动）】\n" + hint
            + "\n\n请修正这些引语对应的人名：改成实际发言者；若整句因此读不通，就删掉这半句。"
              "不要新增内容、不要改其他句子、不要改字数结构。",
            system_prompt=CHAT_VERIFY_PROMPT,
        )
        fixed = sanitize_chat(fixed or "")
        n = len(re.sub(r"\s", "", fixed))
        if n and n >= len(re.sub(r"\s", "", digest)) * 0.6:
            log("引语归属修正：%d 处" % len(mismatches))
            return fixed
    except Exception as e:
        log("引语归属修正失败（保留原稿）：%s" % e)
    return digest


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
    "**🔥 必读重点**（**有几件事写几条，通常 3-7 条**，当天最该知道的全部内容；"
    "每条一句说清「谁/什么 + 关键结论」，写得越短越好、意思说全即可，"
    "并把关键人物、**SN编号/子网名**、事件用 **加粗**；子网异动、上线、rug、机制变更、算力变化都写在这里，"
    "用 ⚠️ 或 ✅ 标注。**本节即子网动向的唯一出口，不要再单列子网汇总小节，避免重复**）\n"
    "**👥 重要人物观点**（点名 **@人物/开发者**，他讲了什么、对什么有影响；与上面重复的事实不用复述，只补充观点与依据；普通群友无关发言不列；**没有值得单列的就不写这一节**）\n"
    "**📎 其他细节**（补遗：前面没覆盖但有价值的细节；**没有就整节省略**，不要硬凑）\n"
    "**💡 一句话总结**（当天全局判断，一句收束）\n"
    "\n"
    "【不要输出潜力观察】「潜力观察/布局建议」由另一个专门流程单独生成，日报正文中**不要**出现该板块，"
    "也不要在其他小节里写布局建议、目标价、催化剂清单。\n"
    "\n"
    "【排版与强调规范（飞书/TG 双平台通用，务必遵守）】\n"
    "- 关键主体一律 **加粗**：人物名、**SN编号/子网名**、代币、事件、关键数字。\n"
    "- **子网第一次出现时写成「SN53 Engy」这种「编号 + 名字」**（名字以子网对照表为准），"
    "同一篇里后文可以只写编号；对照表里查不到名字的，只写编号，不要编名字。\n"
    "- 用 emoji 标注程度：🔥 重大利好/热点、✅ 已落地、⚠️ 风险/异动、📌 值得关注、❌ 利空。\n"
    "- 每条要点单独成行，用 - 项目符号；小节之间空一行；避免横排长句，适合手机端。\n"
    "- 不要用 # 标题、不要用 > 引用（部分平台不渲染）。\n"
    "- 客观、不臆测、引用具体编号与名字。\n"
    "\n"
    "【篇幅与精炼（务必遵守，写长了即不合格）】\n"
    "- **只有字数上限，没有下限**：全文**不超过 1200 字**（含标点）。"
    "**内容少就按少的写**——当天只有 3 件事，就写 3 条、300 字收工，"
    "不要为了「看起来完整」而稀释、铺陈、补背景。简洁 + 信息全 = 合格；长而稀 = 不合格。\n"
    "- 条数以「有几件事」为准，绝不凑数：一件事就一条，五件事就五条；"
    "把一件事拆成两三条、或把无关小事塞进「其他细节」，都算凑字数。\n"
    "- **绝对不许在正文里写任何字数标注**：不写「（45-60字）」「（约 50 字）」「（60字）」这类字样——"
    "那是写给你自己看的约束，写进正文就是废话，代码层会直接删除。\n"
    "- 一条只说一件事：先写结论，再写一句依据（谁说的/原文怎么讲），不铺垫、不结尾升华。\n"
    "- **禁止废话（重点整治）**：不写「值得一提的是」「此外」「总体来看」「可以看出」「这一点值得关注」"
    "这类填充词；不复述前面已经说过的事实；**不要解释自己为什么这么写**"
    "（如「由于信息有限」「考虑到篇幅」「此处只列要点」）；不写「综上所述」「总而言之」这类收尾套话；"
    "同一子网的碎片讨论合并成一条，不要拆成多条凑数。\n"
    "- **每条都必须是「信息」而非「修辞」**：删掉所有不携带新事实的形容词、程度副词、背景铺垫。"
    "删掉某个词后读者理解不变，那个词就该删。\n"
    "- 数字只留关键的（金额、TAO 数、涨幅、时间窗），次要数字删掉；形容词能删就删。\n"
    "- **精炼不等于省略**：当天发生的子网事件、机制变更、明确质疑、未解答的疑问都必须保留，"
    "只是用更短的话说清；宁可删形容词，也不能丢事实。\n"
    "- **压缩禁区（违反即不合格）**：不能为了短把不同时间的独立线索并成一句；"
    "人物的身份/所属子网没有直接原文支持时，删掉归属，只写他讲了什么；"
    "孤立、无上下文的碎片编号（如单独一句「103 capcom」）干脆不写进日报，不许猜它对应谁。\n"
    "\n"
    "【事实核查与验证规则（最高优先级，违反任何一条即为不合格日报）】\n"
    "1. **定性必须有依据**：对「跑路/弃盘/rug/转型/换方向/清仓」等重大定性，只能依据聊天记录中的明确表述或官方公告原文。"
    "若频道内出现官方澄清、更正或后续进展（如团队宣布转型、发布说明），**必须以最新的澄清为准**重写结论，"
    "并明确写出「官方澄清为转型/换方向」等表述；严禁沿用此前群友的猜测作为最终结论。\n"
    "2. **区分事实与猜测**：机器人/转发播报的官方公告是「事实」，群友的解读是「观点」，两者不得混写。"
    "群友的负面猜测若与官方公告矛盾，一律以公告为准；无公告、无明确说法时只能写「原因不明，社区有XX猜测」。\n"
    "3. **排名与最高级必须落地**：凡出现「第一/最大/最高/涨幅冠军」等表述，必须写明**具体的 SN 编号/子网名与数据**（如「SN27 当日 alpha 涨幅第一」）。"
    "若原消息本身没有指明是哪个子网，只能引用原话并标注「（原文未指明具体子网）」，或直接舍弃该条，**严禁脑补编号**。\n"
    "4. **数字/时间/代号照抄**：SN 编号、代币名、时间等必须照抄原文，禁止改写、补全或张冠李戴；不确定的内容宁可不写。"
    "引用数字时必须确认**说话人正确**（是谁在什么时间说的），不得把 A 说的数字记到 B 头上。"
    "**唯一例外（用户已确认的阅读偏好）**：金额可换算成「万/亿」并保留两位小数（$22,383 → 2.24 万美元，$144,607 → 14.46 万美元），"
    "但折算必须准确、量级不能错，且百分比、TAO 数量、SN 编号一律保持原样，不得四舍五入。\n"
    "5. **重要事件多来源交叉**：单一消息来源的重大利好/利空，需标注「（单一消息源，待确认）」；"
    "频道内有多人讨论或有链接佐证的才可直接陈述。\n"
    "\n"
    "【溯源与禁止臆造（最高优先级，逐条自检）】\n"
    "6. **每条结论必须可溯源**：写进日报的每个事实，都必须能在聊天记录中找到对应原文；必要时用「某某称/据某某」标明说话人。"
    "找不到原文支撑的，一律删除，不要用常识补全。\n"
    "7. **禁止推断手段、动机与因果**：原文只说「有人卖了某标的」，就只能写「有群友称有人卖出」；"
    "不得自行添加「通过质押卖出」「本人承认卖出」「为了砸盘」「团队授意」等原文没有的手段、动机或因果链。"
    "原文没说清楚的地方，就写「具体方式/来源原文未说明」。"
    "**也不得给数字补上原文没有的频率与单位**（原文只写「504t chain buys」，就不能写成「每天回购 504 个 TAO」）。\n"
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
    "17. **禁止跨时段拼装身份与归属**：给某人贴上身份标签（如「某子网所有者」「前某子网负责人」）、"
    "或把 A 的话与 B 的事连成因果，**必须有同一条消息或紧邻的上下几句直接支持**。"
    "输入里凡出现「~~~~ 时间跳转 ~~~~」标记，说明前后两条消息相隔数小时、彼此无关，"
    "**绝对不可把标记两侧的人物、事件、子网拼在一起**；"
    "只写「103 capcom」「61」这类没有主语的碎片消息，只能当成一条独立线索，不得用来给任何人贴身份。\n"
    "18. **反例（务必避免）**：原文 13:18 有人说「我的子网被关了排放」，19:11 另一个人单发一句「103 capcom」，"
    "这两条隔了 6 小时、中间毫无连接，**绝不能**写成「某人被注销的就是 SN103」。"
    "建立「人—子网」关系只有两种情况：他本人说出了编号，或者紧邻的几句里有人当场对号入座。"
    "不满足就只写「其子网（原文未指明编号，另有一条无主语的『103 capcom』碎片消息，无法确认是否相关）」。\n"
    "19. **跨消息的推断必须标注**：如果一条结论需要把两条以上消息拼起来才能得出"
    "（典型如「某人说的那个子网，就是另一人单独提到的编号」），必须写成「…（推断，原文未明示）」，"
    "不得使用「确认」「即」「就是」「实为」这类断言词。宁可分开写成两句各自有据的话，也不要合成一句。\n"
    "20. **禁止给编号绑定子网名**：只有原文明确写过「117 就是 Higgsfield」这类对应关系，才能写成「SN117（Higgsfield）」；"
    "只是同一段讨论里同时出现编号和项目名，不许用括号绑定。"
    "凡用「即 / 就是 / 实为 / 后确认 / 确认为 / 对应」把两条消息连起来的句子，一律视为编造嫌疑，必须拆开重写。\n"
    "21. **一人一句，不许合成**：转述多人信息时，必须分别写清「A 说了什么」「B 说了什么」。"
    "典型错误是把「A 说很兴奋，因为英伟达投了某项目」和「B 说已向某人确认他认识创始人」合成"
    "「A 表示 B 已确认该项目拿到投资」——这是编造。原文各自只支持各自的半句，就分开写两句，或者只写有直接依据的那句。\n"
    "22. **子网对照表只用来认名字**：输入开头可能附一份「子网对照表」（SN 编号 ↔ 子网名 ↔ 业务）。"
    "它的作用是让你把「117」「Engy」这类称呼对上号、写出正确的 **SN编号/子网名**，"
    "**不是当日新闻**：不得把表里的业务描述当作今天发生的事写进日报，也不得据此推断价格、团队、融资。"
    "表里写「未知」的不许补全；聊天原文与表冲突时以原文为准，并注明「该编号可能已换项目」。\n"
    "23. **相邻两句不等于因果**：同一个人连着发的两条消息（例如「我看好 117」+「英伟达刚投了 higgsfield」），"
    "除非原文出现 because / so / 因为 / 所以 / 这就是 这类连接词，否则只能**并列陈述**，"
    "严禁写成「理由是…」「因为…所以看好」「受…带动」。想体现关联就写「同一时段他还提到…」，让读者自己判断。\n"
    "24. **背景资料只用来理解，不用来总结**：输入里可能附有【背景资料：前天/大前天】小节，"
    "它们的作用是帮你接上跨天话题（比如当天说的「那个子网」其实前天就开始讨论、某人的表态有前文）。"
    "**背景日发生的事一律不得写成总结日（当天）的新闻**；如果当天消息明确延续背景日的话题，"
    "可以写「延续前几日的话题」，但每件事实必须归属其真实发生的日期，不得把日期混写、"
    "也不得把背景日的内容单独扩成一条。总结对象永远只有【当日消息】。\n"
    "24b. **子网档案是判断底子，不是写作素材**：输入里可能附有【本次聊到的子网 —— "
    "名称对照（+ 此前判断）】，里面每个子网只有名称/业务（有的还带几条「此前判断」）。"
    "它的用处只有两个，**都不是让你往里写**："
    "① **认人**——当天消息只写了编号的，用这里的名称/业务写清楚，别写「某子网」「该子网」；"
    "② **判关联**——只有当今天的事与档案构成「**项目有进展**」（旧事被推进/落地）"
    "或「**早期判断被验证**」（某条「此前判断」被今天的事实证实或推翻）时，"
    "才用**一句话**带过，如「（10-30 曾说会跌破 438，今日已跌破）」。"
    "**没有构成这两种关系就一个字都别写**：不要复述历史讨论、不要把旧事写成本日新闻、"
    "不要把旧数据当今天的行情。日报主体永远是当天谈到的话题，宁可少写也不要凑档案。\n"
    "25. **「次日」消息是今晨补录**：总结「昨天」时，导出范围延伸到当下，"
    "时间带「次日」前缀的消息发生在**总结日的下一天（今天凌晨到现在）**。"
    "这些消息可以写进日报，但必须标注「今晨」，不得当成总结日当天的事件，"
    "也不得与总结日的事件拼接因果（例：昨日 23:00 的表态和今晨 08:00 的砸盘是两件事）。\n"
    "\n"
    "【稳定性要求】相同输入应给出相同结论：优先复述原文事实，不做发散联想；"
    "对同一事件的定性只有一种写法，不要在不同表述之间摇摆。\n"
    "\n"
    "【语言风格（面向普通读者）】\n"
    "- **用大白话写**，像给朋友讲今天发生了什么；避免「SOTA」「BD」「NDAs」「cabal」这类行话直接甩出来，"
    "必须用时就写成「中文（英文缩写）」的形式。\n"
    "- **只有这些可以保留英文**：SN 编号、子网名/项目名（Engy、Lium、Gittensor、Chutes…）、"
    "人名/昵称（BROdin、τFIG、5GuRL…）、代币名（TAO、dTAO、Alpha）。\n"
    "- **其余一律用中文**：emission→排放、buyback→回购、burn→销毁、staking→质押、"
    "liquidity/LP→流动性池、miner→矿工、validator→验证人、owner→所有者、deregister→注销、"
    "registration→注册、incentive→激励、revenue→营收、whitelist→白名单。\n"
    "- 不要把整句英文照搬进中文句子；英文原话只翻成中文，必要关键词放括号里，不逐字直译。\n"
    "- 除上面允许的专有名词外，一句话里出现两个以上英文单词，基本就是没翻译干净，必须改写。"
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
    "你是一名 Bittensor 生态的资深研究员，负责把 Discord 频道「DTAO - price-talk」最近若干天"
    "（天数以输入里的「日期范围」为准，可能是 7 天，也可能只有 2-3 天）的聊天记录，"
    "整理成中文周报，侧重「跨日趋势、升温信号、以及更确定的布局机会」。\n"
    "\n"
    "【内容边界】同日报：只关注子网、代币机制、核心成员动态、催化事件；忽略水聊与无关内容。\n"
    "\n"
    "【日期纪律】输入按日期分段，每条消息都带发生日期。写事实时以所在分段的日期为准：\n"
    "1. 关键事件尽量标注发生日期（如「11-01」），方便判断是「新发生」还是「延续讨论」；\n"
    "2. 严禁把不同日期的事拼成同一天的因果，严禁把单日的一次表态写成「整周持续」；\n"
    "3. 「多次提及/持续升温」必须真的在多个分段出现过，只在一天出现就写「某日」而不写「本周一直」。\n"
    "\n"
    "【溯源纪律（违反即作废）】\n"
    "1. **共现≠关联**：某个公司/事件与某个子网在同一天被提到，不等于它们有股权、投资、合作或因果关系。"
    "原文没有一句话直接说明两者关系的，只能并列陈述（「同段他还提到……」），严禁写成「X 加持 SNxx」「因 X 而涨」。\n"
    "2. **代词必须落到原文**：原文用 he/they/某人指代时，只有前后文明确指名才能写出具体人名；"
    "否则写「有人」「一名成员」，不许替模型自己指定主体。\n"
    "3. **因果必须有连接词**：出现 because/so/因为/所以/导致 才可写因果，同一人相邻两句话不算。\n"
    "4. 排名、最高级、金额、编号一律照抄原文；原文没给数字就不写数字。\n"
    "5. **「承认/自认/证实」必须有其本人原话**：反讽、玩笑、俏皮回应（「我无罪，虽然我确实享受犯罪」）"
    "不算承认，只能写「被多人指责…，他本人以玩笑回应」；别人的指控必须写明是谁指控的。\n"
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
    "不用 # 标题、不用 > 引用；客观不臆测，引用具体编号与名字；"
    "全文不超过 1400 字（**只有上限、没有下限**，内容少就短），"
    "每条能一句话说完就一句话，禁写填充词与重复内容。\n"
    "【事实核查规则】同日报【事实核查与验证规则】：重大定性以最新官方澄清为准；"
    "排名/最高级必须落到具体 SN 编号与数据，原文未指明就标「（原文未指明）」或舍弃；数字/代号照抄，严禁脑补。"
)

WEEKLY_VERIFY_PROMPT = (
    "你是周报校对员，不是作者。用户消息里包含【原始聊天记录（按日期分段）】和一份【周报初稿】。"
    "你的任务：逐条对照原文核对初稿，只做纠错与压缩，不改章节结构，输出修正后的完整周报。\n"
    "\n【必须核对的六类错误】\n"
    "1. **数字与人名**：每个数字（金额、TAO 数、百分比、时间）都要回原文确认，并确认**说话人正确**；"
    "原文对不上就删掉或改成正确归属（金额已写成「万/亿」的保留这种写法，只核对换算）。\n"
    "2. **人物—子网归属**：写「某人的子网是 SNxx」时必须有紧邻原文直接支持；靠相隔很久的碎片推出来的一律删掉归属。\n"
    "3. **日期归属**：初稿里的事件，日期必须与原文分段一致。把 A 日的事写成 B 日、"
    "把单日表态写成「整周持续」、或把两天的两件事并成一条因果的，改写或删除。\n"
    "4. **无依据的定性**：原文没明说的手段、动机、因果（「为了砸盘」「团队授意」「正在预热」）删掉或改回原文表述；"
    "单来源的重大利空/利好标「（单一消息源，待确认）」。\n"
    "5. **拼出来的因果**：出现「理由是」「因为…所以」「受…带动」时，回原文确认有没有 because/so/因为/所以；"
    "只是同一人相邻两句话、或两件互不相干的事，改成并列陈述或删掉因果。\n"
    "6. **语言**：除 SN 编号、子网名、人名、代币名（TAO、dTAO、Alpha）外，不得残留英文单词。\n"
    "7. **共现拼关联**：初稿把「某公司/某事件」和「某子网」绑成投资、加持、合作、因果的，"
    "回原文找是否有一句话直接说明；没有就改成并列陈述或删掉关联词。\n"
    "8. **代词与主体**：初稿把原文的 he/they/有人说成具体人名的，核对前后文是否明确指名；"
    "不明确的改成「有人」或删掉人名。\n"
    "9. **「承认/自认/证实」**：必须有其本人原话；反讽、玩笑、俏皮回应不算承认，改写成「被指控…，本人以玩笑回应」。\n"
    "\n【输出要求】只输出修正后的五个小节：🔥 本周核心趋势 / 👥 重要人物观点 / ⚠️ 风险汇总 / "
    "📎 其他细节 / 💡 一句话总结；篇幅**不得超过 1400 字、且不得比初稿更长**（只有上限，没有下限；内容少就短）——纠错只能「改写或删除」，"
    "不许新增句子、不许补细节、不许加总结段；不要输出核对说明；**不确定的内容宁可删除，也不要保留猜测**。"
)

DEEPEN_HINT = (
    "\n\n【本次为重总结 / 合并总结】严格按上面【输出结构】的四个小节输出："
    "🔥 必读重点 / 👥 重要人物观点 / 📎 其他细节 / 💡 一句话总结，"
    "**不得新增「重点子网动向」「潜力观察」「布局建议」等任何额外小节**。"
    "在此结构内做更深入的分析：对「🔥 必读重点」补充更明确的判断与依据（谁说的、原文如何表述），"
    "把散落在各条消息里的同一事件合并成一条完整脉络；避免流水账式复述，聚焦结论与逻辑。"
    "**长度仍受【篇幅与精炼】约束（上限 1200 字，没有下限），不要因为「更深入」而写长**。"
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

# 第二遍：对照原文校错（数字/归属/定性/残留英文），只纠错不改结构
VERIFY_PROMPT = (
    "你是日报校对员，不是作者。用户消息里包含【原始聊天记录】和一份【日报初稿】。"
    "你的任务：逐条对照原文核对初稿，只做纠错与压缩，不改章节结构，输出修正后的完整日报。\n"
    "\n【必须核对的四类错误】\n"
    "1. **数字与人名**：每个数字（金额、TAO 数、百分比、时间）都要回原文确认，并确认**说话人正确**——"
    "初稿最常见的错误就是把 A 说的数字写成 B 说的。原文对不上就删掉这个数字，或改成正确的归属。"
    "（金额若已换算成「万/亿」，保留这种写法，只核对换算准不准，不要改回原始长数字。）\n"
    "2. **人物—子网归属**：初稿写「某人的子网是 SNxx」「某人的子网被关停/注销」时，"
    "必须有同一条消息或紧邻几句直接支持；如果只是靠相隔很久的孤立碎片（例如单独一句「103 capcom」）推出来的，"
    "一律删掉归属，只保留「某成员称其子网……（原文未指明编号）」。\n"
    "3. **无依据的定性**：原文没有明说的手段、动机、因果（如「为了砸盘」「团队授意」「正在预热」），"
    "删掉或改成原文的表述；单来源的重大利空/利好要标「（单一消息源，待确认）」。\n"
    "4. **语言**：除 SN 编号、子网名、人名、代币名（TAO、dTAO、Alpha）外，不得残留英文单词，一律译成中文。\n"
    "5. **拼出来的因果**：初稿出现「理由是」「因为…所以」「受…带动」「得益于」时，回原文确认有没有 "
    "because/so/因为/所以 这类连接词。若只是同一人相邻两句话、或两件互不相干的事被并到一句，"
    "改成并列陈述「同一时段他还提到…」，或直接删掉因果。\n"
    "6. **日期归属**：输入里若附有【背景资料：前天/大前天】小节，逐条确认初稿里的事实都发生在"
    "**总结日当天**；背景日的事件被写成当天新闻的，删掉或改写为「延续前几日话题」并标明真实日期。"
    "时间带「次日」前缀的消息属于**下一天（今晨）**，被写成总结日事件的，改标「今晨」或删除。\n"
    "7. **共现拼关联**：初稿把「某公司/某事件」和「某子网」绑成投资、加持、合作、因果的，"
    "回原文确认有没有一句话直接说明；只是同一天被提到，就改成并列陈述或删掉关联词。\n"
    "\n【输出要求】只输出修正后的四个小节：🔥 必读重点 / 👥 重要人物观点 / 📎 其他细节 / 💡 一句话总结；"
    "篇幅**不得超过 1200 字、且不得比初稿更长**（只有上限，没有下限；内容少就短）——纠错只能「改写或删除」，不许新增句子、不许补细节、不许加总结段；"
    "不要输出核对说明、不要写「已修改」之类的话；**不确定的内容宁可删除，也不要保留猜测**。"
)

# ----------------------------------------------------------------------------
# 输出结构兜底清洗：无论模型返回什么，都强制剔除不该出现在日报里的板块
# （历史上出现过模型自行补出「潜力观察」「重点子网动向」，或旧代码残留提示词导致
#  重复板块被写回，这里做最后一道硬性过滤）
# ----------------------------------------------------------------------------
_SECTION_WORDS = ("必读重点", "重点子网动向", "重要人物观点", "潜力观察",
                  "可布局", "其他细节", "一句话总结", "本周核心趋势", "风险汇总",
                  # 水聊侧记的小节
                  "今日氛围", "谁在聊什么", "全场最佳梗", "跑题现场", "其他碎屑", "一句话吐槽")
# 日报 / 周报里禁止出现的板块
_FORBIDDEN_SECTIONS = ("重点子网动向", "潜力观察", "可布局")


def polish_digest(cfg, text_block, draft, verify_prompt=None, sanitize_fn=None):
    """第二遍校对（可关：config.llm_verify_pass=false）：
    让模型带着原文再读一遍初稿，专门纠「数字记错人」「人物-子网归属靠碎片推出来」
    「原文没有的定性」「残留英文」四类错。校对失败一律保留初稿，不影响推送。
    verify_prompt 默认用日报版；周报/水聊传自己的版本（板块结构与字数配额不同）。
    sanitize_fn：清洗函数，默认日报版；水聊传 sanitize_chat（不做英文替换，梗要留原话）。"""
    if not draft or not cfg.get("llm_verify_pass", True):
        return draft
    clean = sanitize_fn or sanitize_digest
    try:
        fixed = call_llm(cfg, text_block + "\n\n【日报初稿】\n" + draft,
                         system_prompt=verify_prompt or VERIFY_PROMPT)
        fixed = clean(fixed or "")
        n = len(re.sub(r"\s", "", fixed))
        base = len(re.sub(r"\s", "", draft))
        # 下限只防「校对把稿子删成残篇」，与「不设字数下限」不冲突：
        # 用户要的是不注水，不是允许整篇丢内容。所以门槛设在初稿的 45%，
        # 低于这个比例多半是模型砍掉了整节内容，属于失败而非精简。
        if n < max(200, int(base * 0.45)):
            log("校对稿大幅缩水（%d 字，初稿 %d 字），视为校对失败，保留初稿" % (n, base))
            return draft
        if base and n > base * 1.08:
            log("校对稿变成 %d 字（初稿 %d 字），超过 8%% 视为扩写跑偏，保留初稿" % (n, base))
            return draft
        log("校对完成：数字/人物-子网归属/定性/英文 已按原文复核（%d 字）" % n)
        return fixed
    except Exception as e:
        log("校对失败（保留初稿继续推送）：%s" % e)
        return draft


COMPRESS_PROMPT = (
    "你是日报/周报的定稿编辑，本轮任务只有一件事：**删减字数**，绝对不许新增内容。\n"
    "\n【删什么（按优先级，从高到低）】\n"
    "1. 重复信息：同一事件在不同小节重复出现的，只留信息最全的一处；\n"
    "2. 空话与连接词：铺垫句、评价性套话、「值得注意的是」这类填充语；\n"
    "3. 次要细节：不带数字、不带 SN 编号、不影响判断的转述与调侃；\n"
    "4. 弱信号：只有一个人随口一句、没有后续讨论的碎信息。\n"
    "\n【必须保留】\n"
    "- 所有 SN 编号、金额/比例数字、人名与说话人归属；\n"
    "- 原有的全部小节标题，顺序不变，一个小节都不许删；\n"
    "- 事实性结论和风险提示，宁可压缩表述，不许整条砍掉。\n"
    "\n【输出要求】只输出压缩后的完整稿件：不写说明、不写「已压缩」、不加新句子、"
    "不改写事实、不调整章节顺序。每条能一句话说完就一句话，一条只说一件事；"
    "**不许在正文里留下任何字数标注**（如「（45-60字）」），那是提示词痕迹，必须删。"
)


def compress_digest(cfg, digest, lo, hi, system_prompt=None, sanitize_fn=None, cut_hint=None,
                    rounds=2):
    """字数上限闸门：**只限上限、不设下限**（用户明确要求）。

    超过 hi 就再来一轮纯删减（只删不增）。lo 参数保留仅为兼容旧调用，
    **不再用于「压到 lo-hi 之间」**，也不再因为「压得太短」而回退原稿——
    内容少就该短，短而全是干货比长而稀好。
    模型对上限经常不听话（Kimi K3 写周报能给到 2800 字），所以必须有代码层兜底；
    单轮只能删掉 2% 的情况也见过，因此默认允许两轮（投研内容多时用 3 轮）。
    两轮模型压缩后仍超限的，交给 trim_to_limit() 机械裁剪兜底——不依赖模型自觉。"""
    if not digest:
        return digest
    clean = sanitize_fn or sanitize_digest
    cut = cut_hint or ("先砍最次要的整条要点（优先 📎 其他细节 → ⚠️ 风险汇总 → 👥 人物观点），"
                       "再把保留的每条压到最短能说清的长度；🔥 核心趋势、全部小节标题、"
                       "所有 SN 编号/数字/人名必须保留。")
    cur = digest
    for _attempt in range(max(1, int(rounds))):
        n = _nchars(cur)
        if n <= hi:
            return cur
        if n <= int(hi * 1.06):
            # 只超一点点，不必再花一次 LLM 调用，直接机械裁剪收到线内
            log("篇幅略超（%d 字 > %d），机械裁剪收尾" % (n, hi))
            return trim_to_limit(cur, hi, log_fn=log)
        try:
            fixed = call_llm(
                cfg,
                "【待压缩稿件，当前 %d 字】\n%s\n\n【硬性目标】压到 %d 字以内，至少删掉 %d 字。"
                "**只有上限、没有下限**：删到多短都可以，只要事实与来源标记还在，"
                "宁短不长。不要为凑字数保留任何废话。只删不增：%s"
                % (n, cur, hi, max(n - hi, int(n * 0.15)), cut),
                system_prompt=system_prompt or COMPRESS_PROMPT,
            )
            fixed = clean(fixed or "")
            n2 = len(re.sub(r"\s", "", fixed))
            if n2 >= n:
                log("压缩稿未变短（%d → %d 字），改用机械裁剪兜底" % (n, n2))
                break
            log("篇幅超限（%d 字 > %d），已压缩到 %d 字" % (n, int(hi * 1.06), n2))
            cur = fixed
        except Exception as e:
            log("压缩失败（保留原稿）：%s" % e)
            break
    # 模型让步了但没到线，机械裁剪收尾（保证「不超上限」是确定结果而不是期望）
    if _nchars(cur) > hi:
        cur = trim_to_limit(cur, hi, log_fn=log)
    return cur


def _nchars(s):
    """计字数：去掉所有空白后的字符数（与日志口径一致）。"""
    return len(re.sub(r"\s", "", s or ""))


# 机械裁剪时「先砍谁」：越靠前越先被砍（越次要）
_TRIM_ORDER = ("其他细节", "其他碎屑", "风险汇总", "跑题现场", "其他碎屑",
               "重要人物观点", "谁在聊什么", "今日氛围")
# 永不砍：「一句话总结」是全文结论；「必读重点」是主干
_TRIM_KEEP = ("一句话总结", "一句话吐槽", "必读重点", "本周核心趋势")


def trim_to_limit(digest, hi, log_fn=None):
    """代码层硬兜底：模型不肯删时，按「次要板块 → 条数 → 句子」机械裁剪到上限以内。

    存在原因：实测 compress_digest 让模型删两轮，它能把 1745 压到 1396 就停手
    （第二轮直接原样返回），但上限是 1200。用户要的是「不超上限」，不是「尽量压」，
    所以最后必须有一条不依赖模型的路径兜住。

    裁剪顺序（尽量保住信息骨架）：
      1) 整节省略最次要的板块（📎 其他细节 / ⚠️ 风险汇总 …），从列表最次要的第一个开始；
         某节删完仍超限，接着删下一节；
      2) 还没到限，就从「必读重点」和「人物观点」里从末尾往前整条删要点
         （末尾的通常最琐碎），但至少各留 1 条；
      3) 再不行就截断超长单条（按句末标点切，保留前几句）。
    所有小节标题、SN 编号、`---` 之后的状态行一律不动。
    """
    log_fn = log_fn or (lambda m: None)
    hi = int(hi)
    if not digest or _nchars(digest) <= hi:
        return digest

    lines = digest.split("\n")
    # 拆成 [("head", [...]) / ("sec", 标题, [...])] 结构，便于整节操作
    blocks, cur_sec = [], None
    for ln in lines:
        t = _section_title(ln)
        if t:
            cur_sec = {"title": t, "lines": [ln]}
            blocks.append(cur_sec)
        elif cur_sec is None:
            blocks.append({"title": None, "lines": [ln]})
        else:
            cur_sec["lines"].append(ln)

    def rebuild():
        out = []
        for b in blocks:
            out.extend(b["lines"])
        return "\n".join(out)

    def sec_of(name):
        for b in blocks:
            if b["title"] and name in b["title"]:
                return b
        return None

    # ---- 1) 渐进裁剪：先删条目，删光才删整节；且只删到刚好不超限 ----
    for name in _TRIM_ORDER:
        if _nchars(rebuild()) <= hi:
            break
        b = sec_of(name)
        if not b or any(k in b["title"] for k in _TRIM_KEEP):
            continue
        # 先逐条删（末尾的通常最琐碎），至少留 1 条
        while _nchars(rebuild()) > hi:
            idx = [i for i, ln in enumerate(b["lines"])
                   if ln.strip().startswith(("-", "•", "*"))]
            if len(idx) <= 1:
                break
            b["lines"].pop(idx[-1])
            log_fn("机械裁剪：删「%s」末条 → %d 字" % (b["title"], _nchars(rebuild())))
        # 只剩 1 条还超限，才整节省略
        if _nchars(rebuild()) > hi:
            blocks.remove(b)
            log_fn("机械裁剪：整节省略「%s」→ %d 字" % (b["title"], _nchars(rebuild())))

    # ---- 2) 仍超限，再动主干板块（必读重点），同样逐条删、至少留 1 条 ----
    for name in ("必读重点", "本周核心趋势"):
        if _nchars(rebuild()) <= hi:
            break
        b = sec_of(name)
        if not b:
            continue
        while _nchars(rebuild()) > hi:
            idx = [i for i, ln in enumerate(b["lines"])
                   if ln.strip().startswith(("-", "•", "*"))]
            if len(idx) <= 1:
                break
            b["lines"].pop(idx[-1])
            log_fn("机械裁剪：删「%s」末条 → %d 字" % (b["title"], _nchars(rebuild())))

    # ---- 3) 最后一招：截断超长单条（按句切，保住前几句） ----
    if _nchars(rebuild()) > hi:
        for b in blocks:
            if _nchars(rebuild()) <= hi:
                break
            if not b["title"] or any(k in b["title"] for k in _TRIM_KEEP):
                continue
            for i in range(len(b["lines"]) - 1, -1, -1):
                if _nchars(rebuild()) <= hi:
                    break
                ln = b["lines"][i]
                if _nchars(ln) < 100:
                    continue
                parts = re.split(r"(?<=[。！？；])", ln)
                while len(parts) > 1 and _nchars(rebuild()) > hi:
                    parts.pop()
                    b["lines"][i] = "".join(parts).rstrip()
                if not b["lines"][i].strip():
                    b["lines"].pop(i)
                log_fn("机械裁剪：截断长条 → %d 字" % _nchars(rebuild()))

    return rebuild()


def _section_title(line):
    """若该行是「**xxx**」形式的板块标题，返回标题文字，否则 None。"""
    s = (line or "").strip()
    if not s.startswith("**"):
        return None
    m = re.match(r"^\*\*([^*\n]{1,40})\*\*", s)
    if not m:
        return None
    title = m.group(1)
    for w in _SECTION_WORDS:
        if w in title:
            return title
    return None


_SN_IN_TEXT = re.compile(r"(?i)\bSN\s?(\d{1,3})\b")


def audit_attribution(digest, items, window=2):
    """核验日报里「人物 ↔ 子网编号」的关联是否有原文直接依据。

    模型很容易把相隔数小时的两条无关消息拼成「某人是某子网所有者」，
    实测光靠提示词改不动，所以这里做机器核验，把结果交给调用方提示用户。

    可信条件（满足其一）：
      1) 该人物自己的某条发言里出现过这个编号；
      2) 该人物发言前后 window 条之内，有人提到过这个编号（当场对号入座）。

    返回 [(人物, 编号, 句子片段)]。
    """
    if not digest or not items:
        return []
    authors, _seen = [], set()
    for _r, _t, _a, _c in items:
        if _a and _a not in _seen:
            _seen.add(_a)
            authors.append(_a)
    authors.sort(key=len, reverse=True)   # 长名优先，避免短名把长名切碎

    idx_of, sn_at = {}, []
    for i, (_r, _t, _a, _c) in enumerate(items):
        idx_of.setdefault(_a, []).append(i)
        nums = set()
        for m in re.finditer(r"(?i)\b(?:SN\s?)?(\d{1,3})\b", _c):
            nums.add(int(m.group(1)))
        sn_at.append(nums)

    bad = []
    for line in digest.split("\n"):
        s = line.strip()
        if len(s) < 12:
            continue
        sns = set(int(x) for x in _SN_IN_TEXT.findall(s))
        if not sns:
            continue
        for nm in [a for a in authors if a in s]:
            for num in sns:
                if _attribution_ok(nm, num, idx_of, sn_at, window, len(items)):
                    continue
                bad.append((nm, num, s[:60]))
    return bad


def _attribution_ok(name, num, idx_of, sn_at, window, total):
    for i in idx_of.get(name, []):
        if num in sn_at[i]:
            return True
        lo, hi = max(0, i - window), min(total, i + window + 1)
        for j in range(lo, hi):
            if j != i and num in sn_at[j]:
                return True
    return False


# 「…（后确认为 SN103）」这类断言式绑定：模型最爱在这里编造归属
_LINK_ASSERT = re.compile(
    r"[（(][^（）()\n]{0,12}?(?:即|就是|实为|后确认|最终确认|确认为|对应|指的是)"
    r"[^（）()\n]{0,12}?SN\s?(\d{1,3})[^（）()\n]{0,8}[)）]")
_INLINE_ASSERT = re.compile(r"[，,；;]\s*(?:即|也就是|实为)\s*\**SN\s?(\d{1,3})\**")


def scrub_unsupported_links(digest, items, window=2):
    """兜底：若「A 的子网就是 SNxx」这类断言在原文查无实据，就把它降级成「（原文未指明编号）」。
    只处理上面两种高风险句式，不动其他内容。返回 (新文本, 改动条数)。"""
    if not digest or not items:
        return digest, 0
    authors, seen = [], set()
    for _r, _t, _a, _c in items:
        if _a and _a not in seen:
            seen.add(_a)
            authors.append(_a)
    authors.sort(key=len, reverse=True)
    idx_of, sn_at = {}, []
    for i, (_r, _t, _a, _c) in enumerate(items):
        idx_of.setdefault(_a, []).append(i)
        sn_at.append({int(x) for x in re.findall(r"(?i)\b(\d{1,3})\b", _c)})

    fixed, n = [], 0
    for line in digest.split("\n"):
        def ok(num):
            names = [a for a in authors if a in line]
            if not names:
                return True
            return any(_attribution_ok(nm, num, idx_of, sn_at, window, len(items)) for nm in names)

        def repl_paren(m):
            nonlocal n
            if ok(int(m.group(1))):
                return m.group(0)
            n += 1
            return "（原文未指明编号）"

        def repl_inline(m):
            nonlocal n
            if ok(int(m.group(1))):
                return m.group(0)
            n += 1
            return ""

        new = _LINK_ASSERT.sub(repl_paren, line)
        new = _INLINE_ASSERT.sub(repl_inline, new)
        fixed.append(new)
    if n:
        log("归属兜底：%d 处「某人→SN」断言在原文查无实据，已降级为「原文未指明编号」" % n)
    return "\n".join(fixed), n


# 常见英文术语 -> 中文（人名/子网名/代币名不动，避免误伤）
_EN_TERMS = [
    (r"(?i)\bcrater(?:ing|ed)?\b", "暴跌"),
    (r"(?i)\bcrash(?:ing|ed)?\b", "暴跌"),
    (r"(?i)\bAPY\b", "年化收益率"),
    (r"(?i)\bAPR\b", "年化收益率"),
    (r"(?i)\bbacking\b", "资本支持"),
    (r"(?i)\bbuybacks?\b", "回购"),
    (r"(?i)\bbuy(?:ing)?\s+back\b", "回购"),
    (r"(?i)\bemissions?\b", "排放"),
    (r"(?i)\bburn(?:ing|ed)?\b", "销毁"),
    (r"(?i)\bstaking\b", "质押"),
    (r"(?i)\bliquidity\b", "流动性"),
    (r"(?i)\bminers?\b", "矿工"),
    (r"(?i)\bvalidators?\b", "验证人"),
    (r"(?i)\bowners?\b", "所有者"),
    (r"(?i)\bdereg\w*\b", "注销"),
    (r"(?i)\brevenue\b", "营收"),
    (r"(?i)\bwhitelist\b", "白名单"),
    (r"(?i)\bincentives?\b", "激励"),
    (r"(?i)\bsubnets?\b", "子网"),
    (r"(?i)\bpre[- ]?sale\b", "预售"),
    (r"(?i)\bairdrop\b", "空投"),
    (r"(?i)\brug(?:ged|pull)?\b", "跑路"),
    (r"(?i)\bdodgy\b", "可疑"),
    (r"(?i)\bdump(?:ing|ed)?\b", "抛售"),
    (r"(?i)\bpost[- ]?mortem\b", "事后复盘"),
    (r"(?i)\bextractor\b", "收割者"),
    (r"(?i)\bgrifter\b", "骗子"),
    (r"(?i)\bbacked by\b", "由…投资"),
]


def reduce_english(text):
    """把日报里残留的通用英文术语换成中文；人名/子网名/代币名不在替换表内，不会被误改。"""
    if not text:
        return text
    s = text
    for pat, zh in _EN_TERMS:
        s = re.sub(pat, zh, s)
    # 括号里只剩英文原词（多是「可疑（dodgy）」这类翻译残渣）→ 整段去掉；常见代币/技术缩写保留
    _KEEP = {"tao", "dtao", "alpha", "gpu", "api", "ai", "agi", "llm", "defi", "nft", "sn"}

    def _drop_gloss(m):
        w = m.group(1).strip().lower()
        return m.group(0) if w in _KEEP else ""

    s = re.sub(r"[（(]\s*([A-Za-z][A-Za-z .'\-/]{0,18})\s*[)）]", _drop_gloss, s)
    # 「**@某人**」的 @ 去掉（日报里不需要 at）
    s = re.sub(r"\*\*@([^*\n]{1,24})\*\*", r"**\1**", s)
    # 清掉替换后留下的空括号与重复括号（如「暴跌（暴跌）」→「暴跌」）
    s = re.sub(r"[（(][\s,，、/]*[)）]", "", s)
    s = re.sub(r"([\u4e00-\u9fa5]{2,5})[（(]\1[)）]", r"\1", s)
    # 英文原词被翻成中文后，括号里只剩同一个中文词，直接去掉整个括号
    s = re.sub(r"[（(]\s*(?:暴跌|回购|排放|质押|销毁|流动性|矿工|验证人|所有者|注销|营收|白名单|激励|子网|预售|空投|跑路|年化收益率|资本支持|事后复盘|可疑|抛售|收割者|骗子)\s*[)）]", "", s)
    s = re.sub(r"[，,]{2,}", "，", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s


def _bigrams(s):
    s = re.sub(r"[\s*\-·，。、；：:（）()\[\]【】]", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def dedupe_bullets(text, threshold=0.45):
    """跨小节去重：同一件事在「必读重点」和「其他细节」里各写一遍时，保留先出现的那条。
    只跟**其他小节**的要点比（同小节内的并列要点通常各说一件事，不动），
    用字符二元组 Jaccard 相似度判断，只删高度重复的行。"""
    kept, out, dropped = [], [], 0
    section = -1
    for line in text.split("\n"):
        s = line.strip()
        if _section_title(line) is not None:
            section += 1
        if s.startswith(("-", "•")) and len(s) > 24:
            bg = _bigrams(s)
            dup = False
            for kg, kl, sec in kept:
                if sec == section:
                    continue
                inter = len(bg & kg)
                if not inter:
                    continue
                jac = inter / max(1, len(bg | kg))
                cont = inter / max(1, min(len(bg), len(kg)))
                # 相似度高，或「短的那条几乎被长的那条覆盖」且两者长度接近，即视为重复
                if jac >= threshold or (cont >= 0.75 and len(bg) <= kl * 1.15):
                    dup = True
                    break
            if dup:
                dropped += 1
                continue
            kept.append((bg, len(bg), section))
        out.append(line)
    if dropped:
        log("去重：删除 %d 条与其他小节重复的要点" % dropped)
    return "\n".join(out)


_SEC_EMOJI = "🔥👥📎💡📰🗓📊📌⚠️✅"


def _unstick_sections(text):
    """把小节标题从上一行要点里"抠"出来（模型偶尔会写成一整行）。

    实测缺陷：`- **BROdin**答仍用**Talisman📎 其他细节**` ——
    模型漏了换行，标题被包进上一条的加粗里；紧接着 _tidy_layout 又把列表项
    之间的空行删掉，标题就彻底粘死在正文中间，手机上看是一坨乱码。

    只匹配「emoji + 板块名」这种**标题专有形态**（正文里不会这么写），
    命中就在前面断行，并把上一行被切断的加粗补齐成对。
    """
    if not text:
        return text
    words = "|".join(re.escape(w) for w in _SECTION_WORDS)
    # 标题本身可能带加粗：`**📎 其他细节**`，开头这两个星号要一起吃掉
    pat = re.compile(r"(\*{0,2}[%s])\s*(%s)" % (_SEC_EMOJI, words))
    out = []
    for ln in text.split("\n"):
        m = pat.search(ln)
        if not m or m.start() == 0 or not ln[:m.start()].strip():
            out.append(ln)
            continue
        prefix = ln[:m.start()]
        head = m.group(1)                     # 可能是 `📎` 或 `**📎`
        rest = ln[m.end():]
        if prefix.count("**") % 2 == 1:       # 上一行的加粗被这行截断了，补回来
            prefix += "**"
        if rest.startswith("**"):             # 标题的闭合星号被留在了后面
            rest = rest[2:]
        if not head.startswith("**"):         # 标题本来没加粗，这里统一补上
            head = "**" + head
        out.append(prefix.rstrip())
        out.append("")
        out.append("%s %s**%s" % (head, m.group(2), rest))
    return "\n".join(out)


def _tidy_layout(text):
    """美化：列表项之间不空行（模型常把每条要点之间都空一行，手机上会拉得很长）。"""
    text = _unstick_sections(text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    out = []
    for i, ln in enumerate(lines):
        if ln.strip() == "" and 0 < i < len(lines) - 1:
            prev = next((x for x in reversed(out) if x.strip()), "")
            nxt = None
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    nxt = lines[j]
                    break
            if prev.strip().startswith(("-", "•")) and nxt and nxt.strip().startswith(("-", "•")):
                continue
        out.append(ln)
    txt = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def strip_length_tags(text):
    """删掉模型把「字数配额」写进正文的痕迹。

    实测缺陷：提示词里「每条 45-60 字」这类配额，模型会直接抄进正文，
    变成「**kτwτ**（45-60字）：详解…」——读者看到的是提示词，不是内容，
    既占字数又像机器在凑字数。提示词里已经反复禁止，但模型不听话，
    所以必须有一道代码闸门无脑删掉。

    只处理明确的「字数标注」，不碰正文里的正常数字：
      （45-60字）（约 50 字）（50字左右）[45-60字]（60-70 字）
    """
    if not text:
        return text
    s = text
    # 括号内：可选「约/约莫/左右」+ 数字 + 可选连接符 + 数字 + 可选空格 + 字 + 可选「左右/以内/以下」
    num = r"\d{1,4}"
    pat_bracket = re.compile(
        r"[（(\[【]\s*(?:约|大约|约莫|控制在)?\s*%s(?:\s*[-~～—至到]\s*%s)?\s*字"
        r"(?:左右|以内|以下)?\s*[)）\]】]" % (num, num))
    n = len(pat_bracket.findall(s))
    s = pat_bracket.sub("", s)
    # 行内裸写：「每条 45-60 字」「控制在 50 字左右」→ 整段删掉（它们只出现在元描述里）
    pat_bare = re.compile(r"(?:每条|每条控制在|控制在|约|大约)\s*%s(?:\s*[-~～—至到]\s*%s)?\s*字"
                          r"(?:左右|以内|以下)?" % (num, num))
    n += len(pat_bare.findall(s))
    s = pat_bare.sub("", s)
    # 清掉删完留下的空括号/悬空标点与多余空格
    s = re.sub(r"[（(]\s*[)）]", "", s)
    s = re.sub(r"\*\*\s*\*\*", "", s)
    # 🔥n 是**输入端**的表情回应热度标记（"🔥3 = 被回应 3 次"），不是给读者的内容。
    # 模型偶尔会把它当引用写进正文：`…放话「arbos 迟早背叛主子 const」（🔥2）`。
    # 括号里的直接连括号删；裸写的只删标记本身。
    s = re.sub(r"[（(\[【]\s*🔥\s*\d{1,4}\s*[)）\]】]", "", s)
    s = re.sub(r"(?<![^\s（(])🔥\s*\d{1,4}(?!\d)", "", s)
    # 加粗标记内侧夹空格（`** 补记 **`）会把 markdown 打断，渲染成星号：一律贴齐
    s = re.sub(r"\*\*[ \t]+([^\*\n]{1,80}?)\*\*", r"**\1**", s)
    s = re.sub(r"\*\*([^\*\n]{1,80}?)[ \t]+\*\*", r"**\1**", s)
    # **人名**后面补空格（只对「行首的加粗人名」生效；句中强调词加空格反而更糟）：
    #   `- **SN8 Vanta**遭社区FUD` → `- **SN8 Vanta** 遭社区FUD`
    #   `修复后**补记了这些交易**造成飙升` → 不动
    s = re.sub(r"(?m)^([-•]\s+)\*\*([^\*\n]{1,40})\*\*(?=[\u4e00-\u9fa5])",
               r"\1**\2** ", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"[（(]\s*[：:]\s*", "（", s)
    s = re.sub(r"[，,]\s*[，,]", "，", s)
    s = re.sub(r"：\s*：", "：", s)
    s = re.sub(r"^([-•]\s*)\s*[：:]\s*", r"\1", s, flags=re.M)
    s = re.sub(r"[ \t]+([，。；：！？、])", r"\1", s)
    # 整行被删空的列表项（只剩 `- ` 或 `- **x**`）一并删掉，否则会留下空项目符号
    s = re.sub(r"(?m)^\s*[-•]\s*\**\s*\**\s*$", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    if n:
        log("字数标注清除：%d 处「（45-60字）」这类提示词痕迹已删" % n)
    return s


def sanitize_digest(text):
    """删除日报中不应出现的板块（潜力观察 / 重点子网动向），返回清洗后的文本。"""
    if not text:
        return text
    out, skipping, dropped = [], False, []
    for line in text.split("\n"):
        title = _section_title(line)
        if title is not None:
            if any(w in title for w in _FORBIDDEN_SECTIONS):
                skipping = True
                dropped.append(title)
                continue
            skipping = False
        if skipping:
            continue
        out.append(line)
    txt = re.sub(r"\n[ \t]+\n", "\n\n", "\n".join(out))
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    txt = strip_length_tags(txt)
    txt = reduce_english(txt)
    txt = dedupe_bullets(txt)
    txt = _tidy_layout(txt)
    if dropped:
        log("结构清洗：已剔除多余板块 %s" % "、".join(dropped))
    return txt


def sanitize_chat(text):
    """水聊侧记的清洗：只做结构与排版整理，**不做英文替换**。
    梗、双关、原话片段一旦被翻译就失去味道，且容易改错意思；
    人名的准确性交给校对轮把关。"""
    if not text:
        return text
    out, skipping = [], False
    for line in text.split("\n"):
        title = _section_title(line)
        if title is not None:
            skipping = any(w in title for w in _FORBIDDEN_SECTIONS)
            if skipping:
                continue
        if skipping:
            continue
        out.append(line)
    txt = re.sub(r"\n[ \t]+\n", "\n\n", "\n".join(out))
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    txt = strip_length_tags(txt)
    txt = dedupe_bullets(txt)
    return _tidy_layout(txt)

def call_llm(cfg, text_block, system_prompt=None, tries=3):
    """调用任意 OpenAI 兼容的 /chat/completions 接口。

    所有渠道差异都通过 config 控制，不写死在代码里：
      deepseek_base_url / deepseek_model / deepseek_api_key
      deepseek_timeout         超时秒数（推理模型建议 180-300）
      deepseek_max_tokens      输出上限（推理模型要留足思考空间）
      deepseek_extra_params    额外请求体，原样合并（如 {"enable_thinking": false}）
    """
    api_key = cfg["deepseek_api_key"]
    base = cfg.get("deepseek_base_url", "https://api.deepseek.com/v1").rstrip("/")
    model = cfg.get("deepseek_model", "deepseek-chat")
    timeout = int(cfg.get("deepseek_timeout", 120))
    max_tokens = int(cfg.get("deepseek_max_tokens", 2500))
    url = base + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
            {"role": "user", "content": text_block},
        ],
        "temperature": float(cfg.get("deepseek_temperature", 0)),
        "max_tokens": max_tokens,
    }
    extra = cfg.get("deepseek_extra_params")
    if isinstance(extra, dict):
        payload.update(extra)
    body = json.dumps(payload).encode("utf-8")
    last = None
    op = _opener_for(url)   # 国内 LLM 直连、其余走代理，避免被 Clash 节点 502
    for i in range(tries):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", "Bearer %s" % api_key)
        req.add_header("Content-Type", "application/json")
        try:
            with op.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = (msg.get("content") or "").strip()
            usage = data.get("usage") or {}
            _u = usage.get("completion_tokens_details") or {}
            log("  %s 用量：输入 %s / 输出 %s（其中思考 %s）"
                % (model, usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"),
                   _u.get("reasoning_tokens", 0)))
            if not content:
                # 推理模型常见坑：思考把 max_tokens 吃光，正文被截成空串。
                reasoning = msg.get("reasoning_content") or ""
                raise RuntimeError(
                    "%s 返回空正文（finish_reason=%s，思考 %d 字符，输出 %s tokens）；"
                    "多半是 deepseek_max_tokens 不够，或模型在思考阶段被截断"
                    % (model, choice.get("finish_reason"), len(reasoning),
                       usage.get("completion_tokens", "?")))
            return content
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last = "%s HTTP 错误 %s: %s" % (model, e.code, detail)
            # 4xx（key 错误/参数错误）重试无意义，直接抛出
            if 400 <= e.code < 500 and e.code != 429:
                raise RuntimeError(last)
        except Exception as e:
            last = "%s 调用失败：%s" % (model, e)
        if i < tries - 1:
            log("  ↳ %s 第 %d/%d 次尝试失败（%s），%ds 后重试"
                % (model, i + 1, tries, last, 10 * (i + 1)))
            time.sleep(10 * (i + 1))
    raise RuntimeError(last)


# 向后兼容：老代码（监听、脚本）仍调用 call_deepseek
def call_deepseek(cfg, text_block, system_prompt=None, tries=3):
    return call_llm(cfg, text_block, system_prompt=system_prompt, tries=tries)

def build_text_block(channel_name, day, items, extra_hint_items=None):
    # 发言者名单：给模型一份白名单，防止人名被音译、缩写或凭空编造
    # （只含**总结日**当天的发言者；背景资料里的人不能作为当日发言者出现）
    authors, _seen = [], set()
    for _r, _t, _a, _c in items:
        if _a and _a not in _seen:
            _seen.add(_a)
            authors.append(_a)
    has_next_day = any(str(_t).startswith("次日") for _r, _t, _a, _c in items)
    lines = ["频道：%s    日期：%s    当日消息 %d 条（**按时间顺序**，含少量上下文消息；"
             "🔥n 表示该条被表情回应 n 次，热度参考）"
             % (channel_name, day + ("（含次日今晨补录段）" if has_next_day else ""), len(items)),
             "当日发言者名单（共 %d 人；引用人名时必须原样照抄，不得音译/缩写/改写大小写，"
             "名单外的名字不得作为发言者出现）：%s"
             % (len(authors), "、".join(authors))]
    _hint = subnet_hint_block(items + list(extra_hint_items or []))
    if _hint:
        lines.append(_hint)
    prev_t = None
    for i, (react, t, author, c) in enumerate(items, 1):
        c1 = c.replace("\n", " ").strip()
        if len(c1) > 600:
            c1 = c1[:600] + "…"
        hot = " 🔥%d" % react if react >= 2 else ""
        # 日期跳转标记：昨日报含今晨补录段，防止把跨零点的两件事拼成因果
        if i > 1 and str(t).startswith("次日") and not str(prev_t).startswith("次日"):
            lines.append("    ~~~~ 日期跳转：以下消息发生在**总结日的下一天（今晨至今）**，"
                         "必须标「今晨」，不得当成总结日当天的事件，不得与上文拼接因果 ~~~~")
        # 时间断层标记：防止模型把相隔数小时的两条无关消息拼成因果关系
        if i > 1:
            _gap = _minutes_between(prev_t, t)
            if _gap is not None and _gap >= 60:
                lines.append("    ~~~~ 时间跳转：上一条与本条相隔约 %d 小时 %d 分，"
                             "两者无上下文关系，不得拼接 ~~~~"
                             % (_gap // 60, _gap % 60))
        lines.append("%d. [%s] <%s>%s %s" % (i, t, author, hot, c1))
        prev_t = t
    return "\n".join(lines)


_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _weekday_cn(day_str):
    try:
        return _WEEKDAY_CN[datetime.datetime.strptime(day_str, "%Y-%m-%d").date().weekday()]
    except Exception:
        return ""


def build_weekly_text_block(channel_name, days):
    """周报输入：按日期分段，每条消息前缀带「月-日 时:分」。
    为什么必须带日期：七天记录混在一起、只留 HH:MM 时，模型分不清「周三说过」
    和「今天说过」，最典型的事故就是把不同日期的事拼成同一天、或拼出因果。
    days = [(day_str, items4), ...]，已按时间正序。"""
    authors, _seen, flat = [], set(), []
    for _ds, items in days:
        flat += items
        for _r, _t, _a, _c in items:
            if _a and _a not in _seen:
                _seen.add(_a)
                authors.append(_a)
    lines = [
        "频道：%s    日期范围：%s ~ %s（共 %d 天、%d 条实质消息；按日期分段，段内按时间顺序；"
        "🔥n 表示该条被表情回应 n 次，热度参考）"
        % (channel_name, days[0][0], days[-1][0], len(days), len(flat)),
        "每条消息前面的方括号是它的**发生日期与北京时间**。任何事实都必须以它所在分段的日期为准："
        "不得把不同日期的事写成同一天，不得因为某一层出现过就把某事件说成「本周持续发酵」。",
        "发言者名单（共 %d 人；引用人名必须原样照抄，不得音译/缩写/改写大小写，"
        "名单外的名字不得作为发言者出现）：%s" % (len(authors), "、".join(authors)),
    ]
    _hint = subnet_hint_block(flat)
    if _hint:
        lines.append(_hint)
    for ds, items in days:
        md = ds[5:]
        lines.append("")
        lines.append("===== %s（%s）%d 条 =====" % (ds, _weekday_cn(ds), len(items)))
        prev_t = None
        for i, (react, t, author, c) in enumerate(items, 1):
            c1 = (c or "").replace("\n", " ").strip()
            if len(c1) > 600:
                c1 = c1[:600] + "…"
            hot = " 🔥%d" % react if react >= 2 else ""
            if i > 1:
                _gap = _minutes_between(prev_t, t)
                if _gap is not None and _gap >= 60:
                    lines.append("    ~~~~ 时间跳转：上一条与本条相隔约 %d 小时 %d 分，"
                                 "两者无上下文关系，不得拼接 ~~~~" % (_gap // 60, _gap % 60))
            lines.append("%d. [%s %s] <%s>%s %s" % (i, md, t, author, hot, c1))
            prev_t = t
    return "\n".join(lines)


def _cap_day_items(items, cap):
    """单日条数超预算时按热度留前 cap 条，然后**按时间还原顺序**。
    不能按热度排序交给模型：时间线被打散后，指代和因果全部失锚。"""
    if cap <= 0 or len(items) <= cap:
        return items
    keep = sorted(items, key=lambda x: -x[0])[:cap]
    keep.sort(key=lambda x: x[1])
    return keep


_SUBNET_KB = None


def _subnet_kb():
    """加载本地子网知识库（subnets.json，由 update_subnets.py 每周更新）。"""
    global _SUBNET_KB
    if _SUBNET_KB is None:
        path = os.path.join(HERE, "subnets.json")
        try:
            with open(path, encoding="utf-8") as f:
                _SUBNET_KB = json.load(f)
        except Exception:
            _SUBNET_KB = {}
    return _SUBNET_KB


def subnet_hint_block(items, limit=45):
    """把当天消息里提到的子网编号，映射成「SNxx 名称：业务」的对照表。
    只喂被提到的编号（不整表塞进去），并明确禁止把业务描述当日新闻写。"""
    kb = _subnet_kb() or {}
    subs = kb.get("subnets") or {}
    dead = kb.get("deregistered") or {}
    if not subs and not dead:
        return ""
    text = " ".join(c for _r, _t, _a, c in items)
    weight = {}
    for m in re.finditer(r"(?i)\bSN\s?(\d{1,3})\b", text):
        n = int(m.group(1)); weight[n] = weight.get(n, 0) + 100
    # 裸数字：只收 1-256（排除价格 61.5、版本 2.0、带单位的 2t 等）
    for m in re.finditer(r"(?<![\d.$])(\d{1,3})(?![\d.%tTkKMm])", text):
        n = int(m.group(1))
        if 1 <= n <= 256:
            weight[n] = weight.get(n, 0) + 1
    # 聊天里只写名字、不写编号的（「Engy」「Lium」），按名字反查编号
    # 注意用**区分大小写**的整词匹配：否则 actual / claims 这类常见英文词会把无关子网带进来
    _NAME_STOP = {"unknown", "deprecated", "none", "null", "root", "actual", "claims", "bittensor"}
    for key, v in subs.items():
        nm = (v.get("name") or "").strip()
        base_nm = re.sub(r"\.(io|ai|studio|com|net|org)$", "", nm).strip()
        if len(base_nm) < 4 or base_nm.lower() in _NAME_STOP:
            continue
        for cand in {base_nm, base_nm.capitalize(), base_nm.upper()}:
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(cand) + r"(?![A-Za-z0-9])", text):
                weight[int(key)] = weight.get(int(key), 0) + 50
                break
    # 显式 SNxx / 名字命中的排前面，裸数字按出现次数补位，避免把无关数字塞进来
    nums = sorted(weight, key=lambda n: -weight[n])[:limit]
    nums.sort()

    lines = []
    for n in nums:
        key = str(n)
        if key in subs:
            v = subs[key]
            biz = v.get("zh") or v.get("desc") or "未知"
            alias = "（旧名 %s）" % "、".join(v["aliases"]) if v.get("aliases") else ""
            lines.append("- SN%d %s%s：%s" % (n, v.get("name") or "未知", alias, biz))
        elif key in dead:
            v = dead[key]
            lines.append("- SN%d 已注销（原 %s）" % (n, v.get("name") or "未知"))
    if not lines:
        return ""
    head = ["【子网对照表（本地知识库，链上身份，校对于 %s）】" % (kb.get("updated_at") or "未知"),
            "它的用途只有两个：把聊天里的「编号 ↔ 名字」对上、知道它大致做什么业务。"
            "**严禁把下面的业务描述当成当日新闻写进日报**；标「未知」的不许补全；"
            "若聊天原文与这里不一致（同一编号被重新注册过），以聊天原文为准，并注明「该编号可能已换项目」。"]
    return "\n".join(head + lines[:limit]) + "\n"


def _minutes_between(t1, t2):
    """把 'HH:MM' 两个时间点换算成分钟差（跨零点返回 None）。"""
    try:
        h1, m1 = [int(x) for x in str(t1).split(":")[:2]]
        h2, m2 = [int(x) for x in str(t2).split(":")[:2]]
        d = (h2 * 60 + m2) - (h1 * 60 + m1)
        return d if d >= 0 else None
    except Exception:
        return None

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
_SECTION_EMOJI = ("🔥", "🔢", "👥", "📊", "📎", "💡", "📌", "⚠️", "✅", "❌", "📝", "📈", "🧭",
                  "💰", "📰", "🗓", "😄", "🗣", "🎲", "🥴", "💬")


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


# 模型显示名：底部署名要跟着 config 里的 deepseek_model 走，不能写死
_MODEL_LABELS = {
    "kimi-k3": "Kimi K3",
    "kimi-k2": "Kimi K2",
    "kimi-k2-turbo-preview": "Kimi K2",
    "deepseek-chat": "DeepSeek V3",
    "deepseek-reasoner": "DeepSeek R1",
    "deepseek-v4.1-flash": "DeepSeek V4.1",
    "deepseek-v4.1-flash-1017": "DeepSeek V4.1",
    "deepseek-v4-flash-0731": "DeepSeek V4",
    "gpt-4o": "GPT-4o",
    "gpt-4o-mini": "GPT-4o mini",
    "claude-sonnet-4-5": "Claude Sonnet 4.5",
    "glm-4.6": "GLM-4.6",
    "qwen-max": "Qwen Max",
}


def model_label(cfg):
    """返回用于底部署名的模型名（读 config.deepseek_model，未知型号原样显示）。"""
    raw = str((cfg or {}).get("deepseek_model") or "deepseek-chat").strip()
    return _MODEL_LABELS.get(raw.lower(), raw)


def compare_profiles(cfg):
    """对比模式：config.json 里 `compare_profiles` 配了 ≥2 个模型档案时，
    同一天的日报会**分别用每个模型生成、分别推送**，方便一段时间内对比效果。

    每个档案支持的字段（都可选，缺省继承主配置）：
        label        展示名，如 "DeepSeek V4.1"（日报标题与底部署名用）
        base_url     渠道地址（OpenAI 兼容 /v1）
        model        模型名
        api_key      该渠道的 key（留空则复用 deepseek_api_key）
        extra_params 额外请求体（如 {"thinking":{"type":"disabled"}}）
        temperature / max_tokens / timeout

    返回 [(label, 覆盖后的 cfg 副本), ...]；未配置或不足 2 个时返回 []（走原来的单模型流程）。
    """
    profs = [p for p in (cfg.get("compare_profiles") or []) if isinstance(p, dict)]
    out = []
    for i, p in enumerate(profs):
        label = str(p.get("label") or p.get("model") or ("档案%d" % (i + 1))).strip()
        c = dict(cfg)
        if p.get("base_url"):
            c["deepseek_base_url"] = p["base_url"]
        if p.get("model"):
            c["deepseek_model"] = p["model"]
        if p.get("api_key"):
            c["deepseek_api_key"] = p["api_key"]
        if "extra_params" in p:
            c["deepseek_extra_params"] = p["extra_params"]
        for src, dst in (("temperature", "deepseek_temperature"),
                         ("max_tokens", "deepseek_max_tokens"),
                         ("timeout", "deepseek_timeout")):
            if src in p:
                c[dst] = p[src]
        out.append((label, c))
    return out if len(out) >= 2 else []


def generate_digest(cfg_i, text_block, system_prompt, items, tag=""):
    """一次完整成稿：生成 -> 结构清洗 -> 校对轮 -> 归属兜底清洗。对比模式下每个模型各跑一遍。"""
    digest = call_deepseek(cfg_i, text_block, system_prompt=system_prompt)
    digest = sanitize_digest(digest)
    digest = polish_digest(cfg_i, text_block, digest)
    digest = compress_digest(cfg_i, digest, 0, 1200)
    digest, _ = scrub_unsupported_links(digest, items)
    n = len(re.sub(r"\s", "", digest or ""))
    if n > 1450:
        log("%s⚠️ 篇幅偏长：%d 字（上限 1200）" % (tag, n))
    else:
        log("%s篇幅：%d 字（上限 1200，不设下限）" % (tag, n))
    return digest


def push_feishu(cfg, title, digest):
    url = cfg.get("feishu_webhook_url")
    if not url:
        log("未配置 feishu_webhook_url，跳过飞书推送")
        return
    secret = cfg.get("feishu_secret", "")
    # 手机端：wide_screen_mode 铺满屏宽；板块标题加粗，重点更突出
    mr = re.search(r"(\d{4}-\d{2}-\d{2})\s*[~～]\s*(\d{4}-\d{2}-\d{2})", title)
    if mr:  # 周报：日期行显示整段范围
        day = "%s~%s" % (mr.group(1), mr.group(2))
    else:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", title)
        day = m.group(1) if m else ""
    kind = ("周报" if "周报" in title else ("专题" if "专题" in title
            else ("水聊" if "水聊" in title else "日报")))
    body = prettify_headers(mark_changes_feishu(digest))
    # 正文顶部放「加粗标题 + 日期行」，与正文合并进同一个文本块（不加分隔线），
    # 这样在飞书手机端长按复制时，标题+日期+正文会被一并选中，日报才有完整意义
    title_line = "**%s**" % title
    label = model_label(cfg)
    if kind == "日报":
        # 日报顶部显示真实覆盖范围（到分钟，省空间），如「11-05 00:00 → 11-06 07:10」
        rng = _day_range_text(cfg, title)
        date_line = "📅 %s · 🤖 %s 生成" % (rng or day, label)
    else:
        # 周报/专题：标题本身已是范围或多日汇总，维持日期显示
        date_line = ("📅 %s · 🤖 %s 生成" % (day, label)) if day else ("🤖 %s 生成" % label)
    # 标题、日期、正文合为一个 lark_md 块（以空行分隔，不用 hr），保证整段可复制
    combined = "%s\n%s\n\n%s" % (title_line, date_line, body)
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": combined}},
        {"tag": "hr"},
        {"tag": "note", "content": "DTAO price-talk %s · 由 %s 生成" % (kind, label)},
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

# 红色标注：飞书 lark_md 支持 <font color='red'>，TG 旧版 Markdown 完全不支持颜色。
# 所以同一条稿子要分两条通道处理：飞书原样保留（真红字），TG 降级成「▲/▼ + 粗体」，
# 保证两边都能一眼看出「哪里变了」，而不是 TG 侧露出一坨 HTML 标签。
_RED_RE = re.compile(r"<font\s+color=[\"']?red[\"']?\s*>(.*?)</font>", re.I | re.S)


def mark_changes_feishu(text):
    """飞书通道：把变化值包成 <font color='red'>，已是红色的不重复包。"""
    if not text:
        return text
    # 模型有时用自己的写法，统一规范成 lark_md 认可的引号形式
    return re.sub(r"<font\s+color=[\"']?(?:red|#?f{0,1}f0000)[\"']?\s*>",
                  "<font color='red'>", text, flags=re.I)


def mark_changes_tg(text):
    """TG 通道：红色标签降级成「▲新值」粗体，并去掉 HTML 残渣。

    TG 旧版 Markdown 里没有颜色，硬塞 <font> 会原样显示成文字；
    用 ▲（上升）/ ▼（下降）替代颜色语义，配合粗体足够醒目。
    """
    if not text:
        return text

    def _sub(m):
        inner = m.group(1)
        arrow = "▲" if "+" in inner else ("▼" if "-" in inner or "−" in inner else "●")
        return "**%s %s**" % (arrow, inner)

    out = _RED_RE.sub(_sub, text)
    # 清掉任何漏网的 font 标签，避免 TG 侧露出 HTML
    out = re.sub(r"</?font[^>]*>", "", out, flags=re.I)
    return out


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
    body = _tg_md(prettify_headers(mark_changes_tg(digest)))
    sep = "────────────"
    t = _tg_md(title)
    kind = ("周报" if "周报" in title else ("专题" if "专题" in title
            else ("水聊" if "水聊" in title else "日报")))
    hdr = "*%s*" % t
    if kind == "日报":
        rng = _day_range_text(cfg, title)
        if rng:
            hdr += "\n📅 " + rng
    full = "%s\n%s\n\n%s\n\n%s\n🤖 由 %s 生成 · DTAO price-talk %s" % (
        hdr, sep, body, sep, model_label(cfg), kind)
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
    # 记下本次真实导出用的令牌编号（1 起始），供下次随机时排除（防连续两天用同一个号）
    if _TOKEN_INFO.get("index"):
        state["_meta"] = {
            "last_token_idx": _TOKEN_INFO["index"],
            "last_export_ts": day["exports"][-1]["ts"],
        }
    _save_digest_state(cfg, state)

def _day_exports(cfg, day_str):
    return _load_digest_state(cfg).get(day_str, {}).get("exports", [])


def _fmt_range(after_dt, before_dt):
    """把导出边界压成省空间的范围文本：同日「11-05 00:00 → 23:59」，跨日「11-05 00:00 → 11-06 07:10」。"""
    if before_dt.time() == datetime.time() and before_dt.date() != after_dt.date():
        before_dt = before_dt - datetime.timedelta(minutes=1)  # 完整自然日显示到 23:59
    return "%s → %s" % (after_dt.strftime("%m-%d %H:%M"), before_dt.strftime("%m-%d %H:%M"))


def _record_range(cfg, day_str, after_dt, before_dt):
    """记录某日日报的实际覆盖时间范围（v1.5.3），供推送头部与 /repush 显示。"""
    try:
        state = _load_digest_state(cfg)
        state.setdefault(day_str, {})["range"] = _fmt_range(after_dt, before_dt)
        _save_digest_state(cfg, state)
    except Exception as e:
        log("时间范围记录失败（忽略）：%s" % e)


def _day_range_text(cfg, title):
    """按标题里的日期查该日日报的实际时间范围；旧记录用该日最后一次导出完成时间兜底；查不到返回 None。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", title)
    if not m:
        return None
    day_str = m.group(1)
    state = _load_digest_state(cfg).get(day_str) or {}
    if state.get("range"):
        return state["range"]
    exps = state.get("exports") or []
    if exps:
        try:
            ts = datetime.datetime.fromisoformat(exps[-1]["ts"]).strftime("%m-%d %H:%M")
            start = datetime.datetime.strptime(day_str, "%Y-%m-%d").strftime("%m-%d")
            return "%s 00:00 → %s" % (start, ts)
        except Exception:
            pass
    return None

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

def _day_str_of(path):
    """从导出文件名里解析总结日（dtao_<日期>.N.json / 旧命名 dtao_<日期>.json）。"""
    m = re.search(r"dtao_(\d{4}-\d{2}-\d{2})", os.path.basename(path or ""))
    return m.group(1) if m else None


def _dedupe_items(items):
    """按（作者, 内容）去重，保持原顺序。"""
    seen, out = set(), []
    for it in items:
        k = (it[2], it[3])
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _split_next_day(days):
    """把各天分段里带「次日」前缀的消息挪回真实日期的分段（同段去重、段内按时间排序）。
    days: [(ds, items), ...] -> 返回按日期排序后的 [(ds, items), ...]。"""
    buckets = {}
    for ds, items in days:
        for it in items:
            if str(it[1]).startswith("次日"):
                nd = (datetime.datetime.strptime(ds, "%Y-%m-%d").date()
                      + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                buckets.setdefault(nd, []).append((it[0], it[1][3:].strip(), it[2], it[3]))
            else:
                buckets.setdefault(ds, []).append(it)
    out = []
    for ds in sorted(buckets):
        items = _dedupe_items(buckets[ds])
        items.sort(key=lambda x: x[1])
        out.append((ds, items))
    return out


def merge_substantive(json_paths):
    """合并多个导出 json 的实质消息，按 (作者, 内容) 去重，**按时间升序**。
    （不再按热度排序：打乱时间线会让模型失去指代上下文）"""
    all_items, seen = [], set()
    for jp in json_paths:
        if not os.path.exists(jp):
            continue
        for it in extract_substantive(jp, day_str=_day_str_of(jp)):
            key = (it[2], it[3])
            if key in seen:
                continue
            seen.add(key)
            all_items.append(it)
    all_items.sort(key=lambda x: x[1])
    return all_items


def _day_json_files(cfg, day_str):
    """列出某天已有的全部导出 json（dtao_<日>.1.json / .2.json …，兼容旧命名 dtao_<日>.json）。"""
    out_dir = os.path.join(HERE, cfg.get("output_dir", "exports"))
    files = []
    for seq in (1, 2, 3, 4, 5):
        p = os.path.join(out_dir, "dtao_%s.%d.json" % (day_str, seq))
        if os.path.exists(p):
            files.append(p)
    legacy = os.path.join(out_dir, "dtao_%s.json" % day_str)
    if os.path.exists(legacy):
        files.append(legacy)
    return files


def build_context_block(cfg, focus_day_str, max_per_day=160):
    """构建「背景资料」文本块：总结日之前的 N 天（默认 2 天，config.context_days）的导出记录。
    作用：跨天话题（今天聊的「那个子网」其实前天就开始讨论）需要上下文才能接上。
    背景日的内容**不是总结对象**，块内已写明；焦点日当天消息由 build_text_block 单独给出。
    返回 (背景文本块, 背景日 items 列表)。"""
    n_days = int(cfg.get("context_days", 2) or 0)
    if n_days <= 0:
        return "", []
    try:
        d0 = datetime.datetime.strptime(focus_day_str, "%Y-%m-%d").date()
    except Exception:
        return "", []
    labels = {1: "前天", 2: "大前天", 3: "3天前", 4: "4天前"}
    blocks, ctx_items, counts = [], [], []
    for k in range(n_days, 0, -1):          # 从更早的日子开始，按时间顺序排列
        d = d0 - datetime.timedelta(days=k)
        ds = d.strftime("%Y-%m-%d")
        files = _day_json_files(cfg, ds)
        items = merge_substantive(files) if files else []
        label = labels.get(k, "%d天前" % k)
        if not items:
            blocks.append("【背景资料：%s（%s）】该日无导出记录，忽略。" % (label, ds))
            continue
        if len(items) > max_per_day:
            items = items[-max_per_day:]     # 保时间线末端（离总结日最近的部分）
        counts.append("%s %d条" % (ds, len(items)))
        ctx_items.extend(items)
        lines = []
        for _r, t, a, c in items:
            c1 = c.replace("\n", " ").strip()
            if len(c1) > 300:
                c1 = c1[:300] + "…"
            lines.append("- [%s] <%s> %s" % (t, a, c1))
        blocks.append("【背景资料：%s（%s）】共 %d 条——**仅供理解话题来龙去脉，不是总结对象**：\n%s"
                      % (label, ds, len(items), "\n".join(lines)))
    log("背景资料：附加总结日前 %d 天记录（%s），共 %d 条"
        % (n_days, "、".join(counts) if counts else "全部无记录", len(ctx_items)))
    return "\n\n".join(b for b in blocks if b), ctx_items


def _clip_flat(s, n):
    """压成单行并截断（档案里的原文带换行，直接拼进 bullet 会把版式搞乱）。"""
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[:n] + "…"


def _research_watch_items(body, max_items=3):
    """从投研报告里抽「📌 预期跟踪」的条目 —— 这些天生就是「等待被验证的判断」。

    日报需要的正是这一类：某个判断当初写下来，今天出现的事实把它证实或推翻了。
    收入/回购数据反而是「当时的行情」，拿来补日报容易变成旧数据当新闻，不抽。

    输出只留「盯什么」：原始条目形如
    `盯 owner 锁仓状态 → 目前 owner locked in、unwinding=0；若出现…即为解仓信号`，
    整条可达 90+ 字。箭头左半是「当时在盯什么」（判断主体），右半是**当时的状态值**——
    那是旧数据，既占篇幅又容易被误当成今天的情况，按用户的原则直接丢掉。
    模型判断「有没有被验证」，靠的是「当时在盯什么」+ 今天的事实，不需要旧状态。
    """
    out = []
    m = re.search(r"\*\*📌[^\*]*\*\*[^\n]*\n((?:[-•].+\n?)+)", body or "")
    if not m:
        return out
    for ln in m.group(1).strip().splitlines():
        ln = re.sub(r"^[-•]\s*", "", ln)
        ln = re.sub(r"</?font[^>]*>", "", ln, flags=re.I)
        ln = ln.replace("**", "").strip()
        if not ln:
            continue
        # 「本期无历史跟踪事项」这类占位行没信息量
        if re.search(r"无.{0,6}跟踪事项|首次投研|暂无", ln):
            continue
        left = ln.split("→", 1)[0] if "→" in ln else ln
        left = _clip_flat(left, 46)
        if left:
            out.append(left)
        if len(out) >= max_items:
            break
    return out


def build_subnet_context_block(cfg, items, day_str=None, max_sn=10, max_chars=1200):
    """给日报补一层「档案底子」，只补有意义的那点，绝不倒档案。

    定位（用户明确要求）：档案是**判断辅助**，不是内容来源。日报主体永远是当天谈的话题；
    档案只在两种情况下才有意义 ——
      · **项目有进展**：今天的事是之前某件事的推进（上次说在谈，今天官宣了）；
      · **早期判断被验证**：此前某人做过判断，今天的事实证实或推翻了它。
    所以这里只注入两样东西：
      1. **身份行（恒给）**：`SN64 Chutes（业务）｜今日提到 N 次`。帮模型认人，
         避免写出「某子网」。一行一个，成本极低。
      2. **待验证判断（有投研才给）**：投研报告的「📌 预期跟踪」条目。
         这些天然就是「写下来的、等事实检验的判断」，正是「早期观点被验证」的候选。

    **不给历史原话**——这是实测后的取舍：早先试过用中文 2-gram 做关键词重叠来挑
    「相关旧记录」，结果全是噪声。根因是子网名本身构成重叠：「28-gm 41-almanac
    51-lium 53-engy」这种罗列名单的泛论，因为同时含有多个子网名而拿到最高分，
    可它既不是进展也不是验证。剔掉名称词后重叠又几乎归零（1200+ 当日实词里，
    真正相关的最多命中 2-4 个）。这种匹配不可靠，宁可不给。
    另外 context_days（默认 2 天）已经把最近的聊天记录都给模型了，
    更早的原话价值远低于它带来的噪声。
    """
    if not items:
        return ""
    try:
        import subnet_archive as SA
    except Exception:
        return ""
    try:
        arc_dir = SA.get_arc_dir(cfg)
        kb = SA.load_kb()
        nidx = SA.name_index(kb)
    except Exception as e:
        log("子网档案：初始化失败（%s），本次不注入" % e)
        return ""

    # 1) 逐条消息认子网，累计「今天被提到几次」——只收强命中，
    #    弱命中（裸数字/通用词）误报太多，宁可不注入也不要张冠李戴。
    cnt = {}
    for _r, _t, _a, c in items:
        try:
            hits = SA.classify(c or "", nidx) or {}
        except Exception:
            continue
        for sn, (kind, _via) in hits.items():
            if kind == "strong":
                cnt[sn] = cnt.get(sn, 0) + 1
    if not cnt:
        return ""

    # 2) 聊得多的排前面；同次数则编号小的在前，保证输出稳定（可复现）
    ranked = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:max_sn]

    lines, used, n_hit, n_watch = [], 0, 0, 0
    for sn, times in ranked:
        try:
            arc = SA.load_archive(arc_dir, sn)
        except Exception:
            arc = None
        if not arc:
            continue                     # 没建档的子网不编背景，留给正文自己说
        name = (arc.get("name") or "").strip() or "（知识库未收录）"
        biz = _clip_flat(arc.get("biz") or "", 30)
        head = "- **SN%d %s**" % (sn, name)
        if biz:
            head += "（%s）" % biz
        head += "｜今日提到 %d 次" % times

        # 待验证判断：只看最近一份投研，最多 3 条。
        # 全局再加个闸：最多 3 个子网挂判断——否则聊到的子网一多，
        # 这里会变成「投研报告汇总」，直接违背「主体是当日话题」的定位。
        watch = []
        reps = arc.get("reports") or []
        if reps and n_watch < 3:
            last = reps[-1]
            if not (day_str and (last.get("date") or "") >= day_str):
                for it in _research_watch_items(last.get("body") or "", max_items=3):
                    watch.append("  · 此前判断（%s）：「%s」" % (last.get("date") or "?", it))

        block = head + ("\n" + "\n".join(watch) if watch else "")
        if used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block)
        n_hit += 1
        if watch:
            n_watch += 1

    if not lines:
        return ""
    log("子网档案：当日提到 %d 个子网，注入 %d 个身份（其中 %d 个带此前判断），共 %d 字"
        % (len(cnt), n_hit, n_watch, used))
    return ("【本次聊到的子网 —— 名称对照（+ 此前判断）】\n"
            "这是**判断用的底子，不是写作素材**。只做两件事：\n"
            "① **认人**：当天消息只写了编号的，用这里的名称/业务写清楚，别写「某子网」；\n"
            "② **判关联**：只有当今天的话题与之构成「**项目有进展**」（旧事被推进/落地）"
            "或「**早期判断被验证**」（下面的「此前判断」被今天的事实证实或推翻）时，"
            "才用**一句话**带过，如「（10-30 曾说会跌破 438，今日已跌破）」。\n"
            "**没有构成这两种关系就一个字都别写**——不要复述旧记录、不要把旧事写成本日新闻、"
            "不要把旧数据当今日行情。日报主体永远是当天谈到的话题。\n"
            + "\n".join(lines))


def _build_full_text_block(cfg, channel_name, day_str, items):
    """组装完整的输入文本块：当日记录 + 前几天聊天记录 + 聊到的子网档案。

    集中成一处，是因为 resummarize 与日常总结两条路径都要用同一套背景，
    分开写容易只改一处、另一处悄悄少一层上下文。
    """
    ctx_block, ctx_items = build_context_block(cfg, day_str)
    text_block = build_text_block(channel_name, day_str, items, extra_hint_items=ctx_items)
    if ctx_block:
        text_block += "\n\n" + ctx_block
    sn_block = build_subnet_context_block(cfg, items, day_str=day_str)
    if sn_block:
        text_block += "\n\n" + sn_block
    return text_block


def _audit_names(digest, items, tag=""):
    """人名保真核对：把日报「人物观点」里出现的名字与当日真实发言者名单比对。
    不在名单里的只写日志提示（不阻断推送、不改内容），便于人工复核是否被模型改写或编造。"""
    authors = {a for _r, _t, a, _c in items if a}
    if not authors:
        return
    # 1) 只在「重要人物观点」板块里按「- **名字**：」抓取，其他板块的加粗多为小标题
    seg = digest or ""
    m = re.search(r"\*\*[^*\n]*重要人物观点[^*\n]*\*\*(.*?)(?=\n\*\*[^*\n]+\*\*|\Z)", seg, re.S)
    if m:
        seg = m.group(1)
    cands = re.findall(r"^\s*-\s*\*\*@?([^*\n]{2,30}?)\*\*\s*[：:]", seg, re.M)
    # 2) 全文补充：句中「**名字** 称/表示/说…」这类明确指向发言人的写法
    cands += re.findall(r"\*\*@?([^*\n]{2,24}?)\*\*\s*"
                        r"(?:称|表示|说|认为|指出|提到|回应|质疑|追问|强调|补充|解释|承认|反驳|感叹|建议|披露)",
                        digest or "")
    unknown = []
    for name in cands:
        n = name.strip()
        # 去掉「（SN23）」这类括注后再比对
        n = re.sub(r"[（(].*?[）)]", "", n).strip()
        if not n:
            continue
        # 有的模型爱写「- **小标题**：正文」，含中文且较长的多半是标题不是人名，跳过
        if len(re.findall(r"[\u4e00-\u9fff]", n)) > 0 and len(n) > 6:
            continue
        if any(n == a or n.startswith(a) or a.startswith(n) for a in authors):
            continue
        unknown.append(n)
    if unknown:
        log("%s⚠️ 人名核对：以下名字不在当日发言者名单中，请人工确认是否被改写/编造：%s"
            % (tag, "、".join(sorted(set(unknown)))))
    else:
        log("%s✅ 人名核对通过：日报中的人物均可对应到当日真实发言者" % tag)


def run_one_day(cfg, after_dt, before_dt):
    """导出「某一天」-> 过滤 -> 总结 -> 双推。
    内置每日导出节流（参数可在 config.json 配置）：
      max_exports_per_day        : 当天最多导出次数（默认 2）
      min_export_interval_hours : 两次导出最小间隔小时（默认 12）
    未达间隔或已达上限时，复用 / 合并已有导出记录重总结后推送，避免频繁调用 Discord user token。
    供 main() 在『前一天 / 指定单日 / 指定多日』三种情形复用。"""
    day_str = after_dt.strftime("%Y-%m-%d")
    _record_range(cfg, day_str, after_dt, before_dt)  # 记下真实范围，推送头部显示用
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
        items = extract_substantive(out_json, day_str=day_str)
        _record_export(cfg, day_str, out_json, len(items))
        log("过滤后保留实质消息 %d 条（%s，导出#%d）" % (len(items), day_str, seq))
    else:
        items = merge_substantive(sources)
        log("过滤后保留实质消息 %d 条（%s，%s）" % (len(items), day_str, mode_label))

    # 2) 总结（对比模式下：每个模型各出一版，后面分别推送）
    text_block = ""
    ctx_block, ctx_items = "", []
    if items:
        # 背景：总结日之前 N 天（config.context_days，默认 2）的导出记录，帮模型接上跨天话题；
        # 加上当日提到的子网的本地档案摘要（名称/业务/此前投研结论）。两者都只作理解用。
        text_block = _build_full_text_block(cfg, channel_name, day_str, items)
    sys_p = SYSTEM_PROMPT
    if action in ("resummarize", "merge"):
        sys_p = SYSTEM_PROMPT + DEEPEN_HINT

    profiles = compare_profiles(cfg) if items else []
    results = []      # [{"label": 展示名, "digest": 文本, "cfg": 该模型的配置副本}]
    if not items:
        results.append({
            "label": "",
            "digest": "📭 %s 频道 %s 当日无与 Bittensor 子网/重要成员相关的实质讨论。" % (channel_name, day_str),
            "cfg": cfg,
        })
    elif profiles:
        log("对比模式：%d 个模型分别生成（%s），生成完分别推送"
            % (len(profiles), "、".join(l for l, _ in profiles)))
        for label, pc in profiles:
            try:
                d = generate_digest(pc, text_block, sys_p, items, tag="【%s】" % label)
                results.append({"label": label, "digest": d, "cfg": pc})
            except Exception as e:
                log("【%s】生成失败，跳过这一版：%s" % (label, e))
        if not results:
            raise RuntimeError("对比模式下所有模型都生成失败，请检查 compare_profiles 配置")
    else:
        results.append({"label": "", "digest": generate_digest(cfg, text_block, sys_p, items), "cfg": cfg})

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
    log("日报标注：令牌 %s/%d，当日导出次数 %d/%d%s"
        % (idx_str, t_total, used, max_per_day,
           "" if action.startswith("export") else "（复用，未重新导出）"))

    for r in results:
        r["digest"] = (r["digest"] or "") + footer

    # 人名保真核对（仅写日志提示，不阻断推送）
    for r in results:
        try:
            _audit_names(r["digest"], items, tag=("【%s】" % r["label"]) if r["label"] else "")
        except Exception as e:
            log("人名核对异常（忽略）：%s" % e)

    # 归属核验（仅写日志，噪声较大，供人工复核）
    for r in results:
        tag = ("【%s】" % r["label"]) if r["label"] else ""
        try:
            _bad = audit_attribution(r["digest"], items)
            if _bad:
                log("%s⚠️ 归属核验：%d 处「人物-子网」关联在原文未直接共现，建议 /retry 复核：%s"
                    % (tag, len(_bad), "、".join("%s→SN%d" % (n, s) for n, s, _ in _bad[:6])))
            else:
                log("%s✅ 归属核验通过（人物与子网编号均有原文依据）" % tag)
        except Exception as e:
            log("%s归属核验异常（忽略）：%s" % (tag, e))

    base_title = "🤖 DTAO price-talk 日报 · %s" % day_str

    # 3) 推送（对比模式：每个模型各推一条，标题带版本标签，方便对照）
    if cfg.get("push_enabled", True):
        keys = "ABCDEFGH"
        for i, r in enumerate(results):
            rcfg = r["cfg"]
            title = base_title + ((" ｜ %s" % r["label"]) if r["label"] else "")
            try:
                push_feishu(rcfg, title, r["digest"])
            except Exception as e:
                log("%s飞书推送异常：%s" % (("【%s】" % r["label"]) if r["label"] else "", e))
            try:
                push_telegram(rcfg, title, r["digest"])
            except Exception as e:
                log("%sTelegram 推送异常：%s" % (("【%s】" % r["label"]) if r["label"] else "", e))
            # 落盘：对比模式按 A/B/C 分开存，第一版同时写「标准文件名」供 /repush 与预览使用
            try:
                if r["label"]:
                    with open(os.path.join(out_dir, "dtao_%s.digest.%s.txt" % (day_str, keys[i])), "w",
                              encoding="utf-8") as f:
                        f.write(r["digest"])
                    with open(os.path.join(out_dir, "_last_digest_%s.txt" % keys[i]), "w", encoding="utf-8") as f:
                        f.write(r["digest"])
                if i == 0:
                    with open(os.path.join(out_dir, "dtao_%s.digest.txt" % day_str), "w", encoding="utf-8") as f:
                        f.write(r["digest"])
                    with open(os.path.join(out_dir, "last_digest.txt"), "w", encoding="utf-8") as f:
                        f.write(r["digest"])
            except Exception:
                pass
        # 潜力观察单独生成并落盘，只在 /watch 时推送，不随日报一起发（对比模式只跑一次，用第一版配置）
        try:
            generate_potential(results[0]["cfg"], day_str, items)
        except Exception as e:
            log("潜力观察流程异常（忽略）：%s" % e)
    else:
        for r in results:
            title = base_title + ((" ｜ %s" % r["label"]) if r["label"] else "")
            log("push_enabled=false，仅打印不推送：\n" + title + "\n" + r["digest"])

    # 3) 子网建档（自动，增量）：把本日导出里提到的子网写进各自档案。
    #    已建档的子网只做增量补充，新提到的子网自动建档 —— 无需任何 TG 指令。
    #    这里只对「今天这份导出涉及的文件」扫描，成本很低；失败绝不影响日报已推送的结果。
    if items and cfg.get("archive_auto", True):
        try:
            import subnet_archive as SA
            todo = _day_json_files(cfg, day_str) if action.startswith("export") else list(sources)
            if todo:
                # 顺手补译文（v1.6.2）：只翻本次新增、且缓存里没有的条目。
                # 早期这里写死 translate=False，导致「自动建档进来的条目没有中文译文」，
                # 而手动 `pipeline.py archive` 建的却有 —— 同一份档案一半有译文一半没有。
                # 自动增量每天只新增十几条，且走 _trans_cache，重复导出零成本。
                _tr = bool(cfg.get("archive_translate", True))
                _llm = None
                if _tr:
                    def _tr_llm(body, sp=None):
                        return call_deepseek(cfg, body, system_prompt=sp)
                    _llm = _tr_llm
                r = SA.archive_files(cfg, todo, log=log, llm=_llm, translate=_tr)
                if r.get("subnets"):
                    log("子网建档（自动）：涉及 %d 个子网，新增 %d 条，累计 %d 条"
                        % (r["subnets"], r["new"], sum(v["strong"] for v in r["index"]["subnets"].values())))
                else:
                    log("子网建档（自动）：本批导出未提到任何子网")
        except Exception as e:
            log("子网建档（自动）异常（忽略，不影响日报）：%s" % e)
    return results[0]["digest"]


CHAT_SYSTEM_PROMPT = (
    "你是 Discord 频道「DTAO - price-talk」的社区氛围记录员。这次不看行情、不写投研，"
    "只把当天**被日报过滤掉的水聊（闲聊）**整理成一份轻松的「群聊侧记」。\n"
    "\n【内容边界】\n"
    "- 只写输入里真实出现的闲聊：段子、梗、互怼、情绪、日常生活、跑题话题、表情式的反应；\n"
    "- 不写任何投资结论、买卖建议、子网分析；水聊里提到的涨跌只当闲聊提一句，不做解读；\n"
    "- 群里开玩笑说的「内幕 / 跑路 / 诈骗 / 官方要拉盘」等，一律按玩笑转述，不得写成事实或指控。\n"
    "\n【输出结构（严格按顺序；没内容的小节直接省略；标题用 **加粗**）】\n"
    "**😄 今日氛围**（2-3 句：整体情绪基调、最热的话题、跟行情有没有关系）\n"
    "**🗣 谁在聊什么**（4-6 条，每条一个 **人名** 加一句他今天最有趣的发言；"
    "引语必须用 `原话 → 中文意思` 的格式，见下【双语引语格式】）\n"
    "**🔥 全场最佳梗**（2-3 条：被多人接话或表情回应最多的笑点，写清谁先说的、谁接的，"
    "并**用一句大白话说明这个梗到底在讲什么**）\n"    "**🎲 跑题现场**（与 Bittensor 完全无关的话题，1-3 条；没有就省略）\n"
    "**💡 一句话总结**（用群友口气收尾，别写成研究结论）\n"
    "\n【双语引语格式（硬性要求）】\n"
    "凡引用群友原话，一律写成：「**原话**」→ 中文意思。两个部分都不能省。\n"
    "- 原话照抄英文原文（含缩写、错别字、脏话之外的语气词），不要改写、不要「意译成一句漂亮的英文」；\n"
    "- 箭头后面必须给出**准确的中文意思**，口语化、能看懂就行，不要逐字硬译；\n"
    "- 原话本身就短且浅（如 ok、lol、gm）可以合并成一句并略去译文；\n"
    "- 示例：「it's zils now」→ 现在是 zils 在管了；「Brutal sells on 39」→ 39 被狠狠砸盘。\n"
    "\n【梗的解读（重点）】\n"
    "群里大量笑点来自圈内黑话、串梗和反讽，只看字面会看不懂。遇到梗必须拆开讲清：\n"
    "1. **来源**：这个说法最早是谁、在哪条消息里带出来的（能查到就写，查不到写「起源不详」）；\n"
    "2. **字面 vs 实际**：字面什么意思，群里实际拿它指什么（比如反讽、嘲讽某项目、自嘲亏损）；\n"
    "3. **为什么好笑 / 怎么被接住**：谁接的梗、往哪个方向升级（越吹越大、反着说、点名某人）；\n"
    "4. **不要过度解读**：只解释「这句在群里是什么意思」，不要升华成市场判断或人物评价；\n"
    "5. 纯表情、单词式应答（lol、based、real）不必单独解释，除非它本身就是全场最热的梗。\n"
    "\n【梗条目写法（照这个结构写，缺一环就说明这条还不够）】\n"
    "- **梗名**：**首倡者**「原话」→ 中文意思。**拆解**：字面是 X，群里用来指 Y，"
    "因为 Z（背景/上下文）。**接梗链**：A 怎么接的、B 怎么升级的。\n"
    "- 坏的写法（只有叙述没有解释，禁止）：「某某说了句 X，大家都笑了」；\n"
    "- 好的写法：「**Baldtensor**」：**Seby** 在 kτwτ 抱怨脱发后造词，把 Bittensor 的 tensor 换成 bald，"
    "吐槽「秃头子网」；**orange** 接「kinda sexy」把自嘲推向调情，全场笑翻。\n"
    "\n【溯源纪律（最重要）】\n"
    "1. 只写输入里有的内容；没出现过的梗、没说过的话、没出现的人，一个字都不许编；\n"
    "2. 人名照抄发言者名单，不得音译/缩写/改大小写；不确定是谁说的就不点名，写「有人」；\n"
    "3. 译文只能解释原话的意思，**不得借译文添加原话里没有的判断**"
    "（原话是玩笑，译文也不能写成事实陈述）；\n"
    "4. 语气可以轻松，事实必须准确：调侃归调侃，玩笑不能说成事件，不能替谁加戏；\n"
    "5. 水聊里的传言、猜测、反讽，转述时要带「有人说」「开玩笑称」这类限定词；\n"
    "6. **涉敏内容只概括、不复述**：脏话、人身攻击、种族/性别歧视、政治站队与选举评论，"
    "一律不引用原话、任何小节都不点名到人（不要写「某某聊政治」）；"
    "最多在「今日氛围」写一句「有人聊到政治话题，气氛一度跑偏」；\n"
    "7. **引语纪律**：引用原话必须与原文一致；不得给原话添加上原文没有的解读"
    "（例如把一句玩笑说成「总结了市场情绪」）；\n"
    "8. **时间纪律**：输入里每条都带 `[月-日 时:分]`，跨零点的消息会标「次日」。"
    "写「次日」「后来」「几小时后」之前，先看清两条消息的时间戳；"
    "同一天凌晨说的不算「次日」，也不要把隔了十几小时的两条说成「紧接着」。\n"
    "\n【排版】同日报：**加粗**人名与梗名；- 列表；不用 # 标题、不用 > 引用；手机端友好。"
    "全文 600-1000 字（双语引语会占篇幅，比纯中文版本略长是正常的），"
    "每条 40-80 字，一条只说一件事，不写重复内容。\n"
)

CHAT_VERIFY_PROMPT = (
    "你是社区侧记的校对编辑。输入是【原始水聊记录】+【侧记初稿】。"
    "你只按原文核对，**不做润色、不许扩写、不许新增句子**。\n"
    "\n【逐项检查】\n"
    "1. **人名**：初稿里每个人名都必须以同一写法出现在原文里，否则改成「有人」或删掉；\n"
    "2. **原话**：引号或「」里的内容必须能在原文找到对应片段，找不到就改成转述或删除；\n"
    "3. **双语格式**：每条引语必须是 `「原话」→ 中文意思`。缺译文的补上；"
    "原话被改写成「更通顺的英文」的，改回原文原样；译文曲解原话的，重译；\n"
    "4. **译文不得加戏**：译文只能表达原话本身的意思。原话是玩笑/反讽/假设的，"
    "译文也必须保持玩笑语气，不得写成事实断言或投资判断；\n"
    "5. **梗解读**：梗条目要说明「字面什么意思、群里实际指什么、谁接的梗」。"
    "原文里查不到依据的解释（比如硬说某个说法影射某项目）一律删掉，只保留能对上原文的部分；\n"
    "6. **玩笑**：反讽、假设句（如果…/imagine/假如/要是）、玩梗式发言，不得写成真实发生的事；\n"
    "7. **无中生有**：初稿里原文完全没有的梗、事件、数字、外号，一律删掉；\n"
    "8. **越界**：删掉一切投资结论、买卖建议、子网分析与风险提示；\n"
    "9. **涉敏**：脏话、人身攻击、歧视性/政治性言论必须删除，不得复述；"
    "「谁在聊什么」里若有这类内容，整条删掉，最多在「今日氛围」保留一句中性概括；\n"
    "10. **引语**：原话不得添加原文没有的解读或定性；宁可删掉解读，只留原句；\n"
    "11. **时间**：核对「次日 / 后来 / 紧接着」这类措辞。初稿说「次日」但两条消息同一天的，"
    "改成「同一天」或直接删掉时间词；把相隔十几小时说成「紧接着」的，改掉；\n"
    "12. **结构**：保留全部小节标题与顺序；输出总字数不得超过初稿的 1.1 倍。\n"
    "\n只输出校对后的完整侧记正文，不要任何说明。\n"
)

CHAT_COMPRESS_PROMPT = (
    "你是社区侧记的定稿编辑，本轮只做一件事：**删字数**，绝对不许新增内容。\n"
    "\n【删什么（从高到低）】\n"
    "1. 重复的梗：同一个笑点被多人复述的，只留最先说、最完整的一处；\n"
    "2. 无信息量的附和（「哈哈哈」「+1」「确实」「笑死」）；\n"
    "3. 只有一个人说、没人接的碎碎念；\n"
    "4. 与群聊无关的链接、广告、机器人推送；\n"
    "5. 过长的中文译文——压到最短能看懂的程度（不许删成看不懂的硬译）。\n"
    "\n【必须保留】\n"
    "- 全部小节标题与顺序；\n"
    "- 人名、被点名的梗、**英文原话**（原文一个字都不能删）；\n"
    "- 每条引语的「原话 → 译文」结构：可以压缩译文长度，但不能把译文整段砍掉；\n"
    "- 全场最佳梗与跑题现场的核心笑点、梗的由来说明。\n"
    "\n【输出要求】只输出压缩后的完整侧记，不写说明、不加新句子、不改事实。\n"
)


TOPIC_SYSTEM_PROMPT = (
    "你是一名 Bittensor 生态的资深研究员。用户会给你一个「专题」指令，"
    "你要从按日期分段的聊天记录里，把与该专题相关的讨论整理成一份专题总结，"
    "核心要求是**还原原文人物观点**。\n"
    "\n【还原纪律】\n"
    "1. 人名照抄发言者名单，不得音译/缩写/改写大小写；每个观点必须落到具体的人；\n"
    "2. 尽量引用原话（英文原话保留原文并附中文转述），转述不得加戏、不得替他提高调门；\n"
    "3. 每个观点标注发生日期与时间（用消息前缀里的日期时间），不同日期的事不得拼成因果；\n"
    "4. 共现≠关联：某公司/事件与某子网同日出现不代表有关系；「承认/自认/证实」必须有本人原话，"
    "反讽、玩笑、俏皮回应不算承认；别人的指控必须写明是谁指控的；\n"
    "5. 原文没有的数字、编号、定性一律不写；不确定标「（原文未指明）」或舍弃。\n"
    "\n【输出结构（小节标题加粗，严格按顺序）】\n"
    "**🧭 专题脉络**（该专题的时间线：何时出现、何时升级，4-8 条，每条带日期）\n"
    "**👥 各方观点还原**（按人物逐条：**人物**（日期时间）：观点/原话；同一人多条可合并但保留日期）\n"
    "**⚠️ 争议与未证实说法**（指控、传闻单独列出，注明是单人指控还是多人附和）\n"
    "**📎 其他相关信息**（与专题相关的边角信息，不遗漏）\n"
    "**💡 一句话总结**\n"
    "\n【篇幅】1200-2000 字；宁多引用、少概括；与专题无关的水聊忽略。"
)

TOPIC_VERIFY_PROMPT = (
    "你是专题总结的校对员，不是作者。用户消息包含【原始聊天记录】和【专题总结初稿】。"
    "逐条对照原文核对：人名是否原样照抄、每个观点是否真的属于所标人物、日期时间是否与消息前缀一致、"
    "「承认/因果/关联」是否有原文直接支持（共现≠关联、玩笑不算承认）、有无编造数字与定性。"
    "只纠错与压缩，不改章节结构、不新增内容、**不得比初稿更长**；"
    "只输出修正后的完整专题总结，不要输出核对说明。"
)


def run_topic_summary(cfg, day_str, feedback, targets=None):
    """专题总结（/retry 里反馈带「专题」时自动进入）：
    复用已有导出记录（目标日 + 前 context_days 天 + 今天，有几份算几份），不重新导出、不消耗配额；
    按日期分段喂给模型，按反馈还原某专题的原文人物观点，生成后双推。"""
    global _CFG
    _CFG = cfg
    log("===== 专题总结启动（%s）=====" % day_str)
    log("专题指令：%s" % feedback[:120])
    tz = datetime.timezone(datetime.timedelta(hours=8))
    d0 = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
    n_ctx = max(0, int(cfg.get("context_days", 2) or 0))
    want = [(d0 - datetime.timedelta(days=k)).strftime("%Y-%m-%d") for k in range(n_ctx, 0, -1)]
    want.append(day_str)
    today = datetime.datetime.now(tz).date().strftime("%Y-%m-%d")
    if today not in want:
        want.append(today)
    days = []
    for ds in want:
        files = _day_json_files(cfg, ds)
        if not files:
            log("专题素材：%s 无导出记录，跳过" % ds)
            continue
        items = merge_substantive(files)
        if items:
            days.append((ds, items))
    if not days:
        raise RuntimeError("没有可用的导出记录，无法做专题总结（请先跑 /digest）")
    days = _split_next_day(days)  # 「次日」消息挪回真实日期分段
    cap = int(cfg.get("topic_max_items_per_day", 250) or 250)
    days = [(ds, _cap_day_items(its, cap)) for ds, its in days]
    all_items = [it for _ds, its in days for it in its]
    log("专题素材：%d 天（%s ~ %s），实质消息 %d 条，单日上限 %d 条"
        % (len(days), days[0][0], days[-1][0], len(all_items), cap))
    text_block = build_weekly_text_block(cfg.get("channel_name", "price-talk"), days)
    text_block += ("\n\n【专题指令】%s\n"
                   "按上面【输出结构】立即输出该专题的完整总结正文，不要向用户追问、"
                   "不要输出确认或计划；专题对象以上面这条指令为准（可能是某个 SN 编号/子网/事件/人物）。"
                   % feedback)
    digest = call_deepseek(cfg, text_block, system_prompt=TOPIC_SYSTEM_PROMPT)
    digest = sanitize_digest(digest)
    digest = polish_digest(cfg, text_block, digest, verify_prompt=TOPIC_VERIFY_PROMPT)
    digest = compress_digest(cfg, digest, 0, 1400)
    digest, _n = scrub_unsupported_links(digest, all_items)
    log("专题篇幅：%d 字" % len(re.sub(r"\s", "", digest or "")))
    title = "🔍 DTAO price-talk 专题总结 · %s" % day_str
    out_dir = os.path.join(HERE, cfg.get("output_dir", "exports"))
    try:
        with open(os.path.join(out_dir, "dtao_topic_%s.digest.txt" % day_str), "w", encoding="utf-8") as f:
            f.write("%s\n\n%s\n" % (title, digest))
    except Exception as e:
        log("专题落盘失败（忽略）：%s" % e)
    want_t = [x.lower() for x in (targets or ["feishu", "tg"])]
    if cfg.get("push_enabled", True):
        if "feishu" in want_t:
            try:
                push_feishu(cfg, title, digest)
            except Exception as e:
                log("飞书推送异常：%s" % e)
        if "tg" in want_t:
            try:
                push_telegram(cfg, title, digest)
            except Exception as e:
                log("Telegram 推送异常：%s" % e)
    else:
        log("push_enabled=false，仅打印：\n" + title + "\n" + digest)
    log("===== 专题总结结束 =====")
    return digest


def _latest_export_day(cfg):
    """最近一份导出记录对应的日期（水聊默认取这一天，全程不重新导出）。"""
    out_dir = os.path.join(HERE, cfg.get("output_dir", "exports"))
    days = []
    for p in glob.glob(os.path.join(out_dir, "dtao_*.json")):
        m = re.search(r"dtao_(\d{4}-\d{2}-\d{2})(?:\.\d+)?\.json$", os.path.basename(p))
        if m:
            days.append(m.group(1))
    return max(days) if days else None


def run_chat_summary(cfg, day_str, days_back=1, targets=None):
    """水聊总结（/水聊 或 pipeline.py chat 触发）：
    只读本地已有导出记录，**不重新导出、不消耗 Discord 配额**，
    把当天被日报过滤掉的闲聊单独整理成一份轻量侧记并推送。
    days_back>1 时向前多取几天（一天一条分段），不足的天数自动跳过。"""
    global _CFG
    _CFG = cfg
    days_back = max(1, min(14, int(days_back or 1)))
    log("===== 水聊总结启动（截至 %s，共 %d 天）=====" % (day_str, days_back))
    log("水聊只读本地已有导出记录，不重新导出聊天记录（不消耗 Discord 导出配额）")
    d0 = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
    want = [(d0 - datetime.timedelta(days=k)).strftime("%Y-%m-%d")
            for k in range(days_back - 1, -1, -1)]
    days, empty = [], []
    for ds in want:
        files = _day_json_files(cfg, ds)
        if not files:
            empty.append(ds)
            continue
        items = merge_chitchat(files, day_str=ds)
        if items:
            days.append((ds, items))
        else:
            empty.append(ds)
    if not days:
        raise RuntimeError("最近 %d 天没有可用的导出记录（或全是实质讨论、没有水聊）：%s"
                           % (days_back, "、".join(empty)))
    days = _split_next_day(days)          # 昨日导出延伸出的「次日」消息挪回真实日期
    days = [(ds, its) for ds, its in days if its]
    cap = int(cfg.get("chat_max_items", 500) or 500)
    per_day = max(60, cap // max(1, len(days)))
    days = [(ds, _cap_day_items(its, per_day)) for ds, its in days]
    all_items = [it for _ds, its in days for it in its]
    total = len(all_items)
    log("水聊素材：%d 天分段（%s），%d 条，单日上限 %d 条"
        % (len(days), "、".join(ds for ds, _ in days), total, per_day))
    if empty:
        log("以下日期没有可用导出记录，已跳过：%s" % "、".join(empty))
    text_block = build_chat_text_block(cfg.get("channel_name", "price-talk"), days)
    digest = call_deepseek(cfg, text_block, system_prompt=CHAT_SYSTEM_PROMPT)
    digest = sanitize_chat(digest)
    digest = polish_digest(cfg, text_block, digest, verify_prompt=CHAT_VERIFY_PROMPT,
                           sanitize_fn=sanitize_chat)
    # 引语归属硬核对（放在压缩之前：修正可能加字，最后再统一压到配额内）
    mm, nf = quote_author_audit(digest, all_items)
    if mm:
        digest = fix_quote_authors(cfg, text_block, digest, mm)
        mm2, _ = quote_author_audit(digest, all_items)
        if mm2:
            log("⚠️ 仍有 %d 处引语归属对不上原文：%s"
                % (len(mm2), "；".join("「%s」应为 %s" % (q, "/".join(a)) for _l, q, a, _m in mm2[:3])))
    if nf:
        log("提示：%d 处引语未在原文逐字找到（多为翻译转述，已记录）：%s"
            % (len(nf), "；".join(q for _l, q in nf[:3])))
    digest = compress_digest(cfg, digest, 0, 1000, system_prompt=CHAT_COMPRESS_PROMPT,
                             sanitize_fn=sanitize_chat,
                             cut_hint="每条要点压到 50 字以内，一条只说一件事；"
                                      "🗣 谁在聊什么最多留 5 条、🔥 最佳梗最多 2 条、🎲 跑题现场最多 2 条；"
                                      "删掉解释性从句与形容词，保留「人名 + 英文原话 → 中文意思」；"
                                      "全部小节标题与英文原话必须保留。")
    log("水聊篇幅：%d 字" % len(re.sub(r"\s", "", digest or "")))
    # 标题按「用户要的那几天」写；跨零点补录的次日消息单独注明，不把标题撑成两天
    span = want[0] if days_back == 1 else "%s~%s" % (want[0], want[-1])
    spill = [ds for ds, _ in days if ds not in want]
    if spill:
        span += "（含 %s 凌晨）" % "、".join(ds[5:] for ds in spill)
    title = "💬 DTAO price-talk 水聊 · %s" % span
    out_dir = os.path.join(HERE, cfg.get("output_dir", "exports"))
    fname = (("dtao_chat_%s.digest.txt" % want[0]) if days_back == 1
             else ("dtao_chat_%s_%s.digest.txt" % (want[0], want[-1])))
    try:  # 单独落盘，绝不覆盖当日日报
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
            f.write("%s\n\n%s\n" % (title, digest))
    except Exception as e:
        log("水聊落盘失败（忽略）：%s" % e)
    want_t = [x.lower() for x in (targets or ["feishu", "tg"])]
    if cfg.get("push_enabled", True):
        if "feishu" in want_t:
            try:
                push_feishu(cfg, title, digest)
            except Exception as e:
                log("飞书推送异常：%s" % e)
        if "tg" in want_t:
            try:
                push_telegram(cfg, title, digest)
            except Exception as e:
                log("Telegram 推送异常：%s" % e)
    else:
        log("push_enabled=false，仅打印：\n" + title + "\n" + digest)
    log("===== 水聊总结结束 =====")
    return digest


def resummarize_with_feedback(cfg, day_str, feedback, targets=None):
    """按用户反馈重新总结某天日报（复用已有导出，不重新导出、不消耗 Discord 配额）。
    反馈里带「专题」二字时，改走专题总结流程（按专题还原原文人物观点并推送）。
    targets: None=双推；列表可含 "tg"/"feishu" 只推指定渠道。
    返回新 digest 文本。"""
    feedback = (feedback or "").strip()
    if not feedback:
        raise RuntimeError("反馈内容为空")
    if "专题" in feedback:
        return run_topic_summary(cfg, day_str, feedback, targets=targets)
    out_dir = os.path.join(HERE, cfg.get("output_dir", "exports"))
    sources = [e["json"] for e in _day_exports(cfg, day_str)]
    if not sources:
        raise RuntimeError("%s 还没有导出记录，无法重总结（请先跑 /digest）" % day_str)
    items = merge_substantive(sources)
    if not items:
        raise RuntimeError("%s 的导出记录里没有实质消息" % day_str)

    channel_name = cfg.get("channel_name", "price-talk")
    # 与日常总结走同一套背景（前几天聊天 + 聊到的子网档案），避免两条路径上下文不一致
    text_block = _build_full_text_block(cfg, channel_name, day_str, items)
    sys_p = SYSTEM_PROMPT + (FEEDBACK_HINT % feedback)
    log("按用户反馈重总结（%s，%d 条消息）：%s" % (day_str, len(items), feedback[:80]))
    digest = call_deepseek(cfg, text_block, system_prompt=sys_p)
    digest = sanitize_digest(digest)
    digest = polish_digest(cfg, text_block, digest)
    digest = compress_digest(cfg, digest, 0, 1200,
                             sanitize_fn=lambda s: strip_length_tags(s))
    digest, _ = scrub_unsupported_links(digest, items)

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


# 中文 / 英文「昨天 / 最新 / 今天」等别名，统一解析成具体日期，方便 TG 按钮与手输
YESTERDAY_ALIASES = {"昨日", "昨天", "yesterday", "latest", "最新", "y"}
TODAY_ALIASES = {"今日", "今天", "today", "t"}


def _resolve_day_alias(ds, tz):
    """把「昨日 / 今日 / 最新」等别名解析成 YYYY-MM-DD；不是别名则原样返回。"""
    if ds in YESTERDAY_ALIASES:
        yesterday = (datetime.datetime.now(tz) - datetime.timedelta(days=1)).date()
        return yesterday.strftime("%Y-%m-%d")
    if ds in TODAY_ALIASES:
        return datetime.datetime.now(tz).date().strftime("%Y-%m-%d")
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


def _daily_before_dt(day, now, tz):
    """日报导出的结束边界（v1.5.2 起按用户规则调整）：
    - 总结「昨天」（含默认无参跑法）：昨天 00:00 -> **现在**（跨零点补录今晨消息）；
    - 更早的历史日期：仍为该自然日 00:00–24:00（往更早的日期里塞进后续几天的消息没有意义）。"""
    if day == now.date() - datetime.timedelta(days=1):
        return now
    return datetime.datetime(day.year, day.month, day.day, tzinfo=tz) + datetime.timedelta(days=1)


def main(args):
    log("===== DTAO 流水线启动 v%s（半自动 / 手动触发）=====" % __version__)
    cfg = load_config()
    global _CFG
    _CFG = cfg
    tz = datetime.timezone(datetime.timedelta(hours=8))  # 默认东八区
    now = datetime.datetime.now(tz)
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
            before_dt = _daily_before_dt(d, now, tz)
            run_one_day(cfg, after_dt, before_dt)
    else:
        # 默认：前一天
        yesterday = (now - datetime.timedelta(days=1))
        after_dt = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        before_dt = _daily_before_dt(after_dt.date(), now, tz)
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
# 周报模式：只汇总 exports/ 下已有的导出记录（不重新导出），最近 N 天，不足 N 天有多少算多少
# ----------------------------------------------------------------------------
def run_weekly(cfg):
    global _CFG
    _CFG = cfg
    log("===== DTAO 周报流水线启动 =====")
    log("周报只读取本地已有导出记录，不重新导出聊天记录（不消耗 Discord 导出配额）")
    tz = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(tz).date()
    window = int(cfg.get("weekly_days", 7) or 7)          # 最近 N 天（含今天）
    budget = int(cfg.get("weekly_max_items", 500) or 500)  # 送给模型的实质消息总预算
    day_files = []
    for d in range(window - 1, -1, -1):                   # 从最早一天开始，保证时间正序
        ds = (today - datetime.timedelta(days=d)).strftime("%Y-%m-%d")
        files = _day_json_files(cfg, ds)                  # 兼容 dtao_<日>.1.json / .2.json / 旧命名
        if files:
            day_files.append((ds, files))
    if not day_files:
        raise RuntimeError("exports/ 里最近 %d 天没有任何导出记录（dtao_<日期>.N.json），"
                           "先跑几天 daily 再生成周报" % window)
    per_day = max(30, budget // len(day_files))
    days, empty_days = [], []
    for ds, files in day_files:
        items = merge_substantive(files)   # 同一天导出过多次时按「作者+内容」去重
        if not items:
            empty_days.append(ds)
            continue
        days.append((ds, items))
    if not days:
        raise RuntimeError("最近 %d 天的导出记录里没有可汇总的实质消息（%s）"
                           % (window, "、".join(empty_days)))
    # 「次日」消息（昨日导出延伸到今天的部分）挪回真实日期的分段，避免与今天自己的导出重复
    days = _split_next_day(days)
    empty_days = [ds for ds in empty_days if ds not in {d for d, _ in days}]
    days = [(ds, _cap_day_items(its, per_day)) for ds, its in days]
    all_items = [it for _ds, its in days for it in its]
    log("周报覆盖 %d 天（%s ~ %s），实质消息 %d 条，单日上限 %d 条"
        % (len(days), days[0][0], days[-1][0], len(all_items), per_day))
    if empty_days:
        log("以下日期有导出文件但无实质消息，已跳过：%s" % "、".join(empty_days))
    if len(days) < window:
        log("提示：可用天数不足 %d 天，本次按已有 %d 天汇总" % (window, len(days)))
    text_block = build_weekly_text_block(cfg.get("channel_name", "price-talk"), days)
    if all_items:
        digest = call_deepseek(cfg, text_block, system_prompt=WEEKLY_SYSTEM_PROMPT)
        digest = sanitize_digest(digest)
        digest = polish_digest(cfg, text_block, digest, verify_prompt=WEEKLY_VERIFY_PROMPT)
        digest = compress_digest(cfg, digest, 0, 1400)
        digest, _n = scrub_unsupported_links(digest, all_items)
    else:
        digest = "本期无与 Bittensor 子网/重要成员相关的实质讨论。"
    log("周报篇幅：%d 字" % len(re.sub(r"\s", "", digest or "")))
    title = "🤖 DTAO price-talk 周报 · %s~%s" % (days[0][0], days[-1][0])
    try:  # 落盘留档，方便事后核对与 /repush
        with open(os.path.join(HERE, cfg.get("output_dir", "exports"),
                              "dtao_week_%s_%s.digest.txt" % (days[0][0], days[-1][0])),
                  "w", encoding="utf-8") as f:
            f.write("%s\n\n%s\n" % (title, digest))
    except Exception as e:
        log("周报落盘失败（忽略）：%s" % e)
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
            generate_potential(cfg, "week_%s~%s" % (days[0][0], days[-1][0]), all_items)
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
    ap.add_argument("mode", nargs="?", default="daily",
                    choices=["daily", "weekly", "repush", "resummarize", "chat", "archive", "research"],
                    help="daily=导出并推送（可跟日期，省略则前一天）；weekly=周报；"
                         "repush=仅补推已生成的日报（不重导/不重算）；"
                         "resummarize=按反馈重总结（日期 反馈内容 [tg|feishu]）；"
                         "chat=水聊总结（[日期] [天数]，只读已有导出、不重新导出）；"
                         "archive=子网建档（[天数|日期…]，只读已有导出，按子网单独存档）；"
                         "research=子网投研（子网编号/名称 [天数] [tg|feishu]，读档案+全网交叉验证）")
    ap.add_argument("extra", nargs="*",
                    help="daily: 一个或多个日期 YYYY-MM-DD；repush: 日期 [tg|feishu]（省略则双推）；"
                         "resummarize: 日期 反馈内容 [tg|feishu]；chat: [日期] [天数] [tg|feishu]；"
                         "archive: [天数 N 或 日期…]；research: 子网 [天数] [tg|feishu]")
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
        elif args.mode == "resummarize":
            if len(args.extra) < 2:
                raise RuntimeError(
                    "resummarize 需要：日期 反馈内容 [tg|feishu]\n"
                    "例如：python pipeline.py resummarize 2026-10-31 \"Namikaze 运营哪个子网需核实\"")
            day = args.extra[0]
            rest = list(args.extra[1:])
            targets = []
            while rest and rest[-1].lower() in ("feishu", "tg"):
                targets.insert(0, rest.pop().lower())
            feedback = " ".join(rest).strip()
            if not feedback:
                raise RuntimeError("反馈内容为空")
            resummarize_with_feedback(load_config(), day, feedback, targets or None)
        elif args.mode == "chat":
            cfg = load_config()
            tz = datetime.timezone(datetime.timedelta(hours=8))
            rest = list(args.extra)
            targets = []
            while rest and rest[-1].lower() in ("feishu", "tg"):
                targets.insert(0, rest.pop().lower())
            days_back = 1
            if rest and rest[-1].isdigit():
                days_back = max(1, min(14, int(rest.pop())))
            if rest:
                day = _resolve_day_alias(rest[0], tz)
                try:
                    datetime.datetime.strptime(day, "%Y-%m-%d")
                except Exception:
                    raise RuntimeError("水聊日期无法解析：%s（应为 YYYY-MM-DD / 昨日 / 今日）" % rest[0])
            else:
                day = _latest_export_day(cfg) or _resolve_day_alias("昨日", tz)
            run_chat_summary(cfg, day, days_back, targets or None)
        elif args.mode == "archive":
            # 子网建档：只读本地导出、不耗 Discord 配额；增量幂等（按消息 id 去重）
            import subnet_archive as SA
            cfg = load_config()
            rest = list(args.extra)
            tz = datetime.timezone(datetime.timedelta(hours=8))
            days = None
            if rest:
                if rest[0].isdigit():
                    n = max(1, int(rest[0]))
                    all_days = SA.list_export_days(os.path.join(HERE, cfg.get("output_dir", "exports")))
                    days = all_days[-n:]
                    rest = rest[1:]
                else:
                    days = []
                    for a in list(rest):
                        ds = _resolve_day_alias(a, tz)
                        if re.match(r"^\d{4}-\d{2}-\d{2}$", ds or ""):
                            days.append(ds)
                            rest.remove(a)
                    days = days or None
            # 建档顺手补译文（只翻新条目，走缓存），便于后续投研与人工查阅
            def _llm(body, sp=None):
                return call_deepseek(cfg, body, system_prompt=sp)
            _tr = bool(cfg.get("archive_translate", True))
            if not _tr:
                log("archive_translate=false：只建档不翻译")
            r = SA.archive_days(cfg, days, log=log, llm=_llm if _tr else None,
                                translate=_tr)
            log("档案目录：%s" % r["arc_dir"])
            for sn, info in sorted(r["index"]["subnets"].items(),
                                   key=lambda kv: -kv[1]["strong"])[:12]:
                log("  SN%-4s %-22s %4d 条 / %d 人 / %s~%s"
                    % (sn, (info["name"] or "未知")[:22], info["strong"],
                       info["people"], info["first"] or "-", info["last"] or "-"))
        elif args.mode == "research":
            # 子网投研：读本地档案 + 全网抓取交叉验证，生成重要事件跟踪报告
            import subnet_research as SR
            cfg = load_config()
            rest = list(args.extra)
            targets = []
            while rest and rest[-1].lower() in ("feishu", "tg"):
                targets.insert(0, rest.pop().lower())
            # 关键顺序：**第一个 token 永远是子网**（"29" 本身是数字，不能当回看天数吃掉），
            # 只有第二个及之后的纯数字才是天数。历史坑：/投研 29 被解析成"没给子网"。
            if not rest:
                raise RuntimeError("research 需要指定子网，例如：python pipeline.py research 29")
            # 剩下的全部当作用户反馈（来自 TG `/retry 127 已改名为 Zils`）。
            # 分隔符用 `~fb`：argparse 会把以 `-` 开头的 token 当成选项，
            # `--fb` 会被直接拒掉（实测 unrecognized arguments），`~fb` 安全。
            feedback = ""
            if "~fb" in rest:
                i = rest.index("~fb")
                feedback = " ".join(rest[i + 1:]).strip()
                rest = rest[:i]
            sn_arg = rest.pop(0) if rest else ""
            if not sn_arg:
                raise RuntimeError("research 需要指定子网，例如：python pipeline.py research 29")
            days_n = int(cfg.get("research_days", 14) or 14)
            if rest and rest[0].isdigit():
                days_n = max(3, min(60, int(rest.pop(0))))
            if feedback:
                log("投研将按用户反馈修正：%s" % feedback[:120])
            title, digest, path = SR.run_research(cfg, sn_arg, targets or None,
                                                 days=days_n, log=log, feedback=feedback)
            log("投研报告：%s" % path)
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
