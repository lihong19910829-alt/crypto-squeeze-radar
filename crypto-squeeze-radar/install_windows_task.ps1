param(
  [string]$TaskName = "CryptoSqueezeRadarHourly",
  [string]$FullScanTaskName = "CryptoSqueezeRadarFullScan4H"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $projectRoot "run_hourly_once.ps1"
$fullScanRunner = Join-Path $projectRoot "run_full_scan_once.ps1"
$logFile = Join-Path $projectRoot "logs\hourly_runner.log"
$fullScanLogFile = Join-Path $projectRoot "logs\full_scan_runner.log"

if (-not (Test-Path $runner)) {
  throw "Cannot find hourly runner: $runner"
}
if (-not (Test-Path $fullScanRunner)) {
  throw "Cannot find full scan runner: $fullScanRunner"
}

$now = Get-Date
$hourlyStart = $now.Date.AddHours($now.Hour + 1)
$startTime = $hourlyStart.ToString("HH:mm")
$nextFourHour = $now.Date.AddHours(([Math]::Floor($now.Hour / 4) + 1) * 4).AddMinutes(10)
if ($nextFourHour -le $now) {
  $nextFourHour = $nextFourHour.AddHours(4)
}
$fullScanStartTime = $nextFourHour.ToString("HH:mm")
$taskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`" -MinIntervalHours 1 -TargetSecond 5"
$fullScanTaskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$fullScanRunner`" -MinIntervalHours 4"

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
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -MinIntervalHours 1 -TargetSecond 5" `
    -WorkingDirectory $projectRoot
  $trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $hourlyStart `
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

schtasks.exe /Create `
  /TN $FullScanTaskName `
  /TR $fullScanTaskCommand `
  /SC HOURLY `
  /MO 4 `
  /ST $fullScanStartTime `
  /F | Out-Host

if ($LASTEXITCODE -ne 0) {
  Write-Host "schtasks full-scan install failed; trying current-user ScheduledTask fallback..."
  $fullScanAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$fullScanRunner`" -MinIntervalHours 4" `
    -WorkingDirectory $projectRoot
  $fullScanTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At $nextFourHour `
    -RepetitionInterval (New-TimeSpan -Hours 4) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask `
    -TaskName $FullScanTaskName `
    -Action $fullScanAction `
    -Trigger $fullScanTrigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Crypto Squeeze Radar 4-hour full-market deep OI database backfill" `
    -Force | Out-Host
}

Write-Host "Installed Windows scheduled task: $TaskName"
Write-Host "Runner: $runner"
Write-Host "Log file: $logFile"
Write-Host "First scheduled signal run: ${startTime}:05, then every hour"
Write-Host "Test now: schtasks /Run /TN $TaskName"
Write-Host "Installed Windows scheduled task: $FullScanTaskName"
Write-Host "Full scan runner: $fullScanRunner"
Write-Host "Full scan log file: $fullScanLogFile"
Write-Host "First full-market scan: $fullScanStartTime, then every 4 hours"
Write-Host "Test now: schtasks /Run /TN $FullScanTaskName"
