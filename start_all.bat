@echo off
title Mobile Prompt Bridge + ngrok Launcher
cd /d "%~dp0"

echo ==================================================
echo       Starting ngrok on port 8712...
echo ==================================================
start "ngrok (Port 8712)" cmd /c "ngrok http 8712"

echo.
echo ==================================================
echo       Starting Mobile Prompt Bridge...
echo ==================================================
start "Mobile Prompt Bridge" cmd /c "run.bat"

echo.
echo Both ngrok and the server have been launched in separate windows!
echo You can safely close this launcher window.
pause
