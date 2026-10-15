@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
set "LOG=%~dp0logs\diagnostico_inicial.txt"
echo Diagnostico ShortsFactory - %DATE% %TIME% > "%LOG%"
echo. >> "%LOG%"
echo ===== SISTEMA ===== >> "%LOG%"
ver >> "%LOG%" 2>&1
powershell -NoProfile -Command "(Get-CimInstance Win32_OperatingSystem).Caption; (Get-CimInstance Win32_Processor).Name; (Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name); '{0:N1} GB RAM' -f ((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB)" >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo ===== DISCO (unidad del proyecto) ===== >> "%LOG%"
powershell -NoProfile -Command "$d=(Get-Item '%~dp0').PSDrive; '{0}: libre {1:N1} GB' -f $d.Name, ($d.Free/1GB)" >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo ===== PYTHON ===== >> "%LOG%"
echo [where python] >> "%LOG%"
where python >> "%LOG%" 2>&1
echo [python --version] >> "%LOG%"
python --version >> "%LOG%" 2>&1
echo [py -0p : versiones instaladas via launcher] >> "%LOG%"
py -0p >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo ===== FFMPEG ===== >> "%LOG%"
where ffmpeg >> "%LOG%" 2>&1
ffmpeg -hide_banner -version 2>&1 | findstr /B /C:"ffmpeg version" >> "%LOG%"
where ffprobe >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo ===== OTRAS HERRAMIENTAS ===== >> "%LOG%"
echo [winget] >> "%LOG%"
winget --version >> "%LOG%" 2>&1
echo [git] >> "%LOG%"
git --version >> "%LOG%" 2>&1
echo [node / npx] >> "%LOG%"
node --version >> "%LOG%" 2>&1
where npx >> "%LOG%" 2>&1
echo [piper] >> "%LOG%"
where piper >> "%LOG%" 2>&1
echo [curl] >> "%LOG%"
where curl >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo ===== LM STUDIO (localhost:1234) ===== >> "%LOG%"
curl -s -m 3 http://localhost:1234/v1/models >> "%LOG%" 2>&1
if errorlevel 1 echo LM Studio no responde en localhost:1234 >> "%LOG%"
echo. >> "%LOG%"
echo FIN >> "%LOG%"
echo.
echo Diagnostico completado. Resultado en: %LOG%
echo Esta ventana se cerrara en 5 segundos...
timeout /t 5 >nul
