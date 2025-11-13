@echo off
REM DAM Cleanup Script
REM This script calls the DAM cleanup endpoint to mark missing files as inactive

cd /d C:\FDC_FLASK_APP

REM Log start
python -c "from py.utilities.logging_setup import log_event; log_event('info', 'DAM Cleanup scheduled task started')"

REM Call cleanup endpoint via curl or python
python -c "import requests; from py.utilities.logging_setup import log_event; r = requests.post('http://localhost:5000/dam/cleanup'); result = r.json(); log_event('info', f'DAM Cleanup completed: {result.get(\"cleaned\", 0)} files marked inactive out of {result.get(\"total_checked\", 0)} checked')" 2>&1

REM Log completion
python -c "from py.utilities.logging_setup import log_event; log_event('info', 'DAM Cleanup scheduled task finished')"

exit /b 0
