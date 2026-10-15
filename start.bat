@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "ORT_DISABLE_TELEMETRY=1"
set "GRADIO_ANALYTICS_ENABLED=False"
set "HF_HUB_DISABLE_TELEMETRY=1"
cd /d "%~dp0"
title ShortsFactory
if not exist ".venv\Scripts\python.exe" (
    echo  ShortsFactory no esta instalado todavia. Ejecuta primero install.bat
    pause
    exit /b 1
)
echo  Iniciando ShortsFactory en http://127.0.0.1:7860 ...
echo  Cierra esta ventana para detener la aplicacion.
".venv\Scripts\python.exe" app.py
if errorlevel 1 (
    echo.
    echo  La aplicacion se ha cerrado con un error. Revisa logs\shortsfactory.log
    pause
)
