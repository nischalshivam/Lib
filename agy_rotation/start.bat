@echo off
REM Auto-rotating Antigravity (agy) vision endpoint for the movie/cartoon tool.
REM Serves OpenAI-compatible /chat/completions on http://127.0.0.1:8788/v1 and
REM rotates across every Google account snapshotted with agy-accounts.ps1.
REM
REM Point the tool at it by setting in settings.txt:
REM     gemini_base=http://127.0.0.1:8788/v1
REM     gemini_model=gemini-3.6-flash-low
REM     gemini_key=unused
REM
REM Leave this window open all day; it keeps indexing alive across accounts.

setlocal
if "%SHIM_MODEL%"==""    set SHIM_MODEL=gemini-3.6-flash-low
if "%SHIM_PORT%"==""     set SHIM_PORT=8788
REM 5-hour cooldown for an exhausted account (Antigravity's short window):
if "%SHIM_COOLDOWN_S%"=="" set SHIM_COOLDOWN_S=18000

cd /d "%~dp0"
echo Starting rotating vision shim on port %SHIM_PORT% (model %SHIM_MODEL%)...
python shim.py
