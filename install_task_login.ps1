# Register DTAO TG listener as a logon-triggered persistent task (no admin needed)
# Usage: powershell -ExecutionPolicy Bypass -File install_task_login.ps1
$ErrorActionPreference = "Stop"

$taskName = "DTAO_TG_Listener"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# Find pythonw (available only if "Add python.exe to PATH" was checked at install)
try {
    $pythonw = (Get-Command pythonw -ErrorAction Stop).Source
} catch {
    Write-Host "pythonw.exe not found. Install Python and check 'Add to PATH'." -ForegroundColor Red
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
    -Hidden `
    -MultipleInstancesPolicy IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "DTAO price-talk daily: Telegram command listener (start at logon, auto-restart on crash)" `
    -Force

Write-Host "Registered task '$taskName'. It runs automatically after logon/restart." -ForegroundColor Green
Write-Host "Log file: $scriptDir\tg_listener.log"
