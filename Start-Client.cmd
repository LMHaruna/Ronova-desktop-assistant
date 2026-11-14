@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
set PYTHONNOUSERSITE=1
call "%~dp0scripts\run-python.cmd" "%~dp0scripts\setup_desktop.py" --ensure
if errorlevel 1 goto failed
"%~dp0runtime\voice-venv\Scripts\pythonw.exe" -EsB -X utf8 "%~dp0client_app.py" %*
exit /b %ERRORLEVEL%
:failed
echo Ronova 环境准备失败。请查看上方错误和 README.md 的安装说明。
pause
exit /b 1
