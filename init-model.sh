#!/bin/bash
# Скрипт первоначальной настройки модели hy-mt в Ollama

echo "⏳ Ожидание запуска Ollama..."
until curl -s http://ollama:11434/api/tags > /dev/null 2>&1; do
    sleep 2
done

echo "📥 Создание модели hy-mt..."
cat << 'EOF' | curl -s -X POST http://ollama:11434/api/create -d @-
{
  "name": "hy-mt",
  "modelfile": "FROM hf.co/tencent/HY-MT1.5-1.8B-GGUF:Q8_0\nPARAMETER temperature 0.1\nPARAMETER num_ctx 512\nTEMPLATE \"<|im_start|>user\n{{ .Prompt }}<|im_end|>\n<|im_start|>assistant\""
}
EOF

echo "✅ Готово!"
