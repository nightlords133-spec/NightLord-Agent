@echo off
chcp 65001 >nul
title Установка Python 3.12 для Nightlord агента
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_python.ps1"
