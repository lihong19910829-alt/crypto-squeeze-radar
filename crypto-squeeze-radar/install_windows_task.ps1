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

$startTime = (Get-Date).AddMinutes(2).ToString("HH:mm")
$taskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`""

schtasks.exe /Create `
  /TN $TaskName `
  /TR $taskCommand `
  /SC HOURLY `
  /MO 1 `
  /ST $startTime `
  /F | Out-Host

if ($LASTEXITCODE -ne 0) {
  throw "Failed to install Windows scheduled task. Please run PowerShell as Administrator."
}

Write-Host "Installed Windows scheduled task: $TaskName"
Write-Host "Runner: $runner"
Write-Host "Log file: $logFile"
Write-Host "First scheduled run: $startTime, then every 1 hour"
Write-Host "Test now: schtasks /Run /TN $TaskName"
