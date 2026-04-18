# Сводка аудита Ollama Subtitle Translator

**Дата**: 2026-04-18
**Скоуп**: app.py, translate_srt.py, video_utils.py, templates/index.html, Dockerfile, docker-compose.yml
**Состояние**: после апгрейда моделей (Gemma 4 / Qwen 3.5 / Hunyuan-MT) и добавления LLM-as-judge QE, SQLite TM, glossary enforcement, back-translation

## Апгрейд: что сделано в этой сессии ✅

| # | Улучшение | Файл |
|---|---|---|
| 1 | Дефолт → `gemma4:e12b` (140 языков, reasoning) | translate_srt.py, app.py, index.html |
| 2 | AUX → `qwen3.5:8b` (был 4b) | translate_srt.py |
| 3 | UI пресеты: Gemma 4 (3 размера), Qwen 3.5, Hunyuan-MT, Llama 4 Scout | index.html |
| 4 | **`num_ctx: 8192`** в Ollama options (критично — убирает дефолтный 2048 truncation) | translate_srt.py |
| 5 | Упрощён `_is_translation_only_model` — Gemma/Qwen теперь general-purpose, без aux-костыля | translate_srt.py |
| 6 | QE через **LLM-as-judge** — heuristic pre-pass + семантическая оценка ambiguous сегментов | translate_srt.py |
| 7 | **Glossary enforcement** — мех. замена если LLM проигнорировал словарь | translate_srt.py |
| 8 | **Back-translation** метод — готов к использованию для верификации смысла | translate_srt.py |
| 9 | **SQLite Translation Memory** — персистентный кэш между сеансами (для сериалов) | translate_srt.py (class `TranslationMemory`) |
| 10 | Усиленный system prompt — Netflix subtitle rules (42 chars/line, idioms, interjections) | translate_srt.py |
| 11 | CLI флаги `--tm` / `--no-tm` | translate_srt.py |
| 12 | TM в app.py — общая БД в UPLOAD_DIR | app.py |

**Тесты**: 125/125 ✓ (добавлен 1 новый тест LLM-judge)

## Результаты аудита

### Security — 14 находок [01-security.md](01-security.md)
- **Критические**: SEC-01/02 (path traversal через save_dir/resolve_video_path), SEC-03 (stem), SEC-04 (50GB DoS), SEC-05 (SSRF)
- **Средние**: SEC-06 (sub_index не валидируется), SEC-07 (TM disk DoS), SEC-08 (нет security headers), SEC-09 (root в Docker)

### Bugs — 13 находок [02-bugs.md](02-bugs.md)
- **Критические**:
  - **BUG-02** ⚠️ — моя регрессия: `_enforce_glossary` использует `\b` — не работает для кириллицы/CJK
  - BUG-01 — race condition на `tasks` dict
  - BUG-03 — JSON injection через `_glossary_block`
  - BUG-04 — SQLite TM потокобезопасность под нагрузкой

### Dead code — 12 находок [03-dead-code.md](03-dead-code.md)
- `_call_llm_stream` не вызывается → удалить
- `back_translate` (только что добавил) — не подключён к `retranslate_weak`
- 3 IMPROVEMENTS_*.md файла → объединить/удалить
- LANGUAGES дублируется

### Dependencies — 8 находок [04-dependencies.md](04-dependencies.md)
- **Критические**: Flask 2.0→3.1.3 (CVE), requests 2.25→2.33.1 (CVE), chardet 6.0 несовместим с requests
- Python 3.8 EOL — обновить CLAUDE.md
- `requests-mock` не используется — удалить

### Architecture — 13 находок [05-architecture.md](05-architecture.md)
- translate_srt.py — 1936 строк монолит
- `_translate_chunk` — 207 строк вложенности
- `tasks` dict без lock
- `Translator.__init__` 11 параметров
- Нет единого settings.py / exception types

## Топ-5 что исправить сразу

1. **BUG-02** — починить `_enforce_glossary` для кириллицы (я сломал, я и чиню, до коммита)
2. **SEC-01** — валидация `save_dir` (path traversal)
3. **SEC-02** — `resolve_video_path` path traversal
4. **BUG-01** — Lock на `tasks` dict
5. **DEP-01/02/03** — обновить flask/requests/chardet

## План в 3 фазы

### Phase 1 — хотфиксы (сегодня)
- Фикс BUG-02 (мой regression)
- Security SEC-01, SEC-02, SEC-03
- Lock на tasks (BUG-01)
- Обновить requirements.txt

### Phase 2 — cleanup (эта неделя)
- DEAD-01..04 (удалить `_call_llm_stream`, подключить `back_translate`)
- DEAD-08..10 (git cleanup файлов)
- DEP-07 (удалить requests-mock)
- ARCH-02 (объединить LANGUAGES)
- ARCH-05 (TranslationRequest dataclass)

### Phase 3 — рефакторинг (месяц)
- ARCH-01 (разбить translate_srt.py на модули)
- ARCH-03 (Strategy pattern для _translate_chunk)
- ARCH-04 (TaskManager class)
- ARCH-09 (Pipeline для phases)
