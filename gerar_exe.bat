@echo off
rem Opcional: gera dist\Ponte.exe (um arquivo so) para levar ao outro PC sem instalar Python.
cd /d "%~dp0"
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name Ponte main.py
echo.
echo Executavel em dist\Ponte.exe
pause
