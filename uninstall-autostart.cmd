@echo off
REM RobloxStudioLive - remove the optional logon scheduled task.
REM Safe to run even if the task was never installed.

setlocal
set "TASK=RobloxStudioLive Sync"

echo Removing scheduled task: %TASK%
schtasks /Delete /TN "%TASK%" /F
if errorlevel 1 (
  echo.
  echo The task was not present, or could not be removed. Nothing else was changed.
) else (
  echo.
  echo Removed. Your project folder, backups and logs are untouched.
)
pause
endlocal
