#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大模型平台档案管理：列出 / 切换 / 现场添加 / 删除（供 TG /模型 命令调用）。

背景（2026-11-22）：
    日报分析走 config.json 里的 deepseek_base_url / deepseek_model / deepseek_api_key
    三个字段（字段名是历史遗留，实际是任意 OpenAI 兼容接口）。当前默认是
    Engy（Bittensor SN53）的 api.engy.ai/v1 + kimi-k3。手改三个字段不方便，
    故做「档案 + 切换」：常用平台各存一份档案，TG 里一句话切换。

设计要点：
    1. 档案存在 config.json 的 llm_profiles[] 里，当前用哪个记在 active_llm_profile。
    2. 切换 = 把选中档案的字段写回 deepseek_* 主字段 + 设 active；
       因为日报是子进程重读 config.json，写盘即生效，不需要重启监听。
    3. 原子写（tmp + os.replace），避免半截写坏配置文件。
    4. 所有回执里的 api_key 一律脱敏，绝不回传完整 key 到聊天记录。
    5. 现场添加会把 key 存进 config.json（明文，和现有 deepseek_api_key 一样），
       调用方需知悉 TG 聊天记录会留痕。

档案结构（llm_profiles[] 里的每一项）：
    {
      "name": "sn28",                      # 短名，TG 里用 /模型 sn28
      "label": "SN28 / <model>",           # 展示名（可选，缺省=name）
      "base_url": "https://xxx/v1",        # OpenAI 兼容网关地址
      "model": "model-name",               # 模型名
      "api_key": "sk-xxx",                 # 密钥
      "extra_params": {...},               # 可选，原样并入请求体
      "max_tokens": 5000,                  # 可选
      "timeout": 120                       # 可选
    }
"""
import json
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# 档案字段 -> config 主字段的映射
_FIELD_MAP = [
    ("base_url", "deepseek_base_url"),
    ("model", "deepseek_model"),
    ("api_key", "deepseek_api_key"),
    ("extra_params", "deepseek_extra_params"),
    ("max_tokens", "deepseek_max_tokens"),
    ("timeout", "deepseek_timeout"),
]


def _load():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save(cfg):
    """原子写：先写同目录临时文件再 replace，避免中途崩溃写坏 config.json。"""
    fd, tmp = tempfile.mkstemp(prefix="config.", suffix=".tmp", dir=HERE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _mask(key):
    """脱敏：只留前 6 位和后 4 位，中间用 ... 代替。"""
    s = str(key or "")
    if len(s) <= 10:
        return "***" if s else "(空)"
    return "%s...%s" % (s[:6], s[-4:])


def _profiles(cfg):
    return [p for p in (cfg.get("llm_profiles") or []) if isinstance(p, dict)]


def _find(cfg, name):
    key = str(name or "").strip().lower()
    for p in _profiles(cfg):
        if str(p.get("name") or "").strip().lower() == key:
            return p
    return None


def active_name(cfg=None):
    """当前生效的档案名（active_llm_profile）。"""
    cfg = cfg if cfg is not None else _load()
    return str(cfg.get("active_llm_profile") or "").strip() or None


def list_profiles():
    """返回 (档案展示信息列表, 当前档案名)。不返回完整 key。"""
    cfg = _load()
    act = active_name(cfg)
    out = []
    for p in _profiles(cfg):
        out.append({
            "name": str(p.get("name") or "").strip(),
            "label": str(p.get("label") or p.get("name") or "").strip(),
            "base_url": str(p.get("base_url") or "").strip(),
            "model": str(p.get("model") or "").strip(),
            "key_mask": _mask(p.get("api_key")),
            "active": (str(p.get("name") or "").strip().lower() == (act or "").lower()),
        })
    return out, act


def current():
    """返回当前生效的主配置展示信息（直接读 config 主字段）。"""
    cfg = _load()
    return {
        "base_url": str(cfg.get("deepseek_base_url") or "").strip(),
        "model": str(cfg.get("deepseek_model") or "").strip(),
        "key_mask": _mask(cfg.get("deepseek_api_key")),
        "active": active_name(cfg),
    }


def switch(name):
    """切换到档案 name：写回 deepseek_* 主字段并设 active。返回展示信息 dict。"""
    cfg = _load()
    p = _find(cfg, name)
    if not p:
        raise KeyError("没有名为「%s」的档案；发 /模型 看可用列表" % name)
    for src, dst in _FIELD_MAP:
        if src in p:
            cfg[dst] = p[src]
        elif src in ("extra_params", "max_tokens", "timeout"):
            # 档案没带的可选项要清掉，否则会残留上一个模型的设置
            # （例如上一个模型关了思考、新模型不该继承）
            cfg.pop(dst, None)
    cfg["active_llm_profile"] = str(p.get("name") or name).strip()
    _save(cfg)
    return {
        "name": cfg["active_llm_profile"],
        "label": str(p.get("label") or p.get("name") or cfg["active_llm_profile"]).strip(),
        "base_url": str(p.get("base_url") or "").strip(),
        "model": str(p.get("model") or "").strip(),
        "key_mask": _mask(p.get("api_key")),
    }


def add(name, base_url, model, api_key, label=None):
    """现场添加/覆盖一个档案。返回展示信息 dict。"""
    name = str(name or "").strip()
    base_url = str(base_url or "").strip().rstrip("/")
    model = str(model or "").strip()
    api_key = str(api_key or "").strip()
    if not name or not base_url or not model or not api_key:
        raise ValueError("四个参数都不能为空：名称 base_url 模型名 api_key")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头")
    if "/" in name or "@" in name:
        raise ValueError("档案名称不能含 / 或 @")
    cfg = _load()
    profiles = _profiles(cfg)
    item = {
        "name": name,
        "label": str(label or name).strip(),
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
    }
    replaced = False
    for i, old in enumerate(profiles):
        if str(old.get("name") or "").strip().lower() == name.lower():
            # 覆盖同名档案，但保留它已有的可选项（extra_params/max_tokens/timeout）
            for opt in ("extra_params", "max_tokens", "timeout"):
                if opt in old:
                    item[opt] = old[opt]
            profiles[i] = item
            replaced = True
            break
    if not replaced:
        profiles.append(item)
    cfg["llm_profiles"] = profiles
    _save(cfg)
    item["key_mask"] = _mask(api_key)
    item["replaced"] = replaced
    return item


def remove(name):
    """删除档案 name。返回被删档案名。不能删当前正在用的档案。"""
    cfg = _load()
    p = _find(cfg, name)
    if not p:
        raise KeyError("没有名为「%s」的档案" % name)
    pname = str(p.get("name") or name).strip()
    if (active_name(cfg) or "").lower() == pname.lower():
        raise ValueError("「%s」是当前正在用的档案，请先切到别的再删" % pname)
    cfg["llm_profiles"] = [x for x in _profiles(cfg)
                           if str(x.get("name") or "").strip().lower() != pname.lower()]
    _save(cfg)
    return pname


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        profs, act = list_profiles()
        print("当前档案:", act or "(未设置)")
        print("当前主配置:", json.dumps(current(), ensure_ascii=False))
        for p in profs:
            print(json.dumps(p, ensure_ascii=False))
    elif cmd == "switch" and len(sys.argv) > 2:
        print(json.dumps(switch(sys.argv[2]), ensure_ascii=False))
    elif cmd == "add" and len(sys.argv) > 5:
        print(json.dumps(add(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]), ensure_ascii=False))
    elif cmd == "remove" and len(sys.argv) > 2:
        print("已删除:", remove(sys.argv[2]))
    else:
        print(__doc__)
