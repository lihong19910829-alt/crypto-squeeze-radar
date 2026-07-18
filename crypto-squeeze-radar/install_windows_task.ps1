param(
  [string]$TaskName = "CryptoSqueezeRadarHourly"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $projectRoot "run_hourly_once.ps1"
$logFile = Join-Path $projectRoot "logs\hourly_runner.log"

if (-not (Test-Path $runner)) {
  throw "Cannot find hourly runner: $runner"
}

$now = Get-Date
$startTime = $now.Date.AddHours($now.Hour + 1).AddMinutes(1).ToString("HH:mm")
$taskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`" -MinIntervalHours 1"

schtasks.exe /Create `
  /TN $TaskName `
  /TR $taskCommand `
  /SC HOURLY `
  /MO 1 `
  /ST $startTime `
  /F | Out-Host

if ($LASTEXITCODE -ne 0) {
  Write-Host "schtasks install failed; trying current-user ScheduledTask fallback..."
  $action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -MinIntervalHours 1" `
    -WorkingDirectory $projectRoot
  $trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $now.Date.AddHours($now.Hour + 1).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Crypto Squeeze Radar hourly local refresh and WeChat push" `
    -Force | Out-Host
}

Write-Host "Installed Windows scheduled task: $TaskName"
Write-Host "Runner: $runner"
Write-Host "Log file: $logFile"
Write-Host "First scheduled run: $startTime, then every hour"
Write-Host "Test now: schtasks /Run /TN $TaskName"
