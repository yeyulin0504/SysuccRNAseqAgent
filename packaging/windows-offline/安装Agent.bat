@echo off
setlocal
cd /d "%~dp0"

echo SYSU RNA-seq Agent offline installer
echo.

where py >nul 2>nul
if errorlevel 1 goto :missing_py

py -3.13 -c "import sys; assert sys.maxsize > 2**32" >nul 2>nul
if errorlevel 1 goto :missing_py313

if not exist ".venv\Scripts\python.exe" (
  echo Creating local Python environment...
  py -3.13 -m venv .venv
  if errorlevel 1 goto :failed
)

echo Installing Agent from bundled offline wheels...
".venv\Scripts\python.exe" -m pip install --upgrade --no-index --find-links "%~dp0wheels" sysu-rnaseq-agent
if errorlevel 1 goto :failed

echo.
echo Installation complete.
echo Double-click the launch .bat file to open the GUI.
pause
exit /b 0

:missing_py
echo.
echo ERROR: Python launcher py.exe was not found.
echo Install 64-bit Python 3.13, then run this script again.
pause
exit /b 1

:missing_py313
echo.
echo ERROR: 64-bit Python 3.13 was not found.
echo Install 64-bit Python 3.13, then run this script again.
pause
exit /b 1

:failed
echo.
echo ERROR: Installation failed. Save a screenshot of this window.
pause
exit /b 1
