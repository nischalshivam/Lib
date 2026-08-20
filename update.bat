@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title media_index - update

REM ------------------------------------------------------------------------
REM  cmd reads a batch file from disk as it runs, remembering a byte offset
REM  between lines. This script's whole job is to overwrite the folder it
REM  lives in - including itself - so the moment the copy lands, cmd carries
REM  on reading at that same offset in a DIFFERENT file and executes whatever
REM  fragment happens to sit there. That is where
REM
REM      'DIRTMPDIR'' is not recognized as an internal or external command
REM
REM  came from, immediately after a copy that had in fact succeeded.
REM
REM  So the real work runs from a copy in TEMP, which nothing is going to
REM  replace underneath it.
REM ------------------------------------------------------------------------
if /i "%~1"=="--worker" goto work

set "TARGET=%CD%"
copy /y "%~f0" "%TEMP%\mi_update_worker.bat" >nul
if errorlevel 1 (
    echo.
    echo   Could not write to %TEMP% - update not attempted.
    echo.
    pause
    exit /b 1
)
"%TEMP%\mi_update_worker.bat" --worker "%TARGET%"
exit /b %errorlevel%

:work
set "TARGET=%~2"
cd /d "%TARGET%"

echo.
echo   Fetching the latest version...
echo.

set "BRANCH=claude/video-clip-relevance-issue-khs33k"
set "ZIPURL=https://github.com/nischalshivam/Claude/archive/refs/heads/%BRANCH%.zip"
set "TMPZIP=%TEMP%\media_index_update.zip"
set "TMPDIR=%TEMP%\media_index_update"

REM  $ProgressPreference is not cosmetic. Invoke-WebRequest redraws its
REM  progress bar on every buffer, and on a console that dominates the
REM  transfer - the same download runs many times faster with it silenced.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';" ^
  "Invoke-WebRequest -Uri '%ZIPURL%' -OutFile '%TMPZIP%' -UseBasicParsing;" ^
  "$mb = [math]::Round((Get-Item '%TMPZIP%').Length / 1MB, 1);" ^
  "Write-Host \"  downloaded $mb MB\";" ^
  "if (Test-Path '%TMPDIR%') { Remove-Item -Recurse -Force '%TMPDIR%' };" ^
  "Expand-Archive -Path '%TMPZIP%' -DestinationPath '%TMPDIR%' -Force;" ^
  "$src = Get-ChildItem -Path '%TMPDIR%' -Directory | Select-Object -First 1;" ^
  "$shared = Join-Path $src.FullName 'shared';" ^
  "Copy-Item -Path (Join-Path $shared '*') -Destination '%TARGET%' -Recurse -Force;" ^
  "Write-Host '  updated'"

if errorlevel 1 (
    echo.
    echo   Update failed. Check the internet connection, or download the ZIP
    echo   by hand from:
    echo     %ZIPURL%
    echo.
    pause
    exit /b 1
)

del "%TMPZIP%" >nul 2>&1
rmdir /s /q "%TMPDIR%" >nul 2>&1

echo.
echo   Done. Nothing of yours was touched - settings.txt, library.db,
echo   proof\ and built\ are not in the download and were left alone.
echo.
pause
exit /b 0
