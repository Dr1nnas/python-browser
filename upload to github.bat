@echo off
setlocal
cd /d "%~dp0"

REM Commits all changes and pushes to https://github.com/Dr1nnas/python-browser
REM First-time: create github-token.txt with your GitHub PAT (one line). See .gitignore.

set "MSG=Update"
if not "%~1"=="" set "MSG=%~1"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0upload-to-github.ps1" -Message "%MSG%"
set ERR=%ERRORLEVEL%
if not %ERR%==0 exit /b %ERR%
echo.
pause
exit /b 0
