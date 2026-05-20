@echo off
title TSC Label Printer
echo.
echo  Verificando Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python no encontrado. Instalalo desde https://python.org
    pause
    exit /b 1
)

echo  Instalando dependencias...
pip install -q flask

echo.
echo  Iniciando TSC Label Printer en http://localhost:5050
echo  Cerrá esta ventana para detener el servidor.
echo.
python "%~dp0app.py"
pause
