@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Upload ORVIX to GitHub

echo ============================================================
echo   Upload ORVIX Desktop AI to zobaerio/orvixai
echo ============================================================
echo.

where git >nul 2>nul
if errorlevel 1 goto nogit

set "SOURCE=%~dp0"
set "TARGET=%TEMP%\orvixai-upload-%RANDOM%"
set "BRANCH=upload-orvix-desktop-ai-%RANDOM%"
set "REPO=https://github.com/zobaerio/orvixai.git"

echo [1/5] Cloning the GitHub repository...
git clone "%REPO%" "%TARGET%"
if errorlevel 1 goto clonefail

echo [2/5] Creating upload branch %BRANCH%...
git -C "%TARGET%" checkout -b "%BRANCH%"
if errorlevel 1 goto uploadfail

echo [3/5] Copying project source and character assets...
robocopy "%SOURCE%" "%TARGET%" /E /XD .git .venv __pycache__ .pytest_cache .mypy_cache /XF .env .env.* *.pyc
if errorlevel 8 goto uploadfail

echo [4/5] Creating commit...
git -C "%TARGET%" config user.name "Zobaerio"
git -C "%TARGET%" config user.email "192875779+zobaerio@users.noreply.github.com"
git -C "%TARGET%" add -A
git -C "%TARGET%" commit -m "feat: add ORVIX Windows desktop AI with voice and character gallery"
if errorlevel 1 goto uploadfail

echo [5/5] Pushing to GitHub...
echo Git Credential Manager may open a browser for GitHub sign-in.
git -C "%TARGET%" push -u origin "%BRANCH%"
if errorlevel 1 goto pushfail

echo.
echo SUCCESS: ORVIX source and assets have been pushed to GitHub.
echo Repository: https://github.com/zobaerio/orvixai
echo Branch: %BRANCH%
echo Opening the Pull Request comparison page...
start "" "https://github.com/zobaerio/orvixai/compare/main...%BRANCH%?expand=1"
echo Review the files, then create a Pull Request to merge the branch into main.
echo A successful push does not automatically merge or publish the project.
pause
exit /b 0

:nogit
echo Git for Windows was not found.
echo Install it from https://git-scm.com/download/win, then run this script again.
start "" "https://git-scm.com/download/win"
pause
exit /b 1

:clonefail
echo Could not clone the repository. Check internet access and repository URL.
echo If GitHub asks for authentication, complete its sign-in prompt and retry.
pause
exit /b 1

:pushfail
echo Push failed. The source copy remains at:
echo %TARGET%
echo Check GitHub authentication and repository write permission, then retry.
pause
exit /b 1

:uploadfail
echo Upload preparation failed. The working copy is at:
echo %TARGET%
echo Check the error above, then retry.
pause
exit /b 1
