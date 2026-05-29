#Requires -Version 5.1
<#
.SYNOPSIS
  Admin setup: scheduled tasks to restart Dropbox sync for the logged-in Windows user.
  - FDC Restart Dropbox (SYSTEM): stop service, kill processes, trigger user client
  - FDC Start Dropbox User (interactive): launch Dropbox.exe in stream's session (tray + sync)

.USAGE (elevated PowerShell - stream should be logged in):
  Set-Location C:\FDC_FLASK_APP\auto_run
  .\Setup-DropboxRestartTask.ps1
  .\Setup-DropboxRestartTask.ps1 -DropboxUser stream

  Test:
  schtasks /Run /TN "FDC Restart Dropbox"
#>

[CmdletBinding()]
param(
    [string]$DropboxUser = "stream",
    [switch]$SkipPermissionTest
)

$RestartTaskName = "FDC Restart Dropbox"
$UserStartTaskName = "FDC Start Dropbox User"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $here "restart_dropbox_service.bat"
$dropboxExe = "${env:ProgramFiles(x86)}\Dropbox\Client\Dropbox.exe"
$dropboxDir = Split-Path -Parent $dropboxExe

if (-not (Test-Path $bat)) {
    throw "Missing $bat"
}
if (-not (Test-Path $dropboxExe)) {
    throw "Dropbox.exe not found: $dropboxExe"
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    Write-Warning "Run PowerShell as Administrator to register the scheduled tasks."
    exit 1
}

$DropboxAccount = if ($DropboxUser -match '\\') { $DropboxUser } else { "$env:COMPUTERNAME\$DropboxUser" }
$TaskSddl = "D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FRFW;;;AU)(A;;FRFW;;;BU)(A;;FRFW;;;IU)"

function Register-FdcScheduledTaskCom {
    param(
        [string]$Name,
        [string]$CommandPath,
        [string]$WorkDir,
        [string]$RunAsUserId,
        [int]$LogonType,
        [string]$Description,
        [string]$Sddl
    )
    $service = New-Object -ComObject Schedule.Service
    $service.Connect()
    $folder = $service.GetFolder("\")

    try {
        $folder.DeleteTask($Name, 0)
    } catch {
        # Task may not exist yet.
    }

    $taskDef = $service.NewTask(0)
    $taskDef.RegistrationInfo.Description = $Description
    $taskDef.Settings.AllowDemandStart = $true
    $taskDef.Settings.StartWhenAvailable = $true
    $taskDef.Settings.ExecutionTimeLimit = "PT15M"
    $taskDef.Settings.DisallowStartIfOnBatteries = $false
    $taskDef.Settings.StopIfGoingOnBatteries = $false
    $taskDef.Settings.MultipleInstances = 2

    if ($LogonType -eq 5) {
        $taskDef.Principal.UserId = "SYSTEM"
    } else {
        $taskDef.Principal.UserId = $RunAsUserId
    }
    $taskDef.Principal.LogonType = $LogonType
    $taskDef.Principal.RunLevel = 1

    $action = $taskDef.Actions.Create(0)
    $action.Path = $CommandPath
    $action.WorkingDirectory = $WorkDir

    if ($LogonType -eq 5) {
        $folder.RegisterTaskDefinition($Name, $taskDef, 6, $null, $null, 5, $Sddl) | Out-Null
    } else {
        $folder.RegisterTaskDefinition($Name, $taskDef, 6, $RunAsUserId, $null, $LogonType, $Sddl) | Out-Null
    }
}

function Register-FdcDropboxUserStartTaskCmdlet {
    param(
        [string]$Name,
        [string]$ExePath,
        [string]$WorkDir,
        [string]$Account
    )
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    cmd.exe /c "schtasks /Delete /TN `"$Name`" /F >nul 2>nul"

    $action = New-ScheduledTaskAction -Execute $ExePath -WorkingDirectory $WorkDir
    $principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances StopExisting

    Register-ScheduledTask -TaskName $Name -Action $action -Principal $principal -Settings $settings -Force | Out-Null
}

function Register-FdcDropboxRestartTaskCmdlet {
    param(
        [string]$Name,
        [string]$BatPath,
        [string]$WorkDir
    )
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    cmd.exe /c "schtasks /Delete /TN `"$Name`" /F >nul 2>nul"

    $action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $WorkDir
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances StopExisting

    Register-ScheduledTask -TaskName $Name -Action $action -Principal $principal -Settings $settings -Force | Out-Null
}

$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"

try {
    $userOk = $false
    try {
        Register-FdcScheduledTaskCom -Name $UserStartTaskName -CommandPath $dropboxExe -WorkDir $dropboxDir `
            -RunAsUserId $DropboxAccount -LogonType 3 `
            -Description "Start Dropbox desktop sync in $DropboxAccount interactive session (FDC List Creator)" `
            -Sddl $TaskSddl
        $userOk = $true
    } catch {
        Write-Warning "COM user-start task failed: $($_.Exception.Message)"
        try {
            Register-FdcDropboxUserStartTaskCmdlet -Name $UserStartTaskName -ExePath $dropboxExe -WorkDir $dropboxDir -Account $DropboxAccount
            $userOk = $true
        } catch {
            Write-Warning "Cmdlet user-start task failed: $($_.Exception.Message)"
        }
    }

    if (-not $userOk) {
        throw "Could not register user start task '$UserStartTaskName'."
    }

    $restartOk = $false
    $method = ""
    try {
        Register-FdcScheduledTaskCom -Name $RestartTaskName -CommandPath $bat -WorkDir $here `
            -RunAsUserId "SYSTEM" -LogonType 5 `
            -Description "Restart Dropbox sync for FDC List Creator (stop SYSTEM service, start user client)" `
            -Sddl $TaskSddl
        $restartOk = $true
        $method = "COM + SDDL"
    } catch {
        Write-Warning "COM restart task failed: $($_.Exception.Message)"
        try {
            Register-FdcDropboxRestartTaskCmdlet -Name $RestartTaskName -BatPath $bat -WorkDir $here
            $restartOk = $true
            $method = "Register-ScheduledTask cmdlet"
        } catch {
            Write-Warning "Cmdlet restart task failed: $($_.Exception.Message)"
        }
    }

    if (-not $restartOk) {
        throw "Could not register restart task '$RestartTaskName'."
    }
} finally {
    $ErrorActionPreference = $prevEap
}

foreach ($tn in @($UserStartTaskName, $RestartTaskName)) {
    $verify = schtasks.exe /Query /TN $tn 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Task was not created: $tn - $verify"
    }
}

Write-Host "Registered: $UserStartTaskName -> $dropboxExe (as $DropboxAccount, interactive session)"
Write-Host "Registered: $RestartTaskName ($method) -> $bat"
Write-Host ""
Write-Host "Important: $DropboxAccount must be logged in (console/RDP) for sync + tray icon."
Write-Host ""

if (-not $SkipPermissionTest) {
    Write-Host "Testing schtasks /Run as $env:USERDOMAIN\$env:USERNAME ..."
    $runOut = schtasks.exe /Run /TN $RestartTaskName 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "OK: restart task triggered."
        Start-Sleep -Seconds 8
        $procs = Get-Process -Name Dropbox -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -gt 0 }
        if ($procs) {
            Write-Host "OK: Dropbox running in user session(s): $($procs.SessionId -join ', ')"
        } else {
            Write-Warning "No Dropbox.exe in a user session yet - ensure $DropboxAccount is logged in."
        }
    } else {
        Write-Warning "schtasks /Run failed: $runOut"
    }
}

Write-Host ""
Write-Host "If restart task stuck: schtasks /End /TN `"$RestartTaskName`""
