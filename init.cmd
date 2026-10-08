@echo off
REM ===========================================================================
REM  RobloxStudioLive - one-time setup.  Double-click this file.
REM
REM  It finds a Python interpreter, then hands over to tools\init.py, which
REM  creates everything a fresh clone is missing:
REM
REM      config\settings.json      which services to mirror (from the template)
REM      config\python-path.txt    the interpreter that was validated here
REM      default.project.json      the optional Rojo project file
REM      src\ backups\ logs\       the folders the daemon writes into
REM
REM  Nothing you already have is overwritten, and running it again is harmless.
REM
REM  Options are passed straight through to tools\init.py:
REM
REM      init.cmd --check        report only, change nothing
REM      init.cmd --force        rebuild config\settings.json from the template
REM      init.cmd --roots A,B    mirror only the services you list
REM      init.cmd --no-tests     skip the offline self-test
REM      init.cmd --studio       also check the connection to Roblox Studio
REM      init.cmd --json         machine-readable result
REM      init.cmd --help         every option
REM
REM  This wrapper pauses so a double-click does not close the window.  To run
REM  setup unattended, call tools\init.py directly instead.
REM ===========================================================================

setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>nul

call "%~dp0tools\_py.cmd"
if errorlevel 1 (
  echo   Setup cannot continue without Python.
  echo.
  pause
  exit /b 1
)

%RSS_PY% "%~dp0tools\init.py" %*
set "RSS_RC=%ERRORLEVEL%"

echo.
echo   Press any key to close this window.
pause >nul

endlocal & exit /b %RSS_RC%
