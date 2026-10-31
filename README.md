# DTAO 频道日报小助手（小白使用说明）

> 自动把 Discord 某个频道（这里是 **DTAO 服务器的 price-talk**）的聊天记录导出来 →
> 用 AI（DeepSeek）总结成一份手机端友好的中文日报 → 推送到 **飞书群** 和 **Telegram**。
> 可以手动触发，也可以直接在 Telegram 里发一句话就让它干活。

**当前版本：v1.0.1** ｜ 更新记录见 **[CHANGELOG.md](CHANGELOG.md)**

## ✨ 核心特性

- 🤖 **AI 日报**：DeepSeek 把频道讨论总结成结构化日报，**4 个板块**：🔥 必读重点 / 👥 重要人物观点 / 📎 其他细节 / 💡 一句话总结（子网异动统一写进「必读重点」，不再重复列一遍）
- 🧠 **完整对话上下文**：不是只挑关键词句，而是保留每条实质消息**前后各 2 条上下文**，并按**时间顺序**喂给模型——「2t daily buyback 不值一提」能正确还原成「**SN61** 每天回购 2 个 TAO 不值一提」，而不是瞎猜成 SN2
- ❓ **待解疑问也会记录**：群里对某子网的点名、质疑、提问（如「117 团队是谁」）当天没人回答，也会作为「待解疑问」写进日报——当天的疑点本身就是信号
- 👤 **人名保真**：输入里带「当日发言者名单」，模型只能从中取名字、逐字照抄（含 `τFIG`、`kτwτ` 等特殊字符）；生成后还会自动核对，发现名单外的名字就在 `run.log` 告警
- 🧷 **禁止臆造（溯源规则）**：每条结论必须能在聊天原文找到出处；不确定就写「原文未提及」；「第一/最大」等排名必须落到具体 SN 编号，原文没指明就标注或舍弃；重大定性（跑路/转型等）以官方最新澄清为准，群友猜测不得当结论
- 🎯 **结论稳定**：`deepseek_temperature` 默认 **0**，同一份记录重复总结不会一天一个说法
- ✍️ **TG 反馈重总结（/retry）**：日报有错或你有补充事实，直接在群里发 `/retry <你的反馈>`，AI 按你的反馈重写并重推，不用改代码、不用重导出
- 🗣 **大白话风格**：少用行话，除人名/子网名/代币名外尽量用中文，「SOTA」这类缩写写成「中文（英文缩写）」
- 📱 **手机端友好排版**：飞书卡片铺满屏宽，标题+日期与正文合并在**同一可复制文本块**（长按整段选中）
- ⏱ **每日导出节流（防封号）**：默认每天最多真实导出 **2 次**、间隔 **≥12 小时**；未达间隔/达上限时只复用或合并已有记录做「深刻总结」重推，不消耗 token（阈值可在 `config.json` 配置）
- 🎲 **多账号随机触发**：配置 `discord_tokens` 多个号，每次导出随机抽一个，摊薄风控概率；数组里若有空串会被自动忽略，绝不抽到空 token
- 💬 **Telegram 指令触发**：`/digest` `/weekly` `/repush` `/watch` `/retry` `/help`，并有常驻按钮栏，点一下就发
- 🔓 **开放触发（可选白名单）**：默认任意用户可触发；填 `allowed_user_ids` 即切换为仅白名单
- 📊 **潜力观察独立成篇**：日报**不再**夹带「潜力观察」，改为单独生成落盘；只有你发 `/watch` 时才推送，日报保持精简
- 🚀 **内置 API 导出后端（默认）**：不依赖 DiscordChatExporter，直接调 Discord REST API 导出，秒级完成；若仍用 DCE，超时（120s）会自动回退到 API 后端兜底
- 🛡 **监听崩溃自愈 + 流水线不静默失败**：流水线在后台线程执行、异常全兜底；守护脚本在监听意外退出后 **15 秒自动重启**；流水线出错时会把完整堆栈写进 `run.log`，不再「失败得无声无息」
- 🏷 **导出元信息脚注**：日报末尾标注 `令牌：1/2 · 次数：1/2`，一眼看出本次用了第几个 token、池中共几个、当日还剩几次导出额度
- 🔧 **开机自启（带崩溃自愈）**：监听程序登录自动后台运行，随时收指令

---

## 📑 目录

1. [它能帮你做什么](#一它能帮你做什么)
2. [准备工作清单](#二你需要提前准备好这些清单)
3. [第一次安装](#三第一次安装照着做约-10-分钟)
4. [config.json 配置说明](#四configjson-每一项怎么填)
5. [怎么用（终端 / Telegram）](#五怎么用两种方式)
6. [开机自启（监听常驻）](#六让机器人随时待命开机自启)
7. [安全与合规](#七安全与合规)
8. [常见问题排错](#八常见问题排错)
9. [上传到 GitHub](#九上传到-github小白步骤)
10. [文件说明](#十文件说明)
11. [部署到另一台常开电脑](#十一部署到另一台常开电脑解放你的主力机)

> 更完整的旧电脑 7×24 部署教程见 **[DEPLOY_HOMEPC.md](DEPLOY_HOMEPC.md)**。

---

## 一、它能帮你做什么

- 每天（或你想看的时候）把 price-talk 频道的讨论导出来
- 自动**过滤水聊**，只保留和 Bittensor 子网 / 重要成员相关的实质内容
- 用 DeepSeek 生成一份结构化日报（带重点、项目符号、手机端排版）
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
| **DeepSeek API Key** | AI 总结 | 在 platform.deepseek.com 注册领取 |
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
  "deepseek_api_key": "★DeepSeek 的 key",
  "deepseek_base_url": "https://api.deepseek.com/v1",
  "deepseek_model": "deepseek-chat",
  "deepseek_temperature": 0,                       // 0=结论最稳定（推荐）；调高会更发散，同一份记录可能给出不同说法
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
- token 拿到后尽快用、不要外泄；user token 违反 Discord TOS，封号风险自担（见第七节）。

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
   weekly - 生成本周周报
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
python pipeline.py repush 2026-10-23      # 只把已生成的日报重推一遍(不重导)
python pipeline.py repush 2026-10-23 tg   # 只重推 TG
python pipeline.py --version              # 查看当前版本号

# ⚠️ 注意：第一个参数只能是 daily / weekly / repush 之一。
# 直接写 python pipeline.py 2026-10-23 会报 argument mode: invalid choice。
```

### 方式 B：在 Telegram 里发一句话（最方便）
先让监听程序跑起来（见第六节），然后在你的 TG 群里发：

| 指令 | 作用 |
|---|---|
| `/digest` | 导出昨天并双推 |
| `/digest 2026-10-23` | 导出指定日 |
| `/weekly` | 周报 |
| `/repush` | **补推最近一次日报（不用输日期）** |
| `/repush 2026-10-23` | 补推指定日 |
| `/repush 2026-10-23 tg` | 只补推 TG |
| `/watch` | 查看「潜力观察」（默认最新一天） |
| `/watch 7` | 近 7 天潜力标的**跨日热点汇总**（提前布局参考） |
| `/watch all` | 全部日期汇总 |
| `/retry <反馈>` | **按你的反馈重总结最近一次日报**并重推（如 `/retry SN34 是转型不是跑路`） |
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

**更懒人**：bot 回你消息后，聊天底部会出现一排**按钮**
`📅 导出昨日(/digest 昨日)` / `🔁 补推最新(/repush 最新)` / `📊 近7天热点(/watch 7)` / `📆 周报(/weekly)`，
**点一下就发指令，完全不用打字**。

> 群里若挂了多个机器人，指令可能要写成 `/repush@你的机器人名 2026-10-23`，脚本已兼容。

---

## 六、让机器人随时待命（开机自启）

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

## 七、安全与合规

1. **user token 封号风险**：个人 token 自动读频道违反 Discord TOS。
   - 降低风险：配 `discord_tokens` 多个号随机切换（每次导出随机抽一个账号；数组里若有空串会被自动忽略，不会抽到空）；或让每个 token 走不同代理 IP。
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

## 八、常见问题（排错）

**Q：我在 TG 发了指令，机器人没反应？**
A：监听程序没在跑。按第六节启动 `start_tg_listener.bat` 或重启电脑（开机自启）。
   也可看 `tg_listener.log` 有没有「TG 指令监听已启动」这一行。

**Q：提示连不上 Discord / Telegram（502 / 超时）？**
A：确认 Clash（或你的代理）「系统代理」是开着的；`proxy` 留空即可自动继承。

**Q：日报一直出不来，run.log 停在「检测到系统代理 / 导出命令」后再无动静？**
A：这是 DiscordChatExporter（.NET 程序，无 `--proxy` 参数）卡在 `Resolving channel(s)...` 一步，
   表现为挂起十几分钟不返回、也不报错，跟 token 无关。解决办法：把 `config.json` 里的
   `"dce_backend"` 改成 `"api"`，改用内置的 API 导出后端（直接调 Discord REST API，秒级完成，
   产出格式与 DCE 完全一致）。保持 `"api_fallback": true` 时，即使仍用 DCE，超时
   （默认 120 秒）也会自动切到 API 后端兜底。

**Q：日报末尾的「令牌：1/2 · 次数：1/2」是什么意思？**
A：这是导出元信息脚注，帮你确认多 token 轮换和节流配额是否按预期工作：
   - `令牌 1/2` = 本次**随机抽中的是第 1 个** token，`discord_tokens` 池里**共 2 个**（多号轮换生效）。
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

**Q：总结用的是哪个模型？可以换吗？**
A：默认 `deepseek-chat`（DeepSeek V3），改 `config.json` 里的 `deepseek_model` 即可换。
想用推理更强的可填 `deepseek-reasoner`（更慢、更贵）。
但要注意：**多数「漏信息 / 说错」并不是模型能力问题**，而是喂进去的数据被过滤掉或顺序打乱了
（v1.0.1 已修：裸数字子网号不再被丢、保留上下文、按时间排序）。先确认输入完整，再考虑换模型。

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

## 九、上传到 GitHub（小白步骤）

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

## 十、文件说明

| 文件 | 作用 |
|---|---|
| `pipeline.py` | 主程序：导出 → 过滤 → AI 总结 → 推送 |
| `tg_listener.py` | Telegram 指令监听（发消息触发日报） |
| `config.example.json` | 配置模板（复制到 `config.json` 再填） |
| `CHANGELOG.md` | 版本 / 变动记录 |
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

## 十一、部署到另一台常开电脑（解放你的主力机）

不想让日常用的电脑一直开着、挂着 Clash？可以把整套程序搬到一台**不关机的旧电脑 / 闲置笔记本**上 7×24 跑，
Telegram 指令随时能触发，主力机关机外出都不影响。完整步骤（前置条件、拷贝项目、关休眠、开机自启、恢复每日自动日报）见 **[DEPLOY_HOMEPC.md](DEPLOY_HOMEPC.md)**。

---

## 免责声明
本项目仅供学习与个人使用。使用 Discord user token 自动读取频道可能违反 Discord 服务条款，
由此产生的封号等后果由使用者自行承担。请遵守相关平台规则，勿用于商业或侵权用途。
