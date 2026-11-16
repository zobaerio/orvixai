@echo off
setlocal
cd /d "%~dp0"
title ORVIX Desktop AI
if not exist ".venv\Scripts\python.exe" (
  echo Please run install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 (
  echo.
  echo ORVIX stopped with an error. Copy the message above to troubleshoot.
  pause
)
