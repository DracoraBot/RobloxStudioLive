@echo off
REM ===========================================================================
REM  RobloxStudioLive - run the daemon once, logging to logs\daemon.log.
REM
REM  This is what the optional logon task invokes.  Unlike start-sync.cmd it
REM  never pauses and never opens an interactive prompt, so it is also the
REM  right thing to call from a service manager or another script.
REM
REM  One process per project: `rss watch` takes an exclusive lock, so a second
REM  copy exits by itself with a message rather than racing this one.
REM ===========================================================================

setlocal
cd /d "%~dp0"

if not exist "logs" mkdir "logs"

call "%~dp0tools\_py.cmd"
if errorlevel 1 (
  echo %DATE% %TIME%  no usable Python interpreter; see tools\_py.cmd >> "%~dp0logs\daemon.log"
  exit /b 2
)

%RSS_PY% "%~dp0tools\rss.py" watch >> "%~dp0logs\daemon.log" 2>&1

endlocal & exit /b %ERRORLEVEL%
