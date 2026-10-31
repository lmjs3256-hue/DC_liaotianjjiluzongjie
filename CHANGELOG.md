# 更新日志

本项目采用**语义化版本**（`MAJOR.MINOR.PATCH`），版本号在 `pipeline.py` 的 `__version__` 中维护：

| 变更类型 | 版本位 | 举例 |
|---|---|---|
| 修 bug、内部优化（不影响用法） | `PATCH` +1 | 1.0.0 → 1.0.1 |
| 新增功能（向后兼容） | `MINOR` +1 | 1.0.1 → 1.1.0 |
| 不兼容变更（配置项/命令改动，需迁移） | `MAJOR` +1 | 1.1.0 → 2.0.0 |

**当前版本：v1.0.1** ｜ 查看版本：`python pipeline.py --version`

> 每次发版请做三件事：改 `__version__`、在 README 顶部更新版本行、在下方追加一条记录。

---

## v1.0.1 — 2026-10-31

修复「日报漏掉关键信息」「人名对不上」两类质量问题。**根因在喂给模型的数据，而不是模型能力**。

### 修复

- **裸数字子网号被整条丢弃（关键）**：过滤规则只认 `sn 117` / `subnet 117`，但频道里大家习惯直接写
  「117」「61」「79」。于是「Does anyone know the team behind 117?」「can i have your opinion on 117 team」
  「Has something come out thats wrong about 61?」这类关键提问**全被过滤掉**，模型根本看不到。
  修复：新增 `BARE_SN` 正则（认 1-256 的裸数字，排除链接/版本号/价格），`is_substantive()` 纳入判定。
- **对话上下文被切断，指代失去主语**：原来只保留命中关键词的孤立句子，
  「2t daily buyback」上一句「61 是不是出事了」被删，模型无从判断 2t 指谁。
  修复：`extract_substantive()` 改为「锚点 + 前后各 2 条上下文」（条目过多时自动收窄到 1 条）。
  10-30 实测：保留条数 45 → 128，时间线完整。
- **消息按点赞数排序，时间线被打乱**：`extract_substantive()` / `merge_substantive()` 原来按反应数降序，
  模型读到的是错序碎片，无法理解因果与指代。修复：改为**按时间升序**，热度只作为 `🔥n` 标注保留。
- **`2t` 被误读成「SN2」**：新增规则区分「子网编号」与「数量单位」——`2t` = 2 个 TAO、`100k` = 10 万、
  `0.8B` = 8 亿参数；并要求结合上文还原成「**SN61** 每天回购约 2 个 TAO」这类明确表述。
- **人名可能被改写或编造**：输入里新增「当日发言者名单」（去重后的真实 `nickname`），
  并在提示词中要求人名**逐字照抄**（含 `τFIG`、`kτwτ`、`Squirτle Baggins` 等特殊字符），不得音译/缩写。
- **人名自动核对**：新增 `_audit_names()`，日报生成后把「人物观点」里出现的名字与当日发言者名单比对，
  不匹配就在 `run.log` 打 `⚠️ 人名核对` 告警，全部匹配则打 `✅ 人名核对通过`（不阻断推送）。

### 改进

- **提示词新增 4 条硬规则**（编号 12-16）：
  ① 分清子网编号与数量单位；② 指代必须还原主语；③ **未解答的疑问也是信息**——群内对某子网的点名、质疑、提问
  （如「117 团队是谁」）即使当天没有结论，也要作为「待解疑问」写进日报；④ 同一子网的连续对话合成一条完整叙事；
  ⑤ 人名只能从发言者名单里取。
- **新增【语言风格】要求**：用大白话写，少用行话；除人名、子网名、代币名外尽量用中文，
  「SOTA」「BD」「NDAs」这类缩写须写成「中文（英文缩写）」形式。
- **内容容量放宽**：「必读重点」3-6 条 → 3-7 条，全文 800 字 → 1000 字，避免关键信息被字数挤掉。

### 实测效果（2026-10-30 数据，同一模型 deepseek-v3）

| 检查项 | 修复前 | 修复后 |
|---|---|---|
| SN117 团队身份提问 | 完全缺失 | 「**SN117** 团队身份成谜：Pineapple 两次追问，当天无人回答」 |
| 2t daily buyback | 缺失 / 误读 | 「Pineapple 从未看好 **SN61** 的『每天回购 2 个 TAO』」 |
| SN61 待解疑问 | 缺失 | 「BROdin 质疑是否出事，当天无人给出原因」 |
| 人名核对 | 无此机制 | ✅ 全部可对应真实发言者 |

---

## v1.0.0 — 2026-10-31

首个正式版本。收敛了 2026-10-23 ~ 10-30 期间累积的全部功能，并集中修复了总结质量与进程稳定性问题。

### 新增

- **TG `/retry` 反馈重总结**：日报有错、或你有补充事实时，直接在群里发 `/retry <反馈>`（别名 `/fix`、`/重总结`、`/修正`）。
  反馈会以「据用户补充」的身份进入提示词，不会被当成群内事实；与反馈冲突的旧结论会被强制删除。
  支持 `/retry 2026-10-29 <反馈>` 指定日期；后台线程执行，不阻塞其它指令。
  代码：`tg_listener.py` 新增 `_feedback_worker()` / `run_feedback()`，`pipeline.py` 新增 `resummarize_with_feedback()`。
- **潜力观察独立成篇**：日报不再夹带「潜力观察」板块。每次跑日报/周报时**额外单独生成**一份潜力观察，
  **只落盘不推送**，只有你发 `/watch` 时才推送。新增 `POTENTIAL_PROMPT` 与 `generate_potential()`，
  `watchlist_*.txt` / `watchlist_all.md` 改为从独立文件读取。
- **`deepseek_temperature` 可配置（默认 0）**：原来硬编码 0.3，同一份记录两次总结会给出不同结论
  （例如同一笔卖出，一次写「本人承认卖出」、一次写「通过质押卖出」）。默认降到 0 保证结论稳定。
- **提示词加入「事实核查与溯源」规则**：日报与周报同步继承，核心 6 条——
  ① 每条事实须在原文可查，必要时标「某某称」；
  ② 禁止推断手段/动机/因果（原文只说「有人卖了」就只能这么写，不得补「通过质押」「为了砸盘」）；
  ③ 禁止把 A 的说法与 B 的说法拼成一条因果，禁止观点安错人；
  ④ 同一事实全文各节表述必须一致；
  ⑤ 宁可写「原文未提及」也不推测补空，推测不得进「必读重点」；
  ⑥ 输出前逐条自检（数字、买卖方向、主体归属、时间先后）。
  另加「排名必须落到具体 SN 编号」规则（原「当日第一涨幅」未写子网号即属此类缺陷）。
- **`--version` / `-V`**：终端可查当前版本。
- **失败可诊断**：流水线出错时把**完整堆栈**写入 `run.log`，不再只有一行原因。

### 变更

- **日报模板精简：6 段 → 4 段**。删除「🔢 重点子网动向」（与「🔥 必读重点」内容高度重复），
  子网异动统一并入「必读重点」（3-6 条）；「📊 潜力观察」移出日报。现结构为：
  🔥 必读重点 / 👥 重要人物观点 / 📎 其他细节 / 💡 一句话总结。字数上限 900 → 800。
- **周报同步精简**：「重点子网动向」并入「本周核心趋势」，「潜力观察」移出周报。

### 修复

- **中文 Windows 下日志打印导致整进程崩溃（严重）**。`pipeline.py` 的 `log()` 里 `print()` 无保护，
  而子进程 stdout 被重定向为管道时默认 GBK 编码；日志中一旦出现 `↳`(U+21B3)、`❌`、`✅`、`🔥` 等
  GBK 无法编码的字符，`print` 抛 `UnicodeEncodeError`，异常冒泡到顶层 `except` 后，
  兜底里的 `log("❌ 流水线失败")` 因 `❌` 再次抛出 → 进程退出码 1、**run.log 一个字都没写**。
  典型表现：代理抖动触发 DeepSeek 重试 → 打印「↳ 第 1/3 次尝试失败」→ 整个日报任务静默消失。
  修复：① 模块顶部将 `sys.stdout/stderr` 重配置为 `utf-8, errors=replace`；
  ② `log()` 的 print 包 try 并降级重打；③ 顶层改捕 `BaseException` 并落盘完整堆栈。
- **TG 错误回传乱码**：监听 `_run_pipeline_sync()` 用 `encoding="utf-8"` 读取按 GBK 输出的子进程，
  失败时回传的错误详情变成 `�ձ�ʧ�ܣ...`。修复：给子进程注入 `PYTHONIOENCODING=utf-8` /
  `PYTHONUTF8=1` / `PYTHONFAULTHANDLER=1`，并新增 `_clean_tail()` 清洗替换字符与控制字符，
  只回传最后 25 行 / 1200 字符。
- **DeepSeek 调用无重试**：偶发代理抖动会直接判整次任务失败。现失败自动重试 3 次（间隔 10s / 20s），
  4xx（如 key 错误）不浪费重试直接报错。

### 文档

- README：核心特性重写（溯源规则 / 结论稳定 / `/retry` / 潜力观察独立 / 静默失败修复）；
  指令表补 `/retry`；潜力观察章节改为「独立成篇」；新增 3 条 FAQ（日志空白、乱码、内容与原文不符）；
  `config.json` 示例补 `deepseek_temperature`；顶部标注版本号。
- CHANGELOG：引入语义化版本体系。
- `config.example.json`：新增 `deepseek_temperature: 0`。

---

## 历史记录（v1.0.0 之前，按日期倒序）

## 2026-10-26 — 文档全面同步（API 后端 / 自愈自启 / 令牌脚注 / 命令修正）

- **README.md**
  - 第 2 步「下载 DiscordChatExporter」改为**可选**：默认 `dce_backend: "api"` 不需要任何外部程序，并说明 DCE 为何不建议（.NET 无 `--proxy` 参数，常卡在 `Resolving channel(s)...`）。
  - 核心特性新增 3 条：内置 API 导出后端、监听崩溃自愈（守护脚本 15 秒重启）、导出元信息脚注。
  - **修正命令示例**：`python pipeline.py 2026-10-23` 会报 `argument mode: invalid choice`，正确写法是 `python pipeline.py daily 2026-10-23`（第一个参数只能是 `daily` / `weekly` / `repush`）；补充中文别名 `daily 昨日` 用法。
  - 第六节开机自启改为「`DTAO_Listener_Autostart.bat` 放启动文件夹 → 拉起 `DTAO_TG_Listener_Guard.bat` 守护版」，并说明为何不能直接拷 Guard 本体（`%~dp0` 定位问题）。
  - 文件说明表新增 `DTAO_TG_Listener_Guard.bat`、`DTAO_Listener_Autostart.bat`。
  - 常见问题新增两条：DCE 卡死如何切 API 后端、「令牌：1/2 · 次数：1/2」含义。
- **DEPLOY_HOMEPC.md**：前置条件 .NET 改为「一般不需要」；文件清单 / config 校验项改为 `dce_backend: "api"`（并说明 `api_fallback` / `dce_timeout`）；自启方案改为 Autostart + Guard；排错表新增「导出卡住」「令牌脚注」「心跳日志排查」等条目。
- **config.example.json**：同步 `dce_backend: "api"`、`api_fallback: true`、`dce_timeout: 120`。

## 2026-10-26 — 日报末尾标注「令牌：n/m · 次数：x/y」

- **需求**：给 Discord token 编号，日报末尾显示本次用的是第几个 token、池中共几个；并附带当日导出次数与配额上限，一眼看出 token 轮换是否生效、今天还剩几次导出额度。
- **实现**：`pick_discord_token()` 在随机抽中后把「第几个 / 共几个」记入全局 `_TOKEN_INFO`；`run_one_day()` 生成日报文本后追加一行脚注。
- **显示格式**（飞书 / Telegram / 落盘 txt 三处一致）：
  - 真实导出：`令牌：1/2 · 次数：1/2`（本次抽中第 1 个 token，池中共 2 个；当日已导出 1 次，上限 2 次）
  - 复用未导出：`令牌：-/2 · 次数：1/2（本次复用已有记录，未重新导出）` —— 未发生导出时不归属任何 token，故编号显示 `-`，但总数与已用次数照常显示。
- `次数` 分母取自 `max_exports_per_day`，分子为该自然日累计导出次数（`_digest_state.json` 实测），二者联动，改配置即改显示。

## 2026-10-26 — 新增内置 API 导出后端（根治 DiscordChatExporter 卡死）

- **问题**：10-25/10-26 连续三次导出全部卡死，日志停在「检测到系统代理」后再无输出。实测 `DiscordChatExporter.Cli.exe`（v2.48.0，.NET）卡在 `Resolving channel(s)...` 一步超过 120 秒不返回，导出失败；连带监听进程被拖崩。而同一时刻用 curl/urllib 走 `127.0.0.1:7890` 访问 Discord API 正常（401/200，0.4s），token 有效、频道可读 —— 问题只在 DCE 自身的 .NET 代理解析行为（DCE 无 `--proxy` 参数，只能靠 .NET 读代理）。
- **修复**：新增内置导出后端 `dce_backend="api"` —— 不依赖 DCE，直接用 Discord REST API 分页拉取指定日期消息（snowflake 翻页、`[after 00:00, before 00:00)` 左闭右开、UTC 语义与 DCE 一致），产出 JSON 与 DCE 完全兼容（`messages[].author.name / content / timestamp / reactions[].count`）；429 限速自动等待重试，401/403 明确报「token 无效或无权访问」。
- **自动兜底**：`api_fallback: true`（默认开）+ `dce_timeout: 120`（默认，原为 600）。local/docker 后端超时、非 0 退出或未产出文件时，自动回退到 API 后端，不再干等 10 分钟。
- **实测**：10-25 全天导出由 DCE 的「卡死 10 分钟无产出」变为 **1 秒拿到 70 条消息**，端到端日报 9 秒完成并双推飞书 + TG。
- **配置**：`config.json` 已切为 `dce_backend: "api"`；想用回 DCE 改回 `"local"` 即可（仍有 API 兜底）。

## 2026-10-26 — 开机自启缺失修复

- **问题**：电脑重启后 TG 监听未自动起来 —— 启动文件夹（`shell:startup`）实际是空的，此前并未真正放入自启项。
- **修复**：新增 `DTAO_Listener_Autostart.bat` 到启动文件夹，开机/登录后自动拉起 `DTAO_TG_Listener_Guard.bat`（带崩溃自愈：监听退出后 15 秒自动重启）。

## 2026-10-25 — 防御性加固：discord_tokens 空项自动忽略（绝不抽到空 token）

- **背景**：用户担心 `discord_tokens` 数组里混入空串（例如复制数组行却漏填 token）会被随机抽中，导致 Discord 导出失败。
- **修复**：`pick_discord_token()` 在构建抽取池时用 `[t for t in raw if t]` 过滤空/假值；并在原始项数与实际可用数不一致时打印一行日志：`discord_tokens 共 N 项，已忽略 M 个空/无效项，实际可用 K 个`，便于直观确认空项未被计入。
- **结论**：多个 token 随机模式下，抽取池只含非空 token，绝不会抽到空串导致导出失败；单值 `discord_token`（空串）仅作数组全空时的兜底，且同样校验非空，不会被并入数组计数。无破坏性改动，下次真导出日志会显示真实的「共 M 个（随机）」。

## 2026-10-25 — 文档修正：Discord user token 获取步骤补全英文界面标签

- **问题**：README 原「如何拿 Discord user token」步骤太简略，且未标注 F12 后英文开发者工具界面的按钮/标签名，用户在 `Network` 标签之后找不到下一步。
- **修复**：重写该小节，每一步带上英文标签（`Developer Tools` / `Network` / `Fetch/XHR` / `Preserve log` / `Headers` / `Request Headers` / `authorization`），并补充：必须用浏览器 discord.com（非桌面客户端）、列表为空时如何触发请求、以及备用的 `Console` 一行命令取法。

## 2026-10-25 — 飞书复制优化（标题日期与正文合并）

- **问题**：飞书卡片里标题/日期是独立文本块、且用 `hr` 分隔线与正文隔开；手机端长按复制时默认只选到正文块，标题和日期选不进去，日报失去「标题+日期」的标识意义。
- **修复**：`push_feishu()` 将「加粗标题行 + 日期行 + 正文」合并进**同一个 lark_md 文本块**（仅以换行分隔，去掉中间的 `hr`），分隔线只保留在正文与底部备注之间。现在长按复制会整段选中标题+日期+正文。
- 顶部蓝色 header 标题栏保留（纯视觉），不影响复制。

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

### 飞书复制优化（初版，已被下方合并修复取代）
- `push_feishu()` 卡片正文顶部新增**加粗标题行 + 日期行**；但初版标题/日期仍为独立文本块、且用 `hr` 与正文分隔，手机端长按复制仍只选到正文块。该问题已在同日「飞书复制优化（标题日期与正文合并）」中修复——现标题+日期+正文合并进同一文本块。

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
