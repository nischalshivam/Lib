@echo off
echo ============================================
echo  VText setup
echo ============================================
python -m pip install --upgrade pip
echo.
echo [1/3] Core dependencies...
python -m pip install pillow numpy imageio-ffmpeg opencv-python-headless
echo.
echo [2/3] Speech engine: faster-whisper (recommended)...
python -m pip install faster-whisper
echo.
echo [3/3] Offline fallback engine: pocketsphinx (optional)...
python -m pip install pocketsphinx
if errorlevel 1 (
  echo.
  echo NOTE: pocketsphinx could not be installed on this Python version.
  echo       That is OK as long as faster-whisper installed above.
)
echo.
echo ============================================
python check.py
echo ============================================
pause
