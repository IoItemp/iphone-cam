@echo off
title iPhoneCam
cd /d "%~dp0"
python iphonecam.py --width 1280 --height 720 --fps 60
echo.
echo iPhoneCam stopped. Press any key to close this window.
pause >nul