# Архитектурный анализ Ollama Subtitle Translator

**Дата**: 2026-04-18

## Критические

### ARCH-01: translate_srt.py — 1936 строк, монолит
- **Файл**: [translate_srt.py](../translate_srt.py)
- **Проблема**: один файл содержит парсер SRT, Translator, TranslationMemory, CLI, helpers, script-detection, retry-logic.
- **Рекомендация**: разбить:
  ```
  src/core/translator.py
  src/core/translation_memory.py
  src/data/srt_parser.py
  src/data/tag_protection.py
  src/utils/retry.py
  src/utils/script_detection.py
  src/cli.py
  ```
- **Сложность**: высокая
- **Статус**: [ ] Не исправлено

### ARCH-02: `LANGUAGES` dict дублируется в 2 файлах с разным форматом
- **Файлы**: [app.py:48](../app.py#L48), [translate_srt.py:45](../translate_srt.py#L45)
- **Проблема**: разные ключи, разный охват языков. Добавление языка требует правок в 2 местах.
- **Рекомендация**: единый `settings/languages.py` с функцией `get_language_code(name)`.
- **Сложность**: низкая
- **Статус**: [ ] Не исправлено

### ARCH-03: `_translate_chunk` — 207 строк, 3-tier retry/fallback копипаста
- **Файл**: [translate_srt.py:1272-1478](../translate_srt.py#L1272-L1478)
- **Проблема**: batch retry → JSON parse → SEP parse → validate → alignment check → retranslate weak → per-segment fallback — всё в одном методе. Копипаст per-segment логики.
- **Рекомендация**: Strategy pattern:
  ```python
  strategies = [BatchJSONStrategy(), BatchSEPStrategy(), PerSegmentStrategy()]
  for s in strategies:
      result = s.translate(chunk)
      if result and validate(result): return result
  ```
- **Сложность**: средняя
- **Статус**: [ ] Не исправлено

### ARCH-04: Глобальное `tasks` dict без lock + нет graceful shutdown executor
- **Файл**: [app.py:37](../app.py#L37)
- **Проблема**: multi-thread без синхронизации (уже в BUG-01); `executor.shutdown()` не вызывается чисто.
- **Рекомендация**: `TaskManager` класс с `RLock`, `submit_work`, `shutdown(timeout)`.
- **Сложность**: средняя
- **Статус**: [ ] Не исправлено

### ARCH-05: `translate_worker` принимает 13 параметров — тесный coupling
- **Файл**: [app.py:105-113](../app.py#L105-L113)
- **Рекомендация**: `@dataclass TranslationRequest` — один объект с валидацией через `.validate()`.
- **Сложность**: низкая
- **Статус**: [ ] Не исправлено

## Средние

### ARCH-06: `Translator.__init__` 11 параметров — смешанные ответственности
- **Файл**: [translate_srt.py](../translate_srt.py) `__init__`
- **Рекомендация**: группировка в 3 конфига — `TranslatorConfig`, `OllamaConfig`, `CacheConfig`.
- **Статус**: [ ] Не исправлено

### ARCH-07: `except Exception: pass` и слишком широкие `except (ValueError, KeyError, TypeError)`
- **Проблема**: проглатывает programming errors, маскирует баги.
- **Рекомендация**: узкие except + обязательный `logger.warning(..., exc_info=True)`.
- **Статус**: [ ] Не исправлено

### ARCH-08: Side-effects в `analyze_context` — одновременно возвращает и мутирует `self._context_analysis`
- **Файл**: [translate_srt.py](../translate_srt.py) `analyze_context`
- **Рекомендация**: либо только return, либо только set.
- **Статус**: [ ] Не исправлено

### ARCH-09: `translate_worker` фазы (glossary→analysis→translate→qe→cps) закодированы императивно
- **Файл**: [app.py:147-202](../app.py#L147-L202)
- **Рекомендация**: `Pipeline` класс с `add_phase(name, fn, enabled=bool)`.
- **Статус**: [ ] Не исправлено

## Низкие

### ARCH-10: Cache layer (`_cache`) + TM — fuzzy lookup работает только в памяти
- **Файл**: [translate_srt.py:700-741](../translate_srt.py#L700-L741)
- **Проблема**: Persistent TM не участвует в fuzzy matching.
- **Рекомендация**: `CacheLayer` класс с единым lookup/store.
- **Статус**: [ ] Не исправлено

### ARCH-11: `templates/index.html` — 1192 строки с inline CSS+JS
- **Рекомендация**: разбить на `templates/base.html`, `static/css/*.css`, `static/js/*.js`.
- **Статус**: [ ] Не исправлено

### ARCH-12: Env-vars разбросаны, нет `settings.py`
- **Проблема**: `LOG_LEVEL`, `FILE_TTL`, `TASK_TTL`, `OLLAMA_URL`, `UPLOAD_DIR` и др. лежат в разных местах, без валидации.
- **Рекомендация**: `config.py` с `Settings(BaseSettings)` из pydantic + `.env`.
- **Статус**: [ ] Не исправлено

### ARCH-13: Нет custom exception classes
- **Рекомендация**: `exceptions.py` с `OllamaError`, `ModelNotFoundError`, `ChunkTranslationError`.
- **Статус**: [ ] Не исправлено

## Итого

| Уровень | Кол-во |
|---|---|
| Критические | 5 |
| Средние | 4 |
| Низкие | 4 |

## Фазы рефакторинга

**Phase 1 (1-2 дня)**: ARCH-02, ARCH-05, ARCH-12, ARCH-13 — низкая сложность, большой эффект
**Phase 2 (3-5 дней)**: ARCH-06, ARCH-08, ARCH-09, ARCH-10
**Phase 3 (1-2 недели)**: ARCH-01, ARCH-03, ARCH-04 — крупный рефакторинг
**Phase 4 (optional)**: ARCH-11 — UI refactor
