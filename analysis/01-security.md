# Анализ безопасности Ollama Subtitle Translator

**Дата**: 2026-04-18
**Скоуп**: Flask (app.py) + CLI (translate_srt.py) + video_utils.py + templates + Docker

## Критические

### SEC-01: Path Traversal через save_dir
- **Файл**: [app.py:214-223](../app.py#L214-L223), [app.py:494-499](../app.py#L494-L499)
- **Проблема**: `Path(save_dir) / output_name` без валидации — можно писать куда угодно (`"../../etc/"`, `"C:/Windows/"`).
- **Риск**: Высокий
- **Эксплойт**: `POST /translate` с `save_dir: "../../../../home/user/.ssh"` — запись перезапишет `authorized_keys` (с именем `output.ru.srt`, но атакующий может всё равно захламить FS).
- **Решение**: `Path(save_dir).resolve().is_relative_to(allowed_base)` или запрет `..`/абсолютных путей.
- **Статус**: [ ] Не исправлено

### SEC-02: Path Traversal в `resolve_video_path` (Docker)
- **Файл**: [video_utils.py:131-136](../video_utils.py#L131-L136)
- **Проблема**: `Path(VIDEO_DIR) / user_path.lstrip("/")` — `lstrip` не защищает от `../../etc/passwd`.
- **Риск**: Высокий (в Docker)
- **Решение**: `resolve()` + `relative_to(VIDEO_DIR)`, кинуть `ValueError` при выходе.
- **Статус**: [ ] Не исправлено

### SEC-03: `video_stem` может содержать `/` `\`
- **Файл**: [app.py:489-491](../app.py#L489-L491)
- **Проблема**: `original_name` из клиента идёт в `Path(...).stem`, но stem умеет содержать спец. символы на Windows.
- **Риск**: Средний
- **Решение**: `re.sub(r'[/\\:*?"<>|]', '_', video_stem)`
- **Статус**: [ ] Не исправлено

### SEC-04: 50 GB MAX_CONTENT_LENGTH для всех endpoints
- **Файл**: [app.py:30](../app.py#L30)
- **Проблема**: Лимит применяется даже к `.srt`, делая простую DoS-атаку через 50-GB subtitle-файл.
- **Риск**: Средний
- **Решение**: Per-endpoint limit (100 MB для SRT, 50 GB только для /upload_video).
- **Статус**: [ ] Не исправлено

### SEC-05: SSRF через `OLLAMA_URL`
- **Файлы**: [app.py:55](../app.py#L55), [app.py:325](../app.py#L325), [app.py:344](../app.py#L344)
- **Проблема**: env переменная передаётся в requests без валидации. Если app развёрнут на машине с доступом во внутреннюю сеть — сканирование.
- **Риск**: Средний (low для полностью локального, high для shared deployments)
- **Решение**: whitelist hostnames (`127.0.0.1`, `localhost`, `ollama`), логирование других.
- **Статус**: [ ] Не исправлено

## Средние

### SEC-06: Неотвалидирован `sub_index`
- **Файл**: [app.py:450](../app.py#L450), [app.py:483](../app.py#L483)
- **Проблема**: `int(sub_index)` — отрицательные/огромные значения идут в ffmpeg.
- **Решение**: проверить `0 <= sub_index <= 100`.
- **Статус**: [ ] Не исправлено

### SEC-07: TM SQLite — нет лимита размера
- **Файл**: [translate_srt.py:155-225](../translate_srt.py#L155-L225) (класс TranslationMemory)
- **Проблема**: SQL-запросы параметризованы (SQLi нет), но БД может расти бесконечно — disk DoS.
- **Решение**: Retention-политика (`DELETE WHERE hits < 5 AND created_at < now - 30d`), лимит ~100K записей.
- **Статус**: [ ] Не исправлено

### SEC-08: Отсутствие security-headers
- **Файл**: [app.py](../app.py)
- **Проблема**: нет `X-Content-Type-Options`, `X-Frame-Options`, `CSP`.
- **Риск**: Низкий (локально), но best practice.
- **Решение**: `@app.after_request` добавить три заголовка.
- **Статус**: [ ] Не исправлено

### SEC-09: Dockerfile запускает от root
- **Файл**: [Dockerfile](../Dockerfile)
- **Проблема**: нет `USER` директивы — RCE даёт root.
- **Решение**: `RUN useradd -u 1000 appuser && USER appuser`
- **Статус**: [ ] Не исправлено

## Низкие

### SEC-10: Имена файлов в логах
- **Файл**: [app.py:273](../app.py#L273)
- **Проблема**: `file=secret_subtitles.srt` попадает в логи.
- **Решение**: логировать только `ext=.srt`, не имя.
- **Статус**: [ ] Не исправлено

### SEC-11: Проверка только расширения у видео
- **Файл**: [app.py:391-394](../app.py#L391-L394)
- **Решение**: magic-bytes через `python-magic` (optional).
- **Статус**: [ ] Не исправлено

### SEC-12: Утечка error message клиенту
- **Файл**: [app.py:485](../app.py#L485)
- **Проблема**: `f"Subtitle extraction failed: {e}"` — полный stack в JSON-ответе.
- **Решение**: вернуть generic сообщение, детали в логах.
- **Статус**: [ ] Не исправлено

### SEC-13: Нет rate-limit
- **Файл**: [app.py](../app.py)
- **Решение**: `flask-limiter` если выставляется на 0.0.0.0.
- **Статус**: [ ] Не исправлено

### SEC-14: `ollama/ollama:latest` в compose
- **Файл**: [docker-compose.yml](../docker-compose.yml)
- **Решение**: запинить версию.
- **Статус**: [ ] Не исправлено

## Итого

| Уровень | Кол-во | ID |
|---|---|---|
| Критические | 5 | SEC-01..05 |
| Средние | 4 | SEC-06..09 |
| Низкие | 5 | SEC-10..14 |

Приоритет: **SEC-01, SEC-02** — фиксить сразу. Остальные — после.
