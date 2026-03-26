@echo off
echo Installing PlainText Monitor dependencies...
pip install -r "%~dp0requirements.txt"
echo.
echo Done! Run run.bat to start the app.
pause
