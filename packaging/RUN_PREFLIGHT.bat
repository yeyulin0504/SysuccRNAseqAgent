@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Hospital HPC read-only preflight
echo No FASTQ or sample data will be read or uploaded.
echo.
readonly_hpc_preflight.exe --self-test
if errorlevel 1 goto :failed
echo.
readonly_hpc_preflight.exe
set "RESULT=%ERRORLEVEL%"
echo.
if exist server_preflight.json (
  echo A fresh sanitized report is available: server_preflight.json
) else (
  echo No fresh report was generated.
)
pause
exit /b %RESULT%

:failed
echo Portable runtime self-test failed. No server connection was attempted.
pause
exit /b 2
