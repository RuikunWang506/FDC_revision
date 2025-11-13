@echo off
REM FDC Weekly Complete Rebuild - Runs Sunday 3 AM
REM Performs full system rebuild: rescan + rebuild + fetch + sync + cleanup

echo [%date% %time%] Weekly Complete Rebuild Started >> C:\FDC_FLASK_APP\log.txt

REM Call the DAM sync endpoint with full rebuild:
REM force_rebuild_index=True (rescan all directories)
REM include_fetch=True (fetch from Dropbox)
REM include_cleanup=True (remove broken links)

curl -X POST http://localhost:5000/dam/start ^
  -H "Content-Type: application/json" ^
  -d "{\"force_rebuild_index\": true, \"include_fetch\": true, \"include_cleanup\": true}" ^
  >> C:\FDC_FLASK_APP\log.txt 2>&1

echo [%date% %time%] Weekly Complete Rebuild Completed >> C:\FDC_FLASK_APP\log.txt
