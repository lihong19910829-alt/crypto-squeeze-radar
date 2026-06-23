param(
  [string]$Token = $env:VERCEL_TOKEN,
  [string]$Scope = "leehom"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceRoot = Split-Path -Parent $projectRoot
$siteDir = Join-Path $workspaceRoot "vercel-site"
$vercelCmd = Join-Path $projectRoot "node_modules\.bin\vercel.cmd"

if (-not (Test-Path $siteDir)) {
  throw "Cannot find deploy directory: $siteDir"
}

if (-not (Test-Path $vercelCmd)) {
  throw "Cannot find Vercel CLI: $vercelCmd"
}

if (-not $Token) {
  $secure = Read-Host "Enter Vercel Token" -AsSecureString
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try {
    $Token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  }
  finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
  }
}

Write-Host "Deploying static dashboard: $siteDir"
Write-Host "Vercel scope: $Scope"

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  $deployOutput = & $vercelCmd deploy $siteDir --prod --yes --no-wait --no-color --scope $Scope --token="$Token" 2>&1
  $exitCode = $LASTEXITCODE
}
finally {
  $ErrorActionPreference = $previousErrorActionPreference
}

$deployOutput | ForEach-Object { Write-Host $_ }

if ($exitCode -ne 0) {
  throw "Vercel deploy failed with exit code: $exitCode"
}

$deploymentUrl = $deployOutput | Where-Object {
  $_ -match "^https://.*\.vercel\.app"
} | Select-Object -Last 1

if ($deploymentUrl) {
  Write-Host "Deployment ready: $deploymentUrl"
}
else {
  Write-Host "Deploy command finished. If no URL is shown, check the latest Production Deployment for project vercel-site in Vercel Dashboard."
}
