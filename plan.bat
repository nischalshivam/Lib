@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title media_index - Plan

REM ------------------------------------------------------------------------
REM  Double-click: ek script ko catalogue se match karta hai aur shot-list
REM  banata hai — script ki har line pe konsa asli movie/episode moment lagega.
REM  Catalogue ki jagah poori series ka FOLDER bhi de sakte ho.
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
echo     Script ko library se match karke shot-list banao
echo   ================================================================
echo.
echo   1) Visual (genspark) script ka path daalo:
set /p "SCRIPT=  Script: "
set SCRIPT=%SCRIPT:"=%
if not defined SCRIPT ( echo   Koi script nahi diya. & pause & exit /b 1 )

echo.
echo   2) Catalogue: ek catalog.json, YA poori series ka FOLDER
echo      (jaise E:\Movies\Breaking Bad — saari episodes ki library merge hogi):
set /p "CAT=  Catalogue: "
set CAT=%CAT:"=%
if not defined CAT ( echo   Koi catalogue nahi diya. & pause & exit /b 1 )

REM shot-list ko script ke paas plan.json me likho, taaki bhejni aasan ho
for %%F in ("%SCRIPT%") do set "OUTDIR=%%~dpF"
set "OUT=%OUTDIR%plan.json"

echo.
echo   Chalu ho raha hai...
echo.
%PY% -m media_index plan "%SCRIPT%" "%CAT%" --out "%OUT%"

echo.
echo   shot-list yahan bani: %OUT%
echo.
pause
