@echo off
setlocal enabledelayedexpansion
set max_retries=3
set retry_count=0

:retry
powershell.exe -Command "try { Invoke-WebRequest -Uri 'https://fdcef.com/dam/cleanup' -Method POST; exit 0 } catch { exit 1 }"
if %errorlevel% equ 0 (
    echo Success
    exit /b 0
) else (
    set /a retry_count+=1
    if !retry_count! lss %max_retries% (
        echo Retry !retry_count! of %max_retries%
        timeout /t 10 /nobreak > nul
        goto retry
    ) else (
        echo Failed after %max_retries% retries
        exit /b 1
    )
)