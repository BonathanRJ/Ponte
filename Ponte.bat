@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo A Ponte ainda nao foi instalada nesta pasta. Instalando agora, so desta vez...
    echo.
    set PONTE_AUTO=1
    call instalar.bat
    if not exist ".venv\Scripts\pythonw.exe" (
        echo.
        echo A instalacao nao terminou. Veja as mensagens acima.
        pause
        exit /b 1
    )
)
start "" .venv\Scripts\pythonw.exe main.py
