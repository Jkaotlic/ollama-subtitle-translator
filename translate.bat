@echo off
chcp 65001 >nul
REM 🎬 Быстрый перевод субтитров (CLI)
REM Использование: translate.bat "movie.srt"
REM               translate.bat "movie.srt" Japanese
REM               translate.bat "movie.srt" Russian -s English

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
REM Используем системный Python если виртуальное окружение не создано
if exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
    set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)
set "TRANSLATOR=%SCRIPT_DIR%translate_srt.py"

REM Проверка аргументов
if "%~1"=="" (
    echo ❌ Укажите файл субтитров!
    echo.
    echo Использование:
    echo   translate.bat movie.srt                        # На русский
    echo   translate.bat movie.srt Japanese               # На японский
    echo   translate.bat movie.srt Russian -s English     # С указанием исходного языка
    echo   translate.bat movie.srt Russian --two-pass     # Двухпроходный перевод
    echo.
    echo Все флаги: %PYTHON% %TRANSLATOR% --help
    exit /b 1
)

REM Язык по умолчанию - русский
set "LANG=Russian"
if not "%~2"=="" set "LANG=%~2"

echo 🎬 Перевод: %~1 → %LANG%
"%PYTHON%" "%TRANSLATOR%" "%~1" -l "%LANG%" %3 %4 %5 %6 %7 %8 %9

endlocal
