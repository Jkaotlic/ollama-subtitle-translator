"""
Tests for translate_srt module:
- SRT parsing (encoding, edge cases)
- Tag protection / restoration
- Translator mock tests (JSON, non-JSON, non-200, retry, batch)
- write_srt round-trip
- post_with_retry
"""
import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import translate_srt as ts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_srt_bytes(text: str, encoding: str = "utf-8") -> bytes:
    return text.encode(encoding)


class _MockResp:
    """Lightweight mock for requests.Response."""
    def __init__(self, status_code=200, json_data=None, text_data=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text_data

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


def _mock_get_tags(url, timeout=5):
    """Return a successful /api/tags response with translategemma model."""
    return _MockResp(200, {"models": [{"name": "translategemma:4b"}]})


# ---------------------------------------------------------------------------
# read_srt_file + parse_srt — encoding tests
# ---------------------------------------------------------------------------

class TestReadAndParseSrt:
    def test_utf8(self, tmp_path):
        p = tmp_path / "u.srt"
        s = "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
        p.write_bytes(make_srt_bytes(s, "utf-8"))
        txt, enc = ts.read_srt_file(p)
        assert enc == "utf-8"
        blocks = ts.parse_srt(txt)
        assert len(blocks) == 1
        assert blocks[0].index == 1
        assert blocks[0].lines == ("Hello",)

    def test_utf8_sig(self, tmp_path):
        p = tmp_path / "bom.srt"
        # Do NOT include \ufeff in the string — encode("utf-8-sig") adds the BOM automatically
        s = "1\n00:00:01,000 --> 00:00:02,000\nHi\n\n"
        p.write_bytes(s.encode("utf-8-sig"))
        txt, enc = ts.read_srt_file(p)
        assert enc == "utf-8-sig"
        blocks = ts.parse_srt(txt)
        assert len(blocks) == 1
        assert blocks[0].lines == ("Hi",)

    def test_cp1251(self, tmp_path):
        p = tmp_path / "ru.srt"
        s = "1\n00:00:01,000 --> 00:00:02,000\nПривет\n\n"
        p.write_bytes(s.encode("cp1251"))
        txt, enc = ts.read_srt_file(p)
        assert enc == "cp1251"
        blocks = ts.parse_srt(txt)
        assert len(blocks) == 1
        assert "Привет" in blocks[0].lines[0]


# ---------------------------------------------------------------------------
# parse_srt — edge / malformed cases
# ---------------------------------------------------------------------------

class TestParseSrt:
    def test_missing_index(self):
        s = "00:00:01,000 --> 00:00:02,000\nNo index\n\n"
        assert ts.parse_srt(s) == []

    def test_malformed_timecode(self):
        # dot instead of comma
        s = "1\n00:00:01.000 --> 00:00:02,000\nBad time\n\n"
        assert ts.parse_srt(s) == []

    def test_multiline_text(self):
        s = "1\n00:00:01,000 --> 00:00:02,000\nLine1\nLine2\n\n"
        blocks = ts.parse_srt(s)
        assert len(blocks) == 1
        assert blocks[0].lines == ("Line1", "Line2")

    def test_multiple_blocks(self):
        s = (
            "1\n00:00:01,000 --> 00:00:02,000\nFirst\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nSecond\n\n"
        )
        blocks = ts.parse_srt(s)
        assert len(blocks) == 2
        assert blocks[0].text() == "First"
        assert blocks[1].text() == "Second"

    def test_empty_string(self):
        assert ts.parse_srt("") == []

    def test_crlf_normalization(self):
        s = "1\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\n\r\n"
        blocks = ts.parse_srt(s)
        assert len(blocks) == 1
        assert blocks[0].lines == ("Hello",)

    def test_trailing_whitespace_in_timecode(self):
        s = "1\n00:00:01,000 --> 00:00:02,000  \nHello\n\n"
        blocks = ts.parse_srt(s)
        assert len(blocks) == 1

    def test_non_sequential_indices(self):
        s = (
            "5\n00:00:01,000 --> 00:00:02,000\nA\n\n"
            "10\n00:00:03,000 --> 00:00:04,000\nB\n\n"
        )
        blocks = ts.parse_srt(s)
        assert len(blocks) == 2
        assert blocks[0].index == 5
        assert blocks[1].index == 10

    def test_block_text_method(self):
        block = ts.SrtBlock(index=1, timecode="00:00:01,000 --> 00:00:02,000", lines=("A", "B"))
        assert block.text() == "A\nB"


# ---------------------------------------------------------------------------
# write_srt — round-trip
# ---------------------------------------------------------------------------

class TestWriteSrt:
    def test_roundtrip(self, tmp_path):
        srt_text = "1\n00:00:01,000 --> 00:00:02,000\nHello world\n\n2\n00:00:03,000 --> 00:00:04,000\nBye\n\n"
        blocks = ts.parse_srt(srt_text)
        out = tmp_path / "out.srt"
        ts.write_srt(blocks, out, "utf-8")

        written = out.read_text(encoding="utf-8")
        blocks2 = ts.parse_srt(written)
        assert len(blocks2) == len(blocks)
        for a, b in zip(blocks, blocks2):
            assert a.index == b.index
            assert a.timecode == b.timecode
            assert a.lines == b.lines


# ---------------------------------------------------------------------------
# protect_tags / restore_tags
# ---------------------------------------------------------------------------

class TestTagProtection:
    def test_html_tags(self):
        text = "<i>Hello</i> world <b>bold</b>"
        protected, tags = ts.protect_tags(text)
        assert "<i>" not in protected
        assert "</i>" not in protected
        assert "<b>" not in protected
        assert "Hello" in protected
        assert "world" in protected
        restored = ts.restore_tags(protected, tags)
        assert restored == text

    def test_ass_tags(self):
        text = r"{\an8}Some text"
        protected, tags = ts.protect_tags(text)
        assert r"{\an8}" not in protected
        assert "Some text" in protected
        restored = ts.restore_tags(protected, tags)
        assert restored == text

    def test_no_tags(self):
        text = "No tags here"
        protected, tags = ts.protect_tags(text)
        assert protected == text
        assert tags == {}

    def test_empty_string(self):
        protected, tags = ts.protect_tags("")
        assert protected == ""
        assert tags == {}


# ---------------------------------------------------------------------------
# post_with_retry
# ---------------------------------------------------------------------------

class TestPostWithRetry:
    def test_success_first_attempt(self, monkeypatch):
        call_count = 0

        def mock_post(url, json, timeout):
            nonlocal call_count
            call_count += 1
            return _MockResp(200, {"response": "ok"})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        resp = ts.post_with_retry("http://fake/api", json={}, timeout=5, attempts=3, backoff=0.01)
        assert resp is not None
        assert resp.status_code == 200
        assert call_count == 1

    def test_retries_on_failure(self, monkeypatch):
        import requests as real_requests
        call_count = 0

        def mock_post(url, json, timeout):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise real_requests.RequestException("connection error")
            return _MockResp(200, {"response": "ok"})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        resp = ts.post_with_retry("http://fake/api", json={}, timeout=5, attempts=3, backoff=0.01)
        assert resp is not None
        assert call_count == 3

    def test_all_attempts_fail(self, monkeypatch):
        import requests as real_requests

        def mock_post(url, json, timeout):
            raise real_requests.RequestException("total failure")

        monkeypatch.setattr(ts.requests, "post", mock_post)
        resp = ts.post_with_retry("http://fake/api", json={}, timeout=5, attempts=2, backoff=0.01)
        assert resp is None


# ---------------------------------------------------------------------------
# Translator — mock Ollama responses
# ---------------------------------------------------------------------------

class TestTranslator:
    def _make_translator(self, monkeypatch):
        monkeypatch.setattr(ts.requests, "get", _mock_get_tags)
        return ts.Translator(
            model="translategemma:4b",
            target_lang="Russian",
            ollama_url="http://fake",
        )

    def test_translate_json_response(self, monkeypatch):
        tr = self._make_translator(monkeypatch)

        def mock_post(url, json, timeout):
            return _MockResp(200, {"response": "Привет"}, text_data="Привет")

        monkeypatch.setattr(ts.requests, "post", mock_post)
        out = tr.translate("Hello")
        assert "Привет" in out

    def test_translate_non_json_fallback(self, monkeypatch):
        tr = self._make_translator(monkeypatch)

        class BadJSON(Exception):
            pass

        def mock_post(url, json, timeout):
            return _MockResp(200, BadJSON(), text_data="raw text result")

        monkeypatch.setattr(ts.requests, "post", mock_post)
        out = tr.translate("Hello")
        assert out == "raw text result"

    def test_translate_non_200_returns_original(self, monkeypatch):
        tr = self._make_translator(monkeypatch)

        def mock_post(url, json, timeout):
            return _MockResp(500, {"error": "boom"}, text_data="boom")

        monkeypatch.setattr(ts.requests, "post", mock_post)
        out = tr.translate("Hello")
        assert out == "Hello"

    def test_translate_empty_text(self, monkeypatch):
        tr = self._make_translator(monkeypatch)
        out = tr.translate("")
        assert out == ""

    def test_translate_whitespace_only(self, monkeypatch):
        tr = self._make_translator(monkeypatch)
        out = tr.translate("   ")
        assert out == "   "

    def test_translate_preserves_tags(self, monkeypatch):
        tr = self._make_translator(monkeypatch)

        def mock_post(url, json, timeout):
            # model returns translation but keeps __TAG_xxx__ placeholders
            prompt = json.get("prompt", "")
            import re
            placeholders = re.findall(r"__TAG_[a-f0-9]+__", prompt)
            translated = "Привет " + " ".join(placeholders) + " мир"
            return _MockResp(200, {"response": translated})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        out = tr.translate("<i>Hello</i> world")
        assert "<i>" in out
        assert "</i>" in out

    def test_translate_retry_returns_none(self, monkeypatch):
        """When post_with_retry returns None, translate returns the original text."""
        tr = self._make_translator(monkeypatch)
        import requests as real_requests

        def mock_post(url, json, timeout):
            raise real_requests.RequestException("dead")

        monkeypatch.setattr(ts.requests, "post", mock_post)
        out = tr.translate("Hello")
        assert out == "Hello"

    def test_translate_with_context(self, monkeypatch):
        monkeypatch.setattr(ts.requests, "get", _mock_get_tags)
        tr = ts.Translator(
            model="translategemma:4b",
            target_lang="Russian",
            ollama_url="http://fake",
            context="Medical drama series",
        )
        captured_prompts = []

        def mock_post(url, json, timeout):
            captured_prompts.append(json.get("prompt", ""))
            return _MockResp(200, {"response": "Привет"})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        tr.translate("Hello")
        assert captured_prompts
        assert "Medical drama series" in captured_prompts[0]

    def test_translate_temperature_passed(self, monkeypatch):
        monkeypatch.setattr(ts.requests, "get", _mock_get_tags)
        tr = ts.Translator(
            model="translategemma:4b",
            target_lang="Russian",
            ollama_url="http://fake",
            temperature=0.5,
        )
        captured_payloads = []

        def mock_post(url, json, timeout):
            captured_payloads.append(json)
            return _MockResp(200, {"response": "result"})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        tr.translate("Test")
        assert captured_payloads[0]["temperature"] == 0.5


# ---------------------------------------------------------------------------
# Translator.translate_batch — chunked JSON contract
# ---------------------------------------------------------------------------

class TestTranslateBatch:
    def _make_translator(self, monkeypatch):
        monkeypatch.setattr(ts.requests, "get", _mock_get_tags)
        return ts.Translator(
            model="translategemma:4b",
            target_lang="Russian",
            ollama_url="http://fake",
        )

    def test_batch_empty(self, monkeypatch):
        tr = self._make_translator(monkeypatch)
        assert tr.translate_batch([]) == []

    def test_batch_json_contract(self, monkeypatch):
        tr = self._make_translator(monkeypatch)
        segments = ["Hello", "World"]

        def mock_post(url, json=None, timeout=180):
            resp_data = {"response": json_mod.dumps({"segments": ["Привет", "Мир"]})}
            return _MockResp(200, resp_data)

        import json as json_mod
        monkeypatch.setattr(ts.requests, "post", mock_post)
        results = tr.translate_batch(segments, max_chars=5000)
        assert len(results) == 2

    def test_batch_fallback_on_failure(self, monkeypatch):
        tr = self._make_translator(monkeypatch)
        segments = ["Hello", "World"]

        def mock_post(url, json=None, timeout=180):
            return _MockResp(500, None, text_data="error")

        monkeypatch.setattr(ts.requests, "post", mock_post)
        results = tr.translate_batch(segments, max_chars=5000)
        assert results == segments

    def test_batch_chunking(self, monkeypatch):
        tr = self._make_translator(monkeypatch)
        segments = ["Hello world this is a long text", "Another long text segment here"]
        call_count = 0

        def mock_post(url, json=None, timeout=180):
            nonlocal call_count
            call_count += 1
            return _MockResp(200, {"response": "<<<SEG>>>Translated<<<ENDSEG>>>"})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        results = tr.translate_batch(segments, max_chars=10)
        assert call_count >= 2
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Translator — connection error during init
# ---------------------------------------------------------------------------

class TestTranslatorInit:
    def test_missing_model_exits(self, monkeypatch):
        def mock_get(url, timeout=5):
            return _MockResp(200, {"models": [{"name": "other-model"}]})

        monkeypatch.setattr(ts.requests, "get", mock_get)
        with pytest.raises(SystemExit):
            ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")

    def test_connection_error_exits(self, monkeypatch):
        import requests as real_requests

        def mock_get(url, timeout=5):
            raise real_requests.exceptions.ConnectionError("refused")

        monkeypatch.setattr(ts.requests, "get", mock_get)
        with pytest.raises(SystemExit):
            ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
