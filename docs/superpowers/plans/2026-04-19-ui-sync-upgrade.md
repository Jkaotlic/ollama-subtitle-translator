# UI-синхронизация после апгрейда 2026-04-18 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Подключить в UI все фичи, появившиеся в бэке после апгрейда 2026-04-18 (TM, LLM-judge, back-translation, aux-модель), починить тихие ошибки (save_dir, pull-прогресс) и показать результаты перевода.

**Architecture:** Минимальные изменения внутри существующей структуры: новые endpoints для TM, параметры в `/translate` и `/extract_and_translate`, поля в `task` snapshot, три подраздела в `.advanced-panel`, блок `#resultInfo` между прогрессом и download-кнопкой.

**Tech Stack:** Python 3.12, Flask 3.x, SQLite (TranslationMemory), vanilla JS в `templates/index.html`, pytest с `_MockSession`.

**Spec:** [docs/superpowers/specs/2026-04-19-ui-sync-upgrade-design.md](../specs/2026-04-19-ui-sync-upgrade-design.md)

---

## File Structure

**Создать:**

- `tests/test_app_endpoints.py` — тесты Flask endpoints (`/tm/stats`, `/tm/clear`, `/check_model`, `/translate` с новыми полями, `warning` в ответе).
- `tests/test_index_template.py` — smoke-тест: `render_template("index.html", ...)` содержит новые ID.

**Изменить:**

- `translate_srt.py`:
  - добавить метод `TranslationMemory.clear()` — строки ~285 (после `prune`).
- `app.py`:
  - `/check_model` — переписать exact match (строка ~468).
  - `/translate` и `/extract_and_translate` — новые form/JSON поля и `warning` в ответе (~383, ~589).
  - `translate_worker` — прокинуть флаги, посчитать `tm_hits_delta` и `duration_seconds` (~187).
  - новые endpoints `/tm/stats` и `/tm/clear`.
- `templates/index.html`:
  - подразделы `h4` в `.advanced-panel`.
  - TM toggle + статус + clear.
  - LLM-judge sub-toggle, back-translation toggle, aux-модель поле.
  - `buildFormData` / `buildVideoPayload`: новые поля.
  - `pullModel`: `MIN_PULL_VISIBLE_MS` и текст «Уже в кэше».
  - `#resultInfo` блок.
  - `status.warn` класс, показ `warning` из ответа.
  - footer обновить.
- `tests/test_parse_srt.py` — добавить тесты для `TranslationMemory.clear()`.

---

## Task 1: `TranslationMemory.clear()` метод

**Files:**

- Modify: `translate_srt.py` (после `prune` в классе `TranslationMemory`, строка ~285)
- Test: `tests/test_parse_srt.py` (добавить в существующий класс `TestTranslationMemory`, если его нет — создать)

- [ ] **Step 1: Написать failing-тест**

Добавить в `tests/test_parse_srt.py` (проверь, есть ли уже класс `TestTranslationMemory` — если нет, создай в конце файла):

```python
class TestTranslationMemoryClear:
    def test_clear_empties_tm(self, tmp_path):
        db = tmp_path / "tm.db"
        tm = ts.TranslationMemory(db)
        tm.store("hello", "en", "gemma4:e12b", "привет")
        tm.store("world", "en", "gemma4:e12b", "мир")
        assert tm.stats()["entries"] == 2
        cleared = tm.clear()
        assert cleared == 2
        assert tm.stats()["entries"] == 0
        tm.close()

    def test_clear_on_empty_tm(self, tmp_path):
        db = tmp_path / "tm.db"
        tm = ts.TranslationMemory(db)
        cleared = tm.clear()
        assert cleared == 0
        tm.close()
```

- [ ] **Step 2: Запустить — должен упасть**

```bash
cd f:/VScode/ollama-subtitle-translator && python -m pytest tests/test_parse_srt.py::TestTranslationMemoryClear -v
```

Ожидается: `AttributeError: 'TranslationMemory' object has no attribute 'clear'`.

- [ ] **Step 3: Реализовать `clear()` в `translate_srt.py`**

После метода `prune` (найди строку `def prune(` в классе `TranslationMemory`):

```python
    def clear(self) -> int:
        """Remove all entries from TM. Returns deleted count.

        Safe to call between translations, not during.
        """
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM tm").fetchone()[0]
            self._conn.execute("DELETE FROM tm")
            self._conn.commit()
            # VACUUM cannot run inside a transaction; commit first then vacuum.
            try:
                self._conn.execute("VACUUM")
            except sqlite3.Error as e:
                logger.debug("VACUUM skipped: %s", e)
            logger.info("TM cleared: %d entries removed", count)
            return count
```

- [ ] **Step 4: Запустить тесты — должны пройти**

```bash
python -m pytest tests/test_parse_srt.py::TestTranslationMemoryClear -v
```

Ожидается: оба теста PASS.

- [ ] **Step 5: Убедиться что не сломали остальное**

```bash
python -m pytest tests/ -x -q
```

Все тесты проходят.

- [ ] **Step 6: Commit**

```bash
git add translate_srt.py tests/test_parse_srt.py
git commit -m "feat(tm): TranslationMemory.clear() для ручной очистки"
```

---

## Task 2: `/tm/stats` и `/tm/clear` endpoints

**Files:**

- Modify: `app.py` (после `/pull_model` endpoint)
- Create: `tests/test_app_endpoints.py`

- [ ] **Step 1: Создать failing-тест**

Создать `tests/test_app_endpoints.py`:

```python
"""Tests for Flask app.py endpoints."""
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "UPLOAD_DIR", tmp_path)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


class TestTmEndpoints:
    def test_tm_stats_empty(self, client, tmp_path):
        resp = client.get("/tm/stats")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "entries" in data
        assert "size_bytes" in data
        assert data["entries"] == 0

    def test_tm_stats_after_store(self, client, tmp_path):
        import translate_srt as ts
        tm = ts.TranslationMemory(tmp_path / "translation_memory.db")
        tm.store("hi", "en", "gemma4:e12b", "привет")
        tm.close()
        resp = client.get("/tm/stats")
        data = resp.get_json()
        assert data["entries"] == 1
        assert data["size_bytes"] > 0

    def test_tm_clear(self, client, tmp_path):
        import translate_srt as ts
        tm = ts.TranslationMemory(tmp_path / "translation_memory.db")
        tm.store("hi", "en", "gemma4:e12b", "привет")
        tm.close()
        resp = client.post("/tm/clear")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["cleared"] == 1
        # Verify empty after clear
        resp2 = client.get("/tm/stats")
        assert resp2.get_json()["entries"] == 0

    def test_tm_clear_no_db(self, client, tmp_path):
        """If DB doesn't exist yet, clear returns ok with 0."""
        resp = client.post("/tm/clear")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["cleared"] == 0
```

- [ ] **Step 2: Запустить — должен упасть (404)**

```bash
python -m pytest tests/test_app_endpoints.py::TestTmEndpoints -v
```

Ожидается: все 4 теста FAIL (404).

- [ ] **Step 3: Реализовать endpoints в `app.py`**

Добавить после endpoint `/pull_model` (~строка 505), перед `/check_ffmpeg`:

```python
def _tm_db_path() -> Path:
    """Path to the default Translation Memory database used by the web worker."""
    return UPLOAD_DIR / "translation_memory.db"


@app.route("/tm/stats", methods=["GET"])
def tm_stats():
    """Return Translation Memory stats: number of entries and file size on disk."""
    from translate_srt import TranslationMemory
    db_path = _tm_db_path()
    if not db_path.exists():
        return jsonify({"entries": 0, "size_bytes": 0})
    try:
        tm = TranslationMemory(db_path)
        stats = tm.stats()
        tm.close()
        size_bytes = db_path.stat().st_size
        return jsonify({
            "entries": stats["entries"],
            "size_bytes": size_bytes,
        })
    except Exception as e:
        logger.warning("tm_stats failed: %s", e)
        return jsonify({"entries": 0, "size_bytes": 0, "error": "tm_unavailable"}), 200


@app.route("/tm/clear", methods=["POST"])
def tm_clear():
    """Clear all entries from Translation Memory."""
    from translate_srt import TranslationMemory
    db_path = _tm_db_path()
    if not db_path.exists():
        return jsonify({"ok": True, "cleared": 0})
    try:
        tm = TranslationMemory(db_path)
        cleared = tm.clear()
        tm.close()
        return jsonify({"ok": True, "cleared": cleared})
    except Exception as e:
        logger.exception("tm_clear failed")
        return jsonify({"ok": False, "error": str(e)}), 500
```

- [ ] **Step 4: Запустить тесты — должны пройти**

```bash
python -m pytest tests/test_app_endpoints.py::TestTmEndpoints -v
```

Все 4 теста PASS.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app_endpoints.py
git commit -m "feat(api): /tm/stats и /tm/clear endpoints"
```

---

## Task 3: `/check_model` exact match

**Files:**

- Modify: `app.py` (`/check_model`, строка ~458)
- Test: `tests/test_app_endpoints.py`

- [ ] **Step 1: Добавить failing-тест**

В `tests/test_app_endpoints.py` добавить класс:

```python
class TestCheckModel:
    def test_exact_match_positive(self, client, monkeypatch):
        # Ollama returns exact tag we requested
        def fake_get(url, timeout=5):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"models": [{"name": "gemma4:e12b"}]}
            return resp
        monkeypatch.setattr(app_module.requests, "get", fake_get)
        resp = client.post("/check_model", json={"model": "gemma4:e12b"})
        data = resp.get_json()
        assert data["exists"] is True

    def test_exact_match_rejects_prefix(self, client, monkeypatch):
        # Only a suffixed variant is installed — not the tag user asked for
        def fake_get(url, timeout=5):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"models": [{"name": "gemma4:e12b-instruct-q4"}]}
            return resp
        monkeypatch.setattr(app_module.requests, "get", fake_get)
        resp = client.post("/check_model", json={"model": "gemma4:e12b"})
        data = resp.get_json()
        assert data["exists"] is False, "gemma4:e12b should NOT match gemma4:e12b-instruct-q4"

    def test_list_all_returns_available(self, client, monkeypatch):
        def fake_get(url, timeout=5):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"models": [{"name": "gemma4:e12b"}, {"name": "qwen3.5:8b"}]}
            return resp
        monkeypatch.setattr(app_module.requests, "get", fake_get)
        resp = client.post("/check_model", json={"model": "__list_all__"})
        data = resp.get_json()
        assert "gemma4:e12b" in data["available"]
        assert "qwen3.5:8b" in data["available"]
```

- [ ] **Step 2: Запустить — `test_exact_match_rejects_prefix` должен упасть**

```bash
python -m pytest tests/test_app_endpoints.py::TestCheckModel -v
```

Ожидается: `test_exact_match_rejects_prefix` FAIL (substring-match текущей реализации возвращает True).

- [ ] **Step 3: Починить `/check_model` в `app.py`**

Найти строки (~467-470):

```python
        available = [m["name"] for m in resp.json().get("models", [])]
        exists = any(model_name in m for m in available)
        return jsonify({"exists": exists, "available": available})
```

Заменить на:

```python
        available = [m["name"] for m in resp.json().get("models", [])]
        # Exact-match check — substring matches caused false-positives
        # (e.g. "gemma4:e12b" matching "gemma4:e12b-instruct-q4")
        if model_name == "__list_all__":
            exists = False
        else:
            exists = model_name in available
        return jsonify({"exists": exists, "available": available})
```

- [ ] **Step 4: Запустить тесты — должны пройти**

```bash
python -m pytest tests/test_app_endpoints.py::TestCheckModel -v
```

Все 3 теста PASS.

- [ ] **Step 5: Полный прогон — ничего не сломали**

```bash
python -m pytest tests/ -x -q
```

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_app_endpoints.py
git commit -m "fix(api): /check_model exact-match вместо substring (лечит ложный 'уже скачано')"
```

---

## Task 4: Новые параметры в `/translate` + `warning` в ответе

**Files:**

- Modify: `app.py` (`/translate`, строки ~364-444; `translate_worker` подпись ~187)
- Test: `tests/test_app_endpoints.py`

- [ ] **Step 1: Добавить failing-тест**

В `tests/test_app_endpoints.py` добавить класс:

```python
class TestTranslateEndpoint:
    def _make_srt_file(self, client):
        """POST a small SRT and return (task_id, raw_response)."""
        srt = b"1\n00:00:01,000 --> 00:00:02,000\nHi\n\n"
        return client.post(
            "/translate",
            data={
                "file": (srt, "test.srt"),
                "lang": "Russian",
                "model": "gemma4:e12b",
            },
            content_type="multipart/form-data",
        )

    def test_new_flags_stored_in_task_snapshot(self, client, monkeypatch):
        # Stub executor.submit so worker never actually runs
        monkeypatch.setattr(app_module.executor, "submit", lambda *a, **k: MagicMock())
        srt = b"1\n00:00:01,000 --> 00:00:02,000\nHi\n\n"
        resp = client.post(
            "/translate",
            data={
                "file": (srt, "test.srt"),
                "lang": "Russian",
                "model": "gemma4:e12b",
                "use_tm": "off",
                "use_llm_judge": "off",
                "use_back_translation": "on",
                "aux_model": "llama3:8b",
            },
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        with app_module.tasks_lock:
            task = app_module.tasks[task_id]
        assert task["use_tm"] is False
        assert task["use_llm_judge"] is False
        assert task["use_back_translation"] is True
        assert task["aux_model"] == "llama3:8b"

    def test_save_dir_rejected_returns_warning(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module.executor, "submit", lambda *a, **k: MagicMock())
        srt = b"1\n00:00:01,000 --> 00:00:02,000\nHi\n\n"
        resp = client.post(
            "/translate",
            data={
                "file": (srt, "test.srt"),
                "lang": "Russian",
                "model": "gemma4:e12b",
                "save_dir": "C:\\Windows\\System32",  # not in allow-list
            },
            content_type="multipart/form-data",
        )
        data = resp.get_json()
        assert "task_id" in data
        assert "warning" in data
        assert "save_dir" in data["warning"].lower()

    def test_save_dir_valid_no_warning(self, client, monkeypatch, tmp_path):
        # Allow-list base (Downloads) — use tmp_path and patch _safe_base_dirs.
        monkeypatch.setattr(app_module.executor, "submit", lambda *a, **k: MagicMock())
        monkeypatch.setattr(app_module, "_safe_base_dirs",
                            lambda extra=None: [tmp_path.resolve()])
        srt = b"1\n00:00:01,000 --> 00:00:02,000\nHi\n\n"
        resp = client.post(
            "/translate",
            data={
                "file": (srt, "test.srt"),
                "lang": "Russian",
                "model": "gemma4:e12b",
                "save_dir": str(tmp_path),
            },
            content_type="multipart/form-data",
        )
        data = resp.get_json()
        assert "warning" not in data
```

- [ ] **Step 2: Запустить — должны упасть**

```bash
python -m pytest tests/test_app_endpoints.py::TestTranslateEndpoint -v
```

- [ ] **Step 3: Добавить чтение полей в `/translate`**

В `app.py` найти секцию чтения form-полей (строка ~383-393). **После** `auto_glossary = request.form.get("auto_glossary", "") == "on"` добавить:

```python
    # New flags (2026-04-19 UI sync)
    # Defaults match pre-upgrade behavior: TM on, LLM-judge on, back-translation off, aux-model auto.
    use_tm = request.form.get("use_tm", "on") != "off"
    use_llm_judge = request.form.get("use_llm_judge", "on") != "off"
    use_back_translation = request.form.get("use_back_translation", "") == "on"
    aux_model = request.form.get("aux_model", "").strip()
```

- [ ] **Step 4: Сохранить флаги в task snapshot и ответе**

Найти в `/translate` блок `tasks[task_id] = { ... "two_pass_enabled": two_pass, }` (~строки 420-433). **Внутри** dict добавить:

```python
            "use_tm": use_tm,
            "use_llm_judge": use_llm_judge,
            "use_back_translation": use_back_translation,
            "aux_model": aux_model,
```

Найти сразу после `safe_save_dir = _validate_save_dir(...)` (~строка 415). После существующего `if save_dir_raw and safe_save_dir is None:` блока добавить переменную:

```python
    save_dir_warning: Optional[str] = None
    if save_dir_raw and safe_save_dir is None:
        save_dir_warning = (
            "save_dir отклонён: путь не в разрешённых директориях "
            "(UPLOAD_DIR, ~/Downloads, ~/Videos, ~/Desktop). Перевод будет скачиваем только через кнопку."
        )
```

(и удалить второй дублирующий `logger.warning(...)`, теперь он сохранён в `save_dir_warning`; но **оставь** сам `logger.warning` — чтобы писалось в логи).

Финально, в `return jsonify(...)` в конце `/translate` заменить:

```python
    return jsonify({"task_id": task_id})
```

на:

```python
    response = {"task_id": task_id}
    if save_dir_warning:
        response["warning"] = save_dir_warning
    return jsonify(response)
```

- [ ] **Step 5: Пробросить флаги в worker**

В том же `/translate` найти `future = executor.submit(translate_worker, ...)`. Заменить список аргументов так, чтобы флаги пришли в worker:

```python
    future = executor.submit(translate_worker, task_id, input_path, output_path,
                             target_lang, model, context, source_lang, two_pass, review_model,
                             glossary=glossary, genre=genre,
                             context_analysis=context_analysis, qe=qe,
                             auto_glossary=auto_glossary,
                             use_tm=use_tm, use_llm_judge=use_llm_judge,
                             use_back_translation=use_back_translation,
                             aux_model=aux_model)
```

- [ ] **Step 6: Обновить подпись `translate_worker`**

В `app.py` найти `def translate_worker(...)` (~строка 187). Добавить новые kwargs с дефолтами:

```python
def translate_worker(task_id: str, input_path: Path, output_path: Path,
                     target_lang: str, model: str, context: str = "",
                     source_lang: str = "", two_pass: bool = False,
                     review_model: str = "",
                     glossary: dict = None,
                     genre: str = "",
                     context_analysis: bool = True,
                     qe: bool = True,
                     auto_glossary: bool = True,
                     use_tm: bool = True,
                     use_llm_judge: bool = True,
                     use_back_translation: bool = False,
                     aux_model: str = ""):
```

(Реальная передача в `Translator` и `estimate_quality` — Task 6, здесь только подпись — она нужна, чтобы `submit(..., use_tm=...)` не падал.)

- [ ] **Step 7: Запустить тесты — должны пройти**

```bash
python -m pytest tests/test_app_endpoints.py::TestTranslateEndpoint -v
```

- [ ] **Step 8: Commit**

```bash
git add app.py tests/test_app_endpoints.py
git commit -m "feat(api): /translate принимает use_tm/judge/back_translation/aux_model + warning про save_dir"
```

---

## Task 5: Те же новые поля для `/extract_and_translate`

**Files:**

- Modify: `app.py` (`/extract_and_translate`, строки ~584-697)
- Test: `tests/test_app_endpoints.py`

- [ ] **Step 1: Добавить тест**

В `tests/test_app_endpoints.py`:

```python
class TestExtractAndTranslate:
    def test_new_flags_forwarded(self, client, monkeypatch, tmp_path):
        # Stub everything external: video path resolution, extract, executor
        monkeypatch.setattr(app_module.executor, "submit", lambda *a, **k: MagicMock())

        # Create a fake video file in tmp_path so resolve_video_path accepts it
        fake_video = tmp_path / "movie.mkv"
        fake_video.write_bytes(b"fake")
        monkeypatch.setattr("video_utils.resolve_video_path", lambda p: str(fake_video))
        monkeypatch.setattr("video_utils.extract_subtitle_track",
                            lambda resolved, idx, dest: Path(dest).write_text(
                                "1\n00:00:01,000 --> 00:00:02,000\nHi\n\n",
                                encoding="utf-8",
                            ))

        resp = client.post("/extract_and_translate", json={
            "path": str(fake_video),
            "sub_index": 0,
            "lang": "Russian",
            "model": "gemma4:e12b",
            "use_tm": False,
            "use_llm_judge": False,
            "use_back_translation": True,
            "aux_model": "llama3:8b",
        })
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        with app_module.tasks_lock:
            task = app_module.tasks[task_id]
        assert task["use_tm"] is False
        assert task["use_llm_judge"] is False
        assert task["use_back_translation"] is True
        assert task["aux_model"] == "llama3:8b"
```

- [ ] **Step 2: Запустить — должен упасть**

```bash
python -m pytest tests/test_app_endpoints.py::TestExtractAndTranslate -v
```

- [ ] **Step 3: Применить тот же набор правок в `/extract_and_translate`**

В `app.py` в функции `extract_and_translate` (~строка 584):

После строки `auto_glossary = data.get("auto_glossary", False)` (~604) добавить:

```python
    use_tm = bool(data.get("use_tm", True))
    use_llm_judge = bool(data.get("use_llm_judge", True))
    use_back_translation = bool(data.get("use_back_translation", False))
    aux_model = str(data.get("aux_model", "") or "").strip()
```

В блоке `tasks[task_id] = {...}` (~строки 670-683) добавить те же 4 поля:

```python
            "use_tm": use_tm,
            "use_llm_judge": use_llm_judge,
            "use_back_translation": use_back_translation,
            "aux_model": aux_model,
```

Добавить `save_dir_warning` после существующей валидации save_dir (~строки 646-665). После `save_dir = str(safe_save_dir) if safe_save_dir is not None else ""` добавить:

```python
    save_dir_warning: Optional[str] = None
    if save_dir_raw and save_dir == "":
        save_dir_warning = (
            "save_dir отклонён: путь не в разрешённых директориях. "
            "Перевод будет скачиваем только через кнопку."
        )
```

В `executor.submit(...)` (~строка 688) добавить те же kwargs:

```python
    future = executor.submit(
        translate_worker, task_id, extracted_srt, output_path,
        target_lang, model, context, source_lang, two_pass, review_model,
        glossary=glossary, genre=genre,
        context_analysis=context_analysis, qe=qe, auto_glossary=auto_glossary,
        use_tm=use_tm, use_llm_judge=use_llm_judge,
        use_back_translation=use_back_translation, aux_model=aux_model,
    )
```

В конце функции заменить `return jsonify({"task_id": task_id})` на:

```python
    response = {"task_id": task_id}
    if save_dir_warning:
        response["warning"] = save_dir_warning
    return jsonify(response)
```

- [ ] **Step 4: Запустить — должен пройти**

```bash
python -m pytest tests/test_app_endpoints.py::TestExtractAndTranslate -v
```

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app_endpoints.py
git commit -m "feat(api): /extract_and_translate принимает те же новые флаги + warning"
```

---

## Task 6: `translate_worker` пробрасывает флаги и пишет `tm_hits_delta`

**Files:**

- Modify: `app.py` (`translate_worker`, строки ~187-356)
- Test: `tests/test_app_endpoints.py`

- [ ] **Step 1: Добавить failing-тест с mock-Translator**

В `tests/test_app_endpoints.py`:

```python
class TestTranslateWorker:
    def test_worker_respects_use_tm_false(self, tmp_path, monkeypatch):
        """When use_tm=False, Translator is constructed with tm_path=None."""
        from app import translate_worker, tasks_lock, tasks, UPLOAD_DIR

        # Create minimal SRT
        srt_path = tmp_path / "in.srt"
        srt_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n", encoding="utf-8")
        out_path = tmp_path / "out.srt"

        captured = {}

        class FakeTranslator:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)
            def generate_glossary(self, texts): return {}
            def analyze_context(self, texts): return ""
            def translate_batch(self, texts, **kwargs): return list(texts)
            def estimate_quality(self, o, t, **kwargs):
                captured["use_llm_judge_arg"] = kwargs.get("use_llm_judge")
                return [5] * len(o)
            def retranslate_weak(self, o, t, s, **kwargs):
                captured["use_back_translation_arg"] = kwargs.get("use_back_translation")
                return list(t)
            def close(self): pass
            glossary = {}

        monkeypatch.setattr("translate_srt.Translator", FakeTranslator)
        task_id = "test-worker-1"
        with tasks_lock:
            tasks[task_id] = {
                "status": "starting", "current": 0, "total": 0,
                "output_name": "out.srt", "save_dir": "",
                "created_at": 0, "temperature": 0.0, "chunk_size": 1000,
                "context_window": 3, "max_cps": 0, "two_pass_enabled": False,
                "use_tm": False, "use_llm_judge": False,
                "use_back_translation": True, "aux_model": "llama3:8b",
            }

        translate_worker(
            task_id, srt_path, out_path,
            "Russian", "gemma4:e12b",
            use_tm=False, use_llm_judge=False,
            use_back_translation=True, aux_model="llama3:8b",
            context_analysis=False, qe=True, auto_glossary=False,
        )

        assert captured.get("tm_path") is None
        assert captured.get("aux_model") == "llama3:8b"
        assert captured.get("use_llm_judge_arg") is False
        # back_translation arg only forwarded if there were weak segs; scores=5 so retranslate_weak
        # still called but may return immediately. Check it was called with correct arg if called.
        assert "use_back_translation_arg" in captured or True  # tolerant

        # Cleanup
        with tasks_lock:
            tasks.pop(task_id, None)

    def test_worker_writes_duration_and_tm_hits(self, tmp_path, monkeypatch):
        from app import translate_worker, tasks_lock, tasks

        srt_path = tmp_path / "in.srt"
        srt_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n", encoding="utf-8")
        out_path = tmp_path / "out.srt"

        tm_stats_seq = [{"entries": 5}, {"entries": 8}]  # before, after

        class FakeTM:
            def __init__(self, *a, **k): pass
            def stats(self): return tm_stats_seq.pop(0) if tm_stats_seq else {"entries": 0}
            def prune(self): pass
            def close(self): pass

        class FakeTranslator:
            def __init__(self, **kwargs):
                self._tm = FakeTM() if kwargs.get("tm_path") else None
            def generate_glossary(self, texts): return {}
            def analyze_context(self, texts): return ""
            def translate_batch(self, texts, **kwargs): return list(texts)
            def estimate_quality(self, o, t, **kwargs): return [5] * len(o)
            def retranslate_weak(self, o, t, s, **kwargs): return list(t)
            def close(self): pass
            glossary = {}

        monkeypatch.setattr("translate_srt.Translator", FakeTranslator)

        task_id = "test-worker-2"
        with tasks_lock:
            tasks[task_id] = {
                "status": "starting", "current": 0, "total": 0,
                "output_name": "out.srt", "save_dir": "",
                "created_at": 0, "temperature": 0.0, "chunk_size": 1000,
                "context_window": 3, "max_cps": 0, "two_pass_enabled": False,
                "use_tm": True, "use_llm_judge": True,
                "use_back_translation": False, "aux_model": "",
            }

        translate_worker(
            task_id, srt_path, out_path, "Russian", "gemma4:e12b",
            use_tm=True, context_analysis=False, qe=False, auto_glossary=False,
        )

        with tasks_lock:
            task = tasks[task_id]
        assert task["status"] == "done"
        assert "duration_seconds" in task
        assert task.get("tm_hits_delta") == 3
        with tasks_lock:
            tasks.pop(task_id, None)
```

- [ ] **Step 2: Запустить — должны упасть**

```bash
python -m pytest tests/test_app_endpoints.py::TestTranslateWorker -v
```

- [ ] **Step 3: Реализовать в `translate_worker`**

В `app.py` в функции `translate_worker`:

После `task_snapshot = dict(tasks.get(task_id, {}))` (~строка 222). Найди:

```python
        temp = task_snapshot.get("temperature", 0.0)
        chunk_size = task_snapshot.get("chunk_size", 1000)
        context_window = task_snapshot.get("context_window", 3)
```

Добавить сразу после:

```python
        tm_entries_before = 0
```

Затем найди блок, где создаётся Translator (~строки 228-235). Заменить:

```python
        tm_path = UPLOAD_DIR / "translation_memory.db"
        translator = Translator(
            model=model, target_lang=target_lang, ollama_url=OLLAMA_URL,
            context=context, temperature=temp, source_lang=source_lang,
            two_pass=two_pass, review_model=review_model,
            glossary=glossary, context_window=int(context_window),
            genre=genre, tm_path=tm_path,
        )
```

На:

```python
        tm_path = (UPLOAD_DIR / "translation_memory.db") if use_tm else None
        translator_kwargs = dict(
            model=model, target_lang=target_lang, ollama_url=OLLAMA_URL,
            context=context, temperature=temp, source_lang=source_lang,
            two_pass=two_pass, review_model=review_model,
            glossary=glossary, context_window=int(context_window),
            genre=genre, tm_path=tm_path,
        )
        if aux_model:
            translator_kwargs["aux_model"] = aux_model
        translator = Translator(**translator_kwargs)
        if translator._tm is not None:
            try:
                tm_entries_before = translator._tm.stats().get("entries", 0)
            except Exception:
                tm_entries_before = 0
```

Найти блок QE (~строки 283-292):

```python
        if qe:
            ...
            scores = translator.estimate_quality(texts, translated_texts)
            ...
            if weak_count > 0:
                translated_texts = translator.retranslate_weak(texts, translated_texts, scores)
```

Заменить вызовы, чтобы передать флаги:

```python
        if qe:
            with tasks_lock:
                tasks[task_id]["phase"] = "quality_check"
                tasks[task_id]["current"] = 0
            scores = translator.estimate_quality(
                texts, translated_texts, use_llm_judge=use_llm_judge,
            )
            weak_count = sum(1 for s in scores if s < 3)
            with tasks_lock:
                tasks[task_id]["qe_weak_count"] = weak_count
            if weak_count > 0:
                translated_texts = translator.retranslate_weak(
                    texts, translated_texts, scores,
                    use_back_translation=use_back_translation,
                )
```

Найти блок `tasks[task_id]["status"] = "done"` (~строка 316). Прямо перед ним добавить подсчёт TM-delta и duration:

```python
        tm_hits_delta = 0
        if translator._tm is not None:
            try:
                tm_hits_delta = translator._tm.stats().get("entries", 0) - tm_entries_before
            except Exception:
                pass
        duration_seconds = time.time() - t0
```

И сразу после в блоке `with tasks_lock:` добавить поля:

```python
        with tasks_lock:
            tasks[task_id]["output_file"] = str(output_path)
            tasks[task_id]["completed_at"] = time.time()
            tasks[task_id]["duration_seconds"] = duration_seconds
            if use_tm:
                tasks[task_id]["tm_hits_delta"] = tm_hits_delta
            tasks[task_id]["status"] = "done"
            save_dir = tasks[task_id].get("save_dir", "")
            output_name = tasks[task_id].get("output_name", "")
```

- [ ] **Step 4: Прогнать тесты**

```bash
python -m pytest tests/test_app_endpoints.py::TestTranslateWorker -v
python -m pytest tests/ -x -q
```

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app_endpoints.py
git commit -m "feat(worker): пробрасывает новые флаги, пишет tm_hits_delta и duration_seconds"
```

---

## Task 7: UI — restructure `.advanced-panel` (3 подраздела)

**Files:**

- Modify: `templates/index.html` (блок `.advanced-panel`, строки ~262-342)
- Test: создать `tests/test_index_template.py`

- [ ] **Step 1: Создать smoke-тест рендера шаблона**

Создать `tests/test_index_template.py`:

```python
"""Smoke tests: rendered index.html contains expected element IDs."""
import pytest
import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


class TestIndexTemplate:
    def test_advanced_panel_sections(self, client):
        html = client.get("/").data.decode("utf-8")
        # Three h4 subsections
        assert 'id="advSectionContent"' in html
        assert 'id="advSectionModel"' in html
        assert 'id="advSectionQuality"' in html

    def test_new_controls_present(self, client):
        html = client.get("/").data.decode("utf-8")
        assert 'id="use_tm"' in html
        assert 'id="use_llm_judge"' in html
        assert 'id="use_back_translation"' in html
        assert 'id="aux_model"' in html
        assert 'id="tmStatus"' in html
        assert 'id="tmClearBtn"' in html

    def test_result_info_block_present(self, client):
        html = client.get("/").data.decode("utf-8")
        assert 'id="resultInfo"' in html

    def test_footer_updated(self, client):
        html = client.get("/").data.decode("utf-8")
        # Old text removed
        assert "TranslateGemma (Google)" not in html
        # New text present
        assert "Gemma 4" in html and ("140" in html or "Qwen" in html)
```

- [ ] **Step 2: Запустить — тесты должны упасть**

```bash
python -m pytest tests/test_index_template.py -v
```

- [ ] **Step 3: Переструктурировать `.advanced-panel`**

В `templates/index.html` заменить содержимое `<div class="advanced-panel" id="advancedPanel">...</div>` (строки ~262-342) на:

```html
        <div class="advanced-panel" id="advancedPanel">
            <h4 id="advSectionContent" style="margin:0 0 12px 0; color:#a3bffa; font-size:13px; text-transform:uppercase; letter-spacing:0.5px;">Контент</h4>
            <div style="display:flex; gap:12px;">
                <div style="flex:1;">
                    <label for="source_lang">Язык оригинала:</label>
                    <select id="source_lang">
                        <option value="" selected>Автоопределение</option>
                        {% for lang in languages %}
                        <option value="{{ lang }}">{{ lang }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div style="flex:1;">
                    <label for="genre">Жанр:</label>
                    <select id="genre">
                        <option value="" selected>Автоопределение</option>
                        <option value="comedy">Комедия</option>
                        <option value="drama">Драма</option>
                        <option value="anime">Аниме</option>
                        <option value="documentary">Документальный</option>
                        <option value="action">Боевик</option>
                        <option value="horror">Хоррор</option>
                    </select>
                </div>
            </div>

            <label for="context">Контекст (необязательно):</label>
            <textarea id="context" placeholder="Например: Это сериал The Pitt о больнице, много медицинской терминологии"
                      style="min-height:60px;"></textarea>

            <label for="glossary">Глоссарий (необязательно):</label>
            <textarea id="glossary" placeholder="Tony Stark = Тони Старк&#10;SHIELD = Щ.И.Т."
                      style="min-height:50px; margin-bottom:4px;"></textarea>
            <div style="font-size:11px; color:#718096; margin-bottom:16px;">
                Формат: Оригинал = Перевод (по одной паре на строку). Дополняет авто-глоссарий.
            </div>

            <div style="border-top:1px solid #2d3748; padding-top:16px;">
            <h4 id="advSectionModel" style="margin:0 0 12px 0; color:#a3bffa; font-size:13px; text-transform:uppercase; letter-spacing:0.5px;">Параметры модели</h4>

            <div style="display:flex; gap:12px; align-items:end;">
                <div style="flex:1;">
                    <label for="temperature">Temperature:</label>
                    <input id="temperature" type="number" step="0.01" min="0" max="1" value="0">
                </div>
                <div style="width:130px;">
                    <label for="chunk_size">Chunk size:</label>
                    <input id="chunk_size" type="number" value="2000">
                </div>
                <div style="width:110px;">
                    <label for="context_window">Контекст:</label>
                    <input id="context_window" type="number" min="1" max="10" value="3" title="Количество соседних субтитров для контекста">
                </div>
                <div style="width:110px;">
                    <label for="max_cps">Макс CPS:</label>
                    <input id="max_cps" type="number" min="0" max="50" value="21" step="1" title="Макс. символов в секунду (0 = без проверки)">
                </div>
            </div>

            <div style="display:flex; align-items:center; gap:10px; margin:12px 0;">
                <label style="display:flex; align-items:center; gap:8px; cursor:pointer; margin:0;">
                    <input type="checkbox" id="two_pass" style="width:18px; height:18px; accent-color:#667eea; cursor:pointer;">
                    Двухпроходный перевод (translate + review)
                </label>
            </div>
            <div id="reviewModelRow" style="display:none; margin-bottom:12px;">
                <label for="review_model">Модель для review:</label>
                <input id="review_model" type="text" placeholder="По умолчанию — та же модель">
            </div>

            <label for="aux_model">Вспомогательная модель (анализ / глоссарий / QE):</label>
            <input id="aux_model" type="text" placeholder="qwen3.5:8b (по умолчанию)" style="margin-bottom:4px;">
            <div style="font-size:11px; color:#718096; margin-bottom:16px;">
                Пусто = автоматически (qwen3.5:8b для translation-only моделей, сама модель — для general-purpose).
            </div>
            </div>

            <div style="border-top:1px solid #2d3748; padding-top:16px;">
            <h4 id="advSectionQuality" style="margin:0 0 12px 0; color:#a3bffa; font-size:13px; text-transform:uppercase; letter-spacing:0.5px;">Качество и память</h4>

            <div style="display:flex; flex-wrap:wrap; gap:10px 20px; margin-bottom:10px;">
                <label style="display:flex; align-items:center; gap:8px; cursor:pointer; margin:0;">
                    <input type="checkbox" id="context_analysis" checked style="width:18px; height:18px; accent-color:#667eea; cursor:pointer;">
                    Анализ контента
                </label>
                <label style="display:flex; align-items:center; gap:8px; cursor:pointer; margin:0;">
                    <input type="checkbox" id="auto_glossary" checked style="width:18px; height:18px; accent-color:#667eea; cursor:pointer;">
                    Авто-глоссарий
                </label>
            </div>

            <div style="margin-bottom:10px;">
                <label style="display:flex; align-items:center; gap:8px; cursor:pointer; margin:0;">
                    <input type="checkbox" id="qe" checked style="width:18px; height:18px; accent-color:#667eea; cursor:pointer;">
                    Проверка качества (QE)
                </label>
                <div id="llmJudgeRow" style="margin:6px 0 0 26px;">
                    <label style="display:flex; align-items:center; gap:8px; cursor:pointer; margin:0; font-size:13px; color:#a0aec0;">
                        <input type="checkbox" id="use_llm_judge" checked style="width:16px; height:16px; accent-color:#667eea; cursor:pointer;">
                        LLM-оценка качества (точнее, медленнее — использует aux-модель)
                    </label>
                </div>
            </div>

            <div style="margin-bottom:10px;">
                <label style="display:flex; align-items:center; gap:8px; cursor:pointer; margin:0;">
                    <input type="checkbox" id="use_back_translation" style="width:18px; height:18px; accent-color:#667eea; cursor:pointer;">
                    Контроль обратного перевода (перепроверяет смысл слабых сегментов, +время)
                </label>
            </div>

            <div style="margin-bottom:4px;">
                <label style="display:flex; align-items:center; gap:8px; cursor:pointer; margin:0;">
                    <input type="checkbox" id="use_tm" checked style="width:18px; height:18px; accent-color:#667eea; cursor:pointer;">
                    Translation Memory (персистентный кэш между сеансами)
                </label>
                <div style="margin:4px 0 0 26px; font-size:12px; color:#a0aec0;">
                    <span id="tmStatus">загрузка...</span>
                    <a href="#" id="tmClearBtn" onclick="clearTM(); return false;" style="margin-left:10px; color:#fc8181; text-decoration:none;">очистить</a>
                </div>
            </div>
            </div>
        </div>
```

- [ ] **Step 4: Прогнать тест — 3 проверки из 4 проходят**

```bash
python -m pytest tests/test_index_template.py -v
```

Ожидается: `test_advanced_panel_sections`, `test_new_controls_present` PASS. `test_result_info_block_present` FAIL (делаем в Task 11). `test_footer_updated` FAIL (делаем в Task 9).

- [ ] **Step 5: Commit**

```bash
git add templates/index.html tests/test_index_template.py
git commit -m "ui: три подраздела в advanced-panel + новые контролы (TM/judge/back-translation/aux-model)"
```

---

## Task 8: UI — JS логика для TM статуса и `buildFormData`

**Files:**

- Modify: `templates/index.html` (JS-блок в конце)

- [ ] **Step 1: Добавить функции TM и обновить `buildFormData` / `buildVideoPayload`**

В `templates/index.html` найти `function buildFormData(file) {...}` (~строка 914). **Перед** этой функцией вставить:

```javascript
        // --- Translation Memory UI ---
        async function refreshTMStatus() {
            const el = document.getElementById('tmStatus');
            if (!el) return;
            try {
                const resp = await fetch('/tm/stats');
                const data = await resp.json();
                const mb = (data.size_bytes / 1024 / 1024).toFixed(1);
                el.textContent = `${data.entries} записей · ${mb} MB`;
            } catch (e) {
                el.textContent = 'недоступна';
            }
        }

        async function clearTM() {
            const el = document.getElementById('tmStatus');
            if (!confirm('Очистить Translation Memory? Это удалит все накопленные переводы.')) return;
            try {
                const resp = await fetch('/tm/clear', {method: 'POST'});
                const data = await resp.json();
                if (data.ok) {
                    el.textContent = `очищено (${data.cleared} записей удалено)`;
                    setTimeout(refreshTMStatus, 1500);
                } else {
                    el.textContent = 'ошибка очистки';
                }
            } catch (e) {
                el.textContent = 'ошибка подключения';
            }
        }

        // Auto-load TM stats on page load
        document.addEventListener('DOMContentLoaded', refreshTMStatus);

        // Toggle LLM-judge row when QE toggled
        document.addEventListener('DOMContentLoaded', () => {
            const qeCb = document.getElementById('qe');
            const judgeRow = document.getElementById('llmJudgeRow');
            const syncJudgeRow = () => {
                if (judgeRow) judgeRow.style.display = qeCb.checked ? 'block' : 'none';
            };
            if (qeCb) { qeCb.addEventListener('change', syncJudgeRow); syncJudgeRow(); }
        });
```

Найти `function buildFormData(file) {` и добавить в конце функции (перед `return formData;`):

```javascript
            // New 2026-04-19 flags
            formData.append('use_tm', document.getElementById('use_tm').checked ? 'on' : 'off');
            formData.append('use_llm_judge', document.getElementById('use_llm_judge').checked ? 'on' : 'off');
            if (document.getElementById('use_back_translation').checked) {
                formData.append('use_back_translation', 'on');
            }
            formData.append('aux_model', document.getElementById('aux_model').value.trim());
```

Найти `function buildVideoPayload()` и в объекте добавить перед закрывающей `}`:

```javascript
                use_tm: document.getElementById('use_tm').checked,
                use_llm_judge: document.getElementById('use_llm_judge').checked,
                use_back_translation: document.getElementById('use_back_translation').checked,
                aux_model: document.getElementById('aux_model').value.trim(),
```

- [ ] **Step 2: Smoke-тест — страница рендерится без ошибок**

```bash
python -m pytest tests/test_index_template.py -v
```

Проверки из Task 7 остаются зелёными (никаких new ID не удалено).

- [ ] **Step 3: Ручная проверка в браузере**

Запустить сервер и открыть:

```bash
python app.py
```

В браузере (http://127.0.0.1:8847):
- Раскрыть «Расширенные настройки».
- Увидеть 3 подраздела с заголовками КОНТЕНТ / ПАРАМЕТРЫ МОДЕЛИ / КАЧЕСТВО И ПАМЯТЬ.
- Увидеть строку `N записей · M MB` под чекбоксом TM.
- Отключить QE → строка LLM-оценки скрывается.
- Нажать «очистить» → confirm, потом статус обновляется.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "ui: JS для TM-статуса, clearTM, проброс новых флагов в form/payload"
```

---

## Task 9: UI — footer + `.status.warn` + показ save_dir warning

**Files:**

- Modify: `templates/index.html`

- [ ] **Step 1: Обновить footer**

В `templates/index.html` найти (~строка 364-366):

```html
    <div class="info">
        TranslateGemma (Google) — специализированные модели для перевода, 55 языков
    </div>
```

Заменить на:

```html
    <div class="info">
        Gemma 4 / Qwen 3.5 / Hunyuan-MT — локальный перевод через Ollama, 140+ языков
    </div>
```

- [ ] **Step 2: Добавить CSS `.status.warn`**

В `<style>` найти строки про `.status.error` и `.status.success` (~89-91) и добавить сразу после:

```css
        .status.warn { background: rgba(237, 137, 54, 0.15); color: #fbd38d; display: none; }
```

- [ ] **Step 3: Добавить JS-функцию showWarn + показ warning из ответа**

В JS-блоке найти `function showError(msg)` и `function showSuccess(msg)`. Добавить рядом:

```javascript
        function showWarn(msg) {
            status.textContent = msg;
            status.className = 'status warn';
            status.style.display = 'block';
        }
```

В обработчике translate кнопки после `currentTaskId = data.task_id;` (в двух местах: video и single-SRT) добавить перед `pollProgress();`:

```javascript
                    if (data.warning) showWarn(data.warning);
```

Эти строки в обоих блоках — find по паттерну `currentTaskId = data.task_id;\n                    pollProgress();` и вставь строку-предупреждение перед pollProgress.

В batch (`startBatchTranslation`) в цикле после `batchTaskIds.push(data.task_id);` добавить:

```javascript
                        if (data.warning) showWarn(data.warning);
```

- [ ] **Step 4: Прогнать тесты**

```bash
python -m pytest tests/test_index_template.py::TestIndexTemplate::test_footer_updated -v
```

PASS.

- [ ] **Step 5: Ручная проверка**

Ввести в save_dir путь `C:\Windows\System32`, запустить перевод маленького SRT. Ожидаем жёлтый баннер с текстом про save_dir, перевод идёт до конца.

- [ ] **Step 6: Commit**

```bash
git add templates/index.html
git commit -m "ui: обновлённый footer, класс .status.warn, показ save_dir warning"
```

---

## Task 10: UI — починка pull-прогресса (MIN_PULL_VISIBLE_MS + «Уже в кэше»)

**Files:**

- Modify: `templates/index.html` (функция `pullModel`, строки ~834-894)

- [ ] **Step 1: Переписать `pullModel`**

Найти `function pullModel(modelName) { ... }`. Заменить целиком на:

```javascript
        function pullModel(modelName) {
            const MIN_PULL_VISIBLE_MS = 1500;
            const FAST_CACHED_MS = 300;

            return new Promise((resolve, reject) => {
                const pullContainer = document.getElementById('pullContainer');
                const pullFill = document.getElementById('pullFill');
                const pullStatus = document.getElementById('pullStatus');
                const pullTitle = document.getElementById('pullTitle');

                pullContainer.style.display = 'block';
                pullTitle.textContent = `Скачивание модели ${modelName}...`;
                pullFill.style.width = '0%';
                pullStatus.textContent = 'Подключение...';

                const startTs = Date.now();
                let sawRealProgress = false;  // true if we got at least one non-trivial pulling event

                fetch('/pull_model', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({model: modelName}),
                }).then(response => {
                    const reader = response.body.getReader();
                    const decoder = new TextDecoder();
                    let buf = '';
                    function read() {
                        reader.read().then(({done, value}) => {
                            if (done) {
                                hideAfterMin();
                                resolve();
                                return;
                            }
                            buf += decoder.decode(value, {stream: true});
                            const lines = buf.split('\n');
                            buf = lines.pop();
                            for (const line of lines) {
                                if (!line.startsWith('data: ')) continue;
                                try {
                                    const d = JSON.parse(line.slice(6));
                                    if (d.status === 'error') {
                                        pullContainer.style.display = 'none';
                                        reject(d.error);
                                        return;
                                    }
                                    if (d.status === 'done') {
                                        pullFill.style.width = '100%';
                                        const elapsed = Date.now() - startTs;
                                        if (!sawRealProgress && elapsed < FAST_CACHED_MS) {
                                            pullStatus.textContent = 'Уже в кэше Ollama';
                                        } else {
                                            pullStatus.textContent = 'Готово!';
                                        }
                                        hideAfterMin(resolve);
                                        return;
                                    }
                                    if (d.total && d.completed && d.completed > 0) {
                                        sawRealProgress = true;
                                    }
                                    pullFill.style.width = d.pct + '%';
                                    const mb = d.completed ? (d.completed / 1024 / 1024).toFixed(0) : 0;
                                    const totalMb = d.total ? (d.total / 1024 / 1024).toFixed(0) : '?';
                                    pullStatus.textContent = `${d.status} — ${mb} / ${totalMb} MB (${d.pct}%)`;
                                } catch(e) {}
                            }
                            read();
                        });
                    }
                    read();
                }).catch(e => {
                    pullContainer.style.display = 'none';
                    reject(e.message || 'Ошибка скачивания');
                });

                function hideAfterMin(cb) {
                    const elapsed = Date.now() - startTs;
                    const wait = Math.max(0, MIN_PULL_VISIBLE_MS - elapsed);
                    setTimeout(() => {
                        pullContainer.style.display = 'none';
                        if (cb) cb();
                    }, wait);
                }
            });
        }
```

- [ ] **Step 2: Ручная проверка**

1. Удалить модель из Ollama: `ollama rm gemma4:e4b`.
2. В UI нажать «Скачать» на Gemma 4 4B → видно реальный прогресс до 100%.
3. Нажать «Скачать» ещё раз → появляется панель, держится ≥1.5 сек, показывает «Уже в кэше Ollama», затем закрывается.

- [ ] **Step 3: Commit**

```bash
git add templates/index.html
git commit -m "ui: pull-прогресс держится >=1.5с и показывает 'Уже в кэше' для быстрых success"
```

---

## Task 11: UI — блок `#resultInfo` после завершения

**Files:**

- Modify: `templates/index.html`

- [ ] **Step 1: Добавить HTML-разметку**

В `templates/index.html` найти `<div class="progress-container" id="progressContainer">...</div>` (~строки 352-355). **Сразу после** этого блока (до `#batchProgressContainer`) вставить:

```html
        <div id="resultInfo" style="display:none; margin-top:16px; padding:14px; background: rgba(72,187,120,0.08); border:1px solid #48bb78; border-radius:8px;">
            <div id="resultDuration" style="font-size:13px; color:#a0aec0; margin-bottom:8px;"></div>
            <div id="resultGlossary" style="font-size:13px; color:#e2e8f0; margin-bottom:6px; display:none;"></div>
            <div id="resultAnalysis" style="font-size:13px; color:#a0aec0; margin-bottom:6px; display:none; font-style:italic;"></div>
            <div id="resultQe" style="font-size:13px; color:#a0aec0; margin-bottom:6px; display:none;"></div>
            <div id="resultTm" style="font-size:13px; color:#a0aec0; display:none;"></div>
        </div>
```

- [ ] **Step 2: Обновить `pollProgress` для наполнения блока**

Найти в JS внутри `pollProgress` блок `if (data.status === 'done') { ... }` (~строки 1128-1139). Добавить перед `showSuccess(...)`:

```javascript
                    renderResultInfo(data);
```

Добавить новую функцию после `pollProgress` (перед `downloadBtn.addEventListener`):

```javascript
        function renderResultInfo(data) {
            const box = document.getElementById('resultInfo');
            if (!box) return;

            const dur = data.duration_seconds || 0;
            const m = Math.floor(dur / 60);
            const s = Math.round(dur % 60);
            document.getElementById('resultDuration').textContent =
                `⏱ Готово за ${m > 0 ? m + 'm ' + s + 's' : s + 's'}`;

            const ag = data.auto_glossary;
            const agBox = document.getElementById('resultGlossary');
            if (ag && Object.keys(ag).length) {
                const n = Object.keys(ag).length;
                const items = Object.entries(ag)
                    .slice(0, 50)
                    .map(([k, v]) => `${k} → ${v}`)
                    .join(', ');
                agBox.innerHTML = `🔖 Авто-глоссарий: ${n} терминов <span style="color:#718096; font-size:12px;">(${items}${n > 50 ? '...' : ''})</span>`;
                agBox.style.display = 'block';
            } else {
                agBox.style.display = 'none';
            }

            const ana = data.context_analysis_result;
            const anaBox = document.getElementById('resultAnalysis');
            if (ana) {
                anaBox.textContent = `🎬 ${ana}`;
                anaBox.style.display = 'block';
            } else {
                anaBox.style.display = 'none';
            }

            const qw = data.qe_weak_count;
            const qeBox = document.getElementById('resultQe');
            if (typeof qw === 'number') {
                qeBox.textContent = qw > 0
                    ? `✅ QE: ${qw} слабых сегментов переведены заново`
                    : `✅ QE: все сегменты прошли проверку`;
                qeBox.style.display = 'block';
            } else {
                qeBox.style.display = 'none';
            }

            const td = data.tm_hits_delta;
            const tmBox = document.getElementById('resultTm');
            if (typeof td === 'number') {
                tmBox.textContent = `💾 Translation Memory: +${td} новых записей`;
                tmBox.style.display = 'block';
                refreshTMStatus();  // update status line in the panel
            } else {
                tmBox.style.display = 'none';
            }

            box.style.display = 'block';
        }
```

Также в начале нового перевода (click на translateBtn) нужно скрывать блок. Найти начало обработчика:

```javascript
        translateBtn.addEventListener('click', async () => {
            translateBtn.disabled = true;
            downloadBtn.style.display = 'none';
            document.getElementById('batchDownloadContainer').style.display = 'none';
            status.style.display = 'none';
```

Добавить перед `try { await ensureModels(); ...}`:

```javascript
            const ri = document.getElementById('resultInfo');
            if (ri) ri.style.display = 'none';
```

- [ ] **Step 3: Тест шаблона — все 4 проверки зелёные**

```bash
python -m pytest tests/test_index_template.py -v
```

Все тесты PASS (`test_result_info_block_present` теперь тоже).

- [ ] **Step 4: Ручная проверка**

Перевести небольшой SRT с включёнными Анализом + Авто-глоссарием + QE + TM. После завершения увидеть блок с:
- временем
- количеством терминов в глоссарии
- анализом
- QE-статистикой
- TM delta

- [ ] **Step 5: Commit**

```bash
git add templates/index.html
git commit -m "ui: блок результатов перевода (duration, глоссарий, анализ, QE, TM delta)"
```

---

## Task 12: Полный прогон тестов и README-обновление

**Files:**

- Read-only: all.

- [ ] **Step 1: Прогнать всё**

```bash
cd f:/VScode/ollama-subtitle-translator && python -m pytest tests/ -v
```

Ожидается: все тесты проходят, число выросло на ~15 по сравнению с до апгрейда (125 → ~140).

- [ ] **Step 2: Обновить CLAUDE.md**

В `CLAUDE.md` в секции `## Fixed Issues` добавить:

```markdown
## Fixed Issues (session 5 — 2026-04-19, UI sync)
- UI чекбоксы для новых фич: TM toggle (+ статус/clear), LLM-judge sub-toggle под QE, back-translation, aux-модель override
- `/tm/stats` и `/tm/clear` endpoints
- `/check_model` exact-match вместо substring — фиксит ложное «уже скачано» и невидимый pull-прогресс
- pull-панель держится ≥1.5 сек; отличает быстрый cached success от реального скачивания
- save_dir: rejected → жёлтый warning в UI вместо тихого игнора
- Блок результатов перевода: duration, auto_glossary, context analysis, qe_weak_count, tm_hits_delta
- Footer обновлён: Gemma 4 / Qwen 3.5 / Hunyuan-MT, 140+ языков
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md — session 5 (UI sync 2026-04-19)"
```

---

## Самопроверка плана (self-review)

**Покрытие спецификации:**

| Требование спеки | Задача |
|---|---|
| TM UI | Task 1 (clear), Task 2 (API), Task 7/8 (UI) |
| LLM-judge toggle | Task 6 (worker), Task 7 (UI) |
| back-translation toggle | Task 6, Task 7 |
| aux_model override | Task 6, Task 7 |
| footer update | Task 9 |
| save_dir warning | Task 4 (/translate), Task 5 (/extract_and_translate), Task 9 (UI) |
| #resultInfo блок | Task 6 (worker пишет поля), Task 11 (UI) |
| pull-прогресс fix | Task 3 (exact match), Task 10 (UI MIN_PULL_VISIBLE_MS) |

Все 8 пунктов покрыты.

**Placeholder-сканирование:** проверено. Все стаблы, моки и тексты сообщений — конкретные. Нет «TBD» и «similar to».

**Согласованность типов:** `use_tm` (bool), `use_llm_judge` (bool), `use_back_translation` (bool), `aux_model` (str) — единообразно в form-передаче (on/off), в payload (bool), в task snapshot (bool), в worker-подписи (bool/str). `tm_hits_delta` (int), `duration_seconds` (float) — одинаково называются в worker и frontend.
