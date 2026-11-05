@echo off
rem Запуск сервера «Портфельный Информер». Первый запуск создаёт окружение и ставит зависимости.
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  echo Создаю виртуальное окружение...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist .env (
  copy .env.example .env >nul
  echo Создан файл .env — откройте его в Блокноте, заполните ключи и запустите start.bat снова.
  notepad .env
  exit /b 1
)
:loop
.venv\Scripts\python -m app.main >> informer.log 2>&1
echo Сервер остановился, перезапуск через 10 секунд... >> informer.log
timeout /t 10 /nobreak >nul
goto loop
