@echo off
cd /d "%~dp0"
echo Deixe uma musica ou video do YouTube tocando neste PC antes de continuar.
pause
.venv\Scripts\python.exe diagnostico_audio.py
echo.
pause
