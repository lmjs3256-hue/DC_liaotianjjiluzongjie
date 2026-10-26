# 部署到「家里常开旧电脑」（Windows）

适合场景：你有一台不关机的 Windows 电脑（旧笔记本 / 闲置台式），想让它 7×24 小时帮你跑日报、
随时收 Telegram 指令，从而**解放你日常用的主力机**（主力机关机、外出、休眠都不影响）。

> 这套方案不需要公网 IP、不需要端口映射。Telegram 监听是 bot 主动去 `api.telegram.org`
> 拉消息（出站），家里的宽带 NAT 完全没问题。

---

## 一、旧电脑上的前置条件

1. **Windows 10 / 11**，能正常上网。
2. **必须能连到 Discord / Telegram**（国内直连多半不稳）——建议在旧电脑上也装 **Clash** 并开启
   **系统代理**（和主力机一样）。装好后脚本里 `proxy` 留空即可自动继承系统代理。
   - 如果旧电脑不方便装 Clash，就把 `config.json` 里的 `proxy` 填成你的代理地址，例如
     `"proxy": "http://127.0.0.1:7890"`。
3. **安装 Python 3.10+**：去 https://www.python.org 下载，安装时务必勾选
   **“Add python.exe to PATH”**。
4. **（一般不需要）.NET 运行时**：只有用 DiscordChatExporter 导出（不推荐）才需要。
   默认 `dce_backend: "api"` 走**内置 API 后端**，直接调 Discord REST API，**不依赖 .NET、不用装导出程序**。
   若你改成 `"local"` 且报“找不到 .NET”，再去 https://dotnet.microsoft.com/download 装 **.NET 8 Desktop Runtime**。

---

## 二、把项目弄到旧电脑（二选一）

**方式 A — 直接拷贝文件夹（最简单，推荐）**
把主力机上整个 `dtao-daily-pipeline` 文件夹拷到旧电脑（U 盘 / 局域网共享 / 微信文件传输助手都行）。
里面已经包含：
- `pipeline.py`、`tg_listener.py`、两个 `.bat`
- `config.json`（**含你的所有 token，它不会上传 GitHub，放心跟着文件夹走**）
- `DiscordChatExporter.Cli.exe` 及一堆 `.dll`（**可选**，默认用不到；见下文「导出后端」）
- `README.md`、`.gitignore`

**方式 B — 从 GitHub 克隆**
```powershell
git clone https://github.com/lmjs3256-hue/DC_liaotianjjiluzongjie.git
cd DC_liaotianjjiluzongjie
```
注意：克隆下来**没有** `config.json`（被 .gitignore 屏蔽了），你需要把主力机的 `config.json`
单独拷到这个目录。**默认不需要** `DiscordChatExporter.Cli.exe`（默认用内置 API 后端导出）；
只有把 `dce_backend` 改成 `"local"` 时才需要单独下载
（https://github.com/Tyrrrz/DiscordChatExporter/releases 下 `DiscordChatExporter.Cli-win-x64.zip`，解压到目录里）。

---

## 三、确认关键文件就位

在旧电脑的项目目录里，至少要有这些（用方式 A 拷贝的天然齐全）：

| 文件 | 说明 |
|---|---|
| `pipeline.py` | 主流程 |
| `tg_listener.py` | TG 指令监听 |
| `config.json` | 你的配置+token（方式 B 需手动放） |
| `DiscordChatExporter.Cli.exe` | 导出程序（**可选**，仅 `dce_backend: "local"` 时需要） |
| `DiscordChatExporter.*.dll`、`*.dll` | 导出程序依赖（同上，可选） |

`config.json` 里确认：
- `dce_backend`: `"api"`（**推荐**：内置 API 后端，直接调 Discord REST API，秒级完成、不依赖 .NET；
  想用 DCE 才改 `"local"` 并填 `dce_exe`）
- `api_fallback`: `true`、`dce_timeout`: `120`（用 DCE 时，超时会自动回退到 API 后端兜底）
- `timezone`: `"Asia/Shanghai"`（日报按北京时间算“前一天”）
- `proxy`: `""`（旧电脑开了 Clash 系统代理时）或填代理地址
- `allowed_user_ids`: `[]`（留空=任何人可触发，开放模式；填 ID 数组=仅白名单可触发）

---

## 四、关闭休眠 / 睡眠（重要！）

旧电脑如果自动睡眠，监听进程会停、TG 指令就收不到了。二选一：

**图形界面**：设置 → 系统 → 电源和睡眠 → “接通电源时” 的睡眠改成 **从不**；
“从不”下面也把“休眠”关掉（或见命令行）。

**命令行（管理员 PowerShell）**：
```powershell
powercfg /change standby-timeout-ac 0      # 接通电源永不睡眠
powercfg /change hibernate-timeout-ac 0    # 接通电源永不休眠
```
（笔记本还可加 `powercfg /change standby-timeout-dc 0` 用电池时也别睡，但建议旧电脑一直插电。）

---

## 五、设置开机自启（让监听常驻）

### 方案 1：启动文件夹 + 守护脚本（最简单，推荐，无需管理员）
把 **`DTAO_Listener_Autostart.bat`** 放进启动文件夹：
1. 按 `Win + R`，输入 `shell:startup` 回车，打开启动文件夹。
2. 把项目里的 `DTAO_Listener_Autostart.bat` 复制 / 拖进该文件夹。
   它负责拉起项目目录中的 `DTAO_TG_Listener_Guard.bat`（**守护版**：监听崩溃或退出后
   **15 秒自动重启**，窗口最小化，崩了还能看到报错）。
重启 / 登录后自动后台跑 `tg_listener.py`。

> ⚠️ **不要把 `DTAO_TG_Listener_Guard.bat` 本体拷进启动文件夹。**
> 守护脚本用 `%~dp0` 定位同目录的 `tg_listener.py`，放错位置会找不到程序、自启失效。
> 用 `DTAO_Listener_Autostart.bat`（内部写死了项目路径）就没这个问题；
> 但换电脑后记得用记事本打开它，把里面的路径改成那台机器上的实际项目路径。

### 方案 2：任务计划程序（更稳，失败自动重启，**需管理员**）
以**管理员身份**在 PowerShell 里运行本项目里的 `install_task_login.ps1`：
```powershell
cd C:\路径\到\dtao-daily-pipeline
powershell -ExecutionPolicy Bypass -File install_task_login.ps1
```
它会注册一个“登录时启动”的任务（pythonw 无窗口、断网/崩溃后每分钟自动重试 3 次、
多实例策略=不新建，避免重复拉起）。注销或重启后无需手动开窗口。

> ⚠️ **启用任务计划后，必须把启动文件夹里的快捷方式删掉**（二选一），否则登录时
> 两个机制会同时启动监听，抢 Telegram 消息（409 冲突、指令时灵时不灵）。
>
> 想彻底“无人登录也跑”，需要管理员权限把触发器改成“计算机启动时”并勾“无论用户是否登录都运行”，
> 那一步在任务计划程序图形界面里手动设一下即可（需管理员）。

---

## 六、验证

1. 旧电脑重启一次（或重新登录），确认监听已起：
   任务管理器看一下有没有 `pythonw.exe` 进程，或看 `tg_listener.log` 末尾有
   “TG 指令监听已启动”。
2. 在你的 Telegram 群里（真人身份）发：`/digest`
3. 应收到“⏳ 开始执行”回执，跑完收到日报 + “✅ 完成”。
4. 试试 `🔁 补推最新` 按钮（不发 `/help` 先，bot 回消息后按钮栏才出现）。

---

## 七、可选：恢复「每日自动日报」

旧电脑常开后，也可以把之前关掉的“每天 09:00 自动出日报”加回来，和 TG 手动触发并存：

用任务计划程序再加一个每日任务（普通 PowerShell）：
```powershell
$action = New-ScheduledTaskAction -Execute pythonw -Argument "pipeline.py" -WorkingDirectory "C:\路径\到\dtao-daily-pipeline"
$trigger = New-ScheduledTaskTrigger -Daily -At "09:00"
Register-ScheduledTask -TaskName "DTAO_Daily_Report" -Action $action -Trigger $trigger -Force
```
这样每天 09:00 自动导出前一天并双推；平时你也能随时在 TG 里 `/digest` 手动补。

---

## 八、排错

| 现象 | 原因 / 处理 |
|---|---|
| TG 发指令没反应 | 监听没跑。看 `tg_listener.log` 有没有「TG 指令监听已启动」和每 10 分钟一次的「💓 心跳：监听存活中」；没有就双击 `DTAO_TG_Listener_Guard.bat`（守护版，崩了会自动重启）。用任务计划方案的看 `DTAO_TG_Listener` 的“上次运行结果” |
| 导出报网络错误 / 空 | 旧电脑没开 Clash 系统代理，且 `proxy` 留空。开 Clash 或填 `proxy` |
| 导出卡住不动（日志停在「检测到系统代理 / 导出命令」后无动静） | DiscordChatExporter（.NET，无 `--proxy` 参数）卡在 `Resolving channel(s)...`。把 `config.json` 的 `dce_backend` 改成 `"api"` 即可秒级完成 |
| 报缺少 .NET | 只有 `dce_backend: "local"` 才需要 .NET 8 Desktop Runtime；改成 `"api"` 就不用装 |
| 想确认本次用了哪个 token / 今天导出几次 | 看日报末尾的 `令牌：1/2 · 次数：1/2` 脚注 |
| 睡眠后收不到指令 | 没关睡眠，回到第四步 |
| 多台机器同时跑监听 | 同一时间只跑一个监听实例，否则抢 Telegram 消息；主力机那边的监听要关掉 |

---

## 九、安全提醒

- 旧电脑上也存着 `config.json`（Discord user token / TG bot / 飞书 secret）。旧电脑如果被别人使用，
  注意账号安全；user token 本身违反 Discord TOS，封号风险自担（多号随机切换已内置）。
- 仓库里**不含** `config.json` 和导出数据，可放心公开。
