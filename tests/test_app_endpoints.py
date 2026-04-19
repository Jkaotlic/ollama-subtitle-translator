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
                "file": (io.BytesIO(srt), "test.srt"),
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
                "model": "gemma4:e12b",
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
                "model": "gemma4:e12b",
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
