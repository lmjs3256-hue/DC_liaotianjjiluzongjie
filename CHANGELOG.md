# 更新日志

所有变动按时间倒序记录。版本号采用日期标记。

---

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
