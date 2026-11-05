@echo off
rem Регистрирует автозапуск сервера при входе в Windows (Планировщик заданий). Запускать от имени администратора.
chcp 65001 >nul
cd /d "%~dp0"
schtasks /Create /F /TN "PortfolioInformer" /SC ONLOGON /RL HIGHEST /TR "\"%~dp0start.bat\"" /DELAY 0001:00
if %errorlevel%==0 (echo Автозапуск настроен: задача PortfolioInformer.) else (echo Не удалось. Запустите файл от имени администратора.)
pause
