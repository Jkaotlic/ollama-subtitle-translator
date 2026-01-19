#!/bin/sh
# Скрипт первоначальной настройки модели translategemma:4b в Ollama

echo "⏳ Ожидание запуска Ollama..."
until curl -s http://ollama:11434/api/tags > /dev/null 2>&1; do
    sleep 5
done

echo "📥 Загрузка модели translategemma:4b..."
echo "⚠️  Внимание: Модель ~2.5GB, загрузка может занять несколько минут"

# Проверяем, есть ли уже модель
if curl -s http://ollama:11434/api/tags | grep -q "translategemma:4b"; then
    echo "✅ Модель translategemma:4b уже установлена!"
else
    # Загружаем модель через API
    curl -X POST http://ollama:11434/api/pull -d '{
      "name": "translategemma:4b"
    }'
    echo ""
    echo "✅ Модель translategemma:4b загружена!"
fi

echo "🚀 Готово! Переводчик готов к работе."
