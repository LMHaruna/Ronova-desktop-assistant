@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYTHONHOME="
set "PYTHONPATH="
set "RONOVA_PROBE=import sys, sysconfig; sys.exit(not (sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 11) and sysconfig.get_platform() == 'win-amd64' and sys.prefix == sys.base_prefix))"

if defined RONOVA_PYTHON goto explicit
set "RONOVA_LOCAL_PYTHON=%~dp0..\runtime\python\cpython-3.11.17-windows-x86_64-none\python.exe"
if exist "%RONOVA_LOCAL_PYTHON%" (
    "%RONOVA_LOCAL_PYTHON%" -IB -c "%RONOVA_PROBE%" >nul 2>&1
    if not errorlevel 1 (
        "%RONOVA_LOCAL_PYTHON%" -EsB -X utf8 %*
        exit /b
    )
)
py -3.11 -IB -c "%RONOVA_PROBE%" >nul 2>&1
if not errorlevel 1 goto launcher
for /f "delims=" %%P in ('where.exe python3.11.exe python.exe 2^>nul ^| findstr.exe /i /v /l "\Microsoft\WindowsApps\python"') do (
    "%%P" -IB -c "%RONOVA_PROBE%" >nul 2>&1
    if not errorlevel 1 (
        "%%P" -EsB -X utf8 %*
        exit /b
    )
)

echo 找不到 Windows x64 CPython 3.11。请先安装 Python，或将 RONOVA_PYTHON 设为 python.exe 的完整路径。 1>&2
echo https://www.python.org/downloads/windows/ 1>&2
exit /b 1

:explicit
"%RONOVA_PYTHON%" -IB -c "%RONOVA_PROBE%" >nul 2>&1
if errorlevel 1 (
    echo RONOVA_PYTHON 必须指向已安装的 Windows x64 CPython 3.11 的 python.exe。 1>&2
    exit /b 1
)
"%RONOVA_PYTHON%" -EsB -X utf8 %*
exit /b

:launcher
py -3.11 -EsB -X utf8 %*
exit /b
