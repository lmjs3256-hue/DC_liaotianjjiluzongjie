# 注册「DTAO TG 指令监听」为登录时自启的常驻任务（普通用户权限即可）
# 用法：powershell -ExecutionPolicy Bypass -File install_task_login.ps1
$ErrorActionPreference = "Stop"

$taskName = "DTAO_TG_Listener"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# 找 pythonw（安装 Python 时勾了 Add to PATH 才有）
try {
    $pythonw = (Get-Command pythonw -ErrorAction Stop).Source
} catch {
    Write-Host "未找到 pythonw.exe，请先安装 Python 并勾选 Add to PATH" -ForegroundColor Red
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument "tg_listener.py" `
    -WorkingDirectory $scriptDir

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -Hidden

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "DTAO price-talk 日报：Telegram 指令监听（登录时启动，崩溃自动重启）" `
    -Force

Write-Host "已注册任务 '$taskName'。重新登录或重启后自动运行；可在任务计划程序里查看/停用。" -ForegroundColor Green
Write-Host "日志： $scriptDir\tg_listener.log"
