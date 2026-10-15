@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "ORT_DISABLE_TELEMETRY=1"
set "GRADIO_ANALYTICS_ENABLED=False"
set "HF_HUB_DISABLE_TELEMETRY=1"
cd /d "%~dp0"
title ShortsFactory - diagnostico
if not exist ".venv\Scripts\python.exe" (
    echo  No existe el entorno .venv. Ejecuta primero install.bat
    echo.
    echo  Comprobaciones basicas:
    where python
    python --version
    where ffmpeg
    pause
    exit /b 1
)
".venv\Scripts\python.exe" check_environment.py
echo.
echo  Informe guardado en logs\check_environment.txt
pause
