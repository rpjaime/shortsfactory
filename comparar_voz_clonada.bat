@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "HF_HUB_DISABLE_TELEMETRY=1"
cd /d "%~dp0"
title ShortsFactory - comparativa de voz clonada
if not exist ".venv-voz\Scripts\python.exe" (
    echo  Ejecuta primero install_voz_clonada.bat
    pause
    exit /b 1
)
echo  Descarga el decodificador de Espana (~1 GB, solo la primera vez) y genera 12 muestras.
echo  Tardara unos 6-8 minutos.
echo.
".venv-voz\Scripts\python.exe" voice_clone\chatterbox_worker.py compare
echo.
echo  Escucha muestras_voz\comparativa. Resultado en logs\voz_clonada.txt
pause
