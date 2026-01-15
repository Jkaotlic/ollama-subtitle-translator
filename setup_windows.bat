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

:: Создание модели
echo [i] Загрузка модели HY-MT (может занять 2-3 минуты)...
ollama pull hf.co/tencent/HY-MT1.5-1.8B-GGUF:Q8_0
ollama create hy-mt -f Modelfile

echo.
echo [✓] Модель готова!
echo.

:: Установка Python зависимостей
echo [i] Установка Python зависимостей...
pip install flask requests -q

echo.
echo ========================================
echo   Готово! Теперь можно запускать:
echo.
echo   CLI:  python translate_srt.py "file.srt"
echo   Web:  python app.py
echo         Открыть http://localhost:8847
echo ========================================
pause
