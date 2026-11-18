@echo off
setlocal
rem Instala o Virtual Display Driver (github.com/VirtualDrivers/Virtual-Display-Driver).
rem Com ele, o Windows deste PC ganha um monitor virtual: assim, quem acessa pela
rem Ponte pode usar dois monitores.
rem
rem 1) baixa o pacote oficial pelo winget (se ainda nao tiver)
rem 2) copia as configuracoes para C:\VirtualDisplayDriver (onde o driver as procura)
rem 3) instala o driver assinado com o devcon (o mesmo que o "VDD Control" faz no botao Install)

set "PKG=%~1"
if "%PKG%"=="" set "PKG=%LOCALAPPDATA%\Microsoft\WinGet\Packages\VirtualDrivers.Virtual-Display-Driver_Microsoft.Winget.Source_8wekyb3d8bbwe"

rem precisa de administrador: se nao for, reabre pedindo permissao (passando o caminho do pacote)
net session >nul 2>&1
if errorlevel 1 (
    if not exist "%PKG%\Dependencies\devcon.exe" (
        echo Baixando o pacote oficial pelo winget...
        winget install --id=VirtualDrivers.Virtual-Display-Driver -e
    )
    echo Pedindo permissao de administrador ao Windows...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '\"%PKG%\"' -Verb RunAs"
    exit /b
)

if not exist "%PKG%\Dependencies\devcon.exe" (
    echo Nao encontrei o pacote em:
    echo   %PKG%
    echo Rode este arquivo de novo SEM ser como administrador, para o winget baixar o pacote.
    pause
    exit /b 1
)

set "DEVCON=%PKG%\Dependencies\devcon.exe"
set "INF=%PKG%\SignedDrivers\x86\VDD\MttVDD.inf"

echo Copiando configuracoes para C:\VirtualDisplayDriver ...
if not exist "C:\VirtualDisplayDriver" mkdir "C:\VirtualDisplayDriver"
if not exist "C:\VirtualDisplayDriver\vdd_settings.xml" copy /y "%PKG%\SignedDrivers\x86\VDD\vdd_settings.xml" "C:\VirtualDisplayDriver\vdd_settings.xml" >nul

echo.
"%DEVCON%" find Root\MttVDD | findstr /i "ROOT\\" >nul
if errorlevel 1 (
    echo Instalando o driver do monitor virtual...
    echo Se o Windows perguntar se deseja instalar o software deste dispositivo, clique em Instalar.
    "%DEVCON%" install "%INF%" Root\MttVDD
) else (
    echo O driver ja estava instalado. Reativando...
    "%DEVCON%" enable Root\MttVDD
    "%DEVCON%" restart Root\MttVDD
)
rem devcon: 0 = ok, 1 = ok mas precisa reiniciar, 2 ou mais = falhou
if errorlevel 2 (
    echo.
    echo O Windows nao deixou instalar o driver. Veja a mensagem acima.
    echo Em PC da empresa, a instalacao de drivers pode estar bloqueada pela TI.
    pause
    exit /b 1
)
if errorlevel 1 (
    echo.
    echo Driver instalado. O Windows pediu para REINICIAR o PC para o monitor aparecer.
    pause
    exit /b 0
)

echo.
echo Pronto! Abra Configuracoes ^> Sistema ^> Tela: deve aparecer um segundo monitor.
echo Em "Varias telas", deixe "Estender estas telas".
pause
