@echo off
chcp 65001 >nul
cd /d "%~dp0"
readonly_hpc_preflight.exe --self-test
echo.
pause
