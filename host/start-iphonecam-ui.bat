@echo off
title iPhoneCam control panel
cd /d "%~dp0"
python iphonecam_ui.py --width 1280 --height 720 --fps 60
echo.
echo iPhoneCam stopped. Press any key to close this window.
pause >nul
