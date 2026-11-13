@echo off
setlocal EnableExtensions
rem Alias entry: run this after editing core .py files under scripts/ to re-seal.
rem Real logic lives in setup.bat -- it decides "already set up" vs "needs sealing".
call "%~dp0setup.bat" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%