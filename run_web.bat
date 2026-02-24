@echo off
chcp 65001 >nul
echo Запуск веб-интерфейса переводчика...
echo.
echo Открой в браузере: http://localhost:8847
echo Для остановки нажми Ctrl+C
echo.

REM Используем venv если есть
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0app.py"
) else (
    python "%~dp0app.py"
)
pause
