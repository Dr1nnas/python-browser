@echo off
title Reset Secret Browser choice
echo.
echo Deletes saved search engine and "first run" flag for Secret Browser.
echo Next launch will show the setup screen again.
echo.

reg delete "HKCU\Software\SecretBrowser\Browser" /f >nul 2>&1
reg delete "HKCU\Software\PythonBrowser\Browser" /f >nul 2>&1
if errorlevel 1 (
  echo Nothing was removed ^(settings may already be absent^).
) else (
  echo Done. Your browser choice was cleared.
)
echo.
pause
