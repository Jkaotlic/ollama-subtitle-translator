# Анализ багов Ollama Subtitle Translator

**Дата**: 2026-04-18 (после апгрейда моделей)

## Критические

### BUG-01: Race condition на `tasks` dict
- **Файл**: [app.py:37](../app.py#L37), читается/пишется из трёх мест параллельно
- **Проблема**: три потока (Flask request, cleanup_worker, translate_worker) дёргают `tasks` без lock.
- **Следствие**: `KeyError` при одновременном удалении cleanup-ом и записью воркером; HTTP 500; потеря прогресса.
- **Решение**: `tasks_lock = threading.RLock()` + `with tasks_lock:` во всех точках мутации.
- **Статус**: [ ] Не исправлено

### BUG-02: `_enforce_glossary()` \b не работает для кириллицы/CJK ⚠️ (новое регрессия от моей правки)
- **Файл**: [translate_srt.py](../translate_srt.py) — метод `_enforce_glossary`
- **Проблема**: `\b` word boundary — только латиница/цифры. Для русского/китайского/арабского regex не срабатывает → глоссарий не энфорсится.
- **Следствие**: ключевая новая фича не работает на главной целевой паре (EN→RU).
- **Решение**: заменить `r'\b' + re.escape(src) + r'\b'` на `r'(?<![^\W_])' + re.escape(src) + r'(?![^\W_])'` (unicode word boundary) или просто `re.escape(src)` с проверкой наличия в original.
- **Статус**: [ ] Не исправлено (нужно до коммита)

### BUG-03: JSON injection через `_glossary_block`
- **Файл**: [translate_srt.py:559-564](../translate_srt.py#L559-L564)
- **Проблема**: `f"  {src} = {tgt}"` — если `src`/`tgt` содержат `{}` или кавычки, ломает JSON-prompt при батч-переводе.
- **Следствие**: искажение батч-prompt → плохой перевод всего чанка.
- **Решение**: экранировать через `json.dumps()` при построении строки.
- **Статус**: [ ] Не исправлено

### BUG-04: SQLite TM — `check_same_thread=False` + параллельные воркеры
- **Файл**: [translate_srt.py:166](../translate_srt.py#L166)
- **Проблема**: `threading.Lock()` есть, но одно соединение на все потоки при высокой нагрузке может деградировать до сериализованного доступа.
- **Следствие**: замедление при `parallel_chunks > 1` + tight контенция; при долгих commit — потенциальный deadlock.
- **Решение**: либо connection pool per-thread, либо `timeout=10` в `sqlite3.connect`.
- **Статус**: [ ] Не исправлено

## Средние

### BUG-05: `/stream_progress` KeyError если cleanup удалил task во время SSE
- **Файл**: [app.py:532-586](../app.py#L532-L586)
- **Решение**: читать через снапшот + общий lock (см. BUG-01).
- **Статус**: [ ] Не исправлено

### BUG-06: `resume_from > len(texts)` — все чанки пропускаются
- **Файл**: [translate_srt.py:1504](../translate_srt.py#L1504)
- **Сценарий**: пользователь изменил .srt после частичного перевода.
- **Решение**: `resume_from = min(len(saved_translations), len(texts))`.
- **Статус**: [ ] Не исправлено

### BUG-07: `parallel_chunks=N, но work_items=2` — misleading лог
- **Файл**: [translate_srt.py:1573](../translate_srt.py#L1573)
- **Решение**: `min(parallel_chunks, len(work_items))`.
- **Статус**: [ ] Не исправлено

### BUG-08: TranslationMemory соединение никогда не закрывается
- **Файл**: [translate_srt.py](../translate_srt.py) (класс TranslationMemory), [app.py:134-140](../app.py#L134-L140)
- **Проблема**: `Translator._tm` создан, но нет `__del__` или явного `close()` в воркере. File handle leak при долгих сессиях.
- **Решение**: добавить `Translator.close()` + вызывать в `finally` у translate_worker.
- **Статус**: [ ] Не исправлено

### BUG-09: `retranslate_weak` — если ВСЕ scores < 3, 100+ сериальных LLM-вызовов
- **Файл**: [translate_srt.py](../translate_srt.py) — метод `retranslate_weak`
- **Решение**: если `len(weak_indices) > 10` — батч-ретрансляция.
- **Статус**: [ ] Не исправлено

## Низкие

### BUG-10: Пустой SRT — молчаливый успех
- **Файл**: [app.py:125](../app.py#L125)
- **Решение**: вернуть 400 с `"SRT файл не содержит субтитров"`.
- **Статус**: [ ] Не исправлено

### BUG-11: Невалидные таймкоды пропускаются молча
- **Файл**: [translate_srt.py:294-297](../translate_srt.py#L294-L297)
- **Решение**: `logger.warning` при пропуске.
- **Статус**: [ ] Не исправлено

### BUG-12: Windows-paths в VIDEO_DIR с backslash
- **Файл**: [video_utils.py:34](../video_utils.py#L34)
- **Решение**: `Path(VIDEO_DIR).as_posix()` при склейке.
- **Статус**: [ ] Не исправлено

### BUG-13: `_is_translation_only_model` — неполный список
- **Проблема**: нет m2m100, marianMT, fairseq-* моделей.
- **Решение**: документировать или добавить heuristic.
- **Статус**: [ ] Не исправлено

## Итого

| Уровень | Кол-во |
|---|---|
| Критические | 4 |
| Средние | 5 |
| Низкие | 4 |

Приоритет: **BUG-02** (моя регрессия), **BUG-01** (race condition), **BUG-03** (glossary injection).
