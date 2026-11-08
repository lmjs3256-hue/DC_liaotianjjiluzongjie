# DTAO 频道日报小助手（小白使用说明）

> 自动把 Discord 某个频道（这里是 **DTAO 服务器的 price-talk**）的聊天记录导出来 →
> 用 AI 总结成一份手机端友好的中文日报 → 推送到 **飞书群** 和 **Telegram**。
> 可以手动触发，也可以直接在 Telegram 里发一句话就让它干活。

**当前版本：v1.5.5** ｜ 更新记录见 **[CHANGELOG.md](CHANGELOG.md)**
**当前模型：A·DeepSeek V4.1 + B·Kimi K3（对比模式，Engy / Bittensor SN53）** ｜ 换模型只改 `config.json`，见 [常见问题](#九常见问题排错)

## ✨ 核心特性

- 🤖 **AI 日报**：AI 把频道讨论总结成结构化日报，**4 个板块**：🔥 必读重点 / 👥 重要人物观点 / 📎 其他细节 / 💡 一句话总结（子网异动统一写进「必读重点」，不再重复列一遍）
- 🧠 **完整对话上下文**：不是只挑关键词句，而是保留每条实质消息**前后各 2 条上下文**，并按**时间顺序**喂给模型——「2t daily buyback 不值一提」能正确还原成「**SN61** 每天回购 2 个 TAO 不值一提」，而不是瞎猜成 SN2
- ❓ **待解疑问也会记录**：群里对某子网的点名、质疑、提问（如「117 团队是谁」）当天没人回答，也会作为「待解疑问」写进日报——当天的疑点本身就是信号
- 👤 **人名保真**：输入里带「当日发言者名单」，模型只能从中取名字、逐字照抄（含 `τFIG`、`kτwτ` 等特殊字符）；生成后还会自动核对，发现名单外的名字就在 `run.log` 告警
- 🧷 **禁止臆造（溯源规则）**：每条结论必须能在聊天原文找到出处；不确定就写「原文未提及」；「第一/最大」等排名必须落到具体 SN 编号，原文没指明就标注或舍弃；重大定性（跑路/转型等）以官方最新澄清为准，群友猜测不得当结论
- 🎯 **结论稳定**：`deepseek_temperature` 默认 **0**，同一份记录重复总结不会一天一个说法
- 🔌 **任意 OpenAI 兼容模型可换**：`deepseek_base_url` + `deepseek_model` + key 三处配置即可切换渠道；超时、输出上限、额外请求体（如推理模型关思考）都可配，不写死任何厂商
- ⏱ **时间断层可见**：输入里凡相隔 1 小时以上的两条消息之间会插入「~~~~ 时间跳转 ~~~~」标记，明确告诉模型「这两条无关」，防止把相隔数小时的两件事拼成因果（模型天生爱这么干）
- 🔍 **自动核验写日志**：生成后会核对人名是否都在当日发言者名单内（`✅ 人名核对通过`），并检查「人物↔子网编号」关联是否有原文依据，可疑处写入 `run.log` 供复核
- 🧱 **结构硬过滤**：日报/周报生成后强制剔除「重点子网动向」「潜力观察」等不应出现的板块，模型写歪了也推不出去
- ✍️ **TG 反馈重总结（/retry）**：日报有错或你有补充事实，直接在群里发 `/retry <你的反馈>`，AI 按你的反馈重写并重推，不用改代码、不用重导出
- 📏 **篇幅可控**：日报目标 **1100-1400 字**（手机两屏），按小节配额写（必读重点 5-7 条×50-70 字），生成后自动统计字数，超 1700 字会在日志告警
- 🗣 **大白话 + 中文优先**：只有 SN 编号、子网名、人名、代币名保留英文；emission/buyback/staking 等一律译成「排放/回购/质押」，残留英文术语由代码兜底替换
- 🔁 **两遍成稿（校对轮）**：第一遍出初稿，第二遍带着原文专门核对「数字记错人」「人物-子网归属靠碎片推出来」「原文没有的定性」，校对失败自动回退初稿（可用 `llm_verify_pass: false` 关掉）
- 📚 **子网知识库（每周校对）**：本地 `subnets.json` 存「SN 编号 ↔ 子网名 ↔ 当前业务」，数据来自链上子网身份（团队自己登记的那句描述），**只记最新业务、注销的单独归档、不清楚就写未知**；日报生成时只把当天提到的编号注入提示词（不整表塞），模型从此认得「Engy = SN53」「117 = everyframe.studio」
- 🆚 **双模型对比模式（可选）**：`compare_profiles` 填 2 个以上模型档案，同一天分别生成、分别推送，标题带 `A·DeepSeek` / `B·Kimi K3` 标签，底部署名自动跟随——用来一段时间内对比哪家更准
- 🏷 **底部署名跟随模型**：`🤖 由 Kimi K3 生成` 自动读 `deepseek_model`，换模型不用改代码
- 📝 **说人话、不啰嗦**：少用行话，「SOTA」这类缩写写成「中文（英文缩写）」；同一件事在两个小节重复时自动去重
- 📱 **手机端友好排版**：飞书卡片铺满屏宽，标题+日期与正文合并在**同一可复制文本块**（长按整段选中）
- ⏱ **每日导出节流（防封号）**：默认每天最多真实导出 **2 次**、间隔 **≥12 小时**；未达间隔/达上限时只复用或合并已有记录做「深刻总结」重推，不消耗 token（阈值可在 `config.json` 配置）
- 🎲 **多账号顺序轮换**：配置 `discord_tokens` 多个号，按 1 → 2 → 3 → 1 … 固定顺序轮流使用，每个号负载完全均匀、绝不连续两次用同一个（v1.4.2 起从「排除上次后随机」升级为确定性轮换，避免某号隔天又被抽中）；数组里若有空串会被自动忽略，绝不抽到空 token
- 💬 **Telegram 指令触发**：`/digest` `/weekly` `/repush` `/watch` `/retry` `/keyboard` `/help`，按钮栏按需唤出
- 🔓 **开放触发（可选白名单）**：默认任意用户可触发；填 `allowed_user_ids` 即切换为仅白名单
- 💬 **水聊侧记（可单独触发）**：`/水聊`（或 `pipeline.py chat`）只挑**被日报过滤掉的闲聊**——段子、梗、互怼、跑题话题——单独整理成一份 600-1000 字的轻量侧记并双推。**引语一律「英文原话 → 中文意思」双语呈现**，梗会拆开讲清「字面什么意思、群里实际指什么、谁接的梗怎么升级」。同样只读本地已有导出、不重新导出；政治/脏话/歧视内容在抽取阶段就过滤掉，引语会逐条回原文核对发言者（错挂人名会被定点纠正）
- 📆 **周报只汇总本地记录，绝不重新导出**：`/weekly` 不碰 Discord、不消耗导出配额，直接读 `exports/` 里最近 **7 天（含今天）**的导出 JSON；本地不足 7 天就**有几天算几天**。输入按日期分段、每条消息带「月-日 时:分」，模型无法再把不同日期的事拼成同一天；结尾有字数硬闸门（超 1600 字自动「只删不增」压缩，最多两轮），避免 Kimi K3 这类模型把周报写成 2800 字
- 🕘 **总结带前两天背景**：生成日报时会一并读取**前 2 天**（`context_days` 可调）的导出记录当作背景资料，跨天话题（今天聊的「那个子网」前天就开始讨论）不再被切断；背景只用于理解，**重点仍然只总结当天**，背景日事件不允许写成当天新闻（提示词规则 24 + 校对第 6 项双重约束）
- 🌏 **日报时间范围 = 北京时间自然日**：v1.5.0 前导出按 UTC 日界（北京 08:00 → 次日 08:00），现在精确对齐北京时间 00:00–24:00，日报里的时间也都换算成北京时间显示
- 🌗 **总结「昨天」导出到当下（v1.5.2）**：`/digest 昨日` 的导出范围是**昨日 00:00 → 现在**（跨零点补录今晨讨论）；下一天的消息自动打「次日」标记，日报里必须写「今晨」，不会和昨天的事件混写；「今日」导出不变（今日 00:00 → 现在）；更早的日期仍为自然日
- 🔍 **/retry 专题总结（v1.5.2）**：反馈里带「专题」二字即进入专题模式——不修正日报，而是从最近几天已有记录里**还原该专题的原文人物观点**并双推（不重新导出、不耗配额）。例：`/retry 总结SN127专题并推送，还原原文人物观点`
- 🛡 **监听单实例保护（v1.5.5）**：`tg_listener.py` 用 Windows 命名互斥体保证**全局只有一个监听**。以前开机自启 + 手动启动 + 守护脚本叠加，最多跑过 4 个进程，同一条 `/digest` 被各执行一遍 → **一天推了 3 条日报**，Telegram 还一直报 `409 Conflict`。现在新进程启动会**主动杀掉旧监听**（改完代码重启即生效，不用手工清进程），守护脚本也加了独占锁，无论双击多少次都只会有一个监听在跑
- 📊 **潜力观察独立成篇**：日报**不再**夹带「潜力观察」，改为单独生成落盘；只有你发 `/watch` 时才推送，日报保持精简
- 🚀 **内置 API 导出后端（默认）**：不依赖 DiscordChatExporter，直接调 Discord REST API 导出，秒级完成；若仍用 DCE，超时（120s）会自动回退到 API 后端兜底
- 🛡 **监听崩溃自愈 + 流水线不静默失败**：流水线在后台线程执行、异常全兜底；守护脚本在监听意外退出后 **15 秒自动重启**；流水线出错时会把完整堆栈写进 `run.log`，不再「失败得无声无息」
- 🧾 **导出元信息脚注**：日报末尾标注 `令牌：1/2 · 次数：1/2`，一眼看出本次用了第几个 token、池中共几个、当日还剩几次导出额度
- 🔧 **开机自启（带崩溃自愈）**：监听程序登录自动后台运行，随时收指令

---

## 📑 目录

1. [它能帮你做什么](#一它能帮你做什么)
2. [准备工作清单](#二你需要提前准备好这些清单)
3. [第一次安装](#三第一次安装照着做约-10-分钟)
4. [config.json 配置说明](#四configjson-每一项怎么填)
5. [怎么用（终端 / Telegram）](#五怎么用两种方式)
6. [子网知识库（每周校对）](#六子网知识库每周校对一次)
7. [开机自启（监听常驻）](#七让机器人随时待命开机自启)
8. [安全与合规](#八安全与合规)
9. [常见问题排错](#九常见问题排错)
10. [上传到 GitHub](#十上传到-github小白步骤)
11. [文件说明](#十一文件说明)
12. [部署到另一台常开电脑](#十二部署到另一台常开电脑解放你的主力机)

> 更完整的旧电脑 7×24 部署教程见 **[DEPLOY_HOMEPC.md](DEPLOY_HOMEPC.md)**。

---

## 一、它能帮你做什么

- 每天（或你想看的时候）把 price-talk 频道的讨论导出来
- 自动**过滤水聊**，只保留和 Bittensor 子网 / 重要成员相关的实质内容
- 用 AI 生成一份结构化日报（带重点、项目符号、手机端排版；模型可换，见第九节「换模型怎么改」）
- 同时推到飞书群机器人 + 你的 Telegram
- 飞书日报卡片正文顶部带**可复制的标题与日期**，方便你直接选中粘贴转发
- 在 Telegram 里发指令就能触发，**不用开电脑终端**

---

## 二、你需要提前准备好这些（清单）

| 东西 | 干嘛用 | 怎么弄 |
|---|---|---|
| **Windows 电脑** | 跑脚本 | Win10/11 均可 |
| **Python 3.10+** | 运行脚本 | 官网下载，安装时一定勾「Add Python to PATH」 |
| **Discord 账号** | 读频道 | 你是 DTAO 服务器成员、能看 price-talk 频道 |
| **大模型 API Key** | AI 总结 | 任选一家 OpenAI 兼容渠道：Engy（本项目在用，`kimi-k3`）、DeepSeek、Moonshot、通义、智谱等，见第九节对照表 |
| **飞书群 + 群机器人** | 收推送 | 飞书建群 → 群设置 → 群机器人 → 添加机器人，拿到 Webhook 地址和密钥 |
| **Telegram 机器人** | 发指令触发 | 在 @BotFather 创建（下面有步骤） |
| **代理软件（Clash 等）** | 连 Discord | 开着「系统代理」即可，脚本会自动用 |

> ⚠️ **重要风险提示（必读）**：用 Discord 个人账号的 user token 自动读频道，**违反 Discord 官方规则（TOS）**，有封号风险。
> 本项目只是个工具，风险由你自己承担。降低风险的办法见文末「安全与合规」。

---

## 三、第一次安装（照着做，约 10 分钟）

### 第 1 步：装 Python
1. 打开 https://www.python.org/downloads/ ，下载最新版。
2. 安装时**务必勾选「Add Python to PATH」**，然后点 Install Now。
3. 验证：按 `Win+R` → 输入 `cmd` → 回车，输入 `python --version`，能显示版本号就成功了。

### 第 2 步：（可选）下载 DiscordChatExporter
> **这一步可以不装**。项目默认用**内置 API 后端**（`config.json` 里 `dce_backend: "api"`）
> 直接调 Discord REST API 导出，秒级完成，不需要任何外部程序。
> 只有你想改用 DCE 导出时才需要下面这几步（不建议：DCE 是 .NET 程序、没有 `--proxy` 参数，
> 常卡在 `Resolving channel(s)...` 一步十几分钟不返回）。

1. 打开 https://github.com/Tyrrrz/DiscordChatExporter/releases
2. 下载 **DiscordChatExporter.Cli-win-x64.zip**（带 Cli 的那个，不是 GUI 版）
3. 解压，把里面的 **`DiscordChatExporter.Cli.exe`** 复制到本项目文件夹里
4. 在 `config.json` 里把 `"dce_backend"` 改成 `"local"`，并把 `dce_exe` 填成该 exe 的完整路径

### 第 3 步：准备配置文件
1. 把本项目的 `config.example.json` **复制一份**，改名为 `config.json`
2. 用记事本打开 `config.json`，按下一节填好那几处（主要是 token / key / webhook）
3. **`config.json` 含你的密码，切勿上传到 GitHub**（本项目 `.gitignore` 已帮你拦住）

### 第 4 步：装好就能跑了
在本项目文件夹里打开终端（地址栏输入 `cmd` 回车），试一条：
```bash
python pipeline.py
```
能正常导出并推送到飞书/TG，就说明装好了。

---

## 四、config.json 每一项怎么填

打开 `config.json`，只需要改下面标 ★ 的：

```jsonc
{
  "discord_token": "★你的 Discord user token",      // 见下方「如何拿 token」
  "discord_tokens": [                               // 可选：多个号随机切换；数组里空串会被自动忽略，不会抽到空
    "账号A的token", "账号B的token"                  // 想几个就写几个（≥2 才有随机意义）；数组为空时回退到上面的 discord_token
  ],
  "channel_id": "1341812134807343114",             // 频道 ID，一般不用改
  "guild_name": "DTAO",
  "channel_name": "price-talk",
  "output_dir": "exports",
  "dce_backend": "api",                            // 导出后端：api(推荐)|local|docker
  "dce_exe": "C:/.../DiscordChatExporter.Cli.exe",  // 仅 local 后端需要：第2步放的那个 exe 路径
  "api_fallback": true,                            // local/docker 超时或失败时，自动改用内置 api 后端兜底
  "dce_timeout": 120,                              // DCE 超时秒数（正常导出约 30s），超时即触发上面的兜底
  "deepseek_api_key": "★模型服务的 key",
  "deepseek_base_url": "https://api.deepseek.com/v1",  // 任何 OpenAI 兼容网关都行（见下方「换模型」）
  "deepseek_model": "deepseek-chat",
  "deepseek_temperature": 0,                       // 0=结论最稳定（推荐）；调高会更发散，同一份记录可能给出不同说法
  "deepseek_max_tokens": 5000,                     // 输出上限；推理模型要留足思考空间，否则正文可能为空
  "deepseek_timeout": 120,                         // 单次请求超时秒数；推理模型建议 180-300
  "deepseek_extra_params": {},                     // 额外请求体，原样合并；如 {"thinking":{"type":"disabled"}} 关思考
  "context_days": 2,                               // 总结某天时一并读取其前 N 天导出记录当背景（0=关闭）
  "weekly_days": 7,                                // 周报覆盖最近 N 天（含今天）；本地不足 N 天就有几天算几天
  "weekly_max_items": 500,                         // 周报送给模型的实质消息总预算，按天均分（每天最少 30 条）
  "topic_max_items_per_day": 250,                  // /retry 专题总结：单日最多喂给模型的实质消息条数
  "chat_max_items": 500,                           // /水聊 侧记：最多喂给模型的闲聊消息条数（按天均分，每天最少 60 条）
  "llm_verify_pass": true,                         // 两遍成稿：第二遍带原文校对（数字归属/人物-子网归属/残留英文），失败自动回退初稿
  "compare_profiles": [],                          // 双模型对比模式：填 2 个以上模型档案，日报会分别生成、分别推送（见下方「对比模式」）
  "llm_direct": true,                              // 国内大模型 API 直连、不走代理（默认开，避免被 Clash 节点掐断）
  "no_proxy_hosts": [],                            // 追加要直连的域名，如自建的 OpenAI 兼容网关
  "proxy": "",                                     // 留空=自动用系统代理(Clash)
  "feishu_webhook_url": "★飞书机器人 Webhook 地址",
  "feishu_secret": "★飞书机器人密钥(开启签名校验才需要)",
  "telegram_bot_token": "★TG 机器人 token",
  "telegram_chat_id": "★TG 群/私聊 ID",
  "allowed_user_ids": [],                          // 留空=任何人可触发(开放模式); 填ID数组=仅白名单可触发(见安全)
  "timezone": "Asia/Shanghai",
  "push_enabled": true
}
```

### 双模型对比模式（compare_profiles）

想比较两个模型谁总结得更准，把 `compare_profiles` 填成 2 个以上档案：同一天的日报会用每个模型**各生成一版、分别推送**，
标题带 `｜ A·DeepSeek V4.1` 这样的标签，底部署名也跟着变，日志按版本前缀分行记录，方便逐条对照。

```jsonc
"compare_profiles": [
  { "label": "A·DeepSeek V4.1", "base_url": "https://api.engy.ai/v1", "model": "deepseek-v4.1-flash" },
  { "label": "B·Kimi K3", "base_url": "https://api.engy.ai/v1", "model": "kimi-k3",
    "extra_params": { "thinking": { "type": "disabled" } } }
]
```

- 档案字段：`label`（展示名）、`base_url`、`model`、`api_key`（留空则复用主配置的 key）、
  `extra_params`、`temperature`、`max_tokens`、`timeout`——没写的都继承主配置。
- 对比只变**模型**这一个变量：同一份聊天记录、同一套提示词、同样开校对轮。
- 花费约为单模型的 2 倍（每个模型都跑初稿 + 校对两遍）；某一版生成失败只少一条推送，不影响另一版。
- 想停掉对比：把数组清空成 `[]` 即恢复单模型。`/retry`、`/weekly` 仍只跑主配置那一个模型。
- 落盘：`exports/dtao_<日期>.digest.A.txt`、`.B.txt`（第一版同时写标准名 `dtao_<日期>.digest.txt`，供 `/repush` 用）。

### 如何拿 Discord user token（⚠️ 仅自用，风险自担）

> 必须用**浏览器**打开 https://discord.com （不是桌面客户端），才能用 F12。界面是英文的，下面每一步都标了英文按钮 / 标签名，照着点即可。

**方法一：Network（请求列表）里抓 authorization（最常用）**
1. 浏览器登录 Discord 网页版，进入你要导出的频道（price-talk）。
2. 按 `F12` 打开 **Developer Tools（开发者工具）** 面板（英文机顶部就是这些英文标签）。
3. 点面板顶部的 **`Network`** 标签（有些中文机显示「网络」，英文机就是 `Network`）。
4. 在 Network 面板**左上方过滤栏**点 **`Fetch/XHR`**（只显示接口请求，过滤掉图片/css，更好找）。
   - 若列表是空的：先勾上 **`Preserve log`**（保留日志），然后在频道里点一下 / 发条消息 / 滚动一下，或刷新页面，列表就会出现请求。
5. 在请求列表里找一个 **Name 以 `api` 开头、或 URL 含 `discord.com/api/v9`** 的请求（例如 `messages?limit=14`、`@me`、`guilds`、`badges`——名字常被截短，认得是接口请求即可）。
   - ⚠️ **不要选带红色错误图标的请求**（如 `saved-messages?...` 那种失败请求），它的标头可能不完整；选一个**正常（无红色图标）**的接口请求。
6. **点中那条请求**，右侧弹出详情面板，点 **`Headers`** 标签。
7. 在 **`Request Headers`**（请求标头）区域往下找，有一行叫 **`authorization`**（全小写）——它右边的值（一长串，形如 `MTk4...`；**你的是不带 `Bot ` 前缀的普通用户 token**）就是你的 user token。
   - ⚠️ Headers 面板里**有两块**：顶部是 `Request Headers`（请求标头，token 在这里），底部是 `Response Headers`（响应标头，含 `Server: cloudflare` 等，**不是** token）。若只看到 `Server: cloudflare` 那堆，说明看的是响应标头，**往上滚动**到面板顶部找 `Request Headers` 即可。
8. **连引号一起复制**整个 `authorization` 的值，粘到 `config.json` 的 `discord_token` 里，别漏字符。

**方法二（备用）：Console 一行命令**
> 若方法一在 Network 里找不到，可在 **`Console`** 标签粘贴下面命令回车，会直接把 token 打印出来（复制输出即可）：
> ```js
> webpackJsonp.push([[''],{},e=>{m=[];for(let c in e.c)m.push(e.c[c])}]);
> m.find(m=>m?.exports?.default?.getToken!==void 0).exports.default.getToken()
> ```
> ⚠️ 该命令依赖 Discord 前端打包结构，偶尔会随版本失效；优先用方法一。

**重要注意**
- 一定要在**浏览器** discord.com 操作；桌面客户端按 F12 打不开网页开发者工具。
- token 拿到后尽快用、不要外泄；user token 违反 Discord TOS，封号风险自担（见第八节）。

> 更安全的做法见文末：用 bot token（需服务器管理员把 bot 拉进群）。

### 如何拿 Telegram 机器人
1. Telegram 里搜索 **@BotFather**，发 `/newbot`，按提示起名
2. 它会给你一个 **bot token**（形如 `123456:ABCdef...`），填到 `telegram_bot_token`
3. 把机器人拉进你要收推送的群；在群里发任意一条消息
4. 打开 @RawDataBot 或 @GetIDsBot，转发/查看那条消息，拿到群的 **chat_id**（负数，如 `-5111149587`）填到 `telegram_chat_id`
5. 默认**任何人**都能触发（开放模式）；想只让指定人触发：填 `allowed_user_ids`（TG 数字 ID，@GetIDsBot 可查）
6. （可选）在 @BotFather 发 `/setcommands`，选你的机器人，粘贴下面这段，让输入 `/` 时弹出指令菜单：
   ```
   digest - 导出前一天并双推（可跟日期）
   today - 导出并总结今天的聊天记录
   weekly - 生成本周周报
   water - 水聊侧记（只挑闲聊，可跟日期/天数）
   watch - 查看潜力观察/跨日热点(可跟天数, 如 /watch 7)
   repush - 补推已生成的日报(可跟日期与 tg/feishu)
   retry - 按你的反馈重总结（/retry 反馈内容）
   help - 显示帮助
   ```

### 飞书机器人
1. 飞书群 → 设置 → 群机器人 → 添加机器人
2. 复制 **Webhook 地址** → `feishu_webhook_url`
3. 如果开了「签名校验」，把**密钥**填到 `feishu_secret`；没开签名校验则留空

---

## 五、怎么用（两种方式）

### 方式 A：在终端里手动跑
```bash
python pipeline.py                        # 导出「昨天」并双推飞书+TG
python pipeline.py daily 2026-10-23       # 导出指定某一天（⚠️ 日期前必须写 daily）
python pipeline.py daily 昨日              # 「昨日/昨天/最新」等中文别名也认
python pipeline.py weekly                 # 生成本周周报
python pipeline.py chat                   # 水聊侧记（默认最近一次导出的日期，不重新导出）
python pipeline.py chat 2026-11-06        # 指定日期的水聊侧记
python pipeline.py chat 昨日 3            # 连续 3 天的水聊侧记
python pipeline.py repush 2026-10-23      # 只把已生成的日报重推一遍(不重导)
python pipeline.py repush 2026-10-23 tg   # 只重推 TG
python pipeline.py resummarize 2026-10-23 "orange 说的第一涨幅是 SN27"  # 按反馈重总结(不重导)
python pipeline.py --version              # 查看当前版本号

# ⚠️ 注意：第一个参数只能是 daily / weekly / repush / resummarize / chat 之一。
# 直接写 python pipeline.py 2026-10-23 会报 argument mode: invalid choice。
```

### 方式 B：在 Telegram 里发一句话（最方便）
先让监听程序跑起来（见第七节），然后在你的 TG 群里发：

| 指令 | 作用 |
|---|---|
| `/digest` | 导出昨天并双推 |
| `/digest 2026-10-23` | 导出指定日 |
| `/today` | **导出并总结今天的聊天记录**（北京时间今天 00:00 → 现在；占用当日导出配额） |
| `/weekly` | 周报 |
| `/水聊`（`/water`） | **水聊侧记**：只挑被日报过滤掉的闲聊（段子/梗/互怼/跑题），单独生成一条轻量总结并双推；只读已有导出，不重新导出 |
| `/水聊 昨日` | 指定日期；`/水聊 昨日 3` = 连续 3 天（均不重新导出） |
| `/repush` | **补推最近一次日报（不用输日期）** |
| `/repush 2026-10-23` | 补推指定日 |
| `/repush 2026-10-23 tg` | 只补推 TG |
| `/watch` | 查看「潜力观察」（默认最新一天） |
| `/watch 7` | 近 7 天潜力标的**跨日热点汇总**（提前布局参考） |
| `/watch all` | 全部日期汇总 |
| `/retry <反馈>` | **按你的反馈重总结最近一次日报**并重推（如 `/retry SN34 是转型不是跑路`） |
| `/retry 总结SN127专题并推送` | 反馈带「**专题**」= 专题总结模式：从最近几天已有记录还原该专题原文人物观点并双推 |
| `/retry 2026-10-29 <反馈>` | 对指定日期按反馈重总结 |
| `/help` | 显示帮助 |

发完 bot 会先回「⏳ 开始执行」；日报由流水线自动推送到飞书+TG，**跑完后 bot 只回一句「✅ 已完成」（含当日消息条数），不再刷运行日志**；仅当失败时才回传错误日志便于排查。

#### 📊 潜力观察（独立成篇，只在 /watch 时推送）

**日报正文里不再包含「潜力观察」**，日报只保留 4 个板块（必读重点 / 重要人物观点 / 其他细节 / 一句话总结），手机上更好读。

每次跑 `/digest` 或 `/weekly`，脚本会**额外单独**生成一份「潜力观察」（含标的 → 预期 → 催化剂与时间窗 → 逻辑 → 确定性），**只落盘、不推送**，存到 `exports/` 下：
- `watchlist_YYYY-MM-DD.txt`：单日潜力观察
- `watchlist_all.md`：所有日期累积（跨日对比用，文件末尾追加）

想看了再在 TG 里发 `/watch` 调阅：
- `/watch`（无参数）= 看最新一天；
- `/watch 7` = 把最近 7 天的潜力标的按「被提及天数」排序，标出**跨日高频、值得提前埋伏**的子网/人物（如 `• SN24：3 天被提及`）；
- `/watch all` = 全部汇总。

> 这样设计的好处：日报干净、不掺「还没影的猜测」；布局线索单独成篇，想深挖时再调。

**更懒人**：发 `/keyboard` 可在聊天底部唤出一排**按钮**
`📅 导出昨日(/digest 昨日)` / `🔁 补推最新(/repush 最新)` / `📊 近7天热点(/watch 7)` / `📆 周报(/weekly)`，
**点一下就发指令，完全不用打字**。按钮栏不常驻——bot 每次回复后自动收起（v1.5.3 起），
想收起时也可以发 `/收起`（或 `/hidekeyboard`）立刻收掉。

> 群里若挂了多个机器人，指令可能要写成 `/repush@你的机器人名 2026-10-23`，脚本已兼容。

---

## 六、子网知识库（每周校对一次）

模型本身不知道「Engy 就是 SN53」，聊天里只写名字时很容易张冠李戴（把 Engy 当新项目、把 117 认成别的团队）。
所以项目带了一份本地对照表，相当于给模型的「常识底本」：

| 文件 | 内容 |
|---|---|
| `subnets.json` | 每个编号：`name`（链上当前名称）/ `desc`（链上原始描述）/ `zh`（中文一句话业务）/ `status` / `first_seen` / `aliases` |
| `subnets.md` | 同样内容的表格版，人看的（SN、子网名、业务） |
| `subnets_changelog.md` | 每次更新的变更记录（新增 / 改名 / 注销 / 业务变更，带日期） |

**数据从哪来**：`taostats.io/subnets` 页面内嵌的链上子网身份 JSON（团队自己在链上登记的那句话），
一次能拿到全部活跃子网的名称 + 当前描述。不爬第三方介绍文章，也不用模型记忆补。

**四条硬规则（都落在代码里，不靠模型自觉）**
1. **只记最新业务**：描述取链上「现在」这句话，换赛道前的旧业务不会进表。
2. **注销单独归档**：被注销的编号移进 `deregistered`，保留旧名——所以「103 capcom」这类历史称呼还能认出来，
   同时也会提醒模型「该编号可能已换项目」（SN103 现在是 Terza Inference）。
3. **不清楚就写「未知」**：描述为空、或看不出在做什么的（如 SN82），中文标签直接写未知，提示词禁止模型补全。
4. **只做增量**：每次运行只改「新增 / 注销 / 改名 / 业务描述变化」四类变量，其余沿用旧记录，不重算整表。

**怎么更新**

```bash
python update_subnets.py            # 拉最新 + 只给新增/描述变化的条目翻译中文标签（旧的走缓存）
python update_subnets.py --dry-run  # 只看差异，不写文件
python update_subnets.py --no-llm   # 不调模型，中文标签留空（退回英文描述）
```

一次运行约 4 次小模型调用、十几秒；连跑两次第二次会输出「与上次一致，无变化」，说明 diff 是幂等的。

**怎么喂给日报**：不是把整表塞进提示词。生成时只挑**当天消息里提到的编号**（显式 `SNxx`、1–256 的裸数字、
以及只写名字的「Engy」反查编号），**最多 45 条**，并附硬约束：

> 对照表只用来把编号和名字对上；不许把业务描述当日新闻写；标「未知」的不许补全；
> 与聊天原文冲突时以原文为准，并注明「该编号可能已换项目」。

> 💡 **第一次使用**：仓库里的 `subnets.json` 可直接用（随代码版本化）；想拿最新数据就跑一次
> `python update_subnets.py`。该文件缺失时会自动跳过注入，日报照常生成，只是没有对照表。
> 想每周自动校对：在系统里建个计划任务，或在 WorkBuddy 里让它每周一跑一次上面那条命令即可。

---

## 七、让机器人随时待命（开机自启）

默认脚本不会自己跑，需要有一个「监听程序」在后台常驻，才能随时收你的 TG 指令。

**最简单：开机自启（推荐，无需管理员）**
- 把 `DTAO_Listener_Autostart.bat` 放进「启动」文件夹（路径见下）。它负责拉起项目里的
  `DTAO_TG_Listener_Guard.bat` —— **守护版**，监听崩溃或退出后 **15 秒自动重启**。
  - 启动文件夹路径：`C:/Users/你的用户名/AppData/Roaming/Microsoft/Windows/Start Menu/Startup/`
    （`Win+R` → 输入 `shell:startup` → 回车即可直接打开）
  - ⚠️ 别把 `DTAO_TG_Listener_Guard.bat` 直接拷进启动文件夹：它用 `%~dp0` 定位
    `tg_listener.py`，拷过去会找不到程序、自启失效。用 `DTAO_Listener_Autostart.bat`
    （内部写死了项目路径）就没这个问题。
- 放好后，**下次开机 / 登录 Windows 会自动运行**监听，窗口最小化、不碍事（别关那个黑色小窗）。
- 想现在立刻启动：双击项目里的 `DTAO_TG_Listener_Guard.bat`。

**手动临时跑（看得到窗口）**
双击 `start_tg_listener.bat` 即可，关掉窗口就停。

**可选增强：任务计划程序（崩溃自动重启，需管理员）**
见 [DEPLOY_HOMEPC.md](DEPLOY_HOMEPC.md) 第五节「方案 2」。需**以管理员身份**运行
`install_task_login.ps1`，且启用后要把启动文件夹的快捷方式删掉，避免两个监听抢消息。

> 注意：
> - 同一时间**只跑一个**监听，否则两个会抢消息、指令时灵时不灵
> - 监听运行期间，保持 **Clash 系统代理开着**、电脑不休眠，才能连上 Discord/TG
> - 排错看 `tg_listener.log` 和 `run.log` 两个日志文件

---

## 八、安全与合规

1. **user token 封号风险**：个人 token 自动读频道违反 Discord TOS。
   - 降低风险：配 `discord_tokens` 多个号轮换（按顺序轮流使用；数组里若有空串会被自动忽略，不会抽到空）；或让每个 token 走不同代理 IP。
   - 根治办法：**用 bot token**。在 @BotFather 建 bot，请 DTAO 服务器管理员把 bot 拉进群并给「读取消息历史」权限，然后把 bot token 填进 `discord_token` 即可——这是 Discord 允许的方式，零封号风险。
   - **节流保护（已内置，参数可配置）**：`/digest` 每天最多真正导出指定次数，且两次需间隔指定时长；未达间隔时再触发只复用/合并已有导出记录做「深刻总结」重新推送（不消耗 token）；当日已达上限后无论怎么触发都只合并记录重推。默认值：每天最多 **2 次**、间隔 **≥12 小时**。想调整就在 `config.json` 加两行：
     ```json
     "max_exports_per_day": 2,            // 每日最多真实导出次数（≥1）
     "min_export_interval_hours": 12      // 两次导出的最小间隔（小时，≥0）
     ```
     单日 user token 实际导出 ≤上限次数，大幅降低风控概率。
2. **触发权限（开放模式）**：默认**任意会话、任意用户**发来的指令都会被处理（已取消「仅本人可触发」）。
   每日真实导出仍受 pipeline 内置节流保护（默认每天最多 2 次、间隔 ≥12h），所以即使多人反复触发，单日 user token 实际导出次数也被锁死，不会敞开口消耗。
   - 想恢复「仅指定人可触发」：在 `config.json` 填 `allowed_user_ids: ["允许的TG数字ID", ...]` 即切换为白名单模式（不填/为空则开放）。
3. **密钥别泄露**：`config.json` 含所有密码，**不要上传 GitHub**（本仓库 `.gitignore` 已忽略它）。

---

## 九、常见问题（排错）

**Q：我在 TG 发了指令，机器人没反应？**
A：监听程序没在跑。按第七节启动 `start_tg_listener.bat` 或重启电脑（开机自启）。
   也可看 `tg_listener.log` 有没有「TG 指令监听已启动」这一行。

**Q：提示连不上 Discord / Telegram（502 / 超时）？**
A：确认 Clash（或你的代理）「系统代理」是开着的；`proxy` 留空即可自动继承。

**Q：日报一直出不来，run.log 停在「检测到系统代理 / 导出命令」后再无动静？**
A：这是 DiscordChatExporter（.NET 程序，无 `--proxy` 参数）卡在 `Resolving channel(s)...` 一步，
   表现为挂起十几分钟不返回、也不报错，跟 token 无关。解决办法：把 `config.json` 里的
   `"dce_backend"` 改成 `"api"`，改用内置的 API 导出后端（直接调 Discord REST API，秒级完成，
   产出格式与 DCE 完全一致）。保持 `"api_fallback": true` 时，即使仍用 DCE，超时
   （默认 120 秒）也会自动切到 API 后端兜底。

**Q：日报末尾的「令牌：3/3」连续两天都是第 3 个，是不是写死了？**
A：不是。旧版是纯随机（`random.randrange`），连中同一个号纯属概率（3 天连中同一个的概率约 1/9）。
   v1.3.2 加了防连用，v1.4.2 起改为**顺序轮换**：每次真实导出把用过的令牌编号记进
   `exports/_digest_state.json`，下次固定用它的下一个号（环形，池里只有 1 个号时永远用它），
   因此既**绝不会连续两次用同一个号**，长期看每个号的使用次数也完全均匀。
   日志里能看到「令牌轮换：上次真实导出用的是第 N 个，本次按顺序用第 M 个」。

**Q：日报末尾的「令牌：1/2 · 次数：1/2」是什么意思？**
A：这是导出元信息脚注，帮你确认多 token 轮换和节流配额是否按预期工作：
   - `令牌 1/2` = 本次用的是第 1 个 token，`discord_tokens` 池里**共 2 个**（顺序轮换生效）。
     若显示 `令牌 -/2`，说明本次是复用已有记录重总结、**没有真实导出**，因此不归属任何 token。
   - `次数 1/2` = 该自然日**已累计导出 1 次**，每日上限 **2 次**（`max_exports_per_day`）。
     改配置里的上限，这里的分母会跟着变。

**Q：发了 `/digest`，bot 回「❌ 失败」，但 `run.log` 里一个字都没有？**
A：这是 **v1.0.0 之前**的已知缺陷，已修复。原因：`pipeline.py` 的日志函数里 `print()` 没有保护，
在中文 Windows 下 stdout 是 GBK 编码，日志中一旦出现 `↳`、`❌`、`✅` 这类 GBK 编不了的符号就会抛出
`UnicodeEncodeError`，把整个进程带走、连兜底日志都写不进去（表现为「跑了几秒就没了、日志停在半截」）。
v1.0.0 起：启动时强制 UTF-8 输出 + 日志打印加保护 + 顶层捕获 `BaseException` 并记录完整堆栈。
如果你在 v1.0.0 之后仍看到失败，`run.log` 里现在一定有 `❌ 流水线失败` 及其堆栈。

**Q：TG 里收到的错误提示是 `�ձ�ʧ�ܣ...` 这种乱码？**
A：同样是编码问题（子进程按 GBK 输出、监听按 UTF-8 解码），v1.0.0 已修复：子进程强制 UTF-8 输出，
回传前还会清洗掉异常字符。旧版本遇到时重启一次监听即可。

**Q：日报内容和聊天原文对不上，或者同一份记录两次总结说法不一样？**
A：两种办法：① `deepseek_temperature` 设为 `0`（默认已是 0），避免同数据不同结论；
② 直接在 TG 发 `/retry <你的反馈>`，例如 `/retry 61 那笔是 τFIG 说 CATS 卖了，不是 CATS 自己承认的`，
AI 会按你的补充重写并重推，不会把你的话当成群内事实。

**Q：模型还是把子网名和编号对不上，或者压根不认识某个新子网？**
A：三步排查：① 打开 `subnets.md` 搜一下这个编号在不在表里——不在就是知识库过期了，
   跑一次 `python update_subnets.py`；② 在表里但 `zh` 是「未知」，说明链上那句描述本身看不出业务，
   提示词里已禁止模型补全，日报只会写编号和名字；③ 编号还在、名字对不上，多半是**编号被重新注册**
   （如 103 现在叫 Terza Inference，聊天里仍有人喊旧名 capcom），此时日报会写「…（该编号可能已换项目）」。
   遇到具体某条写错，直接 `/retry <指出哪句错>` 让它按原文重写。

**Q：`subnets.json` 里会不会有敏感信息？能一起传 GitHub 吗？**
A：里面只有公开的链上子网身份（名称、描述、官网/GitHub 链接）和代码生成的中文标签，
不含任何 token、密钥、频道 ID，所以随仓库一起版本化，换机器直接可用。

**Q：周报是怎么生成和推送的？会不会重新导出聊天记录？**
A：不会重新导出，也不消耗 Discord 导出配额。`/weekly` 只做三件事：
1. 从 `exports/` 里取**最近 7 天（含今天）**已有的导出 JSON（`dtao_<日期>.1.json`、`.2.json` 都认，
   同一天导出过多次会按「作者+内容」去重合并）；本地不足 7 天就**有几天算几天**，一天都没有才报错；
2. 按日期分段拼成输入，每条消息带「月-日 时:分」和当天发言者白名单，再附子网对照表；
3. 生成 → 结构清洗 → 带原文校对 → **字数硬闸门**（超 1600 字自动「只删不增」压缩）→ 飞书 + TG 双推。
落盘留档在 `exports/dtao_week_<起>_<止>.digest.txt`，方便事后核对。
想改覆盖天数或喂给模型的条数，调 `weekly_days` / `weekly_max_items` 即可。

**Q：开了对比模式，`/retry` 和 `/weekly` 会用哪个模型？**
A：只用 `config.json` 里的**主配置**（`deepseek_base_url` / `deepseek_model`），也就是第一个档案，
   因为重总结/周报是纠错和补充，不适合一次刷两版。想对照某次 `/retry` 的效果，把想比的模型设成主配置再触发。

**Q：对比模式会不会让导出变多、更容易被风控？**
A：不会。两个模型**共用同一次导出结果**，只是把同一份聊天记录分别喂给两家模型，Discord 侧配额和令牌轮换完全不变；
   多出来的只是大模型调用费用（约为单模型的 2 倍）。

**Q：总结用的是哪个模型？可以换吗？**
A：当前默认接 **Engy（Bittensor SN53）上的 `kimi-k3`**，并关闭了思考模式。日报底部那行 `🤖 由 xxx 生成` 会自动跟着 `deepseek_model` 变，看到什么名字就是什么模型在跑。

**换模型只要改 `config.json` 三个字段**（不用改代码、不用重启监听——监听每次触发都会起新的子进程读最新配置）：

```jsonc
"deepseek_base_url": "https://api.engy.ai/v1",   // ① 服务商地址（OpenAI 兼容的 /v1）
"deepseek_model": "kimi-k3",                     // ② 模型名（服务商文档里的名字，照抄）
"deepseek_api_key": "sk-...",                    // ③ 该服务商的 key
```

配套的两个可选字段：`deepseek_timeout`（推理模型给 180-300 秒）、`deepseek_extra_params`（关思考等厂商专属参数）。
改完先跑 `python pipeline.py daily <某天>` 用历史数据对比，别在当天生产数据上试。

| 渠道 | `deepseek_base_url` | `deepseek_model` | 备注 |
|---|---|---|---|
| **Engy / Kimi K3（当前）** | `https://api.engy.ai/v1` | `kimi-k3` | SN53 验证推理网络，$1.95/$9.75 每百万 token；**需在 `deepseek_extra_params` 里关思考**，否则可能思考到没正文 |
| DeepSeek（最便宜） | `https://api.deepseek.com/v1` | `deepseek-chat` | 国内直连，无需代理 |
| DeepSeek 强推理 | 同上 | `deepseek-reasoner` | 更慢更贵；推理模型对温度不敏感，结构可能更啰嗦 |
| Moonshot 官方 Kimi | `https://api.moonshot.cn/v1` | `kimi-k3` | ¥20/¥100 每百万 token，比 Engy 贵，胜在稳定 |
| 通义千问（稳、便宜） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` / `qwen-max` | 阿里云百炼，稳定性好 |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` | 结构化输出较听话 |

> 具体模型名请以各平台文档为准（模型迭代快，名字会变）。只要接口是 OpenAI 兼容的 `/chat/completions`，
> 本项目都能直接用；`llm_direct` 白名单里已包含国内域名，境外渠道（如 Engy）会走系统代理。
> 换完模型建议先跑 `python pipeline.py daily <某天>` 用历史数据对比，别直接在生产日上看效果。

**Q：为什么「思考模式」要关掉？**
推理模型（Kimi K3、deepseek-reasoner 等）会先输出一段思考，再写正文，**思考也计入 `max_tokens`、也按输出价计费**。
实测同一份日报数据：开着思考时消耗 1.6 万~2 万 token（其中思考占 92%），耗时 65~116 秒，且**多次出现思考吃满预算、正文为空**；
关掉后是 1200~1400 token、约 10 秒，结构更听话。所以：

```jsonc
"deepseek_extra_params": {"thinking": {"type": "disabled"}}   // Kimi K3
```

如果哪天换成别的推理模型，对应的关思考参数可能是 `reasoning_effort: "low"` 或 `enable_thinking: false`，
用 `deepseek_extra_params` 试即可（不同厂商参数名不同，试错成本很低，一次调用几分钱）。

但先提醒一句：**多数「漏信息 / 说错」并不是模型能力问题**，而是喂进去的数据被过滤掉或顺序打乱了
（v1.0.1 已修：裸数字子网号不再被丢、保留上下文、按时间排序）。先确认输入完整，再考虑换模型。

**Q：`/retry` 之后日报反而变差，还多出「重点子网动向」「潜力观察」两个板块？**
A：这是 v1.0.2 修掉的问题。原因是监听是常驻进程，`import pipeline` 时就把当时的代码固化在内存里，
早期 `/retry` 在监听**进程内**调用重总结函数，用的是启动那一刻的旧提示词和旧过滤规则
（现象：消息量从 156 条掉回 69 条，多出早已删除的板块）。
现在 `/retry` 改为启动**独立子进程**执行（`pipeline.py resummarize 日期 反馈`），永远用磁盘上的最新代码；
另外日报/周报输出都加了一层结构硬过滤，模型就算硬写「潜力观察」也会被剔除。
**升级到 v1.0.2 后需要重启一次监听**，`/retry` 才会走新路径。

**Q：日报里出现的人名，我在 Discord 里搜不到？**
A：日报里的人名全部取自消息原始 `author` 字段（服务器昵称），不改写、不音译，生成后还会自动与
「当日发言者名单」核对（全部匹配时 `run.log` 会打印 `✅ 人名核对通过`）。
另外 Discord 搜索框默认搜的是**消息内容**而不是成员列表，所以直接敲名字搜不到人很正常。
想核实某个人，可以把日期+名字发出来，按 user ID 定位最快
（例：2026-10-30 的 `BROdin` = user id `154700982796812299`，当天发言 20 条）。

**Q：飞书推送报 19021？**
A：飞书签名校验没对上。要么 `feishu_secret` 填错，要么机器人没开签名校验却填了 secret。
   核对飞书机器人后台的密钥，或关掉签名校验并把 `feishu_secret` 留空。

**Q：想改 AI 模型 / 频道 / 推送时间？**
A：都在 `config.json` 里。`deepseek_model` 改模型名；`channel_id` 改频道。

**Q：上传 GitHub 会不会泄露我的 token？**
A：不会。`config.json`、日志、`exports/`、DiscordChatExporter 程序都已在 `.gitignore` 忽略，
   仓库里只有 `config.example.json`（全是占位符）和代码。

---

## 十、上传到 GitHub（小白步骤）

> 本项目**已经推送到 GitHub**：https://github.com/lmjs3256-hue/DC_liaotianjjiluzongjie.git
> 下面步骤供**参考**：若你在本机重新 `git init` 想自建仓库、或想教别人上传，按此操作即可。

1. 在 GitHub 上 **New repository** 新建一个仓库（例如 `DC_liaotianjjiluzongjie`），**不要**勾选「Initialize with README / LICENSE」（避免和本地历史冲突导致 rejected）。
2. 在本项目文件夹打开终端，执行（把地址换成你自己的仓库）：
   ```bash
   git remote add origin https://github.com/lmjs3256-hue/DC_liaotianjjiluzongjie.git
   git push -u origin main
   ```
   - 若提示 `remote origin already exists`（远程已添加过），直接 `git push -u origin main` 即可。
   - 若提示 `failed to push` / `rejected` / `non-fast-forward`，先拉取再推：
     ```bash
     git pull origin main --allow-unrelated-histories
     git push -u origin main
     ```
   - 推送时若要求输入密码，请用 **GitHub Personal Access Token**（不是账号密码）。
3. 因为 `.gitignore` 已拦住 `config.json`，推送时**不会带上你的真实 token**。
4. 别人克隆后，只需自己复制 `config.example.json` → `config.json` 填好即可用。

---

## 十一、文件说明

| 文件 | 作用 |
|---|---|
| `pipeline.py` | 主程序：导出 → 过滤 → AI 总结 → 推送 |
| `tg_listener.py` | Telegram 指令监听（发消息触发日报） |
| `config.example.json` | 配置模板（复制到 `config.json` 再填） |
| `CHANGELOG.md` | 版本 / 变动记录 |
| `update_subnets.py` | 子网知识库更新器（每周跑一次，只做增量校对） |
| `subnets.json` / `subnets.md` | 子网对照表（数据 / 人看的表格，随仓库一起版本化） |
| `subnets_changelog.md` | 子网变动记录（新增/改名/注销/业务变更，带日期） |
| `config.json` | 你的真实配置（含密钥，**不入库**） |
| `start_tg_listener.bat` | 手动启动监听（带窗口） |
| `DTAO_TG_Listener.bat` | 开机自启版（放启动文件夹，无窗口） |
| `DTAO_TG_Listener_Guard.bat` | 守护版启动脚本：监听崩溃/退出后 **15 秒自动重启**（带最小化窗口，可看崩溃报错） |
| `DTAO_Listener_Autostart.bat` | 放「启动」文件夹用的自启引导（内部写死项目路径，开机自动拉起上面的守护脚本） |
| `install_task_login.ps1` | 注册任务计划程序自启（更稳，失败自动重启；**需以管理员身份运行**） |
| `DEPLOY_HOMEPC.md` | 部署到另一台常开 Windows 电脑的完整教程 |
| `exports/` | 导出的聊天记录和生成的日报（**不入库**） |
| `exports/watchlist_*.txt` `exports/watchlist_all.md` | 自动沉淀的潜力股跟踪文件（**不入库**） |
| `run.log` / `tg_listener.log` | 运行日志（**不入库**） |

---

## 十二、部署到另一台常开电脑（解放你的主力机）

不想让日常用的电脑一直开着、挂着 Clash？可以把整套程序搬到一台**不关机的旧电脑 / 闲置笔记本**上 7×24 跑，
Telegram 指令随时能触发，主力机关机外出都不影响。完整步骤（前置条件、拷贝项目、关休眠、开机自启、恢复每日自动日报）见 **[DEPLOY_HOMEPC.md](DEPLOY_HOMEPC.md)**。

---

## 免责声明
本项目仅供学习与个人使用。使用 Discord user token 自动读取频道可能违反 Discord 服务条款，
由此产生的封号等后果由使用者自行承担。请遵守相关平台规则，勿用于商业或侵权用途。
