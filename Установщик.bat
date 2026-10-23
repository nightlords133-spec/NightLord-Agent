@echo off
chcp 65001 >nul
title Установщик Nightlord агента
powershell -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Minimized -File "%~dp0install_gui.ps1"
if errorlevel 1 (
  echo.
  echo Установщик завершился с ошибкой. Запустите вручную: install_python.bat и py -3.12 check_deps.py
  pause
)
