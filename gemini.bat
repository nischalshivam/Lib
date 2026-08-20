@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title media_index - Gemini

REM ------------------------------------------------------------------------
REM  Double-click this file to test the vision model (Gemini).
REM
REM  `mi gemini` command sirf tool ke folder ke andar se chalti hai. Isliye
REM  ye file banayi — ise bas double-click karo, ye khud sahi jagah se test
REM  chalati hai aur result screen par rakhti hai.
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
echo     Gemini (vision model) ki jaanch
echo   ================================================================
echo.

%PY% -m media_index gemini

echo.
pause
