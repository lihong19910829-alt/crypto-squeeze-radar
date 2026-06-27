$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $projectRoot "logs"
$logFile = Join-Path $logDir "hourly_runner.log"

if (-not (Test-Path $logDir)) {
  New-Item -ItemType Directory -Path $logDir | Out-Null
}

Set-Location $projectRoot

$env:PYTHONUNBUFFERED = "1"
if (-not $env:BINANCE_MAX_WORKERS) {
  $env:BINANCE_MAX_WORKERS = "12"
}
if (-not $env:MAX_BINANCE_SYMBOLS) {
  $env:MAX_BINANCE_SYMBOLS = "0"
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"[$timestamp] Start hourly update" | Tee-Object -FilePath $logFile -Append
"[$timestamp] MAX_BINANCE_SYMBOLS=$env:MAX_BINANCE_SYMBOLS BINANCE_MAX_WORKERS=$env:BINANCE_MAX_WORKERS" | Tee-Object -FilePath $logFile -Append

try {
  python -u run_once.py 2>&1 | Tee-Object -FilePath $logFile -Append
  $exitCode = $LASTEXITCODE
  if ($exitCode -ne 0) {
    throw "python run_once.py failed with exit code: $exitCode"
  }
  $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  "[$timestamp] Hourly update finished" | Tee-Object -FilePath $logFile -Append
}
catch {
  $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  "[$timestamp] Hourly update failed: $_" | Tee-Object -FilePath $logFile -Append
  throw
}
