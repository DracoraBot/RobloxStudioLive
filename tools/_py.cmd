@echo off
REM ===========================================================================
REM  RobloxStudioLive - locate a Python interpreter that can run this project.
REM
REM  Called by the top-level launchers while the working directory is the
REM  project root:
REM
REM      call "%~dp0tools\_py.cmd"
REM      if errorlevel 1 goto :no_python
REM      %RSS_PY% "%~dp0tools\rss.py" status
REM
REM  On success it sets:
REM      RSS_PY         a runnable command, already quoted if its path needs it
REM      RSS_PY_SOURCE  where it came from, so the caller can say so
REM
REM  Order of preference:
REM      1. %RSS_PYTHON%             an interpreter you asked for explicitly
REM      2. config\python-path.txt   recorded by init.cmd once it validated one
REM      3. py -3                    the official Windows launcher
REM      4. python                   first on PATH
REM      5. python3
REM
REM  A candidate only wins if tools\_pycheck.py exits 0.  That is what rules out
REM  the Microsoft Store `python.exe` stub (which starts, prints an advert and
REM  exits non-zero) and any interpreter that is older than 3.9.
REM
REM  If you set RSS_PYTHON yourself, include the quotes when the path has
REM  spaces:  set RSS_PYTHON="C:\Program Files\Python312\python.exe"
REM ===========================================================================

set "RSS_PY="
set "RSS_PY_SOURCE="
set "RSS_TOOLS=%~dp0"
set "RSS_CHECK=%RSS_TOOLS%_pycheck.py"
set "RSS_RECORD=%RSS_TOOLS%..\config\python-path.txt"

REM --- 1. an interpreter you chose yourself ---------------------------------
if not defined RSS_PY if defined RSS_PYTHON (
  %RSS_PYTHON% "%RSS_CHECK%" >nul 2>nul
  if not errorlevel 1 (
    set "RSS_PY=%RSS_PYTHON%"
    set "RSS_PY_SOURCE=RSS_PYTHON"
  )
)

REM --- 2. the one init.cmd validated on this machine ------------------------
set "RSS_CAND="
if not defined RSS_PY if exist "%RSS_RECORD%" set /p RSS_CAND=<"%RSS_RECORD%"
if not defined RSS_PY if defined RSS_CAND (
  %RSS_CAND% "%RSS_CHECK%" >nul 2>nul
  if not errorlevel 1 (
    set "RSS_PY=%RSS_CAND%"
    set "RSS_PY_SOURCE=config\python-path.txt"
  )
)

REM --- 3-5. whatever the system offers --------------------------------------
if not defined RSS_PY (
  py -3 "%RSS_CHECK%" >nul 2>nul
  if not errorlevel 1 (
    set "RSS_PY=py -3"
    set "RSS_PY_SOURCE=py -3"
  )
)
if not defined RSS_PY (
  python "%RSS_CHECK%" >nul 2>nul
  if not errorlevel 1 (
    set "RSS_PY=python"
    set "RSS_PY_SOURCE=python"
  )
)
if not defined RSS_PY (
  python3 "%RSS_CHECK%" >nul 2>nul
  if not errorlevel 1 (
    set "RSS_PY=python3"
    set "RSS_PY_SOURCE=python3"
  )
)

if defined RSS_PY exit /b 0

echo.
echo   Could not find a Python interpreter that RobloxStudioLive can use.
echo   It needs Python 3.9 or newer.
echo.
echo   Install one, then run init.cmd again:
echo.
echo       winget install -e --id Python.Python.3.12
echo.
echo   or download it from https://www.python.org/downloads/ and tick
echo   "Add python.exe to PATH" in the installer.
echo.
echo   Already have Python somewhere unusual?  Point at it directly:
echo.
echo       set RSS_PYTHON=C:\path\to\python.exe
echo.
exit /b 1
