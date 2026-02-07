@echo off
chcp 65001 >nul
echo ========================================
echo   Установка переводчика субтитров
echo   Windows + RTX 4090
echo ========================================
echo.

:: Проверка Ollama
where ollama >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Ollama не найдена
    echo [i] Скачай с https://ollama.com/download/windows
    echo [i] После установки запусти этот скрипт снова
    pause
    exit /b 1
)

echo [✓] Ollama найдена
echo.

:: Проверка GPU
echo [i] Проверка GPU...
nvidia-smi >nul 2>&1
if %errorlevel% equ 0 (
    echo [✓] NVIDIA GPU обнаружена
) else (
    echo [!] NVIDIA GPU не найдена, будет использоваться CPU
)
echo.

:: Загрузка модели
echo [i] Загрузка модели Translating Gemma (может занять 2-3 минуты)...
ollama pull translategemma:4b

echo.
echo [✓] Модель готова!
echo.

:: Установка Python зависимостей
echo [i] Установка Python зависимостей...
pip install -r "%~dp0requirements.txt" -q

echo.
echo ========================================
echo   Готово! Теперь можно запускать:
echo.
echo   CLI:  python translate_srt.py "file.srt"
echo   Web:  python app.py
echo         Открыть http://localhost:8847
echo ========================================
pause
