#Requires -Version 5.1
<#
.SYNOPSIS
  Creates dam_cron_secret.local.txt + optional dam_sync_base_url.local.txt (gitignored).
  Optionally registers Windows Scheduled Tasks for hourly / weekly DAM sync.
  Flask reads the same secret from auto_run/dam_cron_secret.local.txt if DAM_CRON_SECRET env is unset.

.USAGE (elevated PowerShell recommended for tasks):
  Set-Location C:\FDC_FLASK_APP\auto_run
  .\Setup-DamCron.ps1
  .\Setup-DamCron.ps1 -RegisterScheduledTasks
  .\Setup-DamCron.ps1 -BaseUrl "http://127.0.0.1:5555"
#>

[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:5555",
    [switch]$RegisterScheduledTasks
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $here "..")
$secretPath = Join-Path $here "dam_cron_secret.local.txt"
$urlPath = Join-Path $here "dam_sync_base_url.local.txt"

if (-not (Test-Path $secretPath)) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $secret = -join ($bytes | ForEach-Object { '{0:x2}' -f $_ })
    Set-Content -Path $secretPath -Value $secret -Encoding ascii -NoNewline
    Write-Host "Created secret file: $secretPath"
} else {
    Write-Host "Secret file already exists: $secretPath (leave as-is)"
}

Set-Content -Path $urlPath -Value $BaseUrl.Trim() -Encoding ascii -NoNewline
Write-Host "Wrote base URL: $urlPath -> $BaseUrl"

# Help Flask inherit secret without env var if deployed from this repo path:
$samplesEnv = Join-Path $repoRoot ".env.dam_cron.sample.txt"
@"
# Optional: set these if you do not use auto_run/dam_cron_secret.local.txt on the server copy of the repo
DAM_CRON_SECRET=paste-from-auto_run/dam_cron_secret.local.txt
DAM_SYNC_BASE_URL=$BaseUrl
"@ | Set-Content -Path $samplesEnv -Encoding utf8
Write-Host "Sample env reference: $samplesEnv"

if (-not $RegisterScheduledTasks) {
    Write-Host ""
    Write-Host "Done. Restart Flask so /dam/cron/start picks up the secret file."
    Write-Host "Test: curl -H `"X-DAM-Cron-Secret: <secret>`" $($BaseUrl.TrimEnd('/'))/dam/cron/status"
    Write-Host "To register hourly + weekly tasks, re-run: .\Setup-DamCron.ps1 -RegisterScheduledTasks"
    exit 0
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warning "Scheduled tasks require Administrator. Start PowerShell as Admin and run -RegisterScheduledTasks again."
    exit 1
}

$hourlyBat = Join-Path $here "hourly_auto_sync.bat"
$weeklyBat = Join-Path $here "weekly_complete_rebuild.bat"

# schtasks needs quoted task names — pass full command line through cmd (ArgumentList splits spaces incorrectly)
cmd.exe /c "schtasks /Delete /TN `"FDC DAM Hourly Sync`" /F >nul 2>nul"
cmd.exe /c "schtasks /Create /TN `"FDC DAM Hourly Sync`" /TR `"$hourlyBat`" /SC HOURLY /MO 1 /RU SYSTEM /RL HIGHEST /F"
if ($LASTEXITCODE -ne 0) { throw "schtasks hourly failed: $LASTEXITCODE" }

cmd.exe /c "schtasks /Delete /TN `"FDC DAM Weekly Rebuild`" /F >nul 2>nul"
cmd.exe /c "schtasks /Create /TN `"FDC DAM Weekly Rebuild`" /TR `"$weeklyBat`" /SC WEEKLY /D SUN /ST 03:00 /RU SYSTEM /RL HIGHEST /F"
if ($LASTEXITCODE -ne 0) { throw "schtasks weekly failed: $LASTEXITCODE" }

Write-Host "Scheduled tasks registered (SYSTEM account)."
Write-Host "Verify: schtasks /Query /TN `"FDC DAM Hourly Sync`""
