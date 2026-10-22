@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.12 voice_agent.py
if errorlevel 1 (
  echo.
  echo Агент завершился с ошибкой. Если нет Python 3.12 или пакетов: install_python.bat и check_deps.py
  pause
)
