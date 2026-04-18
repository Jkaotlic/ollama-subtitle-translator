# Анализ мёртвого кода Ollama Subtitle Translator

**Дата**: 2026-04-18

## Полностью мёртвое (удалить)

### DEAD-01: `_call_llm_stream()` никогда не вызывается
- **Файл**: [translate_srt.py:852-898](../translate_srt.py#L852-L898)
- **Проверка**: `grep -r "_call_llm_stream(" .` → 0 вызовов (только в CLAUDE.md как документация).
- **Решение**: удалить метод целиком.
- **Статус**: [ ] Не исправлено

### DEAD-02: `as_completed` импортирован, но не используется
- **Файл**: [translate_srt.py:33](../translate_srt.py#L33)
- **Решение**: `from concurrent.futures import ThreadPoolExecutor` (убрать `as_completed`).
- **Статус**: [ ] Не исправлено

### DEAD-03: `import types` внутри `_call_llm_stream`
- **Файл**: [translate_srt.py:855](../translate_srt.py#L855)
- **Проблема**: импортируется, но вместо объекта используется строковый type hint.
- **Решение**: удалить (автоматически при удалении DEAD-01).
- **Статус**: [ ] Не исправлено

### DEAD-04: `back_translate()` добавлен, но не вызывается нигде ⚠️
- **Файл**: [translate_srt.py:592-603](../translate_srt.py#L592-L603)
- **Проблема**: я добавил метод в этой сессии, но не подключил его к `retranslate_weak` как quality-gate. Либо подключить, либо удалить.
- **Решение**: подключить в `retranslate_weak` как вторую проверку — если back-translation имеет низкую похожесть на оригинал, ретранслировать. ИЛИ оставить как public API.
- **Статус**: [ ] Не исправлено

## Документация

### DEAD-05: Module-docstring `translate_srt.py` упоминает устаревшие примеры
- **Файл**: [translate_srt.py:3-12](../translate_srt.py#L3-L12)
- **Проблема**: "Использует модель Translating Gemma", `ollama pull translategemma:4b`.
- **Решение**: обновить на Gemma 4 / Qwen 3.5 / Hunyuan-MT.
- **Статус**: [ ] Не исправлено

### DEAD-06: Комментарий `_build_system_prompt` упоминает translategemma
- **Файлы**: [translate_srt.py:903](../translate_srt.py#L903), [translate_srt.py:913](../translate_srt.py#L913)
- **Решение**: обновить на "NLLB, ALMA" (legacy translation-only models).
- **Статус**: [ ] Не исправлено

### DEAD-07: CLAUDE.md упоминает `_call_llm_stream()` как существующий
- **Файл**: [CLAUDE.md:28](../CLAUDE.md#L28)
- **Решение**: удалить или пометить как planned/unused.
- **Статус**: [ ] Не исправлено

## Дубликаты / файлы

### DEAD-08: 3 IMPROVEMENTS_*.md файла
- **Файлы**: IMPROVEMENTS.md, IMPROVEMENTS_V2.md, IMPROVEMENTS_V3.md
- **Проблема**: вся история уже реализована и документирована в коде + CLAUDE.md.
- **Решение**: объединить в `CHANGELOG.md` либо удалить (история есть в git).
- **Статус**: [ ] Не исправлено

### DEAD-09: Тестовые .srt в корне репо
- **Файлы**: `The Pitt - S02E09...srt`, `Young.Sherlock.2026...srt`
- **Проблема**: `.gitignore` содержит `*.srt`, но эти в репо.
- **Решение**: `git rm --cached "*.srt"` + проверить .gitignore работает.
- **Статус**: [ ] Не исправлено

### DEAD-10: `__pycache__/` в корне и в tests/
- **Решение**: `git rm -rf --cached __pycache__ tests/__pycache__`
- **Статус**: [ ] Не исправлено

### DEAD-11: `ffmpeg_bin/` статус неясен
- **Проблема**: `.gitignore` исключает, но папка присутствует. Проверить чистоту git.
- **Решение**: `git status --ignored` → убедиться что не трекается.
- **Статус**: [ ] Проверить

### DEAD-12: `LANGUAGES` дублируется
- **Файлы**: [app.py:48-53](../app.py#L48-L53), [translate_srt.py:45-63](../translate_srt.py#L45-L63)
- **Проблема**: разный формат + разные наборы (app.py — {display: code}, translate_srt.py — многоключевой).
- **Решение**: Экспортировать один источник истины из translate_srt.py, импортировать в app.py.
- **Статус**: [ ] Не исправлено

## Итого

| Категория | Кол-во | ID |
|---|---|---|
| Мёртвый код (удалить) | 4 | DEAD-01..04 |
| Устаревшая документация | 3 | DEAD-05..07 |
| Файлы/дубликаты | 5 | DEAD-08..12 |
