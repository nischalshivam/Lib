@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title media_index - Gold (report card)

REM ------------------------------------------------------------------------
REM  Ye file ek "report card" banati hai kisi BANI HUI video ka.
REM  Nayi video banane ki zaroorat nahi. Bas ek double-click:
REM    1. video ke output folder ka path maango,
REM    2. usme se manifest.json padho, ek sheet (gold.csv) banao,
REM    3. wo sheet Notepad me khud khol do,
REM    4. user bhare (exact/ok/wrong/none), save kare, band kare,
REM    5. phir khud score nikaal ke asli accuracy dikha do.
REM ------------------------------------------------------------------------

set "PY="
where python >nul 2>&1
if %errorlevel%==0 set "PY=python"
if not defined PY (
    where py >nul 2>&1
    if !errorlevel!==0 set "PY=py -3"
)
if not defined PY (
    echo.
    echo   Python nahi mila. Pehle setup.bat chalao.
    echo.
    pause
    exit /b 1
)

echo.
echo   ================================================================
echo     Report card — bani hui video kitni sahi hai
echo   ================================================================
echo.
echo   Wo folder chahiye jisme tumhari BANI HUI video hai (usme
echo   manifest.json aur video.mp4 hote hain).
echo.
echo   Example:  C:\Users\Dell\Downloads\Test Gus
echo.
set /p "OUT=  Folder ka path yahan paste karo:  "

REM  Quotes hata do agar user ne paste kiye ho
set "OUT=!OUT:"=!"

if not exist "!OUT!\manifest.json" (
    echo.
    echo   Is folder me manifest.json nahi mila:
    echo     !OUT!
    echo   Wahi folder do jisme video.mp4 aur manifest.json hai.
    echo.
    pause
    exit /b 1
)

set "SHEET=!OUT!\gold.csv"

echo.
echo   Sheet bana rahe hain...
%PY% -m media_index gold --template "!OUT!\manifest.json" --out "!SHEET!"
if errorlevel 1 (
    echo.
    echo   Sheet nahi ban payi. Upar error dekho.
    echo.
    pause
    exit /b 1
)

echo.
echo   ----------------------------------------------------------------
echo   Ab Notepad khul raha hai. Har line ke saamne 'verdict' me likho:
echo.
echo       exact  = bilkul sahi moment
echo       ok     = sahi scene/character, thoda idhar-udhar — chalega
echo       wrong  = galat footage
echo       none   = card / khaali
echo.
echo   Video chala ke, scene number se milaan karke bharo. Phir SAVE
echo   karo (Ctrl+S) aur Notepad BAND kar do. Uske baad yahan wapas aao.
echo   ----------------------------------------------------------------
echo.
pause

start /wait notepad "!SHEET!"

echo.
echo   ================================================================
echo     Asli accuracy — tumne jo bhara uska hisaab
echo   ================================================================
echo.
%PY% -m media_index gold --score "!SHEET!"
echo.
echo   Ye number Claude aur GPT ko bhej do. Yahi asli sach hai.
echo.
pause
