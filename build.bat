@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title media_index - Build Video

REM ------------------------------------------------------------------------
REM  Double-click: genspark script + library + voiceover se poori VIDEO banao.
REM  Matched shots ko source episodes se cut karke, voiceover ke upar time
REM  karke, final mp4 render karta hai. (ffmpeg + source video files chahiye.)
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
echo     Script + Library + Voiceover  ==^>  Final Video
echo   ================================================================
echo.
echo   1) Genspark (visual) script ka path:
set /p "SCRIPT=  Script: "
set SCRIPT=%SCRIPT:"=%
if not defined SCRIPT ( echo   Koi script nahi diya. & pause & exit /b 1 )

echo.
echo   2) Catalogue: catalog.json YA poori series ka folder
echo      (jaise E:\Movies\Breaking Bad):
set /p "CAT=  Catalogue: "
set CAT=%CAT:"=%
if not defined CAT ( echo   Koi catalogue nahi diya. & pause & exit /b 1 )

echo.
echo   3) Voiceover / narration audio (mp3 ya wav):
set /p "AUD=  Audio: "
set AUD=%AUD:"=%
if not defined AUD ( echo   Koi audio nahi diya. & pause & exit /b 1 )

echo.
echo   4) (Recommended) Clean narration (poori) script ka path — accurate
echo      timing ke liye. Skip karne ke liye Enter dabao:
set /p "NARR=  Clean narration: "
set NARR=%NARR:"=%

echo.
echo   5) (IMPORTANT) Cast folder — har character ka subfolder + 5-8 reference
echo      photos (jaise cast\Victor\1.jpg, cast\Hank\1.jpg). Isse tool sahi
echo      character verify karta hai (Victor ko Hank se alag). Skip = Enter:
set /p "CASTDIR=  Cast folder: "
set CASTDIR=%CASTDIR:"=%

echo.
echo   6) (Optional) Sirf ek episode tak seemit? (jaise S04E01)
echo      Poore essay ke liye khaali chhod ke Enter dabao:
set /p "SCOPE=  Scope: "
set SCOPE=%SCOPE:"=%

set "ARGS=makevideo "%SCRIPT%" "%CAT%" "%AUD%""
if defined NARR if not "!NARR!"=="" set "ARGS=!ARGS! --narration "!NARR!""
if defined CASTDIR if not "!CASTDIR!"=="" set "ARGS=!ARGS! --cast "!CASTDIR!""
if defined SCOPE if not "!SCOPE!"=="" set "ARGS=!ARGS! --scope "!SCOPE!""

echo.
echo   Video ban rahi hai... (clips cut + render me kuch minute lag sakte hain)
echo.
%PY% -m media_index !ARGS!

echo.
pause
