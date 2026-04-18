# Анализ зависимостей Ollama Subtitle Translator

**Дата**: 2026-04-18

## Критические

### DEP-01: Flask `>=2.0` слишком старая минимальная
- **Файл**: [requirements.txt](../requirements.txt)
- **Текущая ветка**: Flask 3.1.3 (февраль 2026)
- **CVE**: 3.1.2 и ниже — CVE-2026-27205 (кеш чувств. данных)
- **Решение**: `flask>=3.1.3`.
- **Статус**: [ ] Не исправлено

### DEP-02: Requests `>=2.25` — 5+ лет назад
- **Файл**: [requirements.txt](../requirements.txt)
- **Актуальная**: 2.33.1
- **CVE-2026-25645**: уязвимость в `extract_zipped_paths()`
- **Решение**: `requests>=2.33.1`.
- **Статус**: [ ] Не исправлено

### DEP-03: chardet 6.0 несовместим с новыми requests
- **Файлы**: [requirements.txt](../requirements.txt) (`chardet>=5.0` берёт 6.x), локально установлена 6.0.0.post1
- **Проблема**: requests 2.33+ поддерживает только `chardet<6` → `RequestsDependencyWarning` при каждом импорте.
- **Решение**: либо `chardet<6`, либо **удалить из обязательных** — в коде он optional (try/except ImportError), и перенести в extras или убрать вовсе, используя встроенный `charset_normalizer`.
- **Статус**: [ ] Не исправлено

## Средние

### DEP-04: Python 3.8 EOL с октября 2024
- **Файлы**: [CLAUDE.md](../CLAUDE.md) "Python 3.8+"
- **Решение**: обновить до `Python >= 3.9` или 3.10; Dockerfile уже использует 3.11.
- **Статус**: [ ] Не исправлено

### DEP-05: Нет version lock / pyproject.toml
- **Проблема**: все `>=` — непредсказуемая установка.
- **Решение**: минимально `pip freeze > requirements.lock`; идеально `pyproject.toml` с `[project.dependencies]`.
- **Статус**: [ ] Не исправлено

### DEP-06: `docker-compose.yml` использует `ollama/ollama:latest`
- **Файл**: [docker-compose.yml](../docker-compose.yml)
- **Решение**: запинить версию (например `0.5.2`).
- **Статус**: [ ] Не исправлено

## Низкие

### DEP-07: `requests-mock>=1.9` не используется тестами
- **Файл**: [requirements-dev.txt](../requirements-dev.txt)
- **Проверка**: grep показал — тесты используют `unittest.mock`, не `requests_mock`.
- **Решение**: удалить.
- **Статус**: [ ] Не исправлено

### DEP-08: ffmpeg бинарники в репо (ffmpeg_bin/)
- **Проблема**: 400+ МБ в git? Есть `.gitignore`, но проверить.
- **Решение**: `git ls-files ffmpeg_bin/` — если трекается, `git rm --cached ffmpeg_bin/*`.
- **Статус**: [ ] Проверить

## Итого

| Уровень | Кол-во | ID |
|---|---|---|
| Критические | 3 | DEP-01..03 |
| Средние | 3 | DEP-04..06 |
| Низкие | 2 | DEP-07..08 |
