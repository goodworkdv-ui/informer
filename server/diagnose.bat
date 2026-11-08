@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Папка: %cd%
echo === Python в окружении:
if exist .venv\Scripts\python.exe (.venv\Scripts\python --version) else (echo НЕТ .venv - запустите start.bat)
echo === Зависимости:
.venv\Scripts\python -c "import fastapi, uvicorn, httpx, numpy, tzdata; print('OK')"
echo === Файл .env:
if exist .env (echo есть) else (echo НЕТ - скопируйте .env.example в .env)
echo === Импорт приложения:
.venv\Scripts\python -c "import app.main; print('OK')"
echo === Порт 8800:
netstat -ano | findstr ":8800"
echo === Ответ сервера:
curl -s http://127.0.0.1:8800/healthz
echo.
echo === Доступ к источникам данных:
curl -s -o nul -w "MOEX ISS: %%{http_code}\n" https://iss.moex.com/iss/index.json
curl -s -o nul -w "Finnhub: %%{http_code}\n" https://finnhub.io/api/v1/quote
curl -s -o nul -w "Twelve Data: %%{http_code}\n" https://api.twelvedata.com/time_series
curl -s -o nul -w "Telegram: %%{http_code}\n" https://api.telegram.org
echo === Последние строки informer.log:
if exist informer.log (powershell -NoProfile -Command "Get-Content informer.log -Tail 30 -Encoding UTF8") else (echo лога нет)
pause
