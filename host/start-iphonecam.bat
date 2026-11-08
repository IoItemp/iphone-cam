@echo off
title iPhoneCam
cd /d "%~dp0"
python iphonecam.py --width 1920 --height 1080 --fps 60 --rotate 180
echo.
echo iPhoneCam stopped. Press any key to close this window.
pause >nul