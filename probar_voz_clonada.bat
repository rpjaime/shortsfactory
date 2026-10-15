@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "HF_HUB_OFFLINE=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
cd /d "%~dp0"
title ShortsFactory - prueba de voz clonada
if not exist ".venv-voz\Scripts\python.exe" (
    echo  Ejecuta primero install_voz_clonada.bat
    pause
    exit /b 1
)
echo  Generando 6 frases de prueba con tu voz (la primera vez carga el modelo; puede tardar unos minutos)...
echo.
".venv-voz\Scripts\python.exe" voice_clone\chatterbox_worker.py test
echo.
echo  Escucha los archivos clon_*.wav en la carpeta muestras_voz. Resultado en logs\voz_clonada.txt
pause
