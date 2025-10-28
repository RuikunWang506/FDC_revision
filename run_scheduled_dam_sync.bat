@echo off
REM DAM Sync Service - Scheduled Task Runner
REM Run this every 3 days via Windows Task Scheduler

cd /d C:\FDC_FLASK_APP

echo ================================================
echo DAM Sync Service - Scheduled Run
echo Time: %date% %time%
echo ================================================

REM Activate virtual environment if you have one
REM call venv\Scripts\activate

REM Run the scheduled sync (only runs if 3+ days since last sync)
python py\utilities\dam_sync_service_v2.py --scheduled

echo.
echo ================================================
echo Scheduled sync check completed
echo ================================================

REM Keep window open for 10 seconds to see results
timeout /t 10
