@echo off
REM ===========================================================================
REM  RobloxStudioLive - OPTIONAL: start the sync daemon automatically at logon.
REM
REM  This adds one Scheduled Task named "RobloxStudioLive Sync" that runs
REM  run-daemon.cmd.  It changes nothing else, and uninstall-autostart.cmd
REM  removes it again.
REM
REM  Run this only if you want a daemon running whenever you are logged in.  It
REM  is NOT run for you by init.cmd: it modifies system state, so it is left for
REM  you to invoke deliberately.
REM
REM  Needs an elevated prompt to create a task in some configurations; the
REM  script says so if schtasks refuses.
REM ===========================================================================

setlocal
cd /d "%~dp0"

if not exist "run-daemon.cmd" (
  echo run-daemon.cmd is missing. Run init.cmd first.
  pause
  exit /b 2
)

set "TASK=RobloxStudioLive Sync"

echo Creating scheduled task: %TASK%
echo   runs at logon: "%CD%\run-daemon.cmd"
echo   logs to      : "%CD%\logs\daemon.log"
echo.

schtasks /Create /TN "%TASK%" /TR "\"%CD%\run-daemon.cmd\"" /SC ONLOGON /RL LIMITED /F
if errorlevel 1 (
  echo.
  echo Failed to create the task. Try running this script as Administrator.
  pause
  exit /b 1
)

echo.
echo Done. The daemon will start at your next logon and log to logs\daemon.log
echo Start it now without logging off with:  schtasks /Run /TN "%TASK%"
echo Remove it again with:                   uninstall-autostart.cmd
pause
endlocal
