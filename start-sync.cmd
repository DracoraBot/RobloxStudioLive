@echo off
REM ===========================================================================
REM  RobloxStudioLive - start the live sync daemon.
REM
REM  Leave this window open; press Ctrl+C to stop, or run stop-sync.cmd from
REM  another window.
REM
REM  Studio must be open with your place loaded, in Edit mode.
REM ===========================================================================

setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>nul

if not exist "config\settings.json" (
  echo.
  echo   This project has not been set up yet.
  echo   Run init.cmd first - it creates config\settings.json.
  echo.
  pause
  exit /b 2
)

call "%~dp0tools\_py.cmd"
if errorlevel 1 (
  echo.
  pause
  exit /b 2
)

echo Starting RobloxStudioLive sync daemon...
echo   interpreter: %RSS_PY%   [%RSS_PY_SOURCE%]
echo   project    : %CD%
echo.
echo Studio must be open with your place loaded. Press Ctrl+C to stop.
echo.

%RSS_PY% "%~dp0tools\rss.py" watch
set "RSS_RC=%ERRORLEVEL%"

echo.
echo daemon exited with code %RSS_RC%
pause
endlocal & exit /b %RSS_RC%
