@echo off
REM Generic wrapper - pass any media_index command straight through:
REM   mi.bat check  "D:\Breaking Bad Season 2"
REM   mi.bat build  "D:\Breaking Bad Season 2" --db library.db --verify-sync
REM   mi.bat find   "I am the one who knocks" --db library.db
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1

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

if "%~1"=="" (
    echo.
    echo   Easier: double-click  start.bat  for a menu.

    echo   Or use this directly:  mi.bat ^<command^> [options]
    echo.
    echo     mi.bat check    "D:\Breaking Bad Season 2"
    echo     mi.bat build    "D:\Breaking Bad Season 2" --db library.db --verify-sync
    echo     mi.bat stats    --db library.db
    echo     mi.bat find     "I am the one who knocks" --db library.db
    echo     mi.bat run      jobs.json
    echo.
    pause
    exit /b 0
)

%PY% -m media_index %*
echo.
pause
