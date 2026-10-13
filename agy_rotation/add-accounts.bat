@echo off
title Add Google Accounts (auto-saver)
REM Double-click this. Then just sign accounts into agy one by one -
REM each new login saves automatically. No commands to type.
cd /d "%~dp0"
python watch_enroll.py
echo.
pause
