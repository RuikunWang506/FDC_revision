@echo off
setlocal enableextensions enabledelayedexpansion

rem Determine script directory (trailing backslash retained)
set "SCRIPT_DIR=%~dp0"

echo.
echo ====== FDC Startup ======
echo Script directory: %SCRIPT_DIR%

rem Configure Windows Task Scheduler for Smart Auto-Filter (runs every 3 days at 2:00 AM)
set "TASK_NAME=FDC Smart Auto-Filter"
set "SMART_FILTER=%SCRIPT_DIR%py\standalone\smart_auto_filter.py"

rem Check if scheduled task already exists
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if %errorlevel% equ 0 goto :skip_task_create

echo [Scheduler] Creating task "%TASK_NAME%" to run: python "%SMART_FILTER%"
schtasks /create /tn "%TASK_NAME%" /tr "cmd /c \"cd /d %SCRIPT_DIR% && python py\\standalone\\smart_auto_filter.py\"" /sc daily /mo 3 /st 02:00 /ru "SYSTEM" /f
if %errorlevel% equ 0 (
	echo [Scheduler] Successfully created "%TASK_NAME%" (every 3 days at 2:00 AM)
) else (
	echo [Scheduler] Failed to create "%TASK_NAME%". Try running this script as Administrator.
)

:skip_task_create

echo.
echo [App] Starting Flask app in background...
cd /d "%SCRIPT_DIR%"
start "" /b pythonw "%SCRIPT_DIR%app.py" >"%SCRIPT_DIR%output_new.log" 2>&1
echo [App] Launched. Logs: %SCRIPT_DIR%output_new.log

endlocal
exit /b 0