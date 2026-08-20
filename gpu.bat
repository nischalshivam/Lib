@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title media_index - GPU

REM ------------------------------------------------------------------------
REM  Pehli baar ye file me torch ka version haath se likha hua tha (2.5.1).
REM  Wo galat tha, aur galat hi rehta: pinned version ek daawa hai ki KISI
REM  AUR computer par kya tha. Python 3.14 aate hi wo daawa jhootha ho gaya
REM  aur error ne network ko blame kiya:
REM
REM      ERROR: Could not find a version that satisfies the requirement
REM             torch==2.5.1 (from versions: none)
REM
REM  Ab yahan koi version nahi likha hai. Saara faisla Python me hota hai —
REM  index se poochh kar ki IS Python ke liye kya maujood hai, aur card par
REM  ek asli multiply chala kar. Ye file bas usse bulati hai.
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
echo     GPU ki jaanch
echo   ================================================================
echo.

%PY% -m media_index gpu
if not errorlevel 1 (
    echo.
    echo   Sab theek hai — kuch karne ki zarurat nahi.
    echo.
    pause
    exit /b 0
)

echo.
echo   ----------------------------------------------------------------
echo   Ab dekhte hain ki is Python ke liye CUDA wala torch bana bhi hai
echo   ya nahi. Agar bana hai to wo ~2.5 GB download hai aur CPU wale
echo   torch ko replace karega.
echo.
echo   Aage badhna hai? Ctrl+C dabao rukne ke liye.
echo   ----------------------------------------------------------------
pause

echo.
%PY% -m media_index gpu --install
echo.
echo   Agar upar "GPU par 64x64 multiply chal gaya" likha hai to indexing
echo   ab GPU par chalegi. Agar nahi, to tool CPU par chalta rahega — wo
echo   dhima hai, galat nahi. Ye correctness ki problem nahi hai.
echo.
pause
