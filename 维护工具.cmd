@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
set PYTHONNOUSERSITE=1
call "%~dp0scripts\run-python.cmd" "%~dp0scripts\desktop_maintenance.py" %*
set "RONOVA_MAINTENANCE_EXIT=%ERRORLEVEL%"
if not "%RONOVA_MAINTENANCE_EXIT%"=="0" if "%~1"=="" pause
exit /b %RONOVA_MAINTENANCE_EXIT%
