@echo off
chcp 65001 >nul
title Portfolio Informer
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo [1/2] Создаю виртуальное окружение...
  py -3 -m venv .venv || python -m venv .venv
  if not exist .venv\Scripts\python.exe (
    echo ОШИБКА: Python не найден. Установите Python 3.12 с python.org, отметив "Add python.exe to PATH".
    pause
    exit /b 1
  )
  echo [2/2] Ставлю зависимости...
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist .env (
  copy .env.example .env >nul
  echo Создан файл .env. Заполните ключи в Блокноте, сохраните и запустите start.bat снова.
  notepad .env
  exit /b 1
)
:loop
.venv\Scripts\python -m app.main
if %errorlevel%==2 (
  pause
  exit /b 2
)
echo.
echo Сервер остановился (код %errorlevel%). Ошибка выше и в informer.log. Перезапуск через 10 секунд, Ctrl+C - отмена.
timeout /t 10
goto loop
