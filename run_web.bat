@echo off
chcp 65001 >nul
echo Запуск веб-интерфейса переводчика...
echo.
echo Открой в браузере: http://localhost:8847
echo Для остановки нажми Ctrl+C
echo.
python app.py
pause
