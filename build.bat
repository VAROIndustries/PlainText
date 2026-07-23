@echo off
:: Build standalone .exe files for both PlainText apps
echo Building PlainTextMonitor.exe...
"C:\Users\gvaro\AppData\Local\Programs\Python\Python312\python.exe" -m PyInstaller --onefile --noconsole --name PlainTextMonitor plaintext_monitor.py
copy /Y dist\PlainTextMonitor.exe PlainTextMonitor.exe >nul

echo Building PlainTextForClaude.exe...
"C:\Users\gvaro\AppData\Local\Programs\Python\Python312\python.exe" -m PyInstaller --onefile --noconsole --name PlainTextForClaude plaintext_claude.py
copy /Y dist\PlainTextForClaude.exe PlainTextForClaude.exe >nul

echo Done. Both .exe files updated in project root.
