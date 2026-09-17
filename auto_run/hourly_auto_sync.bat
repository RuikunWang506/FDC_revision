@echo off
REM LEGACY: was "hourly" Task Scheduler. Use daily_auto_sync.bat — task name is now "FDC DAM Daily Sync" (every 24h).
REM You can still run this file manually; it performs the same POST as daily_auto_sync.bat.
REM Secret: DAM_CRON_SECRET env OR auto_run\dam_cron_secret.local.txt
REM Setup: powershell -ExecutionPolicy Bypass -File "%~dp0Setup-DamCron.ps1 -RegisterScheduledTasks"

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
    -d "{\"force_rebuild_index\": false, \"include_fetch\": true, \"include_cleanup\": true, \"include_oms_backfill\": true, \"include_oos_wipe\": true}" ^
  >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo [%date% %time%] Hourly Auto Sync curl finished >> "%LOG%"
