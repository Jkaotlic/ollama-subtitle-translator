#!/bin/bash
# 🚀 Скрипт локального запуска переводчика субтитров на macOS

set -e

echo "🎬 Запуск переводчика субтитров..."
echo ""

# Цвета для вывода
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Проверка Ollama
echo "1️⃣ Проверка Ollama..."
if ! command -v ollama &> /dev/null; then
    echo -e "${RED}❌ Ollama не установлен!${NC}"
    echo "   Установите: brew install ollama"
    exit 1
fi

# Проверка, запущен ли Ollama
if ! curl -s http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠️  Ollama не запущен. Запускаю...${NC}"
    ollama serve > /dev/null 2>&1 &
    OLLAMA_PID=$!
    echo "   Ожидание запуска Ollama..."
    sleep 3
    
    # Проверка снова
    if ! curl -s http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
        echo -e "${RED}❌ Не удалось запустить Ollama${NC}"
        exit 1
    fi
    echo -e "${GREEN}✅ Ollama запущен (PID: $OLLAMA_PID)${NC}"
else
    echo -e "${GREEN}✅ Ollama уже запущен${NC}"
fi

# Проверка модели translategemma:4b
echo ""
echo "2️⃣ Проверка модели translategemma:4b..."
if ollama list | grep -q "translategemma:4b"; then
    echo -e "${GREEN}✅ Модель translategemma:4b установлена${NC}"
else
    echo -e "${YELLOW}⚠️  Модель translategemma:4b не найдена${NC}"
    echo "   Загружаю модель (~2.5GB)..."
    ollama pull translategemma:4b
    echo -e "${GREEN}✅ Модель загружена${NC}"
fi

# Проверка ffmpeg (опционально, для извлечения субтитров из видео)
echo ""
echo "2.5️⃣ Проверка ffmpeg (опционально)..."
if command -v ffmpeg &> /dev/null && command -v ffprobe &> /dev/null; then
    echo -e "${GREEN}✅ ffmpeg установлен (извлечение субтитров из видео доступно)${NC}"
else
    echo -e "${YELLOW}⚠️  ffmpeg не найден. Извлечение субтитров из видео недоступно.${NC}"
    echo "   Установите: brew install ffmpeg (macOS) или apt install ffmpeg (Linux)"
fi

# Проверка виртуального окружения
echo ""
echo "3️⃣ Проверка Python окружения..."
if [ ! -d ".venv" ]; then
    echo -e "${YELLOW}⚠️  Виртуальное окружение не найдено. Создаю...${NC}"
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
    echo -e "${GREEN}✅ Окружение создано${NC}"
else
    echo -e "${GREEN}✅ Виртуальное окружение найдено${NC}"
    source .venv/bin/activate
fi

# Проверка зависимостей
echo ""
echo "4️⃣ Проверка зависимостей..."
if ! python -c "import flask, requests" 2>/dev/null; then
    echo -e "${YELLOW}⚠️  Устанавливаю зависимости...${NC}"
    pip install -r requirements.txt
fi
echo -e "${GREEN}✅ Все зависимости установлены${NC}"

# Запуск приложения
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${GREEN}🚀 Запуск веб-приложения...${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo -e "${GREEN}🌐 Откройте в браузере:${NC}"
echo -e "   ${YELLOW}http://localhost:8847${NC}"
echo ""
echo "Для остановки нажмите Ctrl+C"
echo ""

# Запуск Flask приложения
python app.py
