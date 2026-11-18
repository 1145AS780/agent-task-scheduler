@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title ATS quick seal + visa

rem ASCII-only entry point: cmd.exe misparses Chinese lines in UTF-8 batch files,
rem so all Chinese text lives in seal-visa.ps1, which PowerShell reads correctly.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0seal-visa.ps1" %*
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%