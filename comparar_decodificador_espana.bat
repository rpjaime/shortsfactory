@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "HF_HUB_OFFLINE=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
cd /d "%~dp0"
title ShortsFactory - decodificador de Espana
if not exist ".venv-voz\Scripts\python.exe" (
    echo  Ejecuta primero install_voz_clonada.bat
    pause
    exit /b 1
)
echo  Genera 4 muestras con el decodificador de Espana (cfg 0.8 y 1.0). Unos 2-3 minutos.
echo.
".venv-voz\Scripts\python.exe" voice_clone\chatterbox_worker.py compare --decoders v3 --cfg 0.8,1.0
echo.
echo  Compara muestras_voz\comparativa\dec-v3_cfg08_* con dec-v2_cfg08_*
pause
