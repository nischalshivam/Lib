@echo off
REM The Wire test video - started once by libjobs\supervisor.py after the priority indexing queue is done.
REM Same environment as Desktop\ProStudio\Movie Editor.bat (python.exe, VTEXT_FFMPEG = system ffmpeg).
title Wire test video
cd /d "D:\Lib"
set "PY=C:\Users\Dell\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=python"
for /f "delims=" %%i in ('where ffmpeg 2^>nul') do if not defined VTEXT_FFMPEG set "VTEXT_FFMPEG=%%i"
"%PY%" -u tools\wire_test.py >> tools\wire_test.log 2>&1
