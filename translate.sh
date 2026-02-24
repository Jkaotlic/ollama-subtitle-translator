#!/bin/bash
# 🎬 Быстрый перевод субтитров (CLI)
# Использование: ./translate.sh "movie.srt"
#               ./translate.sh "movie.srt" Japanese
#               ./translate.sh "movie.srt" Russian -s English

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
TRANSLATOR="$SCRIPT_DIR/translate_srt.py"

# Проверка аргументов
if [ -z "$1" ]; then
    echo "❌ Укажите файл субтитров!"
    echo ""
    echo "Использование:"
    echo "  ./translate.sh movie.srt                        # На русский"
    echo "  ./translate.sh movie.srt Japanese               # На японский"
    echo "  ./translate.sh movie.srt Russian -s English     # С указанием исходного языка"
    echo "  ./translate.sh movie.srt Russian --two-pass     # Двухпроходный перевод"
    echo ""
    echo "Все флаги: $PYTHON $TRANSLATOR --help"
    exit 1
fi

INPUT="$1"
shift

# Язык по умолчанию - русский
LANG="${1:-Russian}"
shift 2>/dev/null

echo "🎬 Перевод: $INPUT → $LANG"
"$PYTHON" "$TRANSLATOR" "$INPUT" -l "$LANG" "$@"
