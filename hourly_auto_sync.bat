@echo off
REM FDC Hourly Auto Sync - Runs Mon-Fri 9am-5pm
REM Performs incremental sync: fetch + sync + cleanup

echo [%date% %time%] Hourly Auto Sync Started >> C:\FDC_FLASK_APP\log.txt

REM Call the DAM sync endpoint with parameters:
REM force_rebuild_index=False (use existing index, faster)
REM include_fetch=True (fetch from Dropbox)
REM include_cleanup=True (remove broken links)

curl -X POST http://localhost:5000/dam/start ^
  -H "Content-Type: application/json" ^
  -d "{\"force_rebuild_index\": false, \"include_fetch\": true, \"include_cleanup\": true}" ^
  >> C:\FDC_FLASK_APP\log.txt 2>&1

echo [%date% %time%] Hourly Auto Sync Completed >> C:\FDC_FLASK_APP\log.txt
