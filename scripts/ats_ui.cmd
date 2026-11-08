@echo off
setlocal
rem ATS console one-click launcher: start in background if not running (--ensure), then open browser
rem usage: ats_ui.cmd [ATS_ROOT]
set "SCRIPT=%~dp0ats_ui.py"

if "%~1"=="" (
  python "%SCRIPT%" --ensure --open
) else (
  python "%SCRIPT%" --root "%~1" --ensure --open
)

if errorlevel 1 (
  echo.
  echo Launch failed. Check log: ^<ATS_ROOT^>\ui_tmp\ui.log
  pause
)
endlocal
