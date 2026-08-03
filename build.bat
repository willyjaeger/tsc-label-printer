@echo off
title Compilando EnvioBot...
echo.
echo  ================================================
echo   EnvioBot - Build
echo  ================================================
echo.

:: Verificar Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python no encontrado.
    echo  Instalar desde https://python.org
    pause
    exit /b 1
)

echo  [1/3] Instalando dependencias...
pip install -q flask pyinstaller pymupdf Pillow requests pystray
if errorlevel 1 (
    echo  ERROR al instalar dependencias.
    pause
    exit /b 1
)

echo  [2/3] Limpiando build anterior...
if exist "dist\EnvioBot.exe" del /f /q "dist\EnvioBot.exe"
if exist "build" rmdir /s /q "build"
if exist "EnvioBot.spec" del /f /q "EnvioBot.spec"

echo  [3/3] Compilando...
python -m PyInstaller ^
    --onefile ^
    --noconsole ^
    --add-data "index.html;." ^
    --hidden-import=flask ^
    --hidden-import=werkzeug ^
    --hidden-import=requests ^
    --hidden-import=PIL ^
    --hidden-import=PIL.Image ^
    --hidden-import=pystray ^
    --hidden-import=pystray._win32 ^
    --collect-all=requests ^
    --collect-all=fitz ^
    --collect-all=PIL ^
    --collect-all=pystray ^
    --name "EnvioBot" ^
    app.py

if errorlevel 1 (
    echo.
    echo  ERROR durante la compilacion. Ver mensajes arriba.
    pause
    exit /b 1
)

echo.
echo  ================================================
echo   Listo!
echo   dist\EnvioBot.exe
echo.
echo   Ese unico archivo se puede copiar a cualquier
echo   PC con Windows, no necesita Python instalado.
echo  ================================================
echo.

:: Abrir la carpeta dist
explorer dist

pause
