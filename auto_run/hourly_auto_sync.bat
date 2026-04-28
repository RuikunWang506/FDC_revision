@echo off
REM FDC Hourly Auto Sync — Dropbox fetch + DAM index sync + cleanup
REM Secret: DAM_CRON_SECRET env OR auto_run\dam_cron_secret.local.txt
REM One-time setup: powershell -ExecutionPolicy Bypass -File "%~dp0Setup-DamCron.ps1"

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.."
set "ROOT=%CD%"
popd

set "LOG=%ROOT%\hourly_sync.log"

echo [%date% %time%] Hourly Auto Sync Started >> "%LOG%"

if "%DAM_CRON_SECRET%"=="" if exist "%SCRIPT_DIR%dam_cron_secret.local.txt" (
    for /f "usebackq delims=" %%a in ("%SCRIPT_DIR%dam_cron_secret.local.txt") do set "DAM_CRON_SECRET=%%a"
)
if "!DAM_CRON_SECRET!"=="" (
    echo [%date% %time%] ERROR: No secret. Run Setup-DamCron.ps1 >> "%LOG%"
    exit /b 1
)

if "%DAM_SYNC_BASE_URL%"=="" if exist "%SCRIPT_DIR%dam_sync_base_url.local.txt" (
    for /f "usebackq delims=" %%u in ("%SCRIPT_DIR%dam_sync_base_url.local.txt") do set "DAM_SYNC_BASE_URL=%%u"
)
if "%DAM_SYNC_BASE_URL%"=="" set "DAM_SYNC_BASE_URL=http://127.0.0.1:5555"

curl -s -S -X POST "!DAM_SYNC_BASE_URL!/dam/cron/start" ^
  -H "Content-Type: application/json" ^
  -H "X-DAM-Cron-Secret: !DAM_CRON_SECRET!" ^
  -d "{\"force_rebuild_index\": false, \"include_fetch\": true, \"include_cleanup\": true}" ^
  >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo [%date% %time%] Hourly Auto Sync curl finished >> "%LOG%"
