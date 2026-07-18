param(
  [int]$MinIntervalHours = 1,
  [switch]$Force
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $projectRoot "logs"
$logFile = Join-Path $logDir "hourly_runner.log"
$lastSuccessFile = Join-Path $logDir "last_successful_update.txt"

if (-not (Test-Path $logDir)) {
  New-Item -ItemType Directory -Path $logDir | Out-Null
}

Set-Location $projectRoot

function Write-RunnerLog {
  param([string]$Message)
  try {
    $Message | Tee-Object -FilePath $logFile -Append | Out-Host
  }
  catch {
    Write-Host $Message
    Write-Host "Log write skipped: $_"
  }
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
if (-not $Force -and $MinIntervalHours -gt 0 -and (Test-Path $lastSuccessFile)) {
  $lastText = Get-Content -Path $lastSuccessFile -Raw
  $lastRun = [DateTime]::Parse($lastText.Trim())
  $nextRun = $lastRun.AddHours($MinIntervalHours)
  $currentRun = Get-Date
  $sameHourBucket = $lastRun.ToString("yyyy-MM-dd HH") -eq $currentRun.ToString("yyyy-MM-dd HH")
  if ($sameHourBucket -and $currentRun -lt $nextRun) {
    Write-RunnerLog "[$timestamp] Skip update: last successful run was $lastRun in the same hour; next allowed after $nextRun"
    exit 0
  }
}

$env:PYTHONUNBUFFERED = "1"
if (-not $env:BINANCE_MAX_WORKERS) {
  $env:BINANCE_MAX_WORKERS = "12"
}
if (-not $env:MAX_BINANCE_SYMBOLS) {
  $env:MAX_BINANCE_SYMBOLS = "0"
}
if (-not $env:PATTERN_PUSH_ENABLED -and $env:PUSHPLUS_TOKEN) {
  $env:PATTERN_PUSH_ENABLED = "true"
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-RunnerLog "[$timestamp] Start scheduled update"
Write-RunnerLog "[$timestamp] MAX_BINANCE_SYMBOLS=$env:MAX_BINANCE_SYMBOLS BINANCE_MAX_WORKERS=$env:BINANCE_MAX_WORKERS PATTERN_PUSH_ENABLED=$env:PATTERN_PUSH_ENABLED"

try {
  python -u run_once.py 2>&1 | ForEach-Object { Write-RunnerLog "$_" }
  $exitCode = $LASTEXITCODE
  if ($exitCode -ne 0) {
    throw "python run_once.py failed with exit code: $exitCode"
  }
  $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  (Get-Date).ToString("o") | Set-Content -Path $lastSuccessFile -Encoding UTF8
  Write-RunnerLog "[$timestamp] Scheduled update finished"
}
catch {
  $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Write-RunnerLog "[$timestamp] Scheduled update failed: $_"
  throw
}
