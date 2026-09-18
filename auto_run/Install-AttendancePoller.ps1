# Install a separate NSSM service for office Wi-Fi attendance polling.
# Run elevated PowerShell (Admin):
#   powershell -ExecutionPolicy Bypass -File C:\FDC_FLASK_APP\auto_run\Install-AttendancePoller.ps1
#
# After install: set ATTENDANCE_EXTERNAL_POLLER=1 in nssm-fdcef-environment.env and restart fdcef.

param(
    [string]$ServiceName = "fdcef-attendance",
    [string]$AppRoot = "C:\FDC_FLASK_APP",
    [string]$PythonExe = "",
    [string]$NssmExe = ""
)

$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Find-Nssm {
    param([string]$Hint)
    if ($Hint -and (Test-Path $Hint)) { return (Resolve-Path $Hint).Path }
    foreach ($p in @(
        "C:\nssm\nssm.exe",
        "C:\NSSM\nssm.exe",
        "C:\Tools\nssm\nssm.exe",
        "$env:ProgramFiles\nssm\nssm.exe"
    )) {
        if (Test-Path $p) { return $p }
    }
    $cmd = Get-Command nssm -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "nssm.exe not found. Pass -NssmExe or install NSSM."
}

function Find-Python {
    param([string]$Hint, [string]$Root, [string]$Nssm)
    if ($Hint -and (Test-Path $Hint)) { return (Resolve-Path $Hint).Path }

    # Prefer the same interpreter as the main fdcef NSSM service.
    if ($Nssm -and (Test-Path $Nssm)) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $fdcefPyRaw = & $Nssm get fdcef Application 2>$null
        $ErrorActionPreference = $prev
        # nssm may return UTF-16 with nulls when captured in PowerShell
        $fdcefPy = (("" + $fdcefPyRaw) -replace "`0", "").Trim()
        if ($fdcefPy -and (Test-Path $fdcefPy)) {
            return $fdcefPy
        }
    }

    foreach ($p in @(
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        (Join-Path $Root ".venv\Scripts\python.exe"),
        (Join-Path $Root "venv\Scripts\python.exe")
    )) {
        if (Test-Path $p) { return $p }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "python.exe not found. Pass -PythonExe (use the same Python as fdcef)."
}

function Get-NssmStatus {
    param([string]$Nssm, [string]$Name)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Nssm status $Name 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    $text = (($out | ForEach-Object { (("" + $_) -replace "`0", "") }) -join "`n").Trim()
    return @{
        ExitCode = $code
        Text     = $text
        Exists   = ($code -eq 0)
    }
}

function Invoke-NssmSet {
    param(
        [string]$Nssm,
        [string]$Name,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Args
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & $Nssm set $Name @Args 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) {
        throw ("nssm set {0} failed: {1}" -f ($Args -join " "), (($out | ForEach-Object { "$_" }) -join " "))
    }
}

if (-not (Test-IsAdmin)) {
    throw "Run this script in an elevated PowerShell (Run as administrator). NSSM needs admin to install/start services."
}

$AppRoot = (Resolve-Path $AppRoot).Path
$script = Join-Path $AppRoot "py\standalone\attendance_poller.py"
if (-not (Test-Path $script)) {
    throw "Missing poller script: $script"
}

$logDir = Join-Path $AppRoot "output\logs"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

$NssmExe = Find-Nssm -Hint $NssmExe
$PythonExe = Find-Python -Hint $PythonExe -Root $AppRoot -Nssm $NssmExe

$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $PythonExe -c "import flask" 2>$null
$flaskOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prev
if (-not $flaskOk) {
    throw "Python at $PythonExe is missing flask. Pass -PythonExe to the same interpreter fdcef uses."
}

Write-Host "NSSM:    $NssmExe"
Write-Host "Python:  $PythonExe"
Write-Host "Script:  $script"
Write-Host "Service: $ServiceName"

$status = Get-NssmStatus -Nssm $NssmExe -Name $ServiceName
if ($status.Exists) {
    Write-Host "Service already exists ($($status.Text)); updating settings..."
} else {
    Write-Host "Service not found yet; installing..."
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $installOut = & $NssmExe install $ServiceName $PythonExe "`"$script`"" 2>&1
    $installCode = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($installCode -ne 0) {
        # Retry without extra quotes if install rejected them
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $installOut = & $NssmExe install $ServiceName $PythonExe $script 2>&1
        $installCode = $LASTEXITCODE
        $ErrorActionPreference = $prev
    }
    if ($installCode -ne 0) {
        throw ("nssm install failed (code {0}): {1}" -f $installCode, (($installOut | ForEach-Object { "$_" }) -join " "))
    }
}

# Always point at the correct Python + script (fixes earlier .venv install).
Invoke-NssmSet $NssmExe $ServiceName Application $PythonExe
Invoke-NssmSet $NssmExe $ServiceName AppParameters $script
Invoke-NssmSet $NssmExe $ServiceName AppDirectory $AppRoot
Invoke-NssmSet $NssmExe $ServiceName AppStdout (Join-Path $logDir "attendance_poller.log")
Invoke-NssmSet $NssmExe $ServiceName AppStderr (Join-Path $logDir "attendance_poller.err.log")
Invoke-NssmSet $NssmExe $ServiceName AppRotateFiles 1
Invoke-NssmSet $NssmExe $ServiceName AppRotateBytes 1048576
Invoke-NssmSet $NssmExe $ServiceName Start SERVICE_AUTO_START
Invoke-NssmSet $NssmExe $ServiceName AppExit Default Restart
Invoke-NssmSet $NssmExe $ServiceName AppRestartDelay 5000
# Clear pause-on-failure throttle from previous crash loops.
Invoke-NssmSet $NssmExe $ServiceName AppThrottle 1500

$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $NssmExe stop $ServiceName 2>&1 | Out-Null
Start-Sleep -Seconds 2
$startOut = & $NssmExe start $ServiceName 2>&1
$startCode = $LASTEXITCODE
$ErrorActionPreference = $prev
$startText = (($startOut | ForEach-Object { (("" + $_) -replace "`0", "") }) -join " ").Trim()

if ($startText -match "SERVICE_PAUSED") {
    Write-Host "Service paused after start; sending continue..."
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $NssmExe continue $ServiceName 2>&1 | Out-Null
    Start-Sleep -Seconds 2
    & $NssmExe restart $ServiceName 2>&1 | Out-Null
    $ErrorActionPreference = $prev
}

$final = Get-NssmStatus -Nssm $NssmExe -Name $ServiceName
Write-Host "Status:  $($final.Text)"
if ($final.Text -notmatch "SERVICE_RUNNING") {
    Write-Host "Check logs:"
    Write-Host "  $(Join-Path $logDir 'attendance_poller.err.log')"
    Write-Host "  $(Join-Path $logDir 'attendance_poller.log')"
    throw ("Service is not RUNNING (got: {0}). {1}" -f $final.Text, $startText)
}

Write-Host "Started $ServiceName."
Write-Host "Next: set ATTENDANCE_EXTERNAL_POLLER=1 in nssm-fdcef-environment.env and restart fdcef."
