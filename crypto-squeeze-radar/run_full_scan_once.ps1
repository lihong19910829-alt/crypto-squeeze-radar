param(
  [int]$MinIntervalHours = 4,
  [int]$MinIntervalGraceMinutes = 2,
  [switch]$Force
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $projectRoot "logs"
$logFile = Join-Path $logDir "full_scan_runner.log"
$lastSuccessFile = Join-Path $logDir "last_successful_full_scan.txt"
$lockFile = Join-Path $logDir "radar_pipeline.lock"

if (-not (Test-Path $logDir)) {
  New-Item -ItemType Directory -Path $logDir | Out-Null
}

Set-Location $projectRoot

$envFile = Join-Path $projectRoot ".env"
if (Test-Path $envFile) {
  Get-Content -Path $envFile -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
      return
    }
    $parts = $line.Split("=", 2)
    $name = $parts[0].Trim()
    $value = $parts[1].Trim().Trim('"').Trim("'")
    if ($name) {
      [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
  }
}

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

function Acquire-RunnerLock {
  param([int]$StaleAfterMinutes = 180)
  if (Test-Path $lockFile) {
    $lock = Get-Item $lockFile
    if ($lock.LastWriteTime -lt (Get-Date).AddMinutes(-$StaleAfterMinutes)) {
      Remove-Item -LiteralPath $lockFile -Force
    }
    else {
      Write-RunnerLog "[$(Get-Date -Format "yyyy-MM-dd HH:mm:ss")] Skip full scan: another radar task is still running ($lockFile)"
      exit 0
    }
  }
  "$PID full_scan $(Get-Date -Format o)" | Set-Content -Path $lockFile -Encoding UTF8
}

function Release-RunnerLock {
  if (Test-Path $lockFile) {
    Remove-Item -LiteralPath $lockFile -Force
  }
}

function Get-PythonCommand {
  $knownPython = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
  if (Test-Path $knownPython) {
    return [pscustomobject]@{
      File = $knownPython
      Args = [string[]]@()
    }
  }

  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($python -and $python.Source -notlike "*\Microsoft\WindowsApps\*") {
    return [pscustomobject]@{
      File = $python.Source
      Args = [string[]]@()
    }
  }

  $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
  if ($pyLauncher) {
    return [pscustomobject]@{
      File = $pyLauncher.Source
      Args = [string[]]@("-3")
    }
  }

  $codexPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  if (Test-Path $codexPython) {
    return [pscustomobject]@{
      File = $codexPython
      Args = [string[]]@()
    }
  }

  throw "Cannot find usable Python in scheduled task environment"
}

$pythonCommand = Get-PythonCommand

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
if (-not $Force -and $MinIntervalHours -gt 0 -and (Test-Path $lastSuccessFile)) {
  $lastText = Get-Content -Path $lastSuccessFile -Raw
  $lastRun = [DateTime]::Parse($lastText.Trim())
  $nextRun = $lastRun.AddHours($MinIntervalHours)
  $currentRun = Get-Date
  $effectiveNextRun = $nextRun.AddMinutes(-[Math]::Max(0, $MinIntervalGraceMinutes))
  if ($currentRun -lt $effectiveNextRun) {
    Write-RunnerLog "[$timestamp] Skip full scan: last successful run was $lastRun; next allowed after $nextRun"
    exit 0
  }
}

$env:PYTHONUNBUFFERED = "1"
$env:RADAR_SCAN_MODE = "full_scan"
if (-not $env:BINANCE_MAX_WORKERS) {
  $env:BINANCE_MAX_WORKERS = "12"
}
if (-not $env:MAX_BINANCE_SYMBOLS) {
  $env:MAX_BINANCE_SYMBOLS = "0"
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-RunnerLog "[$timestamp] Start full-market deep OI scan"
Write-RunnerLog "[$timestamp] MAX_BINANCE_SYMBOLS=$env:MAX_BINANCE_SYMBOLS BINANCE_MAX_WORKERS=$env:BINANCE_MAX_WORKERS RADAR_SCAN_MODE=$env:RADAR_SCAN_MODE"

try {
  Acquire-RunnerLock
  & $pythonCommand.File @($pythonCommand.Args) -u run_full_scan_once.py 2>&1 | ForEach-Object { Write-RunnerLog "$_" }
  $exitCode = $LASTEXITCODE
  if ($exitCode -ne 0) {
    throw "python run_full_scan_once.py failed with exit code: $exitCode"
  }
  $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  (Get-Date).ToString("o") | Set-Content -Path $lastSuccessFile -Encoding UTF8
  Write-RunnerLog "[$timestamp] Full-market deep OI scan finished"
}
catch {
  $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Write-RunnerLog "[$timestamp] Full-market deep OI scan failed: $_"
  throw
}
finally {
  Release-RunnerLock
}
