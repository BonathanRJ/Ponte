@echo off
cd /d "%~dp0"
echo Criando ambiente Python da Ponte...
python -m venv .venv || (echo Python nao encontrado. Instale o Python 3.11+ em python.org marcando "Add python.exe to PATH". & pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)
echo.
echo Pronto! Use Ponte.bat para abrir.
if not defined PONTE_AUTO pause
