@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM Restart Dropbox sync for the logged-in Windows user (stream).
REM Stops the NSSM SYSTEM service + all Dropbox.exe, then starts the user-session client.
set "SVC=dropbox"
set "USER_TASK=FDC Start Dropbox User"
set "WAIT=0"

:wait_not_pending
sc query %SVC% | findstr /I "STOP_PENDING START_PENDING" >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    set /a WAIT+=1
    if !WAIT! GEQ 30 goto kill_processes
    timeout /t 2 /nobreak >nul
    goto wait_not_pending
)

sc query %SVC% | findstr /I "RUNNING" >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    net stop %SVC% >nul 2>&1
    set "WAIT=0"
    :wait_stopped
    sc query %SVC% | findstr /I "STOPPED" >nul 2>&1
    if !ERRORLEVEL! EQU 0 goto kill_processes
    set /a WAIT+=1
    if !WAIT! GEQ 30 goto kill_processes
    timeout /t 2 /nobreak >nul
    goto wait_stopped
)

:kill_processes
taskkill /IM Dropbox.exe /F >nul 2>&1
timeout /t 3 /nobreak >nul

schtasks /Query /TN "%USER_TASK%" >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo User start task "%USER_TASK%" is missing. Run Setup-DropboxRestartTask.ps1 as Admin.
    exit /b 1
)

schtasks /Run /TN "%USER_TASK%" >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo schtasks /Run "%USER_TASK%" failed.
    exit /b 1
)

REM Give the tray/sync client a moment to spawn in the user session.
timeout /t 5 /nobreak >nul
exit /b 0
