@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
set PYTHONNOUSERSITE=1
call "%~dp0scripts\run-python.cmd" "%~dp0scripts\setup_desktop.py" --ensure
if errorlevel 1 goto failed
echo Ronova 已安装到当前目录。双击 Ronova.exe 启动。
pause
exit /b 0

:failed
echo Ronova 安装失败。请查看上方错误及 README.md 中的运行条件。
pause
exit /b 1
