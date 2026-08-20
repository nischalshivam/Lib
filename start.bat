@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title media_index

REM ---------------------------------------------------------------- Python
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

REM -------------------------------------------- remember the last used paths
set "MEDIA="
set "DB=library.db"
set "LIBS="
if exist "settings.txt" (
    for /f "usebackq tokens=1,* delims==" %%a in ("settings.txt") do (
        if /i "%%a"=="media" set "MEDIA=%%b"
        if /i "%%a"=="db" set "DB=%%b"
        if /i "%%a"=="libs" set "LIBS=%%b"
    )
)

:menu
cls
echo.
echo  ==========================================================
echo    media_index
echo  ==========================================================
echo.
REM  Built before the echo rather than inside an if-block. A closing
REM  bracket in echoed text ends the block instead of printing, which is
REM  why this line used to lose its final bracket on screen.
set "SHOWMEDIA=!MEDIA!"
if not defined MEDIA set "SHOWMEDIA=none yet - press 8"
echo    media folder : !SHOWMEDIA!
echo    index file   : !DB!
echo.
echo  ----------------------------------------------------------
echo    1.  Check a media folder      - is my download usable?
echo    2.  Attach downloaded subtitles - from a season pack
echo    3.  Make subtitles from audio - when there are none at all
echo    4.  Build the library index
echo    5.  Search for a line         - where is it?
echo    6.  Cut that line to a clip   - WATCH IT. this is the real test
echo    7.  Show what is in the index
echo.
echo    8.  Set the media folder
echo    9.  BUILD A VIDEO from a visual script
echo    L.  Look at the footage       - teach it what your episodes LOOK like
echo    S.  Describe a picture        - PROVE it can see. do this after L
echo    T.  Plan the timing           - how long each shot holds
echo    R.  RENDER THE VIDEO          - the file you can actually watch
echo    W.  OPEN IN A BROWSER         - see every shot, and why it is there
echo    0.  Exit
echo  ----------------------------------------------------------

REM  Say what to do next, so the list is never a guess.
set "NEXT=press 8 and point it at your episode folder"
if defined MEDIA set "NEXT=press 1, then 2, then 4 - in that order"
if defined MEDIA if exist "!DB!" set "NEXT=9 builds, T times it, R renders the video"
echo    NEXT:  !NEXT!
echo.
set "CHOICE="
set /p "CHOICE=  Pick a number, or L: "
if not defined CHOICE goto menu

if "!CHOICE!"=="1" goto do_check
if "!CHOICE!"=="2" goto do_subs
if "!CHOICE!"=="3" goto do_transcribe
if "!CHOICE!"=="4" goto do_build
if "!CHOICE!"=="5" goto do_find
if "!CHOICE!"=="6" goto do_clip
if "!CHOICE!"=="7" goto do_stats
if "!CHOICE!"=="8" goto do_setfolder
if "!CHOICE!"=="9" goto do_queue
if /i "!CHOICE!"=="L" goto do_look
if /i "!CHOICE!"=="S" goto do_see
if /i "!CHOICE!"=="T" goto do_time
if /i "!CHOICE!"=="R" goto do_render
if /i "!CHOICE!"=="W" goto do_web
if "!CHOICE!"=="0" goto bye
echo.
echo   That was not one of the numbers on the list.
echo.
echo   If a whole path just appeared on the line above, the window was
echo   still waiting on "Press any key" when you pasted - the first
echo   letter answered it and the rest arrived here. Nothing is broken.
echo.
pause
goto menu

REM ------------------------------------------------------------------------
REM  Dragging a folder into the window wraps the path in quotes; typing it
REM  does not. Everything below is stored WITHOUT quotes and quoted again at
REM  the point of use, so a path with spaces survives either way.
REM ------------------------------------------------------------------------

:need_folder
if defined MEDIA exit /b 0
echo.
echo   No media folder set yet.
call :ask_folder
if not defined MEDIA exit /b 1
exit /b 0

:ask_folder
echo.
echo   Type the folder path, or drag the folder into this window
echo   and press Enter.
echo.
set "NEWDIR="
set /p "NEWDIR=  Folder: "
if not defined NEWDIR exit /b 0
set "NEWDIR=!NEWDIR:"=!"
if not defined NEWDIR exit /b 0
if not exist "!NEWDIR!\." (
    echo.
    echo   That folder does not exist:  !NEWDIR!
    echo.
    pause
    exit /b 0
)
set "MEDIA=!NEWDIR!"
> "settings.txt" echo media=!MEDIA!
>> "settings.txt" echo db=!DB!
>> "settings.txt" echo libs=!LIBS!
REM  No "press any key" here on purpose. The path is usually pasted, and a
REM  pause straight after a paste swallows the first letter and posts the
REM  rest into the next prompt. The menu header shows the folder anyway.
exit /b 0

REM ------------------------------------------------------------------------
:do_setfolder
call :ask_folder
goto menu

:do_check
call :need_folder || goto menu
echo.
%PY% -m media_index check "!MEDIA!"
echo.
pause
goto menu

:do_subs
call :need_folder || goto menu
echo.
echo   Where are the downloaded .srt files?
echo   Leave blank if they are already in the media folder.
echo.
set "SUBDIR="
set /p "SUBDIR=  Subtitle folder: "
if defined SUBDIR set "SUBDIR=!SUBDIR:"=!"
echo.
if defined SUBDIR (
    %PY% -m media_index subs "!MEDIA!" --subs "!SUBDIR!"
) else (
    %PY% -m media_index subs "!MEDIA!"
)
echo.
pause
goto menu

:do_transcribe
call :need_folder || goto menu
echo.
echo   This reads the audio and writes a .srt next to each video, so the
echo   subtitles come from THIS copy of the film and cannot be out of sync.
echo   Roughly 10 minutes per episode. Safe to stop and restart.
echo.
echo   Say Y below only if this folder's subtitles are WRONG - it will
echo   replace them. Say N to fill in only the episodes that have none.
echo.
set "OVER="
set /p "OVER=  Replace the subtitles that are already there? [y/N]: "
echo.
set "GO="
set /p "GO=  Start? [Y/n]: "
if /i "!GO!"=="n" goto menu
echo.
if /i "!OVER!"=="y" (
    %PY% -m media_index transcribe "!MEDIA!" --overwrite
) else (
    %PY% -m media_index transcribe "!MEDIA!"
)
echo.
pause
goto menu

:do_build
call :need_folder || goto menu
echo.
%PY% -m media_index build "!MEDIA!" --db "!DB!" --verify-sync
echo.
pause
goto menu

:do_find
echo.
set "Q="
set /p "Q=  Type a line of dialogue you remember: "
if not defined Q goto menu
echo.
%PY% -m media_index find "!Q!" --db "!DB!"
echo.
pause
goto menu

:do_clip
echo.
echo   This cuts the real clip WITH SOUND and opens it. If you HEAR the
echo   line, the whole chain is right: the subtitle, the timing, the cut.
echo   Seeing the right scene is not enough - a clip can show the right
echo   scene and still sit seconds away from the line.
echo.
set "Q="
set /p "Q=  Type a line of dialogue: "
if not defined Q goto menu
if not exist "proof" mkdir "proof"
set "CLIP=proof\clip.mp4"
echo.
%PY% -m media_index cut "!Q!" --db "!DB!" --out "!CLIP!" --seconds 5 --full-line --audio
if not exist "!CLIP!" goto clip_done
echo.
echo   Opening !CLIP!
start "" "!CLIP!"
:clip_done
echo.
set "HEARD="
set /p "HEARD=  Did you HEAR that line in the clip? [y/N]: "
if /i "!HEARD!"=="y" (
    echo.
    echo   Then this episode is right, end to end.
    echo.
    pause
    goto menu
)
echo.
echo   Then the subtitle for this episode is out by some seconds, and the
echo   next thing needed is the NUMBER. Cutting 40 seconds around the same
echo   line - the tool will say where in those 40 seconds it expected it.
echo.
set "WCLIP=proof\window.mp4"
%PY% -m media_index cut "!Q!" --db "!DB!" --out "!WCLIP!" --window 40
if exist "!WCLIP!" start "" "!WCLIP!"
echo.
pause
goto menu

:do_look
echo.
echo   This teaches the tool what your episodes LOOK like, so it can check
echo   every shot against the script instead of inferring it from one
echo   quoted line. It is the step that stops one wrong match ruining a
echo   whole scene.
echo.
echo   Slow, and done ONCE per episode. A few minutes each, nothing to
echo   watch while it runs, and every script you build afterwards uses it
echo   for free.
echo.
echo   The first run downloads the picture model, about 1 GB. After that
echo   it needs no internet at all.
echo.
echo   A whole five-season library is HOURS. One script usually needs three
echo   episodes, which is minutes. Give it a script here to do only those,
echo   or leave it blank to do everything you own.
echo.
set "LSCRIPT="
set /p "LSCRIPT=  the .json visual script, or blank for everything: "
if defined LSCRIPT set "LSCRIPT=!LSCRIPT:"=!"
if defined LSCRIPT if not exist "!LSCRIPT!" (
    echo.
    echo   Not found: !LSCRIPT!
    echo.
    pause
    goto menu
)
echo.
set "GO="
set /p "GO=  Start? [Y/n]: "
if /i "!GO!"=="n" goto menu
echo.
if defined LSCRIPT (
    %PY% -m media_index look --db "!DB!" --script "!LSCRIPT!"
) else (
    %PY% -m media_index look --db "!DB!"
)
echo.
pause
goto menu

:do_see
echo.
echo   Type what should be ON SCREEN - not what it means. The tool finds
echo   the closest frame in everything it has looked at and writes it out.
echo.
echo   Good:  a man in a red hazmat suit holding a green box cutter
echo   Bad:   the moment everything changes for Walt
echo.
set "PIC="
set /p "PIC=  Describe a picture: "
if not defined PIC goto menu
echo.
%PY% -m media_index see "!PIC!" --db "!DB!" --out "proof"
if exist "proof\see_01.jpg" start "" "proof\see_01.jpg"
echo.
echo   If the top frame is not what you described, the picture layer is
echo   not working on this footage and nothing built on it will be right.
echo.
pause
goto menu

:do_time
echo.
echo   This decides how long every clip and still holds, and when each one
echo   lands. Nothing is re-cut, so you can re-time as many times as you
echo   like - it takes seconds.
echo.
echo   GIVE IT THE NARRATION AUDIO. Without it the tool guesses from word
echo   counts at 150 words a minute, and your last recording was read at
echo   221 - a guess three minutes out over an eleven minute video.
echo.
set "TFOLD="
set /p "TFOLD=  1 of 4 - the output folder from step 9 [built]: "
if not defined TFOLD set "TFOLD=built"
set "TFOLD=!TFOLD:"=!"
set "TSCRIPT="
set /p "TSCRIPT=  2 of 4 - the same .json visual script: "
if not defined TSCRIPT goto menu
set "TSCRIPT=!TSCRIPT:"=!"
set "TAUDIO="
set /p "TAUDIO=  3 of 4 - the narration mp3: "
if defined TAUDIO set "TAUDIO=!TAUDIO:"=!"
set "TPACE="
set /p "TPACE=  4 of 4 - pace: calm, normal, quick, rapid [normal]: "
if not defined TPACE set "TPACE=normal"
echo.
if defined TAUDIO (
    %PY% -m media_index timeline "!TFOLD!" "!TSCRIPT!" --audio "!TAUDIO!" --pace "!TPACE!"
) else (
    %PY% -m media_index timeline "!TFOLD!" "!TSCRIPT!" --pace "!TPACE!"
)
echo.
pause
goto menu

:do_web
echo.
echo   This opens the tool in your browser.
echo.
echo   The first screen is LIBRARY: every show and film you own, how much
echo   of each one is indexed, and what is stopping the rest. A title that
echo   says "9 of 62 indexed" can still build a video - but in the other 53
echo   episodes a shot is picked from the dialogue alone and never checked
echo   against the picture. That is the difference the screen now shows.
echo.
echo   NEW VIDEO is the form: script, voiceover, title, folder. Check tells
echo   you what will happen BEFORE the build - how many shots will be found,
echo   and which scenes will be guesses - and Build runs it with a bar.
echo.
echo   The shot-by-shot page is still there, at /shots, with its four tags:
echo.
echo      anchor        a quoted line. exact to the millisecond
echo      verified      the picture matched the description
echo      interpolated  worked out between two anchors
echo      filler        right episode, no particular moment of it
echo.
echo   Leave this window open while you use it. Ctrl+C closes it.
echo.
set "WOUT="
set /p "WOUT=  an output folder to open (blank = just the library): "
if defined WOUT set "WOUT=!WOUT:"=!"
echo.
if defined WOUT (
%PY% -m media_index web --db "!DB!" --out "!WOUT!" --libraries "!LIBS!"
) else (
%PY% -m media_index web --db "!DB!" --libraries "!LIBS!"
)
echo.
pause
goto menu

:do_render
echo.
echo   This makes the actual video file. Slow - roughly a minute of
echo   rendering per minute of finished video - and safe to interrupt:
echo   run it again and it picks up where it stopped.
echo.
echo   Run T first. Without a timeline there is nothing to render.
echo.
set "RFOLD="
set /p "RFOLD=  the same output folder from step 9 [built]: "
if not defined RFOLD set "RFOLD=built"
set "RFOLD=!RFOLD:"=!"
echo.
%PY% -m media_index render "!RFOLD!"
if exist "!RFOLD!\video.mp4" start "" "!RFOLD!\video.mp4"
echo.
pause
goto menu

:do_stats
echo.
%PY% -m media_index stats --db "!DB!"
echo.
pause
goto menu

:do_queue
echo.
echo   Give it the JSON visual script. It finds every shot in your own
echo   footage, cuts the clips, pulls the stills, and puts one contact
echo   sheet on screen so you can judge the whole video at a glance.
echo.
set "SCRIPT="
set /p "SCRIPT=  1 of 2 - the .json visual script: "
if not defined SCRIPT goto menu
set "SCRIPT=!SCRIPT:"=!"
if not exist "!SCRIPT!" (
    echo.
    echo   Not found: !SCRIPT!
    echo.
    pause
    goto menu
)
set "OUTDIR=built"
set /p "OUTDIR=  2 of 2 - a NEW output folder [built]: "
if not defined OUTDIR set "OUTDIR=built"
set "OUTDIR=!OUTDIR:"=!"
echo.
%PY% -m media_index make "!SCRIPT!" --db "!DB!" --out "!OUTDIR!" --stills 2
echo.
echo   Building the contact sheet...
%PY% -m media_index sheet "!OUTDIR!" --out "!OUTDIR!\contact_sheet.jpg"
if exist "!OUTDIR!\contact_sheet.jpg" start "" "!OUTDIR!\contact_sheet.jpg"
echo.
echo   Look at the sheet. Every still the tool chose is on it, in order.
echo   Wrong scenes and repeats are obvious side by side.
echo.
pause
goto menu

:bye
endlocal
exit /b 0
