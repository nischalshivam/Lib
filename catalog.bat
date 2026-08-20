@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title media_index - Catalog

REM ------------------------------------------------------------------------
REM  Double-click: poori movie/episode ko tag karke ek searchable library
REM  (catalog.json) banata hai. Har shot ko Gemini dekhta hai aur likhta hai
REM  kaun/kya/kaisa shot hai. Ye ek-baar ka kaam hai — har video reuse karega.
REM
REM  Pehli baar 15 minute ka test karo (sasta), phir poori video.
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
echo     Movie / episode ko tag karke library banao
echo   ================================================================
echo.
echo   Video file ka path daalo — YA poori series/season ka FOLDER
echo   (folder doge to us folder ke saare episodes catalogue honge):
set /p "VIDEO=  Video ya folder: "
if not defined VIDEO (
    echo   Koi video/folder nahi diya.
    pause
    exit /b 1
)
REM strip surrounding quotes if the path was dragged in
set VIDEO=%VIDEO:"=%

echo.
echo   Sirf pehle kitne MINUTE tag karne hain? Sirf NUMBER likho — jaise: 15
echo   Poori video ke liye khaali chhod ke Enter dabao.
set /p "MINS=  Minutes (number only): "

REM Agar "15 minutes" jaisa kuch type ho jaye (number ke baad extra shabd),
REM sirf pehla, space se pehle wala hissa lo — taaki ek extra shabd poori
REM command hi na tod de. Asli sanity check (kya ye sach me number hai)
REM Python side (cmd_catalog) khud karta hai aur saaf error deta hai.
if defined MINS for /f "tokens=1" %%A in ("!MINS!") do set "MINS=%%A"

echo.
echo   (Optional) Character naam consistent karne ke liye ek file de sakte ho —
echo   ek line ek banda, aliases '=' ke baad. Jaise:
echo       Arthur = Arthur Fleck, Joker, Joaquin Phoenix
echo       Murray = Murray Franklin
echo   File ka path daalo, ya skip karne ke liye Enter dabao:
set /p "CHARS=  characters.txt (optional): "
set CHARS=%CHARS:"=%

echo.
echo   (SABSE ZAROORI) Cast folder — har character ka subfolder + 5-8 reference
echo   photos (jaise cast\Victor\1.jpg, cast\Hank\1.jpg). Isse model catalog
echo   BANATE WAQT hi sahi character pehchanta hai (Victor ko Hank se alag).
echo   Yehi library ki foundation accurate banata hai. Skip = Enter:
set /p "CASTDIR=  Cast folder: "
set CASTDIR=%CASTDIR:"=%

set "ARGS=catalog "%VIDEO%""
if defined MINS if not "!MINS!"=="" set "ARGS=!ARGS! --minutes "!MINS!""
if defined CHARS if not "!CHARS!"=="" set "ARGS=!ARGS! --characters "!CHARS!""
if defined CASTDIR if not "!CASTDIR!"=="" set "ARGS=!ARGS! --cast "!CASTDIR!""

echo.
echo   Chalu ho raha hai... (pehle 'mi gemini' se key check kar lena agar error aaye)
echo.
%PY% -m media_index !ARGS!

echo.
pause
