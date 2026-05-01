#Requires -Version 5.1
<#
.SYNOPSIS
  Creates dam_cron_secret.local.txt + optional dam_sync_base_url.local.txt (gitignored).
  Optionally registers Windows Scheduled Tasks for daily (24h) + weekly DAM sync.
  Flask reads the same secret from auto_run/dam_cron_secret.local.txt if DAM_CRON_SECRET env is unset.

.USAGE (elevated PowerShell recommended for tasks):
  Set-Location C:\FDC_FLASK_APP\auto_run
  .\Setup-DamCron.ps1
  .\Setup-DamCron.ps1 -RegisterScheduledTasks
  .\Setup-DamCron.ps1 -BaseUrl "http://127.0.0.1:5555" -DailyStartTime "03:00"
#>

[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:5555",
    [string]$DailyStartTime = "03:00",
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
    Write-Host "To register daily (24h) + weekly tasks, re-run: .\Setup-DamCron.ps1 -RegisterScheduledTasks"
    exit 0
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warning "Scheduled tasks require Administrator. Start PowerShell as Admin and run -RegisterScheduledTasks again."
    exit 1
}

$dailyBat = Join-Path $here "daily_auto_sync.bat"
$weeklyBat = Join-Path $here "weekly_complete_rebuild.bat"
$hourlyBat = Join-Path $here "hourly_auto_sync.bat"

# Remove ANY task that still runs hourly_auto_sync.bat (any task name/path — fixes hourly churn).
try {
    Import-Module ScheduledTasks -ErrorAction Stop
    Get-ScheduledTask -ErrorAction SilentlyContinue | ForEach-Object {
        $t = $_
        foreach ($a in @($t.Actions)) {
            $exe = [string]$a.Execute
            $arg = [string]$a.Arguments
            $blob = "$exe $arg"
            if (($exe -ieq $hourlyBat) -or ($blob -imatch 'hourly_auto_sync\.bat')) {
                $tp = $t.TaskPath
                Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $tp -Confirm:$false -ErrorAction SilentlyContinue
                Write-Host "Removed scheduled task that referenced hourly_auto_sync.bat: ${tp}$($t.TaskName)"
                break
            }
        }
    }
} catch {
    Write-Warning "ScheduledTasks module unavailable or sweep failed — delete legacy hourly tasks manually. $_"
}

# Remove legacy hourly task name by known title and (re)create daily 24h job
$null = & schtasks.exe /Delete /TN 'FDC DAM Hourly Sync' /F 2>&1
$null = & schtasks.exe /Delete /TN 'FDC DAM Daily Sync' /F 2>&1
$null = & schtasks.exe /Create /TN 'FDC DAM Daily Sync' /TR $dailyBat /SC DAILY /MO 1 /ST $DailyStartTime /RU SYSTEM /RL HIGHEST /F 2>&1
if ($LASTEXITCODE -ne 0) { throw "schtasks daily (24h) failed: $LASTEXITCODE" }

$null = & schtasks.exe /Delete /TN 'FDC DAM Weekly Rebuild' /F 2>&1
$null = & schtasks.exe /Create /TN 'FDC DAM Weekly Rebuild' /TR $weeklyBat /SC WEEKLY /D SUN /ST 03:00 /RU SYSTEM /RL HIGHEST /F 2>&1
if ($LASTEXITCODE -ne 0) { throw "schtasks weekly failed: $LASTEXITCODE" }

Write-Host "Scheduled tasks registered (SYSTEM account). Daily sync runs every 24 hours at $DailyStartTime (server local time)."
Write-Host 'Verify: schtasks /Query /TN "FDC DAM Daily Sync"'
