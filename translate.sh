#!/bin/bash
# 🎬 Быстрый перевод субтитров
# Использование: ./translate.sh "movie.srt"
#               ./translate.sh "movie.srt" Japanese
#               ./translate.sh "movie.srt" Russian casual

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
TRANSLATOR="$SCRIPT_DIR/translate_srt_hf.py"

# Проверка аргументов
if [ -z "$1" ]; then
    echo "❌ Укажите файл субтитров!"
    echo ""
    echo "Использование:"
    echo "  ./translate.sh movie.srt                    # На русский (natural)"
    echo "  ./translate.sh movie.srt Japanese           # На японский"
    echo "  ./translate.sh movie.srt Russian casual     # Разговорный стиль"
    echo ""
    echo "Стили: natural (по умолчанию), casual, formal, literal"
    exit 1
fi

# Язык по умолчанию - русский
LANG="${2:-Russian}"
# Стиль по умолчанию - natural
STYLE="${3:-natural}"

echo "🎬 Перевод: $1 → $LANG ($STYLE)"
"$PYTHON" "$TRANSLATOR" "$1" -l "$LANG" -s "$STYLE"
