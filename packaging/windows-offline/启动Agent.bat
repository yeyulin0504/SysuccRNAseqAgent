@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\rnaseq-agent.exe" (
  echo Agent 尚未安装，请先双击“安装Agent.bat”。
  pause
  exit /b 1
)

if not exist "runs" mkdir "runs"
start "" ".venv\Scripts\rnaseq-agent.exe" gui --output-dir "%~dp0runs"
