@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title media_index setup

echo.
echo ============================================
echo   media_index - one time setup
echo ============================================
echo.

REM ---------------------------------------------------------------- Python
set "PY="
where python >nul 2>&1
if %errorlevel%==0 set "PY=python"
if not defined PY (
    where py >nul 2>&1
    if !errorlevel!==0 set "PY=py -3"
)
if not defined PY (
    echo [X] Python was not found.
    echo.
    echo     Install it from https://www.python.org/downloads/
    echo     IMPORTANT: tick "Add python.exe to PATH" during install.
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('%PY% -V 2^>^&1') do set "PYVER=%%v"
echo [OK] Python !PYVER!  ^(%PY%^)

REM ------------------------------------------------------- optional speedup
echo.
echo Installing the optional speed-up (rapidfuzz)...
%PY% -m pip install --quiet --disable-pip-version-check rapidfuzz >nul 2>&1
if %errorlevel%==0 (
    echo [OK] rapidfuzz installed
) else (
    echo [--] rapidfuzz could not be installed - not a problem,
    echo      the tool falls back to Python's own matcher.
)

echo.
echo Some downloads arrive with no subtitles at all. The tool can make them
echo from the audio, which needs one extra package (a few hundred MB).
set /p "WANTTX=Install it now? [Y/n]: "
if /i "!WANTTX!"=="n" (
    echo [--] skipped - install later with:  pip install faster-whisper
) else (
    echo     installing faster-whisper...
    %PY% -m pip install --quiet --disable-pip-version-check faster-whisper
    if !errorlevel!==0 (
        echo [OK] faster-whisper installed
    ) else (
        echo [--] install failed - you can still use downloaded .srt files
    )
)

REM ------------------------------------------------------- the picture model
echo.
echo The tool can LOOK at your footage and check that every shot really
echo shows what the script asked for, instead of inferring it from one
echo quoted line. That needs a picture model - about 2 GB of packages, and
echo a 1 GB download the first time it runs. After that it works offline.
echo.
echo Without it everything still works; shots are placed by inference only.
echo.
set "WANTCV="
set /p "WANTCV=Install it now? [Y/n]: "
if /i "!WANTCV!"=="n" (
    echo [--] skipped - install later with:
    echo      pip install torch transformers sentencepiece
) else (
    echo     installing torch, transformers, sentencepiece...
    echo.
    echo     torch alone is about 2.5 GB. On a normal connection this takes
    echo     10 to 25 minutes. Progress bars are left ON below on purpose -
    echo     a silent screen for twenty minutes looks exactly like a freeze,
    echo     and it is not one. Leave it alone until it finishes.
    echo.
    %PY% -m pip install --disable-pip-version-check torch transformers sentencepiece
    if !errorlevel!==0 (
        echo [OK] picture model packages installed
    ) else (
        echo [--] install failed - the tool still runs, it just cannot
        echo      check shots against the picture.
    )
)

REM ------------------------------------------------------------------ ffmpeg
echo.
set "FFOK="
where ffmpeg >nul 2>&1
if %errorlevel%==0 set "FFOK=1"

if defined FFOK (
    for /f "tokens=3" %%v in ('ffmpeg -version 2^>^&1 ^| findstr /b "ffmpeg version"') do (
        echo [OK] ffmpeg %%v
        goto :ffdone
    )
    echo [OK] ffmpeg found
    goto :ffdone
)

echo [X] ffmpeg was not found - it is REQUIRED for checking and cutting.
echo.
where winget >nul 2>&1
if %errorlevel%==0 (
    echo     Trying to install it automatically with winget...
    echo.
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    echo.
    echo     If that succeeded, CLOSE this window, open a NEW Command Prompt,
    echo     and run setup.bat again so the new PATH is picked up.
) else (
    echo     Install it one of these ways:
    echo       1^) Open PowerShell and run:   winget install Gyan.FFmpeg
    echo       2^) Or download from          https://www.gyan.dev/ffmpeg/builds/
    echo          ^(pick "release essentials", unzip, and add its bin folder to PATH^)
    echo.
    echo     Then run setup.bat again.
)
echo.
pause
exit /b 1

:ffdone
REM ------------------------------------------------------------- self test
echo.
echo Running the self test...
set "SELFTEST=%TEMP%\mi_selftest.txt"
%PY% -m unittest discover tests > "!SELFTEST!" 2>&1
if !errorlevel!==0 (
    echo [OK] all tests passed
) else (
    echo [--] some tests failed. WHICH ones is the whole point, so they are
    echo      printed here - "some tests failed" on its own tells nobody
    echo      anything, least of all Claude.
    echo.
    findstr /b /c:"FAIL:" /c:"ERROR:" /c:"Ran " /c:"FAILED" "!SELFTEST!"
    echo.
    echo      full output saved to: !SELFTEST!
    echo      send that file to Claude.
)

echo.
echo ============================================
echo   Setup finished.
echo ============================================
echo.
echo Next step - check a downloaded folder:
echo.
echo    check.bat "D:\Breaking Bad Season 2"
echo.
echo ...or just drag the folder onto check.bat
echo.
pause
