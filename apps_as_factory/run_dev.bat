@echo off
REM ===========================================================================
REM  ARCA / HGIS de las Indias - DEV server launcher (Windows)
REM
REM  DEV ONLY. Sets ARCA_DEBUG=1, which turns on Flask's interactive debugger
REM  and auto-reload. Do NOT run this on a reachable/public host: the debugger
REM  console is a remote code-execution surface. For production, launch app.py
REM  without ARCA_DEBUG (or run behind a real WSGI server such as waitress).
REM
REM  Must run from apps_as_factory\ so the relative data\ paths resolve; the
REM  cd below forces that regardless of where the .bat is invoked from.
REM ===========================================================================
setlocal

REM Move to this script's own directory (the app root).
cd /d "%~dp0"

REM Enable the dev debugger + auto-reload for this process only.
set "ARCA_DEBUG=1"

REM Optional: uncomment to pin a stable secret across restarts (else a random
REM ephemeral one is generated each start; fine for dev).
REM set "ARCA_SECRET_KEY=some-long-random-dev-string"

echo.
echo  Starting ARCA dev server (ARCA_DEBUG=1)  --  http://127.0.0.1:5000
echo  Press Ctrl+C to stop.
echo.

py -3.9 app.py

REM Keep the window open if the server exits or crashes so the traceback is readable.
echo.
echo  Server stopped (exit code %ERRORLEVEL%).
pause
endlocal
