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
4. **（可选）.NET 运行时**：`DiscordChatExporter.Cli.exe` 依赖 .NET。如果运行导出时报
   “找不到 .NET” 之类的错，去 https://dotnet.microsoft.com/download 装 **.NET 8 Desktop Runtime**。

---

## 二、把项目弄到旧电脑（二选一）

**方式 A — 直接拷贝文件夹（最简单，推荐）**
把主力机上整个 `dtao-daily-pipeline` 文件夹拷到旧电脑（U 盘 / 局域网共享 / 微信文件传输助手都行）。
里面已经包含：
- `pipeline.py`、`tg_listener.py`、两个 `.bat`
- `config.json`（**含你的所有 token，它不会上传 GitHub，放心跟着文件夹走**）
- `DiscordChatExporter.Cli.exe` 及一堆 `.dll`（导出程序本体）
- `README.md`、`.gitignore`

**方式 B — 从 GitHub 克隆**
```powershell
git clone https://github.com/lmjs3256-hue/DC_liaotianjjiluzongjie.git
cd DC_liaotianjjiluzongjie
```
注意：克隆下来**没有** `config.json`（被 .gitignore 屏蔽了）。你需要把主力机的 `config.json`
单独拷到这个目录；`DiscordChatExporter.Cli.exe` 也要单独下载
（https://github.com/Tyrrrz/DiscordChatExporter/releases 下 `DiscordChatExporter.Cli.zip`，解压到目录里）。

---

## 三、确认关键文件就位

在旧电脑的项目目录里，至少要有这些（用方式 A 拷贝的天然齐全）：

| 文件 | 说明 |
|---|---|
| `pipeline.py` | 主流程 |
| `tg_listener.py` | TG 指令监听 |
| `config.json` | 你的配置+token（方式 B 需手动放） |
| `DiscordChatExporter.Cli.exe` | 导出程序（方式 B 需手动下载） |
| `DiscordChatExporter.*.dll`、`*.dll` | 导出程序依赖 |

`config.json` 里确认：
- `dce_backend`: `"local"`、`dce_exe`: `"DiscordChatExporter.Cli.exe"`
- `timezone`: `"Asia/Shanghai"`（日报按北京时间算“前一天”）
- `proxy`: `""`（旧电脑开了 Clash 系统代理时）或填代理地址
- `allowed_user_ids`: `["你的TG数字id"]`（只让你本人触发）

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

### 方案 1：启动文件夹（最简单）
把 `DTAO_TG_Listener.bat` 的**快捷方式**放进启动文件夹：
1. 按 `Win + R`，输入 `shell:startup` 回车，打开启动文件夹。
2. 右键 `DTAO_TG_Listener.bat` → 发送到 → 桌面快捷方式；再把那个快捷方式拖进启动文件夹。
重启/登录后自动以无窗口方式跑 `tg_listener.py`。

### 方案 2：任务计划程序（更稳，失败自动重启，推荐）
以**普通用户**在 PowerShell 里运行本项目里的 `install_task_login.ps1`：
```powershell
cd C:\路径\到\dtao-daily-pipeline
powershell -ExecutionPolicy Bypass -File install_task_login.ps1
```
它会注册一个“登录时启动”的任务（pythonw 无窗口、断网/崩溃后每分钟重试 3 次）。
注销或重启后无需手动开窗口。

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
| TG 发指令没反应 | 监听没跑。看任务计划程序里 `DTAO_TG_Listener` 是否“上次运行结果”正常；或 `tg_listener.log` |
| 导出报网络错误 / 空 | 旧电脑没开 Clash 系统代理，且 `proxy` 留空。开 Clash 或填 `proxy` |
| 报缺少 .NET | 装 .NET 8 Desktop Runtime |
| 睡眠后收不到指令 | 没关睡眠，回到第四步 |
| 多台机器同时跑监听 | 同一时间只跑一个监听实例，否则抢 Telegram 消息；主力机那边的监听要关掉 |

---

## 九、安全提醒

- 旧电脑上也存着 `config.json`（Discord user token / TG bot / 飞书 secret）。旧电脑如果被别人使用，
  注意账号安全；user token 本身违反 Discord TOS，封号风险自担（多号轮换已内置）。
- 仓库里**不含** `config.json` 和导出数据，可放心公开。
