@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title media_index - overnight indexing

REM ------------------------------------------------------------------------
REM  Both halves of building a library, one after the other, unattended.
REM
REM      overnight.bat "D:\Game of Thrones" got.db
REM
REM  ...or just drag the show's folder onto this file.
REM
REM  Half one reads the subtitles: minutes, and the thing everything else
REM  depends on. Half two reads the pictures: hours, and the reason this is
REM  a thing you start before going to bed.
REM
REM  Nothing here asks a question after it starts. A machine that goes to
REM  sleep at 2am with a "Continue? [y/n]" on screen has wasted the night,
REM  which is the one failure this file exists to prevent.
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

set "TARGET=%~1"
if "%TARGET%"=="" (
    echo.
    echo   Show ka folder is file par drag karo, ya:
    echo.
    echo      overnight.bat "D:\Game of Thrones" got.db
    echo.
    pause
    exit /b 1
)

set "DB=%~2"
if "%DB%"=="" set "DB=library.db"

REM Ek library par ek hi kaam. Agar pehle se koi chal raha hai, ye file
REM bata deti hai aur ruk jaati hai - kyonki do kaam ek saath chalane par
REM dono ruk jaate hain aur DONO chalte hue dikhte hain.
if exist "%DB%.lock" (
    echo.
    echo   RUKO. Is library par pehle se koi kaam chal raha hai:
    echo.
    type "%DB%.lock"
    echo.
    echo.
    echo   Browser me Library page khula ho to wahan dekho, ya doosri CMD
    echo   window band karo. Dono ek saath chalane se dono ruk jaate hain.
    echo.
    echo   Agar wo kaam sach me band ho chuka hai ^(laptop band ho gaya tha^),
    echo   to 30 minute baad ye lock apne aap khatam ho jaata hai - ya ise
    echo   khud delete kar do:
    echo       %DB%.lock
    echo.
    pause
    exit /b 1
)

REM Windows ko raat bhar jaagta rakho. Ek sleeping laptop 8 ghante ka kaam
REM 40 minute me rok deta hai aur subah kuch aisa dikhta hai jaise crash ho
REM gaya ho.
powercfg /change standby-timeout-ac 0 >nul 2>&1
powercfg /change hibernate-timeout-ac 0 >nul 2>&1
powercfg /change monitor-timeout-ac 15 >nul 2>&1

echo.
echo   ================================================================
echo     Folder : %TARGET%
echo     Library: %DB%
echo.
echo     Ruk gaya tha? Koi baat nahi - jo episodes ho chuke hain wo
echo     dobara nahi honge. Ye wahin se aage badhta hai.
echo   ================================================================
echo.
echo   [1/2] subtitles padhe ja rahe hain (kuch minute)...
echo.

%PY% -m media_index build "%TARGET%" --db "%DB%"
if errorlevel 1 (
    echo.
    echo   Subtitles wala step fail ho gaya. Picture wala step nahi chalega,
    echo   kyonki uska koi matlab nahi jab tak episodes index na hon.
    echo.
    pause
    exit /b 1
)

echo.
echo   [2/2] ab pictures padhe ja rahe hain. YE LAMBA HAI - poori raat.
echo         Ise band mat karna. Subah tak khud ruk jayega.
echo.

%PY% -m media_index look --db "%DB%"

echo.
echo   ================================================================
echo     Ho gaya. Ab dekho kya-kya index hua:
echo   ================================================================
echo.
%PY% -m media_index stats --db "%DB%"
echo.
echo   Ye window band kar sakte ho.
echo.
pause
