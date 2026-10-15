@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "ORT_DISABLE_TELEMETRY=1"
set "GRADIO_ANALYTICS_ENABLED=False"
set "HF_HUB_DISABLE_TELEMETRY=1"
cd /d "%~dp0"
title ShortsFactory - prueba de aceptacion
if not exist ".venv\Scripts\python.exe" (
    echo  Ejecuta primero install.bat
    pause
    exit /b 1
)
echo  Prueba de aceptacion completa con voz real (tarda uno o dos minutos)...
".venv\Scripts\python.exe" tools\acceptance_test.py
echo.
echo  Resultado guardado en logs\acceptance.txt
pause
