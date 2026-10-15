@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "ORT_DISABLE_TELEMETRY=1"
set "GRADIO_ANALYTICS_ENABLED=False"
set "HF_HUB_DISABLE_TELEMETRY=1"
cd /d "%~dp0"
title ShortsFactory - pruebas
if not exist ".venv\Scripts\python.exe" (
    echo  Ejecuta primero install.bat
    pause
    exit /b 1
)
if not exist logs mkdir logs
echo  Ejecutando las pruebas automaticas (requieren FFmpeg en el PATH para las de video)...
".venv\Scripts\python.exe" -m pytest -q -p no:cacheprovider -rs > logs\tests.txt 2>&1
set "RC=%ERRORLEVEL%"
type logs\tests.txt
echo.
if "%RC%"=="0" (echo  TODAS LAS PRUEBAS SUPERADAS) else (echo  HAY PRUEBAS FALLIDAS: revisa logs\tests.txt)
pause
exit /b %RC%
