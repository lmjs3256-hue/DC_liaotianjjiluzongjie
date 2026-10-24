# DTAO 频道日报小助手（小白使用说明）

> 自动把 Discord 某个频道（这里是 **DTAO 服务器的 price-talk**）的聊天记录导出来 →
> 用 AI（DeepSeek）总结成一份手机端友好的中文日报 → 推送到 **飞书群** 和 **Telegram**。
> 可以手动触发，也可以直接在 Telegram 里发一句话就让它干活。

---

## 一、它能帮你做什么

- 每天（或你想看的时候）把 price-talk 频道的讨论导出来
- 自动**过滤水聊**，只保留和 Bittensor 子网 / 重要成员相关的实质内容
- 用 DeepSeek 生成一份结构化日报（带重点、项目符号、手机端排版）
- 同时推到飞书群机器人 + 你的 Telegram
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

### 第 2 步：下载 DiscordChatExporter
1. 打开 https://github.com/Tyrrrz/DiscordChatExporter/releases
2. 下载 **DiscordChatExporter.Cli-win-x64.zip**（带 Cli 的那个，不是 GUI 版）
3. 解压，把里面的 **`DiscordChatExporter.Cli.exe`** 复制到本项目文件夹里

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
  "discord_tokens": [                               // 可选：多个号轮换，降低封号风险
    "账号A的token", "账号B的token"
  ],
  "channel_id": "1341812134807343114",             // 频道 ID，一般不用改
  "guild_name": "DTAO",
  "channel_name": "price-talk",
  "output_dir": "exports",
  "dce_backend": "local",                          // 用本地 exe，不用 docker
  "dce_exe": "C:/.../DiscordChatExporter.Cli.exe",  // 第2步放的那个 exe 路径
  "deepseek_api_key": "★DeepSeek 的 key",
  "deepseek_base_url": "https://api.deepseek.com/v1",
  "deepseek_model": "deepseek-chat",
  "proxy": "",                                     // 留空=自动用系统代理(Clash)
  "feishu_webhook_url": "★飞书机器人 Webhook 地址",
  "feishu_secret": "★飞书机器人密钥(开启签名校验才需要)",
  "telegram_bot_token": "★TG 机器人 token",
  "telegram_chat_id": "★TG 群/私聊 ID",
  "allowed_user_ids": ["你的TG数字ID"],            // 只让指定的人触发(见安全)
  "timezone": "Asia/Shanghai",
  "push_enabled": true
}
```

### 如何拿 Discord user token（⚠️ 仅自用，风险自担）
1. 电脑浏览器登录 Discord（网页版）
2. 按 `F12` 打开开发者工具 → Network（网络）标签
3. 随便点一下频道，在请求列表里找一个 `api/v9/...` 的请求
4. 看请求头（Headers）里的 **Authorization**，那串值就是 token
5. **复制时连 `""` 一起复制，粘到配置里别漏字符**

> 更安全的做法见文末：用 bot token（需服务器管理员把 bot 拉进群）。

### 如何拿 Telegram 机器人
1. Telegram 里搜索 **@BotFather**，发 `/newbot`，按提示起名
2. 它会给你一个 **bot token**（形如 `123456:ABCdef...`），填到 `telegram_bot_token`
3. 把机器人拉进你要收推送的群；在群里发任意一条消息
4. 打开 @RawDataBot 或 @GetIDsBot，转发/查看那条消息，拿到群的 **chat_id**（负数，如 `-5111149587`）填到 `telegram_chat_id`
5. 想只让自己能触发：填 `allowed_user_ids`（你的 TG 数字 ID，@GetIDsBot 可查）

### 飞书机器人
1. 飞书群 → 设置 → 群机器人 → 添加机器人
2. 复制 **Webhook 地址** → `feishu_webhook_url`
3. 如果开了「签名校验」，把**密钥**填到 `feishu_secret`；没开签名校验则留空

---

## 五、怎么用（两种方式）

### 方式 A：在终端里手动跑
```bash
python pipeline.py                 # 导出「昨天」并双推飞书+TG
python pipeline.py 2026-10-23      # 导出指定某一天
python pipeline.py weekly          # 生成本周周报
python pipeline.py repush 2026-10-23      # 只把已生成的日报重推一遍(不重导)
python pipeline.py repush 2026-10-23 tg   # 只重推 TG
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
| `/help` | 显示帮助 |

发完 bot 会先回「⏳ 开始执行」，跑完回「✅/❌ + 日志」。

**更懒人**：bot 回你消息后，聊天底部会出现一排**按钮**
`📅 导出昨日` / `🔁 补推最新` / `📆 周报` / `❓ 帮助`，
**点一下就发指令，完全不用打字**。

> 群里若挂了多个机器人，指令可能要写成 `/repush@你的机器人名 2026-10-23`，脚本已兼容。

---

## 六、让机器人随时待命（开机自启）

默认脚本不会自己跑，需要有一个「监听程序」在后台常驻，才能随时收你的 TG 指令。

**最简单：开机自启（推荐）**
- 本项目已附带 `DTAO_TG_Listener.bat`，放在你电脑的「启动」文件夹里
  （路径：`C:/Users/你的用户名/AppData/Roaming/Microsoft/Windows/Start Menu/Startup/`）
- 放进去后，**下次登录 Windows 会自动后台运行**，无窗口、不碍事
- 也可以现在就双击它立刻启动

**手动临时跑（看得到窗口）**
双击 `start_tg_listener.bat` 即可，关掉窗口就停。

> 注意：
> - 同一时间**只跑一个**监听，否则两个会抢消息、指令时灵时不灵
> - 监听运行期间，保持 **Clash 系统代理开着**、电脑不休眠，才能连上 Discord/TG
> - 排错看 `tg_listener.log` 和 `run.log` 两个日志文件

---

## 七、安全与合规

1. **user token 封号风险**：个人 token 自动读频道违反 Discord TOS。
   - 降低风险：配 `discord_tokens` 多个号轮换（每天换一个）；或让每个 token 走不同代理 IP。
   - 根治办法：**用 bot token**。在 @BotFather 建 bot，请 DTAO 服务器管理员把 bot 拉进群并给「读取消息历史」权限，然后把 bot token 填进 `discord_token` 即可——这是 Discord 允许的方式，零封号风险。
2. **只允许你自己触发**：在 `config.json` 填 `allowed_user_ids: ["你的TG数字ID"]`，
   这样群里其他人发的指令会被忽略（不会执行也不会回消息）。
3. **密钥别泄露**：`config.json` 含所有密码，**不要上传 GitHub**（本仓库 `.gitignore` 已忽略它）。

---

## 八、常见问题（排错）

**Q：我在 TG 发了指令，机器人没反应？**
A：监听程序没在跑。按第六节启动 `start_tg_listener.bat` 或重启电脑（开机自启）。
   也可看 `tg_listener.log` 有没有「TG 指令监听已启动」这一行。

**Q：提示连不上 Discord / Telegram（502 / 超时）？**
A：确认 Clash（或你的代理）「系统代理」是开着的；`proxy` 留空即可自动继承。

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

1. GitHub 上 **New repository** 新建一个仓库（比如 `dtao-daily-bot`）
2. 在本项目文件夹打开终端，依次执行：
   ```bash
   git init
   git add .
   git commit -m "DTAO 频道日报小助手"
   git branch -M main
   git remote add origin https://github.com/你的用户名/dtao-daily-bot.git
   git push -u origin main
   ```
3. 因为 `.gitignore` 已拦住 `config.json`，推送时**不会带上你的真实 token**
4. 别人克隆后，只需自己复制 `config.example.json` → `config.json` 填好即可用

---

## 十、文件说明

| 文件 | 作用 |
|---|---|
| `pipeline.py` | 主程序：导出 → 过滤 → AI 总结 → 推送 |
| `tg_listener.py` | Telegram 指令监听（发消息触发日报） |
| `config.example.json` | 配置模板（复制到 `config.json` 再填） |
| `config.json` | 你的真实配置（含密钥，**不入库**） |
| `start_tg_listener.bat` | 手动启动监听（带窗口） |
| `DTAO_TG_Listener.bat` | 开机自启版（放启动文件夹，无窗口） |
| `install_task_login.ps1` | 注册任务计划程序自启（更稳，失败自动重启） |
| `DEPLOY_HOMEPC.md` | 部署到另一台常开 Windows 电脑的完整教程 |
| `exports/` | 导出的聊天记录和生成的日报（**不入库**） |
| `run.log` / `tg_listener.log` | 运行日志（**不入库**） |

---

## 十一、部署到另一台常开电脑（解放你的主力机）

不想让日常用的电脑一直开着、挂着 Clash？可以把整套程序搬到一台**不关机的旧电脑 / 闲置笔记本**上 7×24 跑，
Telegram 指令随时能触发，主力机关机外出都不影响。完整步骤（前置条件、拷贝项目、关休眠、开机自启、恢复每日自动日报）见 **[DEPLOY_HOMEPC.md](DEPLOY_HOMEPC.md)**。

---

## 免责声明
本项目仅供学习与个人使用。使用 Discord user token 自动读取频道可能违反 Discord 服务条款，
由此产生的封号等后果由使用者自行承担。请遵守相关平台规则，勿用于商业或侵权用途。
