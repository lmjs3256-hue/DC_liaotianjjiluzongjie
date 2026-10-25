# 更新日志

所有变动按时间倒序记录。版本号采用日期标记。

---

## 2026-10-25 — 放开触发权限 + 多 token 随机化

### 触发权限改为开放模式
- 取消「仅本人（allowed_user_ids）可触发」限制：`tg_listener.py` 不再校验来源用户/会话，**任意会话、任意用户**发来的指令都会被处理。
- 改为「可选白名单」：仅当 `config.json` 里 `allowed_user_ids` 非空时才限制为名单内用户；为空（默认）即开放给任何人。已将本机 `config.json` 的 `allowed_user_ids` 清空以真正开放。
- **节流仍严格生效**：每日真实导出次数上限（默认 2 次）+ 间隔（默认 12h）由 pipeline 内置保护，多人反复触发也不会突破，确保 user token 不被敞开口滥用。
- TG 查询指令（/digest /weekly /repush /watch /help）对任意人均可用。

### 多 user token 改为随机触发
- `pipeline.py` 的 `pick_discord_token()` 由「按日期确定性轮换」改为**每次随机抽一个**（`random.randrange`），把请求摊到多个账号、降低单号风控概率；同一天也可能命中不同号。
- 单 token（仅 `discord_token`）模式向后兼容不变。新增 `import random`。

## 2026-10-25 — 每日导出节流 + 飞书复制优化

### 每日导出节流（防 user token 滥用）
- **规则（按自然日 day_str 计，仅对 `/digest` 生效；`/weekly`、`/repush` 不受影响）**：
  - 当天**第 1 次**触发：正常导出。
  - 当天**第 2 次**触发：仅当距上次导出 **≥12 小时**才重新导出；若 **<12 小时**，不重新导出，只把**最近一次导出记录**用「深刻总结」提示词重新生成日报并推送。
  - 当天**已导出 2 次**（达上限）：不论怎么触发都不再导出，自动**合并两次导出记录**（按 作者+内容 去重）生成一份日报并推送。
- **实现**：`pipeline.py` 新增 `_digest_state.json`（存于 `exports/`，已被 .gitignore 忽略）记录每天导出时间戳与 json 路径；`_plan_digest()` 决策四态 `export1 / export2 / resummarize / merge`；`merge_substantive()` 合并去重；`resummarize / merge` 时 `call_deepseek` 追加 `DEEPEN_HINT` 做更深入总结。
- **效果**：单日 Discord user token 实际导出最多 2 次，且两次间隔 ≥12h，大幅降低风控命中概率；频繁手点只触发本地重总结，不消耗 token。
- **参数可配置**：阈值不再硬编码。在 `config.json` 加 `max_exports_per_day`（默认 2）、`min_export_interval_hours`（默认 12）即可调整；`pipeline.py` 新增 `_throttle_cfg()` 读取并做边界保护（上限 <1 按 1、间隔 <0 按 0）；决策扩展支持 `export3 / export4 / …` 任意次数（不再限于 2 次）。`config.example.json` 已加示例字段。
- 注意：TG 回传已显示「🔧 本次模式」（导出第N次 / 复用重总结 / 合并重总结），可直观判断是否真导出。

### 飞书复制优化
- `push_feishu()` 卡片正文顶部新增**加粗标题行 + 日期行**，在飞书里长按复制文案时可直接选到「标题与日期」（卡片蓝条标题复制时常不带，正文顶部补一份便于粘贴）。

## 2026-10-25 — 监听回传精简（TG 不再刷大段日志）

- **问题**：`tg_listener.py` 的 `run_pipeline()` 把 `pipeline.py` 的全部运行日志（导出命令行、代理地址、字节数、过滤条数、推送完成…）整段回传到 Telegram，导致日报正文之后跟着一大段冗余日志。
- **修复**：日报本就由 `pipeline` 自行推送到飞书+TG，监听回传日志是多余的。现改为：**成功时只回一句简洁确认**（含当日实质消息条数）；**仅失败时回传错误尾部**便于排错。
- 注意：修改后需**重启监听进程**才会生效。

## 2026-10-25 — 修复「昨日日报」无法解析

- **Bug**：TG 按钮 `/digest 昨日`（及 `/repush 最新`）把中文词「昨日 / 最新」当作日期传给 `pipeline.py`，而 daily 模式只认 `YYYY-MM-DD` 或空（空=昨天），导致报 `无法解析日期：昨日`。
- **修复**：`pipeline.py` 新增 `_resolve_day_alias()`，将 `昨日 / 昨天 / yesterday / latest / 最新 / y` 统一解析为昨天日期；`repush` 模式支持 `最新 / latest` 自动取最新已生成日报。修改后 `/digest 昨日`、`python pipeline.py 昨日`、`/repush 最新` 均正常。
- 注意：修改代码后需**重启监听进程**才会生效（监听在启动时加载 `pipeline` 模块）。

## 2026-10-24 — 文档与自启方案收尾

- **`install_task_login.ps1` 修复与增强**
  - 修复脚本内中文导致 PowerShell 以 ANSI 读取、字符串解析报错、无法注册任务计划的问题（改为纯 ASCII 输出）。
  - 新增 `-MultipleInstancesPolicy IgnoreNew`：登录时即使触发多次也只保留一个监听，避免重复拉起抢消息。
- **自启方案明确化**（README / DEPLOY_HOMEPC）
  - 推荐「启动文件夹自启」作为**无需管理员**的默认方案：把 `DTAO_TG_Listener.bat` 的**快捷方式**放进启动文件夹，登录后无窗口后台运行 `tg_listener.py`。
  - 「任务计划程序」列为**可选增强**（崩溃/断网自动重启），但必须以**管理员身份**运行 `install_task_login.ps1`；启用后需移除启动文件夹的快捷方式，否则会双开冲突。
- **Telegram 按钮栏说明与代码对齐**：`/digest 昨日`、`/repush 最新`、`/watch 7`、`/weekly`。
- 确认当前实机采用「启动文件夹自启」即可满足重启/登录后自动接管监听，无需公网 IP。

---

## 2026-10-23（近期累积功能）

- **飞书 / Telegram 排版彻底分离**
  - 飞书：interactive 卡片 + `wide_screen_mode` 铺满画布，板块标题加粗。
  - Telegram：独立渲染 + 分隔线框 + 标题加粗，规避旧版 Markdown `**` 不识别与畸形星号。
- **潜力挖掘与跨日跟踪**
  - DeepSeek 提示词重写：🔥必读重点 / 🔢重点子网动向 / 👥重要人物观点 / 📊潜力观察·近期可布局 / 💡一句话总结。
  - 每次 `/digest`、`/weekly` 自动沉淀「📊 潜力观察」到 `watchlist_YYYY-MM-DD.txt` 与 `watchlist_all.md`。
  - 新增 TG `/watch`：`/watch`（最新一天）、`/watch 7`（近 7 天跨日热点，按提及天数排序）、`/watch all`（全部），并同步推送飞书。
- **多 token 轮换降低封号风险**：`discord_tokens` 池按日期 `% N` 确定性轮换。
- **TG 监听安全加固**：`allowed_user_ids` 仅授权本人触发；指令兼容 `/cmd@机器人名`。
- **修复**：飞书签名 19021、TG `**` 不识别、畸形星号、双监听 409 冲突等。

---

## 初始版本（仓库奠基）

- `pipeline.py`：Discord 导出 → 过滤水聊 → DeepSeek 总结 → 飞书卡片 + TG 双推。
- `tg_listener.py`：Telegram 长轮询监听，`/digest`、`/weekly`、`/repush`、`/watch`、`/help` 指令 + 常驻按钮栏。
- `config.example.json` / `.gitignore`（屏蔽 `config.json`、日志、`exports/`、导出程序等密钥与数据）。
- `README.md`、`DEPLOY_HOMEPC.md`：小白使用说明与旧电脑常开部署教程。
- `start_tg_listener.bat`、`DTAO_TG_Listener.bat`、`install_task_login.ps1`：启动与自启脚本。
