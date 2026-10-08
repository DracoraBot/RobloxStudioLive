@echo off
REM RobloxStudioLive - stop a running sync daemon.

setlocal
cd /d "%~dp0"

set "LOCK=.rss-daemon.lock"
if not exist "%LOCK%" (
  echo No lock file found - the daemon does not appear to be running.
  echo (If it is running in another window, press Ctrl+C in that window.)
  pause
  exit /b 0
)

set /p PID=<"%LOCK%"
if "%PID%"=="" (
  echo Lock file is unreadable; removing it.
  del "%LOCK%"
  pause
  exit /b 0
)

echo Stopping RobloxStudioLive daemon (pid %PID%)...
taskkill /PID %PID% /T /F
if errorlevel 1 (
  echo Process %PID% was not running; removing the stale lock.
) else (
  echo Stopped.
)
if exist "%LOCK%" del "%LOCK%"
pause
endlocal
