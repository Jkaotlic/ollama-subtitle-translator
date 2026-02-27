# Ollama Subtitle Translator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg)](docker-compose.yml)

> **Fully offline subtitle translator** powered by [Ollama](https://ollama.com) + [Translating Gemma](https://ollama.com/library/translategemma).
> No API keys, no cloud — everything runs locally on your machine.
>
> Translate `.srt` subtitle files between **16+ languages** using Google's Translating Gemma models.
> Features a modern **Web UI** with drag & drop, **video subtitle extraction**, **two-pass quality translation**, and a **CLI** for automation.

---

Локальный переводчик субтитров (.srt) на базе **Ollama** + **Translating Gemma** (Google).
Работает полностью офлайн — без API-ключей и облачных сервисов.

## Возможности

- **Полностью офлайн** — после установки интернет не нужен
- **16+ языков** — русский, английский, китайский, японский, корейский, немецкий, французский, испанский, итальянский, португальский, турецкий, арабский, украинский, польский, вьетнамский, нидерландский, тайский
- **Веб-интерфейс** — тёмная тема, drag & drop, прогресс в реальном времени, управление моделями
- **CLI** — командная строка с прогресс-баром для автоматизации
- **Извлечение субтитров из видео** — MKV, MP4, AVI, MOV, WebM через ffmpeg
- **Двухпроходный перевод** — translate + review для максимального качества
- **Управление моделями** — карточки с статусами и скачивание в один клик
- **Пакетный перевод** — группировка субтитров для скорости
- **Контекст диалога** — скользящее окно соседних субтитров для связности
- **Кэш фраз** — одинаковые фразы переводятся один раз
- **Защита тегов** — HTML и ASS теги сохраняются при переводе
- **Автоопределение кодировки** — UTF-8, CP1251, Latin-1, Shift_JIS и др.
- **Docker** — готовый Docker Compose с автоскачиванием модели

---

## Быстрый старт

### 1. Установите Ollama

Скачайте с [ollama.com/download](https://ollama.com/download) и запустите:

```bash
ollama serve
ollama pull translategemma:4b
```

**Доступные модели:**

| Модель | Размер | Качество | Скорость |
| ------ | ------ | -------- | -------- |
| `translategemma:4b` | 3.3 GB | Хорошее | Быстрая |
| `translategemma:12b` | 8.1 GB | Лучше | Средняя |
| `translategemma:27b` | 17 GB | Максимальное | Медленная |

### 2. Установите зависимости

```bash
pip install -r requirements.txt
```

### 3. Запустите

**Веб-интерфейс (рекомендуется):**

```bash
python app.py
# Откроется на http://localhost:8847
```

**Командная строка:**

```bash
python translate_srt.py movie.srt                     # EN→RU (по умолчанию)
python translate_srt.py movie.srt -l Japanese          # EN→JP
python translate_srt.py movie.srt -l German -o de.srt  # EN→DE в de.srt
python translate_srt.py movie.srt -s English           # Указать исходный язык
python translate_srt.py movie.srt --two-pass           # Двухпроходный перевод
```

**Скрипты-помощники:**

```bash
./run_local.sh    # macOS — автоустановка и запуск
run_web.bat       # Windows
```

---

## Веб-интерфейс

- **Drag & drop** загрузка .srt файлов
- **Извлечение субтитров из видео** (MKV, MP4, AVI, MOV, WebM, TS) через ffmpeg
- **Селектор моделей** — карточки с статусами (готова / не установлена / скачивается), автоскачивание
- **Выбор языков** — исходный (автоопределение или вручную) и целевой
- **Контекст перевода** — подсказка для модели (например: «медицинский сериал», «sci-fi»)
- **Двухпроходный режим** — перевод + ревью для повышения качества
- **Temperature и chunk size** — тонкая настройка
- **Прогресс-бар** в реальном времени (SSE)
- **Автосохранение** в указанную папку
- **Автоустановка ffmpeg** на Windows

### Извлечение субтитров из видео

1. Переключитесь на вкладку **«Извлечь из видео»**
2. Укажите путь к файлу (поддерживаются сетевые/NAS пути)
3. Нажмите **«Сканировать»** — приложение покажет список дорожек
4. Выберите нужную дорожку и нажмите **«Перевести»**

> Требуется **ffmpeg** (`brew install ffmpeg` / `apt install ffmpeg` / автоскачивание в UI на Windows).

---

## Командная строка

```bash
python translate_srt.py <файл.srt> [опции]
```

| Флаг | Описание | По умолчанию |
| ---- | -------- | ------------ |
| `-l`, `--lang` | Целевой язык | `Russian` |
| `-s`, `--source-lang` | Исходный язык (автоопределение если не указан) | — |
| `-o`, `--out` | Выходной файл | `<input>.<lang>.srt` |
| `-m`, `--model` | Модель Ollama | `translategemma:4b` |
| `-c`, `--context` | Контекст для перевода | — |
| `-b`, `--batch` | Размер батча (прогресс) | `10` |
| `--two-pass` | Двухпроходный перевод (translate + review) | выкл. |
| `--review-model` | Модель для review-прохода | та же |
| `--chunk-size` | Макс. символов в одном запросе | `2000` |

---

## Поддерживаемые языки

| Язык | Код | | Язык | Код |
| ---- | --- | - | ---- | --- |
| Russian | ru | | Japanese | ja |
| English | en | | Korean | ko |
| Chinese | zh | | German | de |
| French | fr | | Spanish | es |
| Italian | it | | Portuguese | pt |
| Turkish | tr | | Arabic | ar |
| Ukrainian | uk | | Polish | pl |
| Vietnamese | vi | | Dutch | nl |
| Thai | th | | | |

---

## Docker

```bash
docker-compose up --build
# Откроется на http://localhost:8847
```

Для доступа к видеофайлам в Docker укажите путь к медиа-папке:

```bash
VIDEO_HOST_DIR=/path/to/your/media docker-compose up --build
```

Подробнее: [Docker guide](docs/README_DOCKER.md) | [Portainer deployment](docs/PORTAINER_DEPLOY.md)

---

## Настройки (переменные окружения)

| Переменная | Описание | По умолчанию |
| ---------- | -------- | ------------ |
| `OLLAMA_URL` | URL Ollama API | `http://127.0.0.1:11434` |
| `PORT` | Порт веб-сервера | `8847` |
| `MAX_WORKERS` | Макс. параллельных переводов | `3` |
| `UPLOAD_DIR` | Папка для временных файлов | Системная temp |
| `FILE_TTL` | Время жизни файлов (сек) | `86400` (1 день) |
| `TASK_TTL` | Время жизни задач (сек) | `86400` (1 день) |
| `CLEANUP_INTERVAL` | Интервал очистки (сек) | `600` (10 мин) |
| `SHUTDOWN_TIMEOUT` | Таймаут при завершении (сек) | `30` |
| `LOG_LEVEL` | Уровень логирования | `INFO` |

---

## Архитектура

```text
app.py               — Flask веб-сервер (UI + REST API + SSE прогресс)
translate_srt.py     — Ядро: парсер SRT, Translator, пакетный перевод
video_utils.py       — Извлечение субтитров из видео (ffmpeg/ffprobe)
templates/index.html — Веб-интерфейс (тёмная тема, drag-and-drop, карточки моделей)
tests/               — Юнит-тесты (pytest)
```

### Особенности реализации

- **Retry с exponential backoff** для всех HTTP-запросов к Ollama
- **UUID-плейсхолдеры** для защиты HTML/ASS-тегов от перевода
- **Пакетный перевод** с разделителями `|||SEP|||` и fallback на поштучный
- **Sliding window** — соседние субтитры как контекст для связности
- **Валидация переводов** — проверка пустых, слишком длинных, непереведённых сегментов с retry
- **Кэш фраз** — одинаковые фразы переводятся один раз
- **Двухпроходный перевод** — опциональный review-проход
- **Автоскачивание моделей** — UI проверяет наличие модели и скачивает с прогрессом (SSE)
- **ThreadPoolExecutor** для параллельных задач перевода
- **Автоочистка** временных файлов и задач (TTL)
- **Graceful shutdown** — ожидание текущих задач при SIGINT/SIGTERM

---

## Тестирование

```bash
pip install -r requirements-dev.txt
pytest -q
```

Тесты покрывают: парсинг SRT (UTF-8, UTF-8 BOM, CP1251, edge cases), защиту/восстановление тегов, retry-логику HTTP, mock-ответы Ollama, пакетный перевод с чанкингом, round-trip `write_srt`, инициализацию Translator, извлечение субтитров из видео (mocked ffprobe/ffmpeg).

---

## Советы

- **Контекст** значительно улучшает качество — укажите жанр, тематику или имена персонажей
- **Temperature 0** — для точного технического перевода, **0.3–0.5** — для художественного
- **Chunk size** ≤ 2000 символов — оптимально для большинства моделей
- **Двухпроходный перевод** рекомендуется для важных проектов
- **Исходный язык** — укажите явно для более точного перевода
- **8 ГБ+ RAM** рекомендуется для Translating Gemma 4B

---

## Лицензия

[MIT](LICENSE)
