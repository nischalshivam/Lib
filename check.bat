@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title media_index - check a folder

set "TARGET=%~1"
if "%TARGET%"=="" (
    echo.
    echo   Drag a media folder onto this file, or run:
    echo.
    echo      check.bat "D:\Breaking Bad Season 2"
    echo.
    set /p "TARGET=   Folder to check: "
)
if "%TARGET%"=="" exit /b 1

set "PY="
where python >nul 2>&1
if %errorlevel%==0 set "PY=python"
if not defined PY (
    where py >nul 2>&1
    if !errorlevel!==0 set "PY=py -3"
)
if not defined PY (
    echo.
    echo   Python was not found. Run setup.bat first.
    echo.
    pause
    exit /b 1
)

echo.
%PY% -m media_index check "%TARGET%"
echo.
pause
