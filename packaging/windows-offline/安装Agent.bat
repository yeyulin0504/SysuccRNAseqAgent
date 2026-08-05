@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Python 启动器 py.exe。
  echo 请先安装 64 位 Python 3.13，并勾选“Add Python to PATH”。
  pause
  exit /b 1
)

py -3.13 -c "import sys; print(sys.version)" >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 64 位 Python 3.13。
  echo 请安装 Python 3.13 后重新运行本脚本。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 正在创建独立运行环境……
  py -3.13 -m venv .venv
  if errorlevel 1 goto :failed
)

echo 正在离线安装 RNA-seq Agent 及 SSH 依赖……
".venv\Scripts\python.exe" -m pip install --upgrade --no-index --find-links "%~dp0wheels" sysu-rnaseq-agent
if errorlevel 1 goto :failed

echo.
echo 安装完成。以后双击“启动Agent.bat”即可。
pause
exit /b 0

:failed
echo.
echo [错误] 安装失败，请把本窗口的错误截图保存下来。
pause
exit /b 1
