@echo off
REM ProStudio Launcher — Clean + Clue + Audio -> finished, effects-edited video.
REM Double-click this file. The window opens; add a video (three files) and press Run.
cd /d "%~dp0"
python studio.py --gui
if errorlevel 1 pause
