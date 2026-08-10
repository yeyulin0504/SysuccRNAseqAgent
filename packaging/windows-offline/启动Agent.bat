@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\rnaseq-agent.exe" goto :not_installed
if not exist "runs" mkdir "runs"

echo Starting SYSU RNA-seq Agent GUI...
".venv\Scripts\rnaseq-agent.exe" gui --output-dir "%~dp0runs"
if errorlevel 1 goto :failed
exit /b 0

:not_installed
echo.
echo ERROR: Agent is not installed yet.
echo Run the installer .bat file first.
pause
exit /b 1

:failed
echo.
echo ERROR: The Agent GUI closed with an error. Save a screenshot of this window.
pause
exit /b 1
