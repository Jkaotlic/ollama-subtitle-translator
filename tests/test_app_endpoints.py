"""Tests for Flask app.py endpoints."""
import io
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
        tm.store("hi", "en", "gemma4:e4b", "привет")
        tm.close()
        resp = client.get("/tm/stats")
        data = resp.get_json()
        assert data["entries"] == 1
        assert data["size_bytes"] > 0

    def test_tm_clear(self, client, tmp_path):
        import translate_srt as ts
        tm = ts.TranslationMemory(tmp_path / "translation_memory.db")
        tm.store("hi", "en", "gemma4:e4b", "привет")
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

    def test_tm_clear_409_when_tm_active_translation(self, client, tmp_path):
        """While a task with use_tm=True is running, /tm/clear returns 409."""
        # Inject an active task using TM
        import app as app_module
        with app_module.tasks_lock:
            app_module.tasks["busy-task"] = {
                "status": "running",
                "use_tm": True,
                "created_at": 0,
            }
        try:
            resp = client.post("/tm/clear")
            assert resp.status_code == 409
            data = resp.get_json()
            assert data["ok"] is False
            assert "занят" in data["error"].lower() or "busy" in data["error"].lower()
        finally:
            with app_module.tasks_lock:
                app_module.tasks.pop("busy-task", None)

    def test_tm_clear_allowed_when_task_not_using_tm(self, client, tmp_path):
        """A running task WITHOUT TM shouldn't block /tm/clear."""
        import app as app_module
        with app_module.tasks_lock:
            app_module.tasks["non-tm-task"] = {
                "status": "running",
                "use_tm": False,
                "created_at": 0,
            }
        try:
            resp = client.post("/tm/clear")
            assert resp.status_code == 200
            assert resp.get_json()["ok"] is True
        finally:
            with app_module.tasks_lock:
                app_module.tasks.pop("non-tm-task", None)


class TestCheckModel:
    def test_exact_match_positive(self, client, monkeypatch):
        # Ollama returns exact tag we requested
        def fake_get(url, timeout=5):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"models": [{"name": "gemma4:e4b"}]}
            return resp
        monkeypatch.setattr(app_module.requests, "get", fake_get)
        resp = client.post("/check_model", json={"model": "gemma4:e4b"})
        data = resp.get_json()
        assert data["exists"] is True

    def test_exact_match_rejects_prefix(self, client, monkeypatch):
        # Only a suffixed variant is installed — not the tag user asked for
        def fake_get(url, timeout=5):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"models": [{"name": "gemma4:e4b-instruct-q4"}]}
            return resp
        monkeypatch.setattr(app_module.requests, "get", fake_get)
        resp = client.post("/check_model", json={"model": "gemma4:e4b"})
        data = resp.get_json()
        assert data["exists"] is False, "gemma4:e4b should NOT match gemma4:e4b-instruct-q4"

    def test_list_all_returns_available(self, client, monkeypatch):
        def fake_get(url, timeout=5):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"models": [{"name": "gemma4:e4b"}, {"name": "qwen3.5:9b"}]}
            return resp
        monkeypatch.setattr(app_module.requests, "get", fake_get)
        resp = client.post("/check_model", json={"model": "__list_all__"})
        data = resp.get_json()
        assert "gemma4:e4b" in data["available"]
        assert "qwen3.5:9b" in data["available"]


class TestTranslateEndpoint:
    def test_new_flags_stored_in_task_snapshot(self, client, monkeypatch):
        # Stub executor.submit so worker never actually runs
        monkeypatch.setattr(app_module.executor, "submit", lambda *a, **k: MagicMock())
        srt = b"1\n00:00:01,000 --> 00:00:02,000\nHi\n\n"
        resp = client.post(
            "/translate",
            data={
                "file": (io.BytesIO(srt), "test.srt"),
                "lang": "Russian",
                "model": "gemma4:e4b",
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
                "file": (io.BytesIO(srt), "test.srt"),
                "lang": "Russian",
                "model": "gemma4:e4b",
                "save_dir": "C:\\Windows\\System32",  # not in allow-list
            },
            content_type="multipart/form-data",
        )
        data = resp.get_json()
        assert "task_id" in data
        assert "warning" in data
        assert "save_dir" in data["warning"].lower()

    def test_save_dir_valid_no_warning(self, client, monkeypatch, tmp_path):
        # Force allow-list to accept tmp_path (portable across machines).
        monkeypatch.setattr(app_module.executor, "submit", lambda *a, **k: MagicMock())
        monkeypatch.setattr(app_module, "_safe_base_dirs",
                            lambda extra=None: [tmp_path.resolve()])
        srt = b"1\n00:00:01,000 --> 00:00:02,000\nHi\n\n"
        resp = client.post(
            "/translate",
            data={
                "file": (io.BytesIO(srt), "test.srt"),
                "lang": "Russian",
                "model": "gemma4:e4b",
                "save_dir": str(tmp_path),
            },
            content_type="multipart/form-data",
        )
        data = resp.get_json()
        assert "warning" not in data

    def test_default_flags_still_work(self, client, monkeypatch):
        """Backwards compat: existing clients that don't send the new fields."""
        monkeypatch.setattr(app_module.executor, "submit", lambda *a, **k: MagicMock())
        srt = b"1\n00:00:01,000 --> 00:00:02,000\nHi\n\n"
        resp = client.post(
            "/translate",
            data={
                "file": (io.BytesIO(srt), "test.srt"),
                "lang": "Russian",
                "model": "gemma4:e4b",
            },
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        with app_module.tasks_lock:
            task = app_module.tasks[task_id]
        # Defaults: TM on, LLM-judge on, back-translation off, aux-model empty
        assert task["use_tm"] is True
        assert task["use_llm_judge"] is True
        assert task["use_back_translation"] is False
        assert task["aux_model"] == ""


class TestExtractAndTranslate:
    def test_new_flags_forwarded(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module.executor, "submit", lambda *a, **k: MagicMock())

        # Create a fake video file in tmp_path so resolve_video_path accepts it
        fake_video = tmp_path / "movie.mkv"
        fake_video.write_bytes(b"fake")

        import video_utils
        monkeypatch.setattr(video_utils, "resolve_video_path", lambda p: str(fake_video))
        monkeypatch.setattr(video_utils, "extract_subtitle_track",
                            lambda resolved, idx, dest: Path(dest).write_text(
                                "1\n00:00:01,000 --> 00:00:02,000\nHi\n\n",
                                encoding="utf-8",
                            ))

        resp = client.post("/extract_and_translate", json={
            "path": str(fake_video),
            "sub_index": 0,
            "lang": "Russian",
            "model": "gemma4:e4b",
            "use_tm": False,
            "use_llm_judge": False,
            "use_back_translation": True,
            "aux_model": "llama3:8b",
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)
        task_id = resp.get_json()["task_id"]
        with app_module.tasks_lock:
            task = app_module.tasks[task_id]
        assert task["use_tm"] is False
        assert task["use_llm_judge"] is False
        assert task["use_back_translation"] is True
        assert task["aux_model"] == "llama3:8b"

    def test_default_flags(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module.executor, "submit", lambda *a, **k: MagicMock())
        fake_video = tmp_path / "movie.mkv"
        fake_video.write_bytes(b"fake")
        import video_utils
        monkeypatch.setattr(video_utils, "resolve_video_path", lambda p: str(fake_video))
        monkeypatch.setattr(video_utils, "extract_subtitle_track",
                            lambda resolved, idx, dest: Path(dest).write_text(
                                "1\n00:00:01,000 --> 00:00:02,000\nHi\n\n",
                                encoding="utf-8",
                            ))
        resp = client.post("/extract_and_translate", json={
            "path": str(fake_video),
            "sub_index": 0,
            "lang": "Russian",
            "model": "gemma4:e4b",
        })
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        with app_module.tasks_lock:
            task = app_module.tasks[task_id]
        assert task["use_tm"] is True
        assert task["use_llm_judge"] is True
        assert task["use_back_translation"] is False
        assert task["aux_model"] == ""

    def test_json_string_false_treated_as_false(self, client, monkeypatch, tmp_path):
        """Regression: JSON payload with string 'false' must not be truthy."""
        monkeypatch.setattr(app_module.executor, "submit", lambda *a, **k: MagicMock())
        fake_video = tmp_path / "movie.mkv"
        fake_video.write_bytes(b"fake")
        import video_utils
        monkeypatch.setattr(video_utils, "resolve_video_path", lambda p: str(fake_video))
        monkeypatch.setattr(video_utils, "extract_subtitle_track",
                            lambda resolved, idx, dest: Path(dest).write_text(
                                "1\n00:00:01,000 --> 00:00:02,000\nHi\n\n",
                                encoding="utf-8",
                            ))
        resp = client.post("/extract_and_translate", json={
            "path": str(fake_video),
            "sub_index": 0,
            "lang": "Russian",
            "model": "gemma4:e4b",
            "use_tm": "false",          # string, not bool
            "use_back_translation": "false",
        })
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        with app_module.tasks_lock:
            task = app_module.tasks[task_id]
        assert task["use_tm"] is False
        assert task["use_back_translation"] is False


class TestTranslateWorker:
    def test_worker_respects_use_tm_false(self, tmp_path, monkeypatch):
        """When use_tm=False, Translator is constructed with tm_path=None, aux_model forwarded."""
        from app import translate_worker, tasks_lock, tasks

        srt_path = tmp_path / "in.srt"
        srt_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n", encoding="utf-8")
        out_path = tmp_path / "out.srt"

        captured = {}

        class FakeTranslator:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)
                self._tm = None  # tm_path was None
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

        task_id = "test-worker-tm-off"
        with tasks_lock:
            tasks[task_id] = {
                "status": "starting", "current": 0, "total": 0,
                "output_name": "out.srt", "save_dir": "",
                "created_at": 0, "temperature": 0.0, "chunk_size": 1000,
                "context_window": 3, "max_cps": 0, "two_pass_enabled": False,
                "use_tm": False, "use_llm_judge": False,
                "use_back_translation": True, "aux_model": "llama3:8b",
            }

        try:
            translate_worker(
                task_id, srt_path, out_path,
                "Russian", "gemma4:e4b",
                use_tm=False, use_llm_judge=False,
                use_back_translation=True, aux_model="llama3:8b",
                context_analysis=False, qe=True, auto_glossary=False,
            )
            assert captured.get("tm_path") is None
            assert captured.get("aux_model") == "llama3:8b"
            assert captured.get("use_llm_judge_arg") is False
            # retranslate_weak was called only if there were weak segs; scores=5 means
            # weak_count=0, so retranslate_weak won't be called. Tolerate either.
        finally:
            with tasks_lock:
                tasks.pop(task_id, None)

    def test_worker_writes_duration_and_tm_hits(self, tmp_path, monkeypatch):
        """TM delta and duration are written to task snapshot on success."""
        from app import translate_worker, tasks_lock, tasks

        srt_path = tmp_path / "in.srt"
        srt_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n", encoding="utf-8")
        out_path = tmp_path / "out.srt"

        # Sequence: before = 5 entries, after = 8 entries -> delta = 3
        tm_stats_seq = [{"entries": 5}, {"entries": 8}]

        class FakeTM:
            def __init__(self, *a, **k): pass
            def stats(self):
                return tm_stats_seq.pop(0) if tm_stats_seq else {"entries": 0}
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

        task_id = "test-worker-delta"
        with tasks_lock:
            tasks[task_id] = {
                "status": "starting", "current": 0, "total": 0,
                "output_name": "out.srt", "save_dir": "",
                "created_at": 0, "temperature": 0.0, "chunk_size": 1000,
                "context_window": 3, "max_cps": 0, "two_pass_enabled": False,
                "use_tm": True, "use_llm_judge": True,
                "use_back_translation": False, "aux_model": "",
            }

        try:
            translate_worker(
                task_id, srt_path, out_path, "Russian", "gemma4:e4b",
                use_tm=True, context_analysis=False, qe=False, auto_glossary=False,
            )
            with tasks_lock:
                task = tasks[task_id]
            assert task["status"] == "done"
            assert "duration_seconds" in task
            assert task.get("tm_hits_delta") == 3
        finally:
            with tasks_lock:
                tasks.pop(task_id, None)

    def test_worker_no_tm_hits_when_disabled(self, tmp_path, monkeypatch):
        """When use_tm=False, tm_hits_delta must not appear in task snapshot."""
        from app import translate_worker, tasks_lock, tasks

        srt_path = tmp_path / "in.srt"
        srt_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n", encoding="utf-8")
        out_path = tmp_path / "out.srt"

        class FakeTranslator:
            def __init__(self, **kwargs): self._tm = None
            def generate_glossary(self, texts): return {}
            def analyze_context(self, texts): return ""
            def translate_batch(self, texts, **kwargs): return list(texts)
            def estimate_quality(self, o, t, **kwargs): return [5] * len(o)
            def retranslate_weak(self, o, t, s, **kwargs): return list(t)
            def close(self): pass
            glossary = {}

        monkeypatch.setattr("translate_srt.Translator", FakeTranslator)

        task_id = "test-worker-no-tm"
        with tasks_lock:
            tasks[task_id] = {
                "status": "starting", "current": 0, "total": 0,
                "output_name": "out.srt", "save_dir": "",
                "created_at": 0, "temperature": 0.0, "chunk_size": 1000,
                "context_window": 3, "max_cps": 0, "two_pass_enabled": False,
                "use_tm": False, "use_llm_judge": True,
                "use_back_translation": False, "aux_model": "",
            }

        try:
            translate_worker(
                task_id, srt_path, out_path, "Russian", "gemma4:e4b",
                use_tm=False, context_analysis=False, qe=False, auto_glossary=False,
            )
            with tasks_lock:
                task = tasks[task_id]
            assert task["status"] == "done"
            assert "tm_hits_delta" not in task
            assert "duration_seconds" in task
        finally:
            with tasks_lock:
                tasks.pop(task_id, None)


class TestPullModelErrorReporting:
    def test_pull_emits_error_event_when_ollama_returns_error(self, client, monkeypatch):
        """Bug A regression: Ollama error response must NOT be swallowed as 'done'."""

        class _FakeResp:
            def __init__(self):
                self._lines = [
                    b'{"error": "pull model manifest: file does not exist"}',
                ]
            def iter_lines(self):
                for ln in self._lines:
                    yield ln
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(app_module.requests, "post",
                            lambda *a, **k: _FakeResp())

        resp = client.post("/pull_model", json={"model": "nonexistent:bogus"})
        body = resp.get_data(as_text=True)
        # SSE payload should contain an error event, not a done event.
        assert '"status": "error"' in body
        assert "does not exist" in body
        assert '"status": "done"' not in body

    def test_pull_emits_done_on_successful_stream(self, client, monkeypatch):
        """Happy-path regression: normal stream still ends with done."""
        class _FakeResp:
            def __init__(self):
                self._lines = [
                    b'{"status": "pulling manifest"}',
                    b'{"status": "pulling abc123", "total": 1000, "completed": 500}',
                    b'{"status": "success"}',
                ]
            def iter_lines(self):
                for ln in self._lines:
                    yield ln
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(app_module.requests, "post",
                            lambda *a, **k: _FakeResp())
        resp = client.post("/pull_model", json={"model": "legit-model"})
        body = resp.get_data(as_text=True)
        assert '"status": "done"' in body


class TestParseFlag:
    def test_parse_flag_none_returns_default(self):
        from app import _parse_flag
        assert _parse_flag(None, default=True) is True
        assert _parse_flag(None, default=False) is False

    def test_parse_flag_bool_passes_through(self):
        from app import _parse_flag
        assert _parse_flag(True, default=False) is True
        assert _parse_flag(False, default=True) is False

    def test_parse_flag_string_false_is_false(self):
        """Fixes the bool('false') == True pitfall."""
        from app import _parse_flag
        assert _parse_flag("false", default=True) is False
        assert _parse_flag("off", default=True) is False
        assert _parse_flag("0", default=True) is False

    def test_parse_flag_string_true_is_true(self):
        from app import _parse_flag
        assert _parse_flag("true", default=False) is True
        assert _parse_flag("on", default=False) is True
        assert _parse_flag("1", default=False) is True

    def test_parse_flag_empty_string_is_false(self):
        from app import _parse_flag
        assert _parse_flag("", default=True) is False

    def test_parse_flag_unknown_returns_default(self):
        from app import _parse_flag
        assert _parse_flag("maybe", default=True) is True
        assert _parse_flag("maybe", default=False) is False
