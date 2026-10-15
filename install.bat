@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "ORT_DISABLE_TELEMETRY=1"
set "GRADIO_ANALYTICS_ENABLED=False"
set "HF_HUB_DISABLE_TELEMETRY=1"
cd /d "%~dp0"
title ShortsFactory - instalacion
if not exist logs mkdir logs
set "LOG=%~dp0logs\install.log"
echo ==== Instalacion ShortsFactory %DATE% %TIME% ==== > "%LOG%"
echo.
echo  ShortsFactory - instalacion del entorno
echo  Registro detallado: %LOG%
echo.

rem ---------------------------------------------------------------- 1. Python
echo [1/6] Buscando Python 3.11...
set "PY="
py -3.11 --version >nul 2>&1 && set "PY=py -3.11"
if not defined PY (
    python --version 2>&1 | findstr /R /C:"Python 3\.11\." >nul && set "PY=python"
)
if not defined PY (
    echo  ERROR: no se encuentra Python 3.11. Es la version probada con piper-tts 1.8 y gradio 5.50.
    echo         Instalala con:  winget install --id Python.Python.3.11 -e
    echo         o desde https://www.python.org/downloads/windows/  y vuelve a ejecutar install.bat
    echo ERROR: Python 3.11 no encontrado >> "%LOG%"
    goto :fail
)
%PY% --version >> "%LOG%" 2>&1
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo        %%v

rem ---------------------------------------------------------------- 2. venv
echo [2/6] Preparando el entorno virtual .venv...
if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo  ERROR: no se pudo crear el entorno virtual .venv
        goto :fail
    )
)
set "VPY=%~dp0.venv\Scripts\python.exe"
"%VPY%" -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,11) else 1)" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo  ERROR: .venv no usa Python 3.11. Borra la carpeta .venv y vuelve a ejecutar install.bat
    goto :fail
)

rem ---------------------------------------------------------------- 3. dependencias
echo [3/6] Instalando dependencias fijadas de requirements.txt (puede tardar unos minutos)...
"%VPY%" -m pip install --disable-pip-version-check --no-input -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
    echo  ERROR: fallo la instalacion de dependencias. Revisa el final del registro:
    goto :fail
)

rem ---------------------------------------------------------------- 4. verificacion
echo [4/6] Verificando que los modulos se cargan...
"%VPY%" -c "import gradio, piper, onnxruntime, PIL, numpy, pandas; print('gradio', gradio.__version__, '- onnxruntime', onnxruntime.__version__)" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo  ERROR: alguna dependencia no se puede importar.
    goto :fail
)
echo        Dependencias correctas.

rem ---------------------------------------------------------------- 5. FFmpeg
echo [5/6] Comprobando FFmpeg...
set "FFWARN="
where ffmpeg >nul 2>&1
if errorlevel 1 (
    set "FFWARN=1"
    echo  AVISO: FFmpeg no esta en el PATH. Instalalo con:  winget install --id Gyan.FFmpeg -e
    echo         o indica su ruta en la pestana Ajustes de la aplicacion.
    echo AVISO: FFmpeg no encontrado >> "%LOG%"
) else (
    for /f "tokens=1-3" %%a in ('ffmpeg -hide_banner -version 2^>^&1 ^| findstr /B /C:"ffmpeg version"') do echo        %%a %%b %%c
)

rem ---------------------------------------------------------------- 6. voz
echo [6/6] Comprobando la voz espanola de Piper...
"%VPY%" -c "from config import load_settings; import sys; s=load_settings(); s.ensure_dirs(); sys.exit(0 if any(s.voices_path.glob('*.onnx')) else 1)" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo        No hay ninguna voz instalada.
    echo        Se puede descargar es_ES-davefx-medium ^(unos 63 MB^) desde la fuente oficial:
    echo        https://huggingface.co/rhasspy/piper-voices  ^(etiqueta v1.0.0, verificacion de tamano y MD5^)
    choice /C SN /T 30 /D S /M "       Descargarla ahora? (S/N, por defecto S en 30 s)"
    if errorlevel 2 (
        echo        Descarga omitida. Puedes hacerla despues desde la pestana Voz.
    ) else (
        "%VPY%" tools\download_voice.py es_ES-davefx-medium >> "%LOG%" 2>&1
        if errorlevel 1 (
            echo  ERROR: la descarga de la voz fallo. Revisa la conexion e intentalo desde la pestana Voz.
            goto :fail
        )
        echo        Voz descargada y verificada.
    )
) else (
    echo        Ya hay voces instaladas.
)

echo.
echo  Diagnostico final del entorno:
echo.
"%VPY%" check_environment.py
set "CHK=%ERRORLEVEL%"
type logs\check_environment.txt >> "%LOG%" 2>nul
echo.
if "%CHK%"=="0" (
    echo  INSTALACION COMPLETADA. Inicia la aplicacion con start.bat
    echo RESULTADO: OK >> "%LOG%"
) else (
    echo  Instalacion terminada con avisos: revisa los [ERROR] de arriba.
    echo RESULTADO: CON AVISOS >> "%LOG%"
)
echo.
pause
exit /b 0

:fail
echo.
echo  ---- ultimas lineas del registro ----
powershell -NoProfile -Command "Get-Content -Tail 25 -Encoding UTF8 '%LOG%'"
echo RESULTADO: ERROR >> "%LOG%"
echo.
echo  La instalacion se ha detenido. Registro completo: %LOG%
pause
exit /b 1
