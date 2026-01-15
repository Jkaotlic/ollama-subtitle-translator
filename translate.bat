@echo off
chcp 65001 >nul
REM 🎬 Быстрый перевод субтитров
REM Использование: translate.bat "movie.srt"
REM               translate.bat "movie.srt" Japanese
REM               translate.bat "movie.srt" Russian casual

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "TRANSLATOR=%SCRIPT_DIR%translate_srt_hf.py"

REM Проверка аргументов
if "%~1"=="" (
    echo ❌ Укажите файл субтитров!
    echo.
    echo Использование:
    echo   translate.bat movie.srt                    # На русский (natural)
    echo   translate.bat movie.srt Japanese           # На японский
    echo   translate.bat movie.srt Russian casual     # Разговорный стиль
    echo.
    echo Стили: natural (по умолчанию), casual, formal, literal
    exit /b 1
)

REM Язык по умолчанию - русский
set "LANG=Russian"
if not "%~2"=="" set "LANG=%~2"

REM Стиль по умолчанию - natural
set "STYLE=natural"
if not "%~3"=="" set "STYLE=%~3"

echo 🎬 Перевод: %~1 → %LANG% (%STYLE%)
"%PYTHON%" "%TRANSLATOR%" "%~1" -l "%LANG%" -s "%STYLE%"

endlocal
