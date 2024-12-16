@echo off
echo Creating registry entry for SuperBrowser custom protocol...
reg add "HKEY_CLASSES_ROOT\superbrowser" /ve /d "URL:SuperBrowser Protocol" /f
reg add "HKEY_CLASSES_ROOT\superbrowser" /v "URL Protocol" /f
reg add "HKEY_CLASSES_ROOT\superbrowser\shell" /f
reg add "HKEY_CLASSES_ROOT\superbrowser\shell\open" /f
reg add "HKEY_CLASSES_ROOT\superbrowser\shell\open\command" /ve /d "\"C:\Users\Kyle\SuperBrowser\5.245.1.19\Core\Launcher.exe\" \"%%1\"" /f
echo Custom protocol 'superbrowser' created successfully!
pause
