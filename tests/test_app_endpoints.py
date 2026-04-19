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
