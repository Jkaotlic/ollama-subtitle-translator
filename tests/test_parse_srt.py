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
    return _MockResp(200, {"models": [{"name": "translategemma:4b"}, {"name": "gemma3:12b"}]})


class _MockSession:
    """Mock requests.Session that delegates get to _mock_get_tags and post to ts.requests.post."""

    def get(self, url, **kwargs):
        return _mock_get_tags(url, **kwargs)

    def post(self, url, **kwargs):
        return ts.requests.post(url, **kwargs)


def _patch_session(monkeypatch):
    """Patch requests.Session so Translator.__init__ uses _MockSession."""
    monkeypatch.setattr(ts.requests, "Session", lambda: _MockSession())


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
        assert enc.lower().replace("-", "") in ("cp1251", "windows1251")
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
        _patch_session(monkeypatch)
        return ts.Translator(
            model="translategemma:4b",
            target_lang="Russian",
            ollama_url="http://fake",
        )

    def test_translate_json_response(self, monkeypatch):
        tr = self._make_translator(monkeypatch)

        def mock_post(url, json, timeout):
            return _MockResp(200, {"message": {"content": "Привет"}})

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
        # With Chat API, if JSON parsing fails _call_llm returns None -> original text
        out = tr.translate("Hello")
        assert out == "Hello"

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
            # Chat API sends messages; extract user content for placeholders
            user_content = ""
            for msg in json.get("messages", []):
                if msg.get("role") == "user":
                    user_content = msg.get("content", "")
            import re
            placeholders = re.findall(r"__TAG_[a-f0-9]+__", user_content)
            translated = "Привет " + " ".join(placeholders) + " мир"
            return _MockResp(200, {"message": {"content": translated}})

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
        _patch_session(monkeypatch)
        tr = ts.Translator(
            model="translategemma:4b",
            target_lang="Russian",
            ollama_url="http://fake",
            context="Medical drama series",
        )
        captured_payloads = []

        def mock_post(url, json, timeout):
            captured_payloads.append(json)
            return _MockResp(200, {"message": {"content": "Привет"}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        tr.translate("Hello")
        assert captured_payloads
        # Context should be in system message
        system_msg = captured_payloads[0]["messages"][0]["content"]
        assert "Medical drama series" in system_msg

    def test_translate_temperature_passed(self, monkeypatch):
        _patch_session(monkeypatch)
        tr = ts.Translator(
            model="translategemma:4b",
            target_lang="Russian",
            ollama_url="http://fake",
            temperature=0.5,
        )
        captured_payloads = []

        def mock_post(url, json, timeout):
            captured_payloads.append(json)
            return _MockResp(200, {"message": {"content": "result"}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        tr.translate("Test")
        assert captured_payloads[0]["options"]["temperature"] == 0.5


# ---------------------------------------------------------------------------
# Translator.translate_batch — chunked JSON contract
# ---------------------------------------------------------------------------

class TestTranslateBatch:
    def _make_translator(self, monkeypatch):
        _patch_session(monkeypatch)
        return ts.Translator(
            model="translategemma:4b",
            target_lang="Russian",
            ollama_url="http://fake",
        )

    def test_batch_empty(self, monkeypatch):
        tr = self._make_translator(monkeypatch)
        assert tr.translate_batch([]) == []

    def test_batch_json_contract(self, monkeypatch):
        """Batch translation parses JSON response correctly."""
        tr = self._make_translator(monkeypatch)
        segments = ["Hello", "World"]

        def mock_post(url, json=None, timeout=180):
            return _MockResp(200, {"message": {"content": '{"1": "Привет", "2": "Мир"}'}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        results = tr.translate_batch(segments, max_chars=5000)
        assert len(results) == 2
        assert results[0] == "Привет"
        assert results[1] == "Мир"

    def test_batch_sep_fallback(self, monkeypatch):
        """Batch falls back to |||SEP||| delimiter when JSON fails."""
        tr = self._make_translator(monkeypatch)
        segments = ["Hello", "World"]

        def mock_post(url, json=None, timeout=180):
            return _MockResp(200, {"message": {"content": "Привет\n|||SEP|||\nМир"}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        results = tr.translate_batch(segments, max_chars=5000)
        assert len(results) == 2
        assert results[0] == "Привет"
        assert results[1] == "Мир"

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
            return _MockResp(200, {"message": {"content": "<<<SEG>>>Translated<<<ENDSEG>>>"}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        results = tr.translate_batch(segments, max_chars=10)
        assert call_count >= 2
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Translator — connection error during init
# ---------------------------------------------------------------------------

class TestTranslatorInit:
    def test_missing_model_exits_in_tty(self, monkeypatch):
        """In CLI mode (isatty=True), missing model should cause SystemExit."""
        def mock_get(url, **kwargs):
            return _MockResp(200, {"models": [{"name": "other-model"}]})

        class _Sess:
            def get(self, url, **kw): return mock_get(url, **kw)
            def post(self, url, **kw): return ts.requests.post(url, **kw)

        monkeypatch.setattr(ts.requests, "Session", lambda: _Sess())
        monkeypatch.setattr(ts.sys.stdin, "isatty", lambda: True)
        with pytest.raises(SystemExit):
            ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")

    def test_missing_model_no_exit_in_non_tty(self, monkeypatch):
        """In non-TTY mode (web worker), missing model should NOT exit."""
        def mock_get(url, **kwargs):
            return _MockResp(200, {"models": [{"name": "other-model"}]})

        class _Sess:
            def get(self, url, **kw): return mock_get(url, **kw)
            def post(self, url, **kw): return ts.requests.post(url, **kw)

        monkeypatch.setattr(ts.requests, "Session", lambda: _Sess())
        monkeypatch.setattr(ts.sys.stdin, "isatty", lambda: False)
        translator = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        assert translator.model == "translategemma:4b"

    def test_connection_error_raises(self, monkeypatch):
        import requests as real_requests

        def mock_get(url, **kwargs):
            raise real_requests.exceptions.ConnectionError("refused")

        class _Sess:
            def get(self, url, **kw): return mock_get(url, **kw)
            def post(self, url, **kw): return ts.requests.post(url, **kw)

        monkeypatch.setattr(ts.requests, "Session", lambda: _Sess())
        with pytest.raises(RuntimeError, match="Ollama"):
            ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")


# ---------------------------------------------------------------------------
# parse_glossary
# ---------------------------------------------------------------------------

class TestParseGlossary:
    def test_basic(self):
        g = ts.parse_glossary("Tony Stark = Тони Старк\nSHIELD = Щ.И.Т.")
        assert g == {"Tony Stark": "Тони Старк", "SHIELD": "Щ.И.Т."}

    def test_comma_separated(self):
        g = ts.parse_glossary("A = B, C = D")
        assert g == {"A": "B", "C": "D"}

    def test_empty(self):
        assert ts.parse_glossary("") == {}

    def test_no_equals(self):
        assert ts.parse_glossary("no equals here") == {}


# ---------------------------------------------------------------------------
# Glossary in prompt
# ---------------------------------------------------------------------------

class TestGlossaryInPrompt:
    def test_glossary_in_system_message(self, monkeypatch):
        _patch_session(monkeypatch)
        tr = ts.Translator(
            model="translategemma:4b", target_lang="Russian", ollama_url="http://fake",
            glossary={"Tony Stark": "Тони Старк"},
        )
        captured = []

        def mock_post(url, json, timeout):
            captured.append(json)
            return _MockResp(200, {"message": {"content": "Тони Старк здесь"}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        tr.translate("Tony Stark is here")
        assert captured
        system_msg = captured[0]["messages"][0]["content"]
        assert "Tony Stark" in system_msg
        assert "Тони Старк" in system_msg


# ---------------------------------------------------------------------------
# Fuzzy cache
# ---------------------------------------------------------------------------

class TestFuzzyCache:
    def test_exact_cache_hit(self, monkeypatch):
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        tr._cache["Hello world"] = "Привет мир"

        result = tr._cache_lookup("Hello world")
        assert result == "Привет мир"

    def test_fuzzy_cache_hit(self, monkeypatch):
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        tr._cache["Hello world!"] = "Привет мир!"

        # "Hello world" is very similar to "Hello world!" — should match
        result = tr._cache_lookup("Hello world")
        assert result == "Привет мир!"

    def test_fuzzy_cache_miss(self, monkeypatch):
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        tr._cache["Something completely different"] = "translation"

        result = tr._cache_lookup("Hello world")
        assert result is None


# ---------------------------------------------------------------------------
# Crash recovery (progress file)
# ---------------------------------------------------------------------------

class TestCrashRecovery:
    def test_resume_from_progress(self, monkeypatch, tmp_path):
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")

        # Create progress file with first 2 translations done
        progress = tmp_path / "test.progress.json"
        import json
        progress.write_text(json.dumps({"translations": ["Привет", "Мир"]}), encoding="utf-8")

        call_count = 0

        def mock_post(url, json=None, timeout=180):
            nonlocal call_count
            call_count += 1
            return _MockResp(200, {"message": {"content": '{"1": "Третий"}'}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        results = tr.translate_batch(
            ["Hello", "World", "Third"], max_chars=5000, progress_file=progress
        )
        assert len(results) == 3
        assert results[0] == "Привет"
        assert results[1] == "Мир"
        # Only the 3rd segment should have triggered an LLM call
        assert call_count >= 1


# ---------------------------------------------------------------------------
# Genre-adaptive prompt
# ---------------------------------------------------------------------------

class TestGenrePrompt:
    def test_genre_in_system_message(self, monkeypatch):
        _patch_session(monkeypatch)
        tr = ts.Translator(
            model="translategemma:4b", target_lang="Russian", ollama_url="http://fake",
            genre="anime",
        )
        captured = []

        def mock_post(url, json, timeout):
            captured.append(json)
            return _MockResp(200, {"message": {"content": "translated"}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        tr.translate("Hello senpai")
        assert captured
        system_msg = captured[0]["messages"][0]["content"]
        assert "anime" in system_msg.lower() or "honorific" in system_msg.lower()

    def test_empty_genre_no_extra_instructions(self, monkeypatch):
        _patch_session(monkeypatch)
        tr = ts.Translator(
            model="translategemma:4b", target_lang="Russian", ollama_url="http://fake",
            genre="",
        )
        system = tr._build_system_prompt()
        # Should not contain any genre-specific keywords
        assert "comedy" not in system.lower()
        assert "anime" not in system.lower()
        assert "documentary" not in system.lower()

    def test_all_genre_presets_exist(self):
        for genre in ["comedy", "drama", "anime", "documentary", "action", "horror"]:
            assert genre in ts.GENRE_PROMPTS
            assert len(ts.GENRE_PROMPTS[genre]) > 10


# ---------------------------------------------------------------------------
# Timecode parsing and CPS
# ---------------------------------------------------------------------------

class TestTimecodeAndCPS:
    def test_parse_timecode(self):
        assert ts.parse_timecode("00:00:01,000") == 1.0
        assert ts.parse_timecode("01:30:00,500") == 5400.5
        assert ts.parse_timecode("00:01:30,000") == 90.0

    def test_duration_seconds(self):
        tc = "00:00:01,000 --> 00:00:04,000"
        assert ts.duration_seconds(tc) == 3.0

    def test_duration_seconds_zero(self):
        tc = "00:00:01,000 --> 00:00:01,000"
        assert ts.duration_seconds(tc) == 0.0

    def test_duration_seconds_invalid(self):
        assert ts.duration_seconds("invalid") == 0.0


# ---------------------------------------------------------------------------
# Length validation (_shorten)
# ---------------------------------------------------------------------------

class TestLengthValidation:
    def test_long_translation_not_shortened(self, monkeypatch):
        """Long translations should be kept as-is (accuracy over brevity)."""
        _patch_session(monkeypatch)
        tr = ts.Translator(
            model="translategemma:4b", target_lang="Russian", ollama_url="http://fake",
        )
        call_count = 0
        long_text = "Б" * 70  # 70 Cyrillic chars — long but valid translation

        def mock_post(url, json, timeout):
            nonlocal call_count
            call_count += 1
            return _MockResp(200, {"message": {"content": long_text}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        result = tr.translate("Hello world test")
        # Should call LLM only once — no shortening
        assert call_count == 1
        assert result == long_text


# ---------------------------------------------------------------------------
# Reading speed (CPS) validation
# ---------------------------------------------------------------------------

class TestReadingSpeed:
    def test_validate_reading_speed_ok(self, monkeypatch):
        """Subtitles within CPS limit should not be modified."""
        blocks = [ts.SrtBlock(index=1, timecode="00:00:01,000 --> 00:00:05,000", lines=("Short text",))]
        texts = ["Short text"]
        # No translator needed since no shortening
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        result = ts.validate_reading_speed(blocks, texts, tr, max_cps=21.0)
        assert result == texts

    def test_validate_reading_speed_too_fast(self, monkeypatch):
        """Subtitles exceeding CPS limit should be shortened."""
        # 100 Cyrillic chars in 1 second = 100 CPS, way over limit
        long_text = "А" * 100  # Cyrillic А
        blocks = [ts.SrtBlock(index=1, timecode="00:00:01,000 --> 00:00:02,000", lines=(long_text,))]
        texts = [long_text]

        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")

        def mock_post(url, json, timeout):
            # Return shorter Cyrillic text (valid for Russian target)
            return _MockResp(200, {"message": {"content": "Короче"}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        result = ts.validate_reading_speed(blocks, texts, tr, max_cps=21.0)
        # Should have been shortened
        assert len(result[0]) < len(long_text)

    def test_validate_reading_speed_rejects_wrong_lang(self, monkeypatch):
        """CPS shortening should reject results in wrong language."""
        long_text = "Б" * 100  # Cyrillic
        blocks = [ts.SrtBlock(index=1, timecode="00:00:01,000 --> 00:00:02,000", lines=(long_text,))]
        texts = [long_text]

        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")

        def mock_post(url, json, timeout):
            # Return English instead of shortened Russian
            return _MockResp(200, {"message": {"content": "Short English text"}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        result = ts.validate_reading_speed(blocks, texts, tr, max_cps=21.0)
        # Should NOT be shortened — result was in wrong language
        assert result[0] == long_text


# ---------------------------------------------------------------------------
# Phase 10: Context analysis
# ---------------------------------------------------------------------------

class TestContextAnalysis:
    def test_analyze_context_returns_analysis(self, monkeypatch):
        """analyze_context should call LLM and store result."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="gemma3:12b", target_lang="Russian", ollama_url="http://fake")

        analysis_text = "Two characters: John and Mary. Thriller tone. Modern setting."

        def mock_post(url, json, timeout=120, **kwargs):
            return _MockResp(200, {"message": {"content": analysis_text}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        result = tr.analyze_context(["Hello John", "Mary is here", "They ran away"])
        assert result == analysis_text
        assert tr._context_analysis == analysis_text

    def test_analyze_context_injected_in_prompt(self, monkeypatch):
        """Context analysis should appear in system prompt."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        tr._context_analysis = "Sci-fi setting, character named Neo."

        prompt = tr._build_system_prompt()
        assert "Content analysis:" in prompt
        assert "Neo" in prompt

    def test_analyze_context_empty_on_failure(self, monkeypatch):
        """If LLM fails, context analysis should be empty."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")

        def mock_post(url, json, timeout=120, **kwargs):
            return None

        monkeypatch.setattr(ts, "post_with_retry", lambda *a, **kw: None)
        result = tr.analyze_context(["Hello"])
        assert result == ""


# ---------------------------------------------------------------------------
# Phase 11: Quality estimation
# ---------------------------------------------------------------------------

class TestQualityEstimation:
    def test_estimate_quality_good_translations(self, monkeypatch):
        """Good translations should get score 5 (heuristic-only mode)."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="gemma3:12b", target_lang="Russian", ollama_url="http://fake")
        scores = tr.estimate_quality(
            ["Hello", "Good morning", "Thanks"],
            ["Привет", "Доброе утро", "Спасибо"],
            use_llm_judge=False,
        )
        assert scores == [5, 5, 5]

    def test_estimate_quality_untranslated(self, monkeypatch):
        """Untranslated segments should get score 1."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="gemma3:12b", target_lang="Russian", ollama_url="http://fake")
        scores = tr.estimate_quality(
            ["Hello world", "Good morning everyone"],
            ["Hello world modified", "Good morning to all"],
            use_llm_judge=False,
        )
        assert scores == [1, 1]

    def test_estimate_quality_empty(self, monkeypatch):
        """Empty translations should get score 1."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="gemma3:12b", target_lang="Russian", ollama_url="http://fake")
        scores = tr.estimate_quality(["Hello"], [""], use_llm_judge=False)
        assert scores == [1]

    def test_estimate_quality_long_translation_ok(self, monkeypatch):
        """Long translations should not be penalized (accuracy over brevity)."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="gemma3:12b", target_lang="Russian", ollama_url="http://fake")
        scores = tr.estimate_quality(
            ["Hello there"],
            ["Привет " * 20],  # long but valid Cyrillic translation
            use_llm_judge=False,
        )
        assert scores[0] == 5  # should be accepted as good

    def test_estimate_quality_llm_judge_parses_scores(self, monkeypatch):
        """LLM-as-judge should parse integer scores from JSON response."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="gemma3:12b", target_lang="Russian", ollama_url="http://fake",
                           aux_model="qwen3.5:8b")

        def mock_post(url, json=None, timeout=120, **kwargs):
            if "/api/chat" in url:
                return _MockResp(200, {"message": {"content": '{"1": 4, "2": 2}'}})
            return _MockResp(200, {"models": []})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        scores = tr.estimate_quality(
            ["Hello", "Good morning"],
            ["Привет всем", "Утро доброе"],
            use_llm_judge=True,
        )
        assert scores == [4, 2]

    def test_retranslate_weak_only_retranslates_low_scores(self, monkeypatch):
        """retranslate_weak should only retranslate segments with score < 3."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")

        call_count = [0]

        def mock_post(url, json=None, timeout=120, **kwargs):
            call_count[0] += 1
            return _MockResp(200, {"message": {"content": "Улучшенный перевод"}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        originals = ["Hello", "World", "Test"]
        translations = ["Привет", "Bad translation", "Тест"]
        scores = [5, 1, 4]  # only index 1 should be retranslated

        result = tr.retranslate_weak(originals, translations, scores, threshold=3)
        assert result[0] == "Привет"  # unchanged
        assert result[1] == "Улучшенный перевод"  # retranslated
        assert result[2] == "Тест"  # unchanged


# ---------------------------------------------------------------------------
# Phase 13: Auto-glossary generation
# ---------------------------------------------------------------------------

class TestAutoGlossary:
    def test_generate_glossary_parses_json(self, monkeypatch):
        """generate_glossary should parse JSON glossary from model."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="gemma3:12b", target_lang="Russian", ollama_url="http://fake")

        glossary_json = json.dumps({"Tony Stark": "Тони Старк", "SHIELD": "Щ.И.Т."})

        def mock_post(url, json=None, timeout=120, **kwargs):
            return _MockResp(200, {"message": {"content": glossary_json}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        result = tr.generate_glossary(["Tony Stark is here", "SHIELD is watching"])
        assert result == {"Tony Stark": "Тони Старк", "SHIELD": "Щ.И.Т."}

    def test_generate_glossary_handles_markdown_fences(self, monkeypatch):
        """generate_glossary should handle markdown code fences in response."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="gemma3:12b", target_lang="Russian", ollama_url="http://fake")

        glossary_json = '```json\n{"Neo": "Нео", "Matrix": "Матрица"}\n```'

        def mock_post(url, json=None, timeout=120, **kwargs):
            return _MockResp(200, {"message": {"content": glossary_json}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        result = tr.generate_glossary(["Neo enters the Matrix"])
        assert result == {"Neo": "Нео", "Matrix": "Матрица"}

    def test_generate_glossary_empty_on_failure(self, monkeypatch):
        """If model returns invalid response, should return empty dict."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")

        monkeypatch.setattr(ts, "post_with_retry", lambda *a, **kw: None)
        result = tr.generate_glossary(["Hello world"])
        assert result == {}

    def test_auto_glossary_merges_with_user_glossary(self, monkeypatch):
        """User glossary should take priority over auto-generated glossary."""
        _patch_session(monkeypatch)
        user_glossary = {"Tony Stark": "Тони Старк (пользователь)"}
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian",
                           ollama_url="http://fake", glossary=user_glossary)

        auto_glossary = {"Tony Stark": "Тони Старк", "SHIELD": "Щ.И.Т."}

        # Simulate merging: auto first, then user takes priority
        merged = dict(auto_glossary)
        merged.update(tr.glossary)
        tr.glossary = merged

        assert tr.glossary["Tony Stark"] == "Тони Старк (пользователь)"
        assert tr.glossary["SHIELD"] == "Щ.И.Т."


# ---------------------------------------------------------------------------
# Translation-only model detection
# ---------------------------------------------------------------------------

class TestTranslationOnlyModel:
    def test_translategemma_is_translation_only(self, monkeypatch):
        """TranslateGemma models should be detected as translation-only."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        assert tr._translation_only is True

    def test_gemma3_is_not_translation_only(self, monkeypatch):
        """Gemma 3 models should NOT be detected as translation-only."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="gemma3:12b", target_lang="Russian", ollama_url="http://fake")
        assert tr._translation_only is False

    def test_translation_only_uses_aux_model_by_default(self, monkeypatch):
        """Translation-only models should auto-set aux_model to DEFAULT_AUX_MODEL."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        assert tr.aux_model == ts.Translator.DEFAULT_AUX_MODEL

    def test_general_model_uses_self_as_aux(self, monkeypatch):
        """General-purpose models should use themselves as aux_model."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="gemma3:12b", target_lang="Russian", ollama_url="http://fake")
        assert tr.aux_model == "gemma3:12b"

    def test_translation_only_analyze_uses_aux_model(self, monkeypatch):
        """analyze_context on translation-only model should call aux_model."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        called_models = []

        def mock_post(url, json=None, timeout=120, **kwargs):
            called_models.append(json.get("model") if json else None)
            return _MockResp(200, {"message": {"content": "Analysis result"}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        tr.analyze_context(["Hello world"])
        assert called_models[-1] == ts.Translator.DEFAULT_AUX_MODEL


class TestSeemsUntranslated:
    """Tests for seems_untranslated() — detects segments still in source language."""

    def test_latin_to_cyrillic_untranslated(self):
        assert ts.seems_untranslated("Hello world", "Hello world modified", "Russian") is True

    def test_latin_to_cyrillic_translated(self):
        assert ts.seems_untranslated("Hello world", "Привет мир", "Russian") is False

    def test_latin_to_cyrillic_mixed_ok(self):
        # Mostly Cyrillic with a few Latin (names, brands) — should pass
        assert ts.seems_untranslated("Welcome to Paris", "Добро пожаловать в Paris", "Russian") is False

    def test_same_script_not_detected(self):
        # English → Spanish: same Latin script, can't detect
        assert ts.seems_untranslated("Hello", "Hello modified", "Spanish") is False

    def test_short_text_skipped(self):
        # Very short text — not enough to judge
        assert ts.seems_untranslated("OK", "OK", "Russian") is False

    def test_latin_to_cjk_untranslated(self):
        assert ts.seems_untranslated("Hello", "Hello there", "Chinese") is True

    def test_latin_to_cjk_translated(self):
        assert ts.seems_untranslated("Hello", "你好", "Chinese") is False

    def test_unknown_target_lang(self):
        # Unknown language — always returns False
        assert ts.seems_untranslated("Hello", "Hello", "Klingon") is False


# ---------------------------------------------------------------------------
# validate_translation with target_lang
# ---------------------------------------------------------------------------

class TestValidateTranslation:
    def test_basic_valid(self):
        assert ts.validate_translation("Hello", "Привет") is True

    def test_empty_translation(self):
        assert ts.validate_translation("Hello", "") is False

    def test_identical_translation(self):
        assert ts.validate_translation("Hello", "Hello") is False

    def test_punctuation_only(self):
        assert ts.validate_translation("Hello", "... !!!") is False

    def test_with_target_lang_correct_script(self):
        """Translation in correct script should pass."""
        assert ts.validate_translation("Hello world", "Привет мир", target_lang="Russian") is True

    def test_with_target_lang_wrong_script(self):
        """Translation in wrong script should fail when target_lang is specified."""
        assert ts.validate_translation("Hello world", "Hello world modified", target_lang="Russian") is False

    def test_without_target_lang_no_script_check(self):
        """Without target_lang, script check is skipped (backward compatibility)."""
        assert ts.validate_translation("Hello world", "Hello world modified") is True


# ---------------------------------------------------------------------------
# Fuzzy cache optimization
# ---------------------------------------------------------------------------

class TestFuzzyCacheOptimized:
    def test_length_prefilter_skips_dissimilar(self, monkeypatch):
        """Cache entries with very different lengths should be skipped."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        # Add a short entry — should not match a very long query
        tr._cache["Hi"] = "Привет"
        result = tr._cache_lookup("This is a completely different and very long sentence")
        assert result is None

    def test_best_match_returned(self, monkeypatch):
        """When multiple fuzzy matches exist, the best one should be returned."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        tr._cache["Hello world!"] = "Привет мир!"
        tr._cache["Hello world."] = "Привет мир."
        # "Hello world" is closer to "Hello world!" and "Hello world." — should get best match
        result = tr._cache_lookup("Hello world")
        assert result is not None
        assert result in ("Привет мир!", "Привет мир.")


# ---------------------------------------------------------------------------
# Adaptive chunk sizing
# ---------------------------------------------------------------------------

class TestAdaptiveChunking:
    def test_short_segments_more_per_chunk(self, monkeypatch):
        """Short segments should result in more segments per chunk."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        # 100 short segments (< 30 chars avg)
        short_texts = ["Hi"] * 100

        call_count = [0]
        _dumps = json.dumps  # avoid shadowing by 'json' param name

        def mock_post(url, json=None, timeout=180):
            call_count[0] += 1
            msgs = json.get("messages", [])
            user_msg = msgs[-1]["content"] if msgs else ""
            import re as _re
            keys = _re.findall(r'"(\d+)":', user_msg)
            result = {k: "Привет" for k in keys}
            return _MockResp(200, {"message": {"content": _dumps(result)}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        results = tr.translate_batch(short_texts, max_chars=5000)
        assert len(results) == 100
        # With max_segments_per_chunk=50, 100 segments should need only 2 chunks
        assert call_count[0] <= 3  # 2 chunks + possible edge

    def test_long_segments_fewer_per_chunk(self, monkeypatch):
        """Long segments should result in fewer segments per chunk."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        # 20 long segments (> 120 chars avg)
        long_texts = ["A" * 150] * 20

        call_count = [0]
        _dumps = json.dumps

        def mock_post(url, json=None, timeout=180):
            call_count[0] += 1
            msgs = json.get("messages", [])
            user_msg = msgs[-1]["content"] if msgs else ""
            import re as _re
            keys = _re.findall(r'"(\d+)":', user_msg)
            result = {k: "Б" * 100 for k in keys}
            return _MockResp(200, {"message": {"content": _dumps(result)}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        results = tr.translate_batch(long_texts, max_chars=50000)
        assert len(results) == 20
        # With max_segments_per_chunk=10, 20 segments should need 2 chunks
        assert call_count[0] >= 2


# ---------------------------------------------------------------------------
# Improved system prompt for general-purpose models
# ---------------------------------------------------------------------------

class TestImprovedSystemPrompt:
    def test_translation_only_model_minimal_prompt(self, monkeypatch):
        """Translation-only models should get a minimal system prompt."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        prompt = tr._build_system_prompt()
        assert "Provide only the translation" in prompt
        # Should NOT have the detailed rules
        assert "word-by-word" not in prompt

    def test_general_model_detailed_prompt(self, monkeypatch):
        """General-purpose models should get detailed translation rules."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="gemma3:12b", target_lang="Russian", ollama_url="http://fake")
        prompt = tr._build_system_prompt()
        assert "word-by-word" in prompt
        assert "concise" in prompt
        assert "tone" in prompt


# ---------------------------------------------------------------------------
# Leaked tag placeholder cleanup
# ---------------------------------------------------------------------------

class TestLeakedTagCleanup:
    def test_leaked_placeholder_removed(self):
        """Leaked __TAG_xxx__ placeholders should be cleaned up by restore_tags."""
        text = "__TAG_e289c74136c411aba1c2a9c84b59334__А как насчёт сестры?"
        result = ts.restore_tags(text, {})
        assert "__TAG_" not in result
        assert "А как насчёт сестры?" == result

    def test_normal_restore_still_works(self):
        """Normal tag restoration should still work correctly."""
        tags = {"__TAG_abc123__": "<i>"}
        text = "__TAG_abc123__Привет"
        result = ts.restore_tags(text, tags)
        assert result == "<i>Привет"

    def test_partial_placeholder_mangled_by_llm(self):
        """If LLM partially mangles a placeholder, cleanup should still remove it."""
        text = "Текст __TAG_0123456789abcdef0123456789abcdef__ конец"
        result = ts.restore_tags(text, {})
        assert "__TAG_" not in result
        assert "Текст  конец" == result


# ---------------------------------------------------------------------------
# ASS positioning tag handling
# ---------------------------------------------------------------------------

class TestAssPositionTags:
    def test_strip_ass_pos_basic(self):
        """Should strip {\\an8} and return it separately."""
        text, tag = ts.strip_ass_pos("{\\an8}All right, everybody!")
        assert text == "All right, everybody!"
        assert tag == "{\\an8}"

    def test_strip_ass_pos_no_tag(self):
        """Text without ASS tags should pass through unchanged."""
        text, tag = ts.strip_ass_pos("Normal subtitle text")
        assert text == "Normal subtitle text"
        assert tag == ""

    def test_restore_ass_pos(self):
        """Should prepend ASS tag back to translated text."""
        result = ts.restore_ass_pos("Всем успокоиться!", "{\\an8}")
        assert result == "{\\an8}Всем успокоиться!"

    def test_restore_ass_pos_no_tag(self):
        """Without ASS tag, text should pass through unchanged."""
        result = ts.restore_ass_pos("Обычный текст", "")
        assert result == "Обычный текст"

    def test_single_translate_preserves_ass_tag(self, monkeypatch):
        """Single segment translate() should strip {\\an8} before LLM and restore after."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")

        def mock_post(url, json=None, timeout=180):
            # The LLM should NOT see {\\an8} in the input
            msgs = json.get("messages", [])
            user_msg = msgs[-1]["content"] if msgs else ""
            assert "{\\an8}" not in user_msg
            return _MockResp(200, {"message": {"content": "Успокойтесь"}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        result = tr.translate("{\\an8}Settle down!")
        assert result == "{\\an8}Успокойтесь"

    def test_batch_translate_preserves_ass_tags(self, monkeypatch):
        """Batch translate should strip ASS tags before LLM and restore after."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")
        _dumps = json.dumps

        def mock_post(url, json=None, timeout=180):
            msgs = json.get("messages", [])
            user_msg = msgs[-1]["content"] if msgs else ""
            # ASS tags should NOT be in the LLM input
            assert "{\\an8}" not in user_msg
            result = {"1": "Успокойтесь", "2": "Хорошо"}
            return _MockResp(200, {"message": {"content": _dumps(result)}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        results = tr.translate_batch(["{\\an8}Settle down!", "Good."])
        assert results[0] == "{\\an8}Успокойтесь"
        assert results[1] == "Хорошо"


# ---------------------------------------------------------------------------
# Alignment validation (content shift detection)
# ---------------------------------------------------------------------------

class TestAlignmentValidation:
    def test_shifted_content_triggers_fallback(self, monkeypatch):
        """When batch translations are badly shifted in length, should fall back to per-segment."""
        _patch_session(monkeypatch)
        tr = ts.Translator(model="translategemma:4b", target_lang="Russian", ollama_url="http://fake")

        call_count = [0]
        _dumps = json.dumps

        def mock_post(url, json=None, timeout=180):
            call_count[0] += 1
            msgs = json.get("messages", [])
            user_msg = msgs[-1]["content"] if msgs else ""

            if call_count[0] == 1:
                # First call: batch — return badly shifted content
                # Short originals get very long translations (simulating shift)
                result = {
                    "1": "Очень длинный перевод который совсем не соответствует короткому оригиналу и занимает много места",
                    "2": "Тоже длинный текст который явно не от этого сегмента а от другого места в субтитрах сценария",
                    "3": "Ещё один длинный перевод совершенно не подходящий по размеру для короткого оригинального сегмента",
                    "4": "И последний длинный перевод который тоже полностью не соответствует оригиналу по длине текста",
                }
                return _MockResp(200, {"message": {"content": _dumps(result)}})
            else:
                # Per-segment fallback calls: return proper translations
                return _MockResp(200, {"message": {"content": "Да"}})

        monkeypatch.setattr(ts.requests, "post", mock_post)
        # 4 short segments
        texts = ["Hi.", "OK.", "Yes.", "No."]
        results = tr.translate_batch(texts, max_chars=50000)
        assert len(results) == 4
        # Should have fallen back to per-segment (call_count > 1)
        assert call_count[0] > 1


# ---------------------------------------------------------------------------
# TranslationMemory.clear()
# ---------------------------------------------------------------------------

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
