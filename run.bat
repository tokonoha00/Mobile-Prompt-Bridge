@echo off
title Mobile Prompt Bridge Launcher
cd /d "%~dp0"

echo ==================================================
echo       Starting Mobile Prompt Bridge MVP ...
echo ==================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in system PATH.
    echo Please install Python 3.11+ and add it to your PATH.
    echo.
    pause
    exit /b 1
)

python src/main.py

echo.
echo ==================================================
echo       Mobile Prompt Bridge has exited.
echo ==================================================
pause
