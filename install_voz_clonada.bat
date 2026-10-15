@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "DO_NOT_TRACK=1"
cd /d "%~dp0"
title ShortsFactory - instalar voz clonada (Chatterbox)
if not exist logs mkdir logs
set "LOG=%~dp0logs\install_voz.log"
echo ==== Instalacion voz clonada %DATE% %TIME% ==== > "%LOG%"
echo.
echo  Voz clonada con Chatterbox Multilingual (MIT) - version espanol de Espana
echo  Se instala en una carpeta aparte (.venv-voz). Descarga aproximada: 3-4 GB.
echo  Registro: %LOG%
echo.

echo [1/4] Buscando Python 3.11...
set "PY="
py -3.11 --version >nul 2>&1 && set "PY=py -3.11"
if not defined PY (
    python --version 2>&1 | findstr /R /C:"Python 3\.11\." >nul && set "PY=python"
)
if not defined PY (
    echo  ERROR: no se encuentra Python 3.11.
    goto :fail
)

echo [2/4] Creando el entorno .venv-voz...
if not exist ".venv-voz\Scripts\python.exe" (
    %PY% -m venv .venv-voz >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo  ERROR: no se pudo crear .venv-voz
        goto :fail
    )
)
set "VPY=%~dp0.venv-voz\Scripts\python.exe"

echo [3/4] Instalando Chatterbox y PyTorch para CPU (varios minutos, unos 1,5 GB)...
"%VPY%" -m pip install --disable-pip-version-check --no-input -r requirements-voz.txt >> "%LOG%" 2>&1
if errorlevel 1 (
    echo  ERROR: fallo la instalacion de dependencias.
    goto :fail
)
"%VPY%" -c "import torch, chatterbox, perth; print('torch', torch.__version__, '- CPU threads', torch.get_num_threads()); assert perth.PerthImplicitWatermarker is not None" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo  ERROR: Chatterbox no se puede importar.
    goto :fail
)
echo        Dependencias correctas.

echo [4/4] Descargando los modelos desde Hugging Face (unos 3,2 GB):
echo        - ResembleAI/Chatterbox-Multilingual-es-es  (t3_es_es, espanol de Espana)
echo        - ResembleAI/chatterbox                     (codificador de voz y decodificador)
"%VPY%" voice_clone\chatterbox_worker.py download
if errorlevel 1 (
    echo  ERROR: fallo la descarga de los modelos. Revisa logs\voz_clonada.txt
    goto :fail
)
echo.
echo  INSTALACION COMPLETADA.
echo  Ahora guarda tu grabacion en assets\voz_referencia (lee LEEME.txt) y ejecuta probar_voz_clonada.bat
echo INSTALACION OK >> "%LOG%"
pause
exit /b 0

:fail
echo.
echo  ---- ultimas lineas del registro ----
powershell -NoProfile -Command "Get-Content -Tail 25 -Encoding UTF8 '%LOG%'"
echo RESULTADO: ERROR >> "%LOG%"
pause
exit /b 1
