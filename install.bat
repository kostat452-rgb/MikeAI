@echo off
chcp 65001 >nul
title Mike AI - Установка
echo ============================================
echo    🚀 Mike AI - Установка
echo ============================================
echo.
echo [1/2] Установка зависимостей...
pip install -r requirements.txt --quiet
echo ✅ Зависимости установлены
echo.
echo [2/2] Запуск системы...
echo ✅ Бот и веб-интерфейс запускаются...
echo 📋 Веб-панель: http://localhost:8000
echo.
python run.py
pause