@echo off
REM ===========================================================================
REM  RobloxStudioLive - run the CLI without needing Python on PATH.
REM
REM      rss.cmd status
REM      rss.cmd conflicts
REM      rss.cmd import
REM      rss.cmd watch
REM      rss.cmd resolve --path "ServerScriptService.Foo" --take studio
REM
REM  Arguments are passed straight through to tools\rss.py, so `rss.cmd --help`
REM  and `rss.cmd sync --dry-run` behave exactly as documented.  Running
REM  `python tools\rss.py ...` yourself is equivalent, when the `python` on your
REM  PATH is a real interpreter and not the Microsoft Store placeholder.
REM
REM  This wrapper never pauses: it is meant to be used from a terminal and from
REM  other scripts.
REM ===========================================================================

setlocal
cd /d "%~dp0"

call "%~dp0tools\_py.cmd"
if errorlevel 1 exit /b 2

%RSS_PY% "%~dp0tools\rss.py" %*

endlocal & exit /b %ERRORLEVEL%
