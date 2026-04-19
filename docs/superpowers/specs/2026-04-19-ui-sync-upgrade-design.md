# UI-синхронизация после апгрейда 2026-04-18

**Дата:** 2026-04-19
**Контекст:** после апгрейда `80757dd` бэк получил Translation Memory, LLM-as-judge QE,
back-translation, aux_model и ряд security-валидаций. В UI эти настройки не появились,
а кнопка «Скачать модель» плохо показывает прогресс из-за false positive в `/check_model`.

## Цели

1. Дать пользователю контроль над новыми фичами бэка (TM, LLM-judge, back-translation, aux-модель).
2. Устранить «тихие» ошибки: отклонённый `save_dir` и мгновенно исчезающий pull-прогресс.
3. Показать полезную обратную связь: авто-глоссарий, анализ контекста, QE-статистику, TM hits.
4. Освежить устаревший текст (подпись про TranslateGemma/55 языков).

Из scope исключено: редизайн макета, тёмная/светлая тема, i18n-работа, новые модели
(поcтавляем только то, что уже умеет бэк).

## Архитектурная раскладка (A3)

`.advanced-panel` остаётся одним коллапсом, но внутри делится на три визуальных
подраздела с заголовками `<h4>` и тонкими разделителями (`border-top`):

```
┌─ Расширенные настройки ▼ ─────────────────────────────┐
│ ▸ Контент                                             │
│    source_lang / genre / context / glossary           │
│                                                       │
│ ─────────────────────────────────────                 │
│ ▸ Параметры модели                                    │
│    temperature / chunk_size / context_window / cps    │
│    two_pass + review_model                            │
│    aux_model (новое)                                  │
│                                                       │
│ ─────────────────────────────────────                 │
│ ▸ Качество и память                                   │
│    [✓] Анализ контента  [✓] Авто-глоссарий            │
│    [✓] Проверка качества (QE)                         │
│       └ [ ] LLM-оценка (точнее, медленнее)            │
│    [ ] Контроль обратного перевода                    │
│    [✓] Translation Memory   hits: N / size: MB  [очистить] │
└───────────────────────────────────────────────────────┘
```

Отдельный блок вне advanced — `#resultInfo` между прогресс-баром и `downloadBtn`, появляется
после `status === "done"`:

```
┌─ Результаты перевода ────────────────────────────────┐
│ ⏱ 2m 14s                                             │
│ 🔖 Авто-глоссарий: 12 имён  ▸ развернуть              │
│ 🎬 Анализ: медицинский сериал, жаргон ER              │
│ ✅ QE: 3 слабых сегмента перевели заново              │
│ 💾 TM: +47 новых записей (всего 1 234)                │
└──────────────────────────────────────────────────────┘
```

## Контракт изменений

### 1. Translation Memory (UI + API)

**UI:**
- Чекбокс `#use_tm` (default `true`) в подразделе «Качество и память».
- Строка статуса рядом с чекбоксом: `size: N записей · Mb MB`. Значения берутся из
  нового поля `/check_model` ответа — или из нового endpoint `/tm/stats` (см. ниже).
- Кнопка-ссылка `[очистить]` → POST `/tm/clear`, после успеха — обновить статус.

**API:**
- `/tm/stats` (GET) → `{"entries": int, "size_bytes": int}`. Использует
  `TranslationMemory.stats()` (метод уже есть).
- `/tm/clear` (POST) → `{"ok": true, "cleared": int}`. Метод `TranslationMemory.clear()`
  нужно добавить (один `DELETE FROM translations` + `VACUUM`).
- `/translate` и `/extract_and_translate`: новое поле `use_tm` (form/JSON, default `true`).
  Worker передаёт `tm_path=None` если выключено.

### 2. LLM-as-judge (UI)

- Вложенный чекбокс `#use_llm_judge` под `#qe` (default `true`), показывается только
  когда `#qe` отмечен. Подсказка: «эвристика + семантическая оценка aux-моделью».
- Передаётся в `/translate` и `/extract_and_translate` как `use_llm_judge` (default `true`).
- Worker пробрасывает в `translator.estimate_quality(..., use_llm_judge=...)`.

### 3. Back-translation (UI)

- Чекбокс `#use_back_translation` (default `false`), рядом с QE.
- Передаётся в worker, `retranslate_weak(..., use_back_translation=...)`.
- Подсказка: «проверяет смысл через обратный перевод, +время».

### 4. aux-модель override (UI)

- Текстовое поле `#aux_model` в подразделе «Параметры модели», placeholder
  `qwen3.5:8b (по умолчанию)`. Пустая строка — не передавать в бэк (дефолтное поведение
  `Translator`-а).
- Worker: если задано — передать `aux_model=...` в конструктор `Translator`.
- Баннер `#auxModelBanner` остаётся, но пересобирается при смене значения.
  `checkPresetModels` добавляет пересборку после каждого pull.

### 5. Подпись в footer (UI)

Заменить:
```
TranslateGemma (Google) — специализированные модели для перевода, 55 языков
```
На:
```
Gemma 4 / Qwen 3.5 / Hunyuan-MT — локальный перевод через Ollama, 140+ языков
```

### 6. save_dir: видимая ошибка (UI + API)

**API:** `/translate` и `/extract_and_translate` при rejected `save_dir_raw` возвращают
в успешном ответе дополнительное поле `warning`:
```json
{"task_id": "...", "warning": "save_dir отклонён: путь не в разрешённых директориях (Downloads/Videos/Desktop)"}
```
При этом задача всё равно запускается (сохранение только в `UPLOAD_DIR`). Это не ошибка.

**UI:** если есть `warning` — показать жёлтый `status` («status.warn», новый класс)
с текстом и не блокировать перевод.

### 7. Результаты перевода (UI)

Новый блок `#resultInfo`, скрыт по умолчанию. В `pollProgress` при `status === "done"`
наполнение из:
- `data.started_at` / `data.completed_at` → длительность.
- `data.auto_glossary` → dict → количество + раскрывающийся список.
- `data.context_analysis_result` → первые 300 симв.
- `data.qe_weak_count` → целое.
- `data.tm_hits_delta` (новое поле, см. ниже).

**API:** worker пишет в task при завершении:
- `tm_hits_delta`: разница `translator._tm.stats()["entries"]` до и после прогона.
  Если TM отключён — не пишем поле.
- `duration_seconds`: float.

### 8. Починка pull-прогресса (UI + API)

**API — `/check_model`:**
- Заменить substring `any(model_name in m for m in available)` на exact match:
  ```python
  exists = model_name in available  # прямое сравнение полной строки "gemma4:e12b"
  ```
- Для кейса `__list_all__` возвращаем `available` как есть — UI сам решает.
- Дополнительно: нормализация тега — если пользователь ввёл `gemma4:e12b` а Ollama
  хранит как `gemma4:e12b-instruct-q4_K_M`, считать это **разными** моделями. Пользователь
  хочет именно тот тег, что в пресете.

**UI — `pullModel`:**
- Ввести константу `MIN_PULL_VISIBLE_MS = 1500`.
- Запоминать `startTs = Date.now()` перед `fetch('/pull_model', ...)`.
- При получении `status === 'done'` считать оставшийся `wait = max(0, MIN_PULL_VISIBLE_MS - (Date.now() - startTs))`
  и заменить жёсткий `setTimeout(..., 500)` на `setTimeout(..., wait)`.
- Если первый pulling event пришёл за <300 мс и total не рос — заменить текст «Готово!»
  на «Уже в кэше Ollama».

**API — logging pull:**
- В `/pull_model` добавить `logger.info("pull=%s status=done elapsed=%.1fs", model, elapsed)`.

## Обратная совместимость

- Все новые form/JSON поля имеют дефолты, совпадающие с текущим поведением. Старые
  клиенты (CLI, curl) не сломаются.
- `/check_model` — exact match — потенциальный regression: если кто-то полагался на то,
  что `"gemma"` в запросе матчит любую gemma-модель, получит `false`. Но в коде
  проекта так никто не делает (см. `checkPresetModels` + `ensureModels` — там всегда
  полный тег).

## Тестирование

### Автотесты (pytest)

- `test_check_model_exact_match`: мокаем `/api/tags` с `["gemma4:e12b-instruct"]`,
  запрашиваем `"gemma4:e12b"` → `exists: false`.
- `test_tm_stats_endpoint`: TM с 5 записями → `{"entries": 5, "size_bytes": >0}`.
- `test_tm_clear_endpoint`: после clear — `entries == 0`.
- `test_translate_use_tm_false`: form с `use_tm=false` → Translator создан без `tm_path`.
- `test_save_dir_warning_returned`: rejected path → JSON содержит `warning`.
- `test_translate_use_llm_judge_forwarded`: mock `estimate_quality`, проверить аргумент.

### Ручное тестирование (перечислить явно в плане)

1. Скачать модель, которой нет в Ollama → прогресс виден до конца.
2. «Скачать» уже скачанную модель → pull-панель держится ≥1.5 сек, текст «Уже в кэше».
3. Включить TM → перевести файл → результаты показывают `tm_hits_delta > 0`.
4. Выключить TM → `.db` файл не создаётся / не растёт.
5. QE + LLM-judge off → скорость выше (чисто эвристика), `qe_weak_count` всё равно считается.
6. Back-translation on → в логах warning при низкой similarity (там где оно падает).
7. Указать aux_model = `llama3:8b` → в логах `aux=llama3:8b`.
8. save_dir = `C:\Windows\System32` → жёлтый warning в UI, перевод завершается.
9. Обновить страницу в середине перевода → прогресс восстанавливается (регрессия).

## Риски и известные ограничения

- `TranslationMemory.clear()` не должно выполняться во время активного перевода:
  endpoint `/tm/clear` при занятом TM вернёт `409 Conflict`. Реализация: попробовать
  взять `_tm.lock` с `timeout=0.5` — если не удалось, 409.
- Размер TM (`size_bytes`) на Windows читается через `Path.stat().st_size`. Пойдёт.
- LLM-judge отключённый → `estimate_quality` вернёт только heuristic scores, ветка
  retranslate_weak всё равно сработает. Это ОК, просто грубее.
- В A3 всё ещё один большой коллапс — если у пользователя плохая вертикаль (720p),
  появится скролл внутри панели. Принимаем — альтернатива (три коллапса) хуже по UX.

## Порядок работ (для writing-plans)

Работы делятся на **4 независимых блока**, которые можно делать параллельно:

1. **Backend: API и worker**
   - `/tm/stats`, `/tm/clear`, `TranslationMemory.clear()`
   - `/check_model` exact match + тест
   - `/translate` / `/extract_and_translate`: `use_tm`, `use_llm_judge`,
     `use_back_translation`, `aux_model`, `warning` в ответе
   - Worker: пробрасывание всех новых флагов, `tm_hits_delta`, `duration_seconds`

2. **Frontend: новые контролы**
   - Подразделы в `.advanced-panel`, чекбоксы TM/judge/back-translation, поле aux-модели
   - Передача новых полей в `buildFormData` и `buildVideoPayload`
   - Подпись footer, класс `.status.warn`
   - Показ `warning` в UI

3. **Frontend: pull-прогресс**
   - `MIN_PULL_VISIBLE_MS`, логика «уже в кэше», починка `downloadPresetModel`

4. **Frontend: `#resultInfo` блок**
   - HTML + CSS + наполнение в `pollProgress` при `done`

Между собой блоки почти не конфликтуют: #1 — app.py + translate_srt.py, #2–4 —
`templates/index.html`. Но #2 и #4 оба трогают JS — их лучше делать последовательно
или в одном подагенте.
