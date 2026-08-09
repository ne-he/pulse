@echo off
REM Double-click launcher for the PULSE demo (Windows).
REM Starts redis-free, in one process, and opens the dashboard in a browser.
REM Any argument is passed straight through, e.g.  demo.cmd --warm 0

setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -c "import fakeredis, river, fastapi" >nul 2>&1
if errorlevel 1 (
  echo Dependencies missing. Installing into the current environment...
  "%PY%" -m pip install -r requirements.txt || goto :failed
)

"%PY%" -m deploy.demo %*
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo The demo failed to start. Scroll up for the error.
pause
exit /b 1
