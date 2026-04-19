#!/usr/bin/env python3
"""
🎬 Переводчик субтитров (.srt) через Ollama
Поддерживает современные Ollama модели: Gemma 4 (рекомендуется), Qwen 3.5,
Hunyuan-MT, Llama 4 Scout; + legacy translation-only модели (NLLB, ALMA, Tower, TranslateGemma).

Поддерживает множество языков: русский, английский, китайский, японский, корейский,
немецкий, французский, испанский, итальянский, португальский и др.

Установка:
  1. Установить Ollama: https://ollama.com/download
  2. ollama pull gemma4:e12b
  3. pip install requests

Примеры:
  python translate_srt.py movie.srt                    # EN→RU (по умолчанию)
  python translate_srt.py movie.srt -l Japanese        # EN→JP
  python translate_srt.py movie.srt -l German -o de.srt
"""

import argparse
import difflib
import json as _json
import re
import sqlite3
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional, Dict
import time
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("translate_srt")

try:
    import requests
except ImportError:
    print("❌ Не установлен requests: pip install requests")
    sys.exit(1)


# Поддерживаемые языки. Единый источник истины — остальные модули используют
# SUPPORTED_LANGUAGES (для UI) и get_language_code() (для нормализации имени → код).
# Ключ — каноничное English display-имя; значение — ISO 639-1 код.
SUPPORTED_LANGUAGES: Dict[str, str] = {
    "Russian": "ru",
    "English": "en",
    "Chinese": "zh",
    "Japanese": "ja",
    "Korean": "ko",
    "German": "de",
    "French": "fr",
    "Spanish": "es",
    "Italian": "it",
    "Portuguese": "pt",
    "Turkish": "tr",
    "Arabic": "ar",
    "Thai": "th",
    "Vietnamese": "vi",
    "Polish": "pl",
    "Dutch": "nl",
    "Ukrainian": "uk",
}

# Aliases: все варианты написания, включая русские, локализованные и 2-буквенные коды.
_LANGUAGE_ALIASES: Dict[str, str] = {
    "русский": "ru", "английский": "en", "китайский": "zh", "японский": "ja",
    "корейский": "ko", "немецкий": "de", "французский": "fr", "испанский": "es",
    "итальянский": "it", "португальский": "pt", "турецкий": "tr", "арабский": "ar",
    "тайский": "th", "вьетнамский": "vi", "польский": "pl", "голландский": "nl",
    "украинский": "uk",
}


def get_language_code(name: str) -> str:
    """Normalize any language name (English display, localized, or 2-letter code) → ISO code.

    Unknown names fall back to the first two lowercase letters (best-effort).
    """
    if not name:
        return "xx"
    key = name.strip().lower()
    # 1) Match against display names (case-insensitive)
    for display, code in SUPPORTED_LANGUAGES.items():
        if display.lower() == key or code == key:
            return code
    # 2) Localized aliases
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]
    # 3) Fallback — first 2 chars
    return key[:2]


# Backwards compatibility alias: old code used LANGUAGES = {name: code, ...}
# with many keys. Keep a flat dict that covers all forms we accept.
LANGUAGES: Dict[str, str] = {}
for _display, _code in SUPPORTED_LANGUAGES.items():
    LANGUAGES[_display.lower()] = _code   # "russian"
    LANGUAGES[_code] = _code              # "ru"
for _alias, _code in _LANGUAGE_ALIASES.items():
    LANGUAGES[_alias] = _code

# Регулярка для таймкодов SRT
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}.*$")
# Регулярка для HTML-тегов (сохраняем их)
TAG_RE = re.compile(r"(<[^>]+>|{\\\w+\d*})")
# ASS positioning tags like {\an8} — strip before translation, re-add after
ASS_POS_RE = re.compile(r'\{\\an\d\}')
# Leaked tag placeholder pattern
LEAKED_TAG_RE = re.compile(r'__TAG_[0-9a-f]+__')

# Genre presets for adaptive prompts
GENRE_PROMPTS = {
    "": "",  # default — no extra instructions
    "comedy": (
        "This is a comedy. Preserve humor, jokes, wordplay, and comedic timing. "
        "Use informal, lively tone. Adapt puns to work in the target language."
    ),
    "drama": (
        "This is a drama. Maintain emotional weight and natural dialogue flow. "
        "Preserve tone shifts and character voice."
    ),
    "anime": (
        "This is anime. Keep Japanese honorifics (-san, -kun, -chan, -sama, -sensei, -senpai) "
        "as-is. Preserve cultural references. Use casual tone for informal speech."
    ),
    "documentary": (
        "This is a documentary. Use formal, precise language. "
        "Maintain factual accuracy and academic tone. Preserve proper nouns and technical terms."
    ),
    "action": (
        "This is an action film. Keep dialogue punchy and concise. "
        "Preserve intensity and urgency of exclamations."
    ),
    "horror": (
        "This is a horror film. Maintain suspense, tension, and eerie tone. "
        "Preserve whispers, screams, and dramatic pauses."
    ),
}


def post_with_retry(url: str, json: dict, timeout: int = 120, attempts: int = 3,
                    backoff: float = 1.0, session: Optional[requests.Session] = None,
                    max_backoff: float = 60.0) -> Optional[requests.Response]:
    """POST with exponential backoff retry.

    Connection errors (Ollama down/restarting) get extra retry cycles
    with longer backoff to wait for reconnection. Returns Response or None.
    """
    do_post = session.post if session else requests.post
    last_exc = None
    # Connection errors get more attempts to wait for Ollama restart
    connection_bonus_attempts = 10
    total_attempts = attempts
    attempt = 0

    while attempt < total_attempts:
        attempt += 1
        t0 = time.time()
        try:
            resp = do_post(url, json=json, timeout=timeout)
            elapsed = time.time() - t0
            if attempt > 1:
                logger.info("POST %s attempt=%d status=%d elapsed=%.2fs (recovered)", url, attempt, resp.status_code, elapsed)
            else:
                logger.info("POST %s status=%d elapsed=%.2fs", url, resp.status_code, elapsed)
            return resp
        except (requests.ConnectionError, ConnectionRefusedError, ConnectionResetError) as e:
            # Connection-level failure — Ollama might be restarting
            elapsed = time.time() - t0
            last_exc = e
            # Extend total attempts on first connection error
            if total_attempts == attempts:
                total_attempts = attempts + connection_bonus_attempts
                logger.warning("POST %s connection lost, extending retries to %d", url, total_attempts)
            sleep_time = min(backoff * (2 ** (attempt - 1)), max_backoff)
            logger.warning("POST %s attempt=%d/%d connection error (%.2fs): %s — waiting %.1fs",
                           url, attempt, total_attempts, elapsed, type(e).__name__, sleep_time)
            time.sleep(sleep_time)
        except requests.RequestException as e:
            # Other errors (timeout, encoding, etc.) — normal retry
            elapsed = time.time() - t0
            last_exc = e
            sleep_time = min(backoff * (2 ** (attempt - 1)), max_backoff)
            logger.warning("POST %s attempt=%d/%d failed (%.2fs): %s — retrying in %.1fs",
                           url, attempt, total_attempts, elapsed, e, sleep_time)
            time.sleep(sleep_time)

    logger.error("post_with_retry exhausted %d attempts to %s: %s", total_attempts, url, last_exc)
    return None


class TranslationMemory:
    """Persistent SQLite-backed cache keyed by (source, target_lang, model).

    Survives across sessions — great for translating a whole series with
    consistent character names, recurring phrases, and glossary terms.
    Thread-safe (used by parallel chunk workers).
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        # WAL mode improves concurrency — readers don't block writers
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as e:
            logger.debug("TM WAL mode not available: %s", e)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tm (
                source TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                model TEXT NOT NULL,
                translation TEXT NOT NULL,
                hits INTEGER DEFAULT 0,
                created_at REAL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (source, target_lang, model)
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tm_lang ON tm(target_lang)")
        self._conn.commit()
        self._lock = threading.Lock()

    def lookup(self, source: str, target_lang: str, model: str) -> Optional[str]:
        key = source.strip()
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT translation FROM tm WHERE source=? AND target_lang=? AND model=?",
                (key, target_lang, model),
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE tm SET hits = hits + 1 WHERE source=? AND target_lang=? AND model=?",
                    (key, target_lang, model),
                )
                self._conn.commit()
                return row[0]
            return None

    def store(self, source: str, target_lang: str, model: str, translation: str) -> None:
        key = source.strip()
        if not key or not translation:
            return
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO tm(source, target_lang, model, translation) VALUES(?,?,?,?)",
                    (key, target_lang, model, translation),
                )
                self._conn.commit()
            except sqlite3.Error as e:
                logger.warning("TM store failed: %s", e)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM tm"
            ).fetchone()
            return {"entries": row[0] or 0, "total_hits": row[1] or 0}

    def prune(self, max_entries: int = 100_000) -> int:
        """Trim TM to max_entries, removing least-used (hits ASC), oldest-first.

        Prevents disk DoS when TM grows unboundedly over many sessions.
        Returns deleted count.
        """
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM tm").fetchone()[0]
            if total <= max_entries:
                return 0
            to_delete = total - max_entries
            self._conn.execute(
                "DELETE FROM tm WHERE rowid IN (SELECT rowid FROM tm ORDER BY hits ASC, created_at ASC LIMIT ?)",
                (to_delete,),
            )
            self._conn.commit()
            logger.info("TM prune: removed %d entries (kept %d)", to_delete, max_entries)
            return to_delete

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

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass


@dataclass
class SrtBlock:
    """Один блок субтитров."""
    index: int
    timecode: str
    lines: Tuple[str, ...]

    def text(self) -> str:
        return "\n".join(self.lines)


def read_srt_file(path: Path) -> Tuple[str, str]:
    """Читает файл с автоопределением кодировки (chardet → fallback цепочка)."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    # Пробуем chardet если доступен
    try:
        import chardet
        detected = chardet.detect(raw)
        enc = detected.get("encoding")
        if enc:
            logger.info("chardet detected encoding=%s confidence=%.2f", enc, detected.get("confidence", 0))
            return raw.decode(enc), enc
    except ImportError:
        pass
    except (UnicodeDecodeError, LookupError):
        pass
    # Fallback на распространённые кодировки субтитров
    for enc in ("cp1251", "latin-1", "iso-8859-2", "shift_jis"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    # Крайний fallback
    return raw.decode("utf-8", errors="replace"), "utf-8"


def parse_srt(text: str) -> List[SrtBlock]:
    """Парсит SRT текст в список блоков."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    
    blocks: List[SrtBlock] = []
    i = 0
    n = len(lines)
    
    while i < n:
        while i < n and lines[i].strip() == "":
            i += 1
        if i >= n:
            break

        idx_line = lines[i].strip()
        if not idx_line.isdigit():
            i += 1
            continue
        index = int(idx_line)
        i += 1
        
        if i >= n:
            break

        timecode = lines[i].strip()
        if not TIME_RE.match(timecode):
            logger.warning("parse_srt: skipping invalid timecode near block %d: %r",
                           index if 'index' in locals() else -1, timecode[:80])
            i += 1
            continue
        i += 1

        text_lines: List[str] = []
        while i < n and lines[i].strip() != "":
            text_lines.append(lines[i])
            i += 1

        blocks.append(SrtBlock(index=index, timecode=timecode, lines=tuple(text_lines)))
        i += 1

    return blocks


def write_srt(blocks: List[SrtBlock], path: Path, encoding: str) -> None:
    """Записывает блоки в SRT файл."""
    out_lines: List[str] = []
    for b in blocks:
        out_lines.append(str(b.index))
        out_lines.append(b.timecode)
        out_lines.extend(b.lines)
        out_lines.append("")
    path.write_text("\n".join(out_lines).rstrip("\n") + "\n", encoding=encoding)


def protect_tags(text: str) -> Tuple[str, Dict[str, str]]:
    """Защищает теги от перевода, возвращает защищённый текст и map placeholder->tag."""
    tags: Dict[str, str] = {}

    def replacer(match):
        key = f"__TAG_{uuid.uuid4().hex}__"
        tags[key] = match.group(0)
        return key

    protected = TAG_RE.sub(replacer, text)
    return protected, tags


def restore_tags(text: str, tags: Dict[str, str]) -> str:
    """Восстанавливает теги из словаря плейсхолдер->оригинал."""
    for k, v in tags.items():
        text = text.replace(k, v)
    # Cleanup any leaked __TAG_xxx__ placeholders that the LLM mangled
    text = LEAKED_TAG_RE.sub('', text)
    return text


def strip_ass_pos(text: str) -> Tuple[str, str]:
    """Strip ASS positioning tags like {\\an8} from text.
    Returns (cleaned_text, ass_tag) where ass_tag is the first match or ''."""
    m = ASS_POS_RE.search(text)
    if m:
        return ASS_POS_RE.sub('', text).strip(), m.group()
    return text, ''


def restore_ass_pos(text: str, ass_tag: str) -> str:
    """Re-add ASS positioning tag to translated text."""
    if ass_tag:
        return ass_tag + text
    return text


def parse_timecode(tc: str) -> float:
    """Convert SRT timecode 'HH:MM:SS,mmm' to seconds."""
    tc = tc.strip().replace(",", ".")
    parts = tc.split(":")
    if len(parts) != 3:
        return 0.0
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except (ValueError, IndexError):
        return 0.0


def duration_seconds(timecode_line: str) -> float:
    """Calculate display duration from SRT timecode line 'HH:MM:SS,mmm --> HH:MM:SS,mmm'."""
    parts = timecode_line.split("-->")
    if len(parts) != 2:
        return 0.0
    start = parse_timecode(parts[0])
    end = parse_timecode(parts[1])
    return max(0.0, end - start)


# ---------------------------------------------------------------------------
# Script detection — used to catch untranslated segments after batch
# ---------------------------------------------------------------------------

_SCRIPT_RANGES = {
    'cyrillic': [(0x0400, 0x04FF)],
    'latin':    [(0x0041, 0x005A), (0x0061, 0x007A), (0x00C0, 0x024F)],
    'cjk':      [(0x4E00, 0x9FFF), (0x3400, 0x4DBF)],
    'hangul':   [(0xAC00, 0xD7AF)],
    'arabic':   [(0x0600, 0x06FF)],
    'devanagari': [(0x0900, 0x097F)],
    'hiragana': [(0x3040, 0x309F)],
    'katakana': [(0x30A0, 0x30FF)],
    'thai':     [(0x0E00, 0x0E7F)],
    'greek':    [(0x0370, 0x03FF)],
    'hebrew':   [(0x0590, 0x05FF)],
}

# Languages that use a distinct non-Latin script
_LANG_TO_SCRIPT = {
    'russian': 'cyrillic', 'ukrainian': 'cyrillic', 'bulgarian': 'cyrillic',
    'serbian': 'cyrillic', 'belarusian': 'cyrillic',
    'chinese': 'cjk', 'mandarin': 'cjk',
    'japanese': 'cjk',
    'korean': 'hangul',
    'arabic': 'arabic',
    'hindi': 'devanagari',
    'thai': 'thai',
    'greek': 'greek',
    'hebrew': 'hebrew',
}


def _count_script(text: str, script: str) -> int:
    """Count characters that belong to *script* in *text*."""
    ranges = _SCRIPT_RANGES.get(script, [])
    count = 0
    for ch in text:
        cp = ord(ch)
        for lo, hi in ranges:
            if lo <= cp <= hi:
                count += 1
                break
    return count


def seems_untranslated(original: str, translated: str, target_lang: str) -> bool:
    """Return True when *translated* appears to still be in the source language.

    Only works when source and target use different scripts (e.g. Latin → Cyrillic).
    For same-script pairs (English → Spanish) always returns False.
    """
    expected_script = _LANG_TO_SCRIPT.get(target_lang.lower())
    if not expected_script:
        return False  # same-script or unknown target — can't detect

    # Only check texts with enough alphabetic characters
    alpha_count = sum(1 for c in translated if c.isalpha())
    if alpha_count < 3:
        return False

    target_chars = _count_script(translated, expected_script)
    # If less than 30% of alphabetic characters are in the target script → untranslated
    return target_chars < alpha_count * 0.3


def validate_translation(original: str, translated: str, target_lang: str = "") -> bool:
    """Check if translated text looks reasonable compared to original.

    Returns True if the translation passes basic quality checks.
    When target_lang is provided, also checks that the translation is in the correct script.
    """
    if not translated or not translated.strip():
        return False
    # Translation is identical to source (model didn't translate)
    if translated.strip() == original.strip():
        return False
    # Translation is just punctuation or whitespace
    stripped = re.sub(r'[\s\W]+', '', translated)
    if not stripped:
        return False
    # Check target language script (catches untranslated segments early)
    if target_lang and seems_untranslated(original, translated, target_lang):
        return False
    return True


class Translator:
    """Переводчик через Ollama + Translating Gemma"""

    # Default auxiliary model for analysis/glossary/QE (general-purpose, reasoning-capable)
    DEFAULT_AUX_MODEL = "qwen3.5:8b"

    def __init__(self, model: str = "gemma4:e12b", target_lang: str = "Russian",
                 ollama_url: str = "http://127.0.0.1:11434", context: str = "",
                 temperature: float = 0.0, source_lang: str = "",
                 two_pass: bool = False, review_model: str = "",
                 glossary: Optional[Dict[str, str]] = None,
                 context_window: int = 3,
                 genre: str = "",
                 aux_model: str = "",
                 tm_path: Optional[Path] = None):
        self.model = model
        self.target_lang = target_lang
        self.source_lang = source_lang  # пустая строка = автоопределение
        self.base_url = ollama_url
        self.context = context
        self.temperature = float(temperature)
        # Persistent HTTP session for connection pooling (reuses TCP connections)
        self._session = requests.Session()

        self.two_pass = two_pass
        self.review_model = review_model or model  # по умолчанию та же модель
        self.glossary = glossary or {}
        self.context_window = max(1, int(context_window))
        self.genre = genre.lower().strip()
        self._context_analysis: str = ""
        self._translation_only = self._is_translation_only_model(model)
        # Auxiliary model for analysis/glossary/QE tasks
        # If translation model is translation-only, use aux_model for smart tasks
        if aux_model:
            self.aux_model = aux_model
        elif self._translation_only:
            self.aux_model = self.DEFAULT_AUX_MODEL
        else:
            self.aux_model = model  # general-purpose model can do everything
        self._cache: Dict[str, str] = {}
        self._cache_hits = 0
        self._fuzzy_threshold = 0.9  # minimum similarity ratio for fuzzy cache match
        # Persistent translation memory (optional)
        self._tm: Optional[TranslationMemory] = None
        if tm_path is not None:
            try:
                self._tm = TranslationMemory(tm_path)
                logger.info("Translation Memory loaded: %s (%s)", tm_path, self._tm.stats())
                # Cap TM growth once per session — prevents unbounded disk growth
                try:
                    self._tm.prune()
                except sqlite3.Error as e:
                    logger.debug("TM prune skipped: %s", e)
            except sqlite3.Error as e:
                logger.warning("Failed to open Translation Memory at %s: %s", tm_path, e)

        # Quick connectivity check (model availability already verified by web UI)
        try:
            resp = self._session.get(f"{self.base_url}/api/tags", timeout=3)
            if resp.status_code != 200:
                raise Exception("Ollama не отвечает")
            # When called from CLI, verify model exists
            if sys.stdin and sys.stdin.isatty():
                available = [m["name"] for m in resp.json().get("models", [])]
                if not any(model in m for m in available):
                    print(f"⚠️  Модель {model} не найдена. Доступные: {available}")
                    print(f"   Запустите: ollama pull {model}")
                    sys.exit(1)
                if self.two_pass and self.review_model != model:
                    if not any(self.review_model in m for m in available):
                        print(f"⚠️  Review-модель {self.review_model} не найдена.")
                        sys.exit(1)
                # Check aux model availability
                if self.aux_model != model and not any(self.aux_model in m for m in available):
                    print(f"⚠️  Вспомогательная модель {self.aux_model} не найдена.")
                    print(f"   Запустите: ollama pull {self.aux_model}")
                    print(f"   (нужна для анализа контекста, глоссария, оценки качества)")
                    self.aux_model = model  # fallback to main model
        except requests.exceptions.ConnectionError:
            raise RuntimeError("Ollama не запущен! Запустите: ollama serve")

        logger.info("Translator ready: model=%s aux=%s lang=%s two_pass=%s glossary_entries=%d",
                     model, self.aux_model, target_lang, two_pass, len(self.glossary))

    def close(self) -> None:
        """Release external resources (TM SQLite connection, HTTP session)."""
        if self._tm is not None:
            self._tm.close()
            self._tm = None
        try:
            self._session.close()
        except Exception:
            pass

    def __enter__(self) -> "Translator":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @staticmethod
    def _is_translation_only_model(model_name: str) -> bool:
        """Detect models that can ONLY translate and cannot perform analysis/glossary/QE tasks.

        Note: modern general-purpose models (Gemma 4, Qwen 3.5, Llama 4) handle translation + analysis + QE
        equally well, so aux_model is only needed for legacy narrow translation models (NLLB, ALMA, etc).
        """
        name = model_name.lower()
        return any(tag in name for tag in ("translategemma", "nllb", "opus-mt", "madlad", "alma", "tower-"))

    def _glossary_block(self) -> str:
        """Build glossary instruction block for prompts.

        Uses JSON-safe serialization: if src/tgt contain braces or quotes,
        raw f-string interpolation can break a batch-JSON prompt downstream.
        """
        if not self.glossary:
            return ""
        lines = [f"  {_json.dumps(src, ensure_ascii=False)} → {_json.dumps(tgt, ensure_ascii=False)}"
                 for src, tgt in self.glossary.items()]
        return "Glossary (use these exact translations for the terms below):\n" + "\n".join(lines)

    def _enforce_glossary(self, translated: str, original: str = "") -> str:
        """Mechanically replace glossary violations after translation.

        If the original subtitle contains a source term but the translation
        leaks the source term (untranslated), substitute the target term.
        Catches cases where the LLM ignored the glossary directive.

        Uses Unicode-aware word boundaries so this works for Cyrillic / CJK /
        Arabic scripts (Python's \\b only covers ASCII word chars).
        """
        if not self.glossary or not translated:
            return translated
        result = translated
        for src, tgt in self.glossary.items():
            if not src or not tgt:
                continue
            # Unicode-aware boundaries: "not a word char" on both sides,
            # where \w is Unicode by default in Python 3 re.
            src_pattern = r'(?<!\w)' + re.escape(src) + r'(?!\w)'
            # Only enforce if source is in the original subtitle (avoids false positives)
            if original and not re.search(src_pattern, original, re.IGNORECASE | re.UNICODE):
                continue
            # If target is already present, we're fine
            if tgt in result:
                continue
            # Source term leaked untranslated → substitute
            if re.search(src_pattern, result, re.IGNORECASE | re.UNICODE):
                result = re.sub(src_pattern, tgt, result, count=1,
                                flags=re.IGNORECASE | re.UNICODE)
                logger.debug("glossary enforce: '%s' -> '%s'", src, tgt)
        return result

    def back_translate(self, translated: str) -> str:
        """Translate back to source language to verify semantic preservation.

        Used as quality signal for ambiguous segments. Cheap on local Ollama.
        """
        if not translated or not translated.strip():
            return translated
        src = self.source_lang or "English"
        system_msg = "You are a professional translator. Output ONLY the translation, no explanations."
        user_msg = f"Translate the following from {self.target_lang} to {src}:\n\n{translated}"
        result = self._call_llm(system_msg, user_msg, model=self.aux_model, attempts=1)
        return result or translated

    def analyze_context(self, texts: List[str]) -> str:
        """Analyze full subtitle text to identify characters, themes, tone, style.

        Sends a sample of the text to the model and returns a structured analysis
        that will be injected into the system prompt for all subsequent translations.
        Uses aux_model for translation-only models.
        """
        # Take a representative sample (first ~3000 chars)
        sample_lines = []
        total_chars = 0
        for t in texts:
            sample_lines.append(t)
            total_chars += len(t)
            if total_chars > 3000:
                break

        sample = "\n".join(sample_lines)
        system = (
            "You are a script analyst. Analyze the subtitle text below and provide a brief summary "
            "(max 200 words) covering:\n"
            "- Main characters and their names\n"
            "- Setting and themes\n"
            "- Tone and style (formal/informal, humorous/serious, etc.)\n"
            "- Any recurring terms, slang, or jargon\n"
            "Output ONLY the analysis, nothing else."
        )
        from_part = f" (language: {self.source_lang})" if self.source_lang else ""
        user = f"Subtitle text{from_part}:\n\n{sample}"

        use_model = self.aux_model if self._translation_only else None
        result = self._call_llm(system, user, model=use_model, timeout=180, attempts=3)
        # Free VRAM after aux task so translation model can reload
        if use_model and use_model != self.model:
            self._unload_model(use_model)
        if result:
            self._context_analysis = result
            logger.info("analyze_context: got %d chars of analysis (model=%s)", len(result), use_model or self.model)
        else:
            logger.warning("analyze_context: model returned no result")
            self._context_analysis = ""
        return self._context_analysis

    def generate_glossary(self, texts: List[str]) -> Dict[str, str]:
        """Auto-detect proper names, places, and recurring terms in subtitle text.

        Sends a sample to the model and asks it to propose a glossary with unified
        translations. Returns dict of source_term -> suggested_translation.
        Uses aux_model for translation-only models.
        """
        # Take a representative sample (first ~3000 chars)
        sample_lines = []
        total_chars = 0
        for t in texts:
            sample_lines.append(t)
            total_chars += len(t)
            if total_chars > 3000:
                break

        sample = "\n".join(sample_lines)
        from_part = f" from {self.source_lang}" if self.source_lang else ""
        system = (
            f"You are a translation assistant. Analyze the subtitle text and identify all proper names "
            f"(characters, places, organizations) and recurring terms that need consistent translation{from_part} "
            f"into {self.target_lang}.\n"
            f"Return a JSON object where keys are original terms and values are their translations.\n"
            f"Output ONLY valid JSON, nothing else. Example: {{\"Tony Stark\": \"Тони Старк\", \"SHIELD\": \"Щ.И.Т.\"}}"
        )
        user = f"Subtitle text:\n\n{sample}"

        use_model = self.aux_model if self._translation_only else None
        result = self._call_llm(system, user, model=use_model, timeout=180, attempts=3)
        if use_model and use_model != self.model:
            self._unload_model(use_model)
        if not result:
            logger.warning("generate_glossary: model returned no result")
            return {}

        # Parse JSON response
        try:
            json_text = result.strip()
            if json_text.startswith("```"):
                json_text = re.sub(r"^```(?:json)?\s*", "", json_text)
                json_text = re.sub(r"\s*```$", "", json_text)
            parsed = _json.loads(json_text)
            if isinstance(parsed, dict):
                glossary = {str(k).strip(): str(v).strip() for k, v in parsed.items() if k and v}
                logger.info("generate_glossary: found %d terms", len(glossary))
                return glossary
        except (ValueError, TypeError):
            logger.warning("generate_glossary: failed to parse JSON: %.200s", result)
        return {}

    def _cache_lookup(self, text: str) -> Optional[str]:
        """Look up translation: in-memory cache first, then SQLite TM, then fuzzy.

        Fuzzy matching uses length pre-filter to avoid O(n) SequenceMatcher
        on entries that can't possibly match (strings of very different lengths
        can never reach the similarity threshold).
        """
        key = text.strip()
        # Exact match (in-memory)
        if key in self._cache:
            self._cache_hits += 1
            logger.debug("cache: exact hit #%d for '%s'", self._cache_hits, key[:40])
            return self._cache[key]
        # Persistent TM lookup (exact match across sessions)
        if self._tm is not None:
            tm_hit = self._tm.lookup(text, self.target_lang, self.model)
            if tm_hit:
                self._cache[key] = tm_hit  # promote to in-memory cache
                self._cache_hits += 1
                logger.debug("TM: hit for '%s'", key[:40])
                return tm_hit
        # Fuzzy match — only if cache is small enough to scan
        if len(self._cache) <= 5000:
            key_len = len(key)
            # Length pre-filter: SequenceMatcher ratio can't exceed
            # 2*min(len_a, len_b) / (len_a + len_b), so skip entries
            # where length difference alone rules out a match
            min_len = int(key_len * self._fuzzy_threshold)
            max_len = int(key_len / self._fuzzy_threshold) + 1 if self._fuzzy_threshold > 0 else key_len * 10
            best_ratio = 0.0
            best_val = None
            for cached_key, cached_val in self._cache.items():
                cached_len = len(cached_key)
                if cached_len < min_len or cached_len > max_len:
                    continue
                ratio = difflib.SequenceMatcher(None, key, cached_key).ratio()
                if ratio >= self._fuzzy_threshold and ratio > best_ratio:
                    best_ratio = ratio
                    best_val = cached_val
            if best_val is not None:
                self._cache_hits += 1
                logger.debug("cache: fuzzy hit #%d (ratio=%.2f) for '%s'",
                             self._cache_hits, best_ratio, key[:40])
                return best_val
        return None

    def _build_system_prompt(self) -> str:
        """Build system message with context, genre, glossary, and context analysis."""
        parts: List[str] = []
        if self._translation_only:
            # Translation-only models (NLLB, ALMA, etc.) work best with minimal instructions
            parts.append("You are a professional subtitle translator. "
                          "Provide only the translation, nothing else.")
        else:
            # General-purpose models benefit from detailed guidance.
            # Rules informed by Netflix subtitle style guide and professional MT practice.
            parts.append(
                "You are a professional subtitle translator. Follow these rules strictly:\n"
                "- Translate MEANING naturally, never word-by-word or literally\n"
                "- Keep each subtitle line concise — max 42 characters per line when possible\n"
                "- Preserve the speaker's tone, register (formal/informal), and emotion\n"
                "- Interjections (oh, uh, hmm, wow) → use target-language equivalents\n"
                "- Idioms and wordplay → adapt, don't translate literally\n"
                "- Preserve proper nouns (character names, places) unless glossary overrides\n"
                "- Numbers, dates, units → adapt to target-language conventions\n"
                "- Profanity and slang → match intensity in the target language\n"
                "- Output ONLY the translation — no explanations, no notes, no source text"
            )
        genre_instruction = GENRE_PROMPTS.get(self.genre, "")
        if genre_instruction:
            parts.append(genre_instruction)
        if self.context and self.context.strip():
            parts.append(f"Context: {self.context.strip()}")
        if self._context_analysis and self._context_analysis.strip():
            parts.append(f"Content analysis:\n{self._context_analysis.strip()}")
        glossary_block = self._glossary_block()
        if glossary_block:
            parts.append(glossary_block)
        return "\n".join(parts)

    def _unload_model(self, model_name: str) -> None:
        """Unload a model from Ollama VRAM to free space for another model."""
        try:
            self._session.post(f"{self.base_url}/api/generate",
                               json={"model": model_name, "prompt": "", "keep_alive": 0},
                               timeout=30)
            logger.info("Unloaded model %s from VRAM", model_name)
        except Exception as e:
            logger.warning("Failed to unload model %s: %s", model_name, e)

    def _call_llm(self, system: str, user: str, model: Optional[str] = None,
                  timeout: int = 120, attempts: int = 3,
                  num_ctx: Optional[int] = None) -> Optional[str]:
        """Send a chat request to Ollama and return the response text.

        num_ctx defaults to 8192 — Ollama's built-in default of 2048 truncates
        large batch chunks and produces empty/misaligned output.
        """
        use_model = model or self.model
        # Disable thinking mode for Qwen3+ models (saves time and tokens)
        actual_user = user
        if "qwen3" in use_model.lower():
            actual_user = user + " /no_think"
        payload: dict = {
            "model": use_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": actual_user},
            ],
            "stream": False,
        }
        options: dict = {"num_ctx": int(num_ctx) if num_ctx else self._default_num_ctx()}
        if self.temperature is not None:
            options["temperature"] = float(self.temperature)
        payload["options"] = options

        resp = post_with_retry(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=timeout,
            attempts=attempts,
            backoff=1.0,
            session=self._session,
        )

        if resp is None:
            logger.error("Ollama request failed after retries: model=%s", use_model)
            return None

        if resp.status_code != 200:
            body = resp.text if hasattr(resp, "text") else ""
            logger.warning("Ollama non-200: status=%d model=%s body=%.200s",
                           resp.status_code, use_model, body)
            return None

        try:
            data = resp.json()
            return data.get("message", {}).get("content", "").strip()
        except Exception:
            try:
                body = resp.text
            except Exception:
                body = ""
            logger.exception("Failed to parse Ollama JSON: model=%s body=%.500s", use_model, body)
            return None

    def _default_num_ctx(self) -> int:
        """Context window for Ollama. Overrides tiny default (2048) that truncates batch chunks."""
        name = self.model.lower()
        # Llama 4 Scout has 10M native context — can use much more
        if "llama4" in name and "scout" in name:
            return 32768
        # Modern models support 128k, but 8192 is plenty for subtitle batches
        return 8192

    def _shorten(self, text: str, original: str) -> str:
        """Ask LLM to shorten a translation that is too long for subtitles.

        Uses aux model if available (translation models like translategemma
        may translate back to source language instead of shortening).
        """
        system = "You are a subtitle editor. Shorten the given translation to fit subtitle timing. Keep the meaning. Output ONLY the shortened text."
        user = (
            f"The original subtitle is {len(original)} characters. "
            f"The translation below is {len(text)} characters — too long for subtitles.\n"
            f"Shorten it to at most {int(len(original) * 3)} characters while preserving the meaning.\n\n"
            f"{text}"
        )
        # Use aux model for shortening — translation-specific models (translategemma)
        # may translate back to source language instead of shortening
        use_model = self.aux_model if self.aux_model != self.model else self.model
        result = self._call_llm(system, user, model=use_model, attempts=1)

        if result and len(result) < len(text):
            # Verify the shortened text is still in the target language
            if seems_untranslated(text, result, self.target_lang):
                logger.warning("_shorten: result is in wrong language, keeping original")
                return text
            logger.info("_shorten: %d -> %d chars", len(text), len(result))
            return result
        return text

    def estimate_quality(self, originals: List[str], translations: List[str],
                         use_llm_judge: bool = True) -> List[int]:
        """Rate each translation 1-5 combining heuristics and LLM-as-judge.

        Heuristic pre-pass catches obvious failures (empty, wrong script, identical).
        Ambiguous segments get semantic evaluation via aux_model in batches.
        Returns list of integer scores (1=terrible, 5=perfect).

        Set use_llm_judge=False to skip LLM calls (fast heuristic only).
        """
        if not originals or not translations:
            return []

        scores: List[Optional[int]] = []
        ambiguous_indices: List[int] = []

        for i, (orig, trans) in enumerate(zip(originals, translations)):
            if not validate_translation(orig, trans):
                scores.append(1)
                continue
            if seems_untranslated(orig, trans, self.target_lang):
                scores.append(1)
                continue
            if trans.strip().lower() == orig.strip().lower():
                scores.append(2)
                continue
            if len(orig.strip()) > 20 and len(trans.strip()) < len(orig.strip()) * 0.15:
                scores.append(2)
                continue
            # Needs semantic judgment from LLM
            scores.append(None)
            ambiguous_indices.append(i)

        if not ambiguous_indices or not use_llm_judge:
            final = [s if s is not None else 5 for s in scores]
            logger.info("estimate_quality (heuristic): %d scored, weak=%d",
                        len(final), sum(1 for s in final if s < 3))
            return final

        use_model = self.aux_model
        batch_size = 15
        for batch_start in range(0, len(ambiguous_indices), batch_size):
            batch_idxs = ambiguous_indices[batch_start:batch_start + batch_size]
            pairs = {str(i + 1): {"original": originals[idx], "translation": translations[idx]}
                     for i, idx in enumerate(batch_idxs)}
            system_msg = (
                "You are a subtitle translation reviewer. Rate each translation from 1 to 5:\n"
                "5 = excellent, natural, preserves meaning and tone\n"
                "4 = good, minor awkwardness but accurate\n"
                "3 = acceptable, meaning mostly preserved but unnatural\n"
                "2 = poor, meaning partially lost or significantly wrong\n"
                "1 = terrible, meaning lost or translation broken\n"
                f"Target language: {self.target_lang}\n"
                "Return ONLY valid JSON mapping each key to an integer score, e.g. {\"1\": 5, \"2\": 3}."
            )
            user_msg = _json.dumps(pairs, ensure_ascii=False)
            result = self._call_llm(system_msg, user_msg, model=use_model,
                                    timeout=120, attempts=2)
            if not result:
                for idx in batch_idxs:
                    scores[idx] = 3
                continue
            try:
                txt = result.strip()
                if txt.startswith("```"):
                    txt = re.sub(r"^```(?:json)?\s*", "", txt)
                    txt = re.sub(r"\s*```$", "", txt)
                match = re.search(r'\{.*\}', txt, re.DOTALL)
                if match:
                    txt = match.group()
                parsed = _json.loads(txt)
                for i, idx in enumerate(batch_idxs):
                    val = parsed.get(str(i + 1), 3)
                    if isinstance(val, dict):
                        val = val.get("score", 3)
                    try:
                        score = int(val)
                        scores[idx] = max(1, min(5, score))
                    except (ValueError, TypeError):
                        scores[idx] = 3
            except (ValueError, KeyError, TypeError, AttributeError):
                logger.warning("estimate_quality: LLM judge parse failed, defaulting to 3")
                for idx in batch_idxs:
                    if scores[idx] is None:
                        scores[idx] = 3

        if use_model != self.model:
            self._unload_model(use_model)

        final = [s if s is not None else 5 for s in scores]
        weak = sum(1 for s in final if s < 3)
        logger.info("estimate_quality (LLM-judge): %d scored, weak=%d, judged=%d",
                    len(final), weak, len(ambiguous_indices))
        return final

    def retranslate_weak(self, originals: List[str], translations: List[str],
                         scores: List[int], threshold: int = 3,
                         use_back_translation: bool = False) -> List[str]:
        """Re-translate segments with quality score below threshold.

        Returns updated translations list with weak segments re-translated.

        If ``use_back_translation`` is True, each re-translated segment is
        back-translated to the source language and low-similarity hits are
        logged as warnings (informational signal, no second re-translate).

        For >10 weak segments, uses batch mode to avoid per-segment latency.
        """
        result = list(translations)
        weak_indices = [i for i, s in enumerate(scores) if s < threshold]
        if not weak_indices:
            logger.info("retranslate_weak: no segments below threshold %d", threshold)
            return result

        logger.info("retranslate_weak: %d/%d segments below threshold %d",
                     len(weak_indices), len(translations), threshold)

        # Batch-mode optimization for large weak sets — amortizes model overhead
        if len(weak_indices) > 10:
            logger.info("retranslate_weak: using batch mode for %d segments", len(weak_indices))
            weak_origs = [originals[i] for i in weak_indices]
            # Clear cache so batch path doesn't return the same bad translation
            for i in weak_indices:
                self._cache.pop(originals[i].strip(), None)
            # No progress_file (we're mid-pipeline) and parallel_chunks=1 (keep it simple)
            weak_new = self.translate_batch(weak_origs, max_chars=1000, parallel_chunks=1)
            for idx, new_tr in zip(weak_indices, weak_new):
                if new_tr and new_tr.strip() != originals[idx].strip():
                    result[idx] = new_tr
            return result

        for idx in weak_indices:
            orig = originals[idx]
            old_trans = translations[idx]
            # Build context from neighbors
            w = self.context_window
            prev_txts = [originals[j] for j in range(max(0, idx - w), idx)]
            next_txts = [originals[j] for j in range(idx + 1, min(len(originals), idx + 1 + w))]

            # Clear cache for this segment to force fresh translation
            cache_key = orig.strip()
            self._cache.pop(cache_key, None)

            new_trans = self.translate(orig, prev_texts=prev_txts, next_texts=next_txts)
            if new_trans != orig:  # only update if we got a real translation
                result[idx] = new_trans
                logger.info("retranslate_weak: segment %d re-translated: '%s' -> '%s'",
                            idx, old_trans[:40], new_trans[:40])
                # Optional quality signal: back-translate and measure similarity
                if use_back_translation:
                    try:
                        bt = self.back_translate(new_trans)
                        similarity = difflib.SequenceMatcher(None, orig.lower(), bt.lower()).ratio()
                        if similarity < 0.3:
                            logger.warning("retranslate_weak: back-translation similarity low (%.2f) for seg %d",
                                            similarity, idx)
                    except Exception as e:
                        logger.debug("back-translation check failed for seg %d: %s", idx, e)

        return result

    def translate(self, text: str, prev_text: str = "", next_text: str = "",
                  prev_texts: Optional[List[str]] = None,
                  next_texts: Optional[List[str]] = None) -> str:
        """Переводит текст с учётом соседних субтитров для связности.

        prev_texts/next_texts take priority over prev_text/next_text when provided.
        """
        if not text.strip():
            return text

        # Cache lookup — exact + fuzzy match
        cache_key = text.strip()
        cached = self._cache_lookup(text)
        if cached is not None:
            return cached

        logger.debug("translate: input length=%d chars", len(text))
        # Strip ASS positioning tags before translation (re-added after)
        clean_text, ass_tag = strip_ass_pos(text)
        protected_text, tags = protect_tags(clean_text)

        # Build user message with context window and text
        user_parts: List[str] = []
        _prev = prev_texts if prev_texts else ([prev_text] if prev_text else [])
        _next = next_texts if next_texts else ([next_text] if next_text else [])
        if _prev or _next:
            user_parts.append("Surrounding subtitles for reference (do NOT translate these):")
            for p in _prev:
                user_parts.append(f"[BEFORE]: {p}")
            for n in _next:
                user_parts.append(f"[AFTER]: {n}")
            user_parts.append("")

        from_part = f" from {self.source_lang}" if self.source_lang else ""
        user_parts.append(
            f"Translate the following subtitle{from_part} into {self.target_lang}. "
            "Translate accurately, preserving the full meaning."
        )
        user_parts.append(f"\n{protected_text}")
        user_msg = "\n".join(user_parts)

        system_msg = self._build_system_prompt()
        translated = self._call_llm(system_msg, user_msg)

        if translated is None:
            return text

        logger.debug("translate: model=%s input_len=%d output_len=%d", self.model, len(text), len(translated))

        # Validate quality; retry once if suspicious
        if not validate_translation(text, translated, self.target_lang):
            logger.warning("translate: validation failed (original=%d chars, translated=%d chars), retrying once",
                           len(text), len(translated))
            retry_result = self._call_llm(system_msg, user_msg, attempts=1)
            if retry_result:
                translated = retry_result
            if not validate_translation(text, translated, self.target_lang):
                logger.warning("translate: retry also failed validation, returning original text")
                return text

        result = restore_tags(translated, tags)
        result = restore_ass_pos(result, ass_tag)
        # Mechanical glossary enforcement — catches cases where LLM ignored the directive
        result = self._enforce_glossary(result, original=text)
        self._cache[cache_key] = result
        # Persistent translation memory (survives across sessions)
        if self._tm is not None:
            self._tm.store(text, self.target_lang, self.model, result)
        return result

    def review(self, original: str, translated: str,
               prev_original: str = "", prev_translated: str = "",
               next_original: str = "",
               prev_originals: Optional[List[str]] = None,
               prev_translateds: Optional[List[str]] = None,
               next_originals: Optional[List[str]] = None) -> str:
        """Второй проход: проверяет и правит перевод с учётом контекста.

        Возвращает исправленный перевод или оригинальный, если модель
        решила, что правок не нужно.
        """
        if not translated.strip() or not original.strip():
            return translated

        # Build context lists (list form takes priority over single string)
        _prev_origs = prev_originals if prev_originals else ([prev_original] if prev_original else [])
        _prev_trans = prev_translateds if prev_translateds else ([prev_translated] if prev_translated else [])
        _next_origs = next_originals if next_originals else ([next_original] if next_original else [])

        # Build user message for review
        user_parts: List[str] = []
        if _prev_origs or _next_origs:
            user_parts.append("Surrounding subtitles for reference:")
            for po, pt in zip(_prev_origs, _prev_trans + [""] * len(_prev_origs)):
                line = f"[BEFORE]: {po}"
                if pt:
                    line += f" -> {pt}"
                user_parts.append(line)
            for no in _next_origs:
                user_parts.append(f"[AFTER]: {no}")
            user_parts.append("")

        from_part = f" from {self.source_lang}" if self.source_lang else ""
        user_parts.append(
            f"Review this subtitle translation{from_part} into {self.target_lang}.\n"
            "Check for:\n"
            "- Accuracy: does it convey the original meaning?\n"
            "- Natural flow: does it sound natural in the target language?\n"
            "- Consistency with surrounding subtitles\n"
            "- Completeness: the full meaning must be preserved\n\n"
            f"Original: {original}\n"
            f"Translation: {translated}\n\n"
            "If the translation is good, output it exactly as-is.\n"
            "If it needs fixes, output ONLY the corrected translation, nothing else."
        )
        user_msg = "\n".join(user_parts)

        system_msg = self._build_system_prompt()
        reviewed = self._call_llm(system_msg, user_msg, model=self.review_model, attempts=2)

        if reviewed is None:
            logger.warning("review: request failed, keeping original translation")
            return translated

        # Валидация: reviewed должен быть разумным
        if not reviewed or not reviewed.strip():
            return translated
        # Если review вернул что-то в 5 раз длиннее оригинала — мусор
        if len(reviewed) > len(original) * 5 and len(original) > 10:
            logger.warning("review: result too long (%d chars vs %d original), keeping first pass",
                           len(reviewed), len(original))
            return translated

        if reviewed != translated:
            logger.info("review: corrected '%s' → '%s'", translated[:50], reviewed[:50])

        return reviewed

    def _review_chunk(self, originals: List[str], translations: List[str],
                      chunk_idx: int, total_chunks: int,
                      max_retries: int = 2) -> List[str]:
        """Review a batch of translations in a single LLM call. Falls back to per-segment on failure."""

        # Build JSON input: {"1": {"original": ..., "translation": ...}, ...}
        pairs = {}
        for i, (orig, trans) in enumerate(zip(originals, translations)):
            pairs[str(i + 1)] = {"original": orig, "translation": trans}

        from_part = f" from {self.source_lang}" if self.source_lang else ""
        system_msg = self._build_system_prompt()
        user_msg = (
            f"Review these subtitle translations{from_part} into {self.target_lang}.\n"
            "For each segment check: accuracy, natural flow, completeness of meaning.\n"
            "Return a JSON object with the same keys and corrected translations as values.\n"
            "If a translation is already good, include it unchanged.\n"
            "Output ONLY valid JSON, nothing else.\n\n"
            f"{_json.dumps(pairs, ensure_ascii=False)}"
        )

        result = None
        for retry in range(max_retries + 1):
            result = self._call_llm(system_msg, user_msg, model=self.review_model, timeout=300)
            if result is not None:
                break
            if retry < max_retries:
                wait = 5 * (retry + 1)
                logger.warning("review_batch: chunk %d/%d attempt %d failed, retrying in %ds",
                               chunk_idx + 1, total_chunks, retry + 1, wait)
                time.sleep(wait)

        if result is None:
            logger.error("review_batch: chunk %d/%d all retries failed, keeping originals",
                         chunk_idx + 1, total_chunks)
            return list(translations)

        # Parse JSON response
        try:
            json_text = result.strip()
            if json_text.startswith("```"):
                json_text = re.sub(r"^```(?:json)?\s*", "", json_text)
                json_text = re.sub(r"\s*```$", "", json_text)
            parsed = _json.loads(json_text)
            if isinstance(parsed, dict) and len(parsed) == len(originals):
                reviewed = []
                for i in range(len(originals)):
                    val = parsed.get(str(i + 1), translations[i])
                    if isinstance(val, dict):
                        val = val.get("translation", val.get("corrected", translations[i]))
                    val = str(val).strip()
                    # Validate: not empty, not absurdly long
                    if not val or (len(val) > len(originals[i]) * 5 and len(originals[i]) > 10):
                        val = translations[i]
                    reviewed.append(val)
                logger.info("review_batch: chunk %d/%d JSON parse OK (%d segments)",
                            chunk_idx + 1, total_chunks, len(reviewed))
                return reviewed
        except (ValueError, KeyError, TypeError, AttributeError):
            pass

        logger.warning("review_batch: chunk %d/%d parse failed, falling back to per-segment",
                       chunk_idx + 1, total_chunks)
        # Fallback: per-segment review with neighbor context
        reviewed = []
        for seg_i, (orig, trans) in enumerate(zip(originals, translations)):
            w = self.context_window
            prev_origs = [originals[j] for j in range(max(0, seg_i - w), seg_i)]
            prev_trans = [translations[j] for j in range(max(0, seg_i - w), seg_i)]
            next_origs = [originals[j] for j in range(seg_i + 1, min(len(originals), seg_i + 1 + w))]
            corrected = self.review(orig, trans,
                                    prev_originals=prev_origs,
                                    prev_translateds=prev_trans,
                                    next_originals=next_origs)
            reviewed.append(corrected)
        return reviewed

    def _translate_chunk(self, chunk: List[str], chunk_idx: int, total_chunks: int,
                         texts: List[str], chunk_start: int,
                         prev_chunk_tail: List[str],
                         max_retries: int = 2) -> List[str]:
        """Translate a single chunk with retry logic. Returns list of translated strings.

        On batch failure, retries the batch request before falling back to per-segment.
        Never returns original text silently — always attempts translation.
        """
        # Strip ASS positioning tags and protect HTML tags per segment
        ass_tags_list: List[str] = []
        protected_list = []
        tags_list: List[Dict[str, str]] = []
        for seg in chunk:
            clean, ass_tag = strip_ass_pos(seg)
            ass_tags_list.append(ass_tag)
            p, tags = protect_tags(clean)
            protected_list.append(p)
            tags_list.append(tags)

        # Build JSON input for batch
        json_input = {str(i + 1): seg for i, seg in enumerate(protected_list)}

        # Build user message for batch
        user_parts_batch: List[str] = []

        # Cross-chunk context: show tail of previous chunk so the model keeps coherence
        if prev_chunk_tail:
            prev_lines = "\n".join(prev_chunk_tail)
            user_parts_batch.append(
                "Previous subtitles for reference (do NOT translate these, they are already translated):\n"
                f"{prev_lines}\n"
            )

        from_part = f" from {self.source_lang}" if self.source_lang else ""
        user_parts_batch.append(
            f"Translate each subtitle segment below{from_part} into {self.target_lang}.\n"
            "Translate accurately, preserving the full meaning of each segment.\n"
            "Preserve any placeholders exactly (e.g. __TAG_xxx__).\n"
            "Return a JSON object with the same keys and translated values.\n"
            "Output ONLY valid JSON, nothing else.\n\n"
            f"{_json.dumps(json_input, ensure_ascii=False)}"
        )
        user_msg_batch = "\n".join(user_parts_batch)
        system_msg = self._build_system_prompt()

        # Retry batch translation on failure
        model_response = None
        for retry in range(max_retries + 1):
            model_response = self._call_llm(system_msg, user_msg_batch, timeout=300)
            if model_response is not None:
                break
            if retry < max_retries:
                wait = 5 * (retry + 1)
                logger.warning("translate_batch: chunk %d/%d attempt %d failed, retrying in %ds",
                               chunk_idx + 1, total_chunks, retry + 1, wait)
                time.sleep(wait)

        if model_response is None:
            # All batch retries exhausted — fall back to per-segment (which has its own retries)
            logger.error("translate_batch: chunk %d/%d all batch retries failed, per-segment fallback",
                         chunk_idx + 1, total_chunks)
            chunk_results: List[str] = []
            for seg_idx, seg in enumerate(chunk):
                global_idx = chunk_start + seg_idx
                w = self.context_window
                prev_txts = [texts[j] for j in range(max(0, global_idx - w), global_idx)]
                next_txts = [texts[j] for j in range(global_idx + 1, min(len(texts), global_idx + 1 + w))]
                translated = self.translate(seg, prev_texts=prev_txts, next_texts=next_txts)
                chunk_results.append(translated)
            return chunk_results

        # Three-tier parsing: JSON -> |||SEP||| -> per-segment fallback
        translated_list: List[str] = []
        logger.info("translate_batch: chunk %d/%d response length=%d chars", chunk_idx + 1, total_chunks, len(model_response))

        # Tier 1: Try JSON parsing
        try:
            json_text = model_response.strip()
            if json_text.startswith("```"):
                json_text = re.sub(r"^```(?:json)?\s*", "", json_text)
                json_text = re.sub(r"\s*```$", "", json_text)
            # Try to extract JSON object even if surrounded by extra text
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', json_text, re.DOTALL)
            if json_match:
                json_text = json_match.group()
            parsed = _json.loads(json_text)
            if isinstance(parsed, dict) and len(parsed) >= len(chunk):
                translated_list = []
                for i in range(len(chunk)):
                    val = parsed.get(str(i + 1), "")
                    if isinstance(val, dict):
                        val = val.get("translation", val.get("text", ""))
                    translated_list.append(str(val).strip())
                logger.info("translate_batch: chunk %d/%d JSON parse OK", chunk_idx + 1, total_chunks)
        except (ValueError, KeyError, TypeError, AttributeError):
            translated_list = []

        # Tier 2: Try |||SEP||| delimiter fallback
        if not translated_list:
            try:
                sep_parts = [p.strip() for p in model_response.split("|||SEP|||")]
                sep_parts = [p for p in sep_parts if p]
                sep_parts = [re.sub(r"^\[\d+\]\s*", "", p) for p in sep_parts]
                if len(sep_parts) == len(chunk):
                    translated_list = sep_parts
                    logger.info("translate_batch: chunk %d/%d SEP fallback OK", chunk_idx + 1, total_chunks)
            except Exception:
                translated_list = []

        # Validate each translated segment in the batch
        if translated_list and len(translated_list) == len(chunk):
            bad_count = sum(1 for orig, tr in zip(chunk, translated_list) if not validate_translation(orig, tr, self.target_lang))
            if bad_count > len(chunk) * 0.5:
                logger.warning("translate_batch: chunk %d/%d has %d/%d bad translations, falling back",
                               chunk_idx + 1, total_chunks, bad_count, len(chunk))
                translated_list = []
            elif bad_count > 0:
                logger.info("translate_batch: chunk %d/%d has %d/%d suspicious translations",
                            chunk_idx + 1, total_chunks, bad_count, len(chunk))

        # Alignment check: detect content shift (translations assigned to wrong segments)
        # Compare length ratios — if many translations have wildly different length from their
        # originals while neighboring originals would be a better match, the content is shifted.
        if translated_list and len(translated_list) == len(chunk) and len(chunk) >= 4:
            shifted = 0
            for i in range(len(chunk)):
                orig_len = max(len(chunk[i].strip()), 1)
                tr_len = len(translated_list[i].strip())
                ratio = tr_len / orig_len
                # A translation that's 4x longer or 4x shorter is suspicious
                if ratio > 4.0 or ratio < 0.25:
                    shifted += 1
            shift_pct = shifted / len(chunk)
            if shift_pct > 0.3:
                logger.warning(
                    "translate_batch: chunk %d/%d alignment check failed (%.0f%% suspicious length ratios), per-segment fallback",
                    chunk_idx + 1, total_chunks, shift_pct * 100)
                translated_list = []

        if translated_list and len(translated_list) == len(chunk):
            logger.info("translate_batch: chunk %d/%d parsed %d segments OK", chunk_idx + 1, total_chunks, len(translated_list))
            # Re-translate segments that are empty, identical to original, or still in source language
            retranslate_count = 0
            for seg_i in range(len(translated_list)):
                tr = translated_list[seg_i]
                orig = chunk[seg_i]
                prot = protected_list[seg_i]
                needs_retranslate = False
                reason = ""
                # Check: empty translation
                if not tr or not tr.strip():
                    needs_retranslate = True
                    reason = "empty"
                # Check: identical to original or protected version
                elif tr.strip() == orig.strip() or tr.strip() == prot.strip():
                    needs_retranslate = True
                    reason = "identical to original"
                # Check: still in source language (script detection)
                elif seems_untranslated(orig, tr, self.target_lang):
                    needs_retranslate = True
                    reason = "wrong script"

                if needs_retranslate:
                    retranslate_count += 1
                    global_idx = chunk_start + seg_i
                    w = self.context_window
                    prev_txts = [texts[j] for j in range(max(0, global_idx - w), global_idx)]
                    next_txts = [texts[j] for j in range(global_idx + 1, min(len(texts), global_idx + 1 + w))]
                    logger.info("translate_batch: chunk %d/%d seg %d needs re-translation (%s): '%.40s'",
                                chunk_idx + 1, total_chunks, seg_i + 1, reason, tr)
                    retranslated = self.translate(orig, prev_texts=prev_txts, next_texts=next_txts)
                    if retranslated and retranslated.strip() != orig.strip() and not seems_untranslated(orig, retranslated, self.target_lang):
                        logger.info("translate_batch: chunk %d/%d seg %d re-translated OK",
                                    chunk_idx + 1, total_chunks, seg_i + 1)
                        translated_list[seg_i] = retranslated
                    else:
                        logger.warning("translate_batch: chunk %d/%d seg %d still untranslated after retry",
                                       chunk_idx + 1, total_chunks, seg_i + 1)
            if retranslate_count > 0:
                logger.info("translate_batch: chunk %d/%d had %d segments re-translated",
                            chunk_idx + 1, total_chunks, retranslate_count)
            # Restore tags and ASS positioning, enforce glossary, store to TM
            chunk_results = []
            for orig, translated, tags, ass_tag in zip(chunk, translated_list, tags_list, ass_tags_list):
                restored = restore_tags(translated, tags)
                restored = restore_ass_pos(restored, ass_tag)
                restored = self._enforce_glossary(restored, original=orig)
                chunk_results.append(restored)
                if self._tm is not None:
                    self._tm.store(orig, self.target_lang, self.model, restored)
            return chunk_results
        else:
            # Tier 3: per-segment fallback with sliding window
            logger.warning("translate_batch: chunk %d/%d parse failed (got %d, expected %d), per-segment fallback",
                           chunk_idx + 1, total_chunks, len(translated_list) if translated_list else 0, len(chunk))
            chunk_results = []
            for seg_idx, seg in enumerate(chunk):
                global_idx = chunk_start + seg_idx
                w = self.context_window
                prev_txts = [texts[j] for j in range(max(0, global_idx - w), global_idx)]
                next_txts = [texts[j] for j in range(global_idx + 1, min(len(texts), global_idx + 1 + w))]
                translated = self.translate(seg, prev_texts=prev_txts, next_texts=next_txts)
                chunk_results.append(translated)
                logger.debug("translate_batch fallback: seg %d/%d done", seg_idx + 1, len(chunk))
            return chunk_results

    def translate_batch(self, texts: List[str], max_chars: int = 1000,
                        on_progress: Optional["callable"] = None,
                        on_phase: Optional["callable"] = None,
                        progress_file: Optional[Path] = None,
                        parallel_chunks: int = 2) -> List[str]:
        """Translate a list of texts as a single request (or multiple chunked requests).

        Uses sliding window context for per-segment fallback to maintain dialogue coherence.
        on_progress(done, total) is called after each chunk completes.
        on_phase(phase_name) is called when the processing phase changes.
        progress_file: if set, save/resume progress to this JSON file.
        parallel_chunks: number of chunks to process concurrently (default 2).
        Returns list of translated strings in the same order.
        """
        if not texts:
            return []

        # Resume from progress file if it exists
        resume_from = 0
        results: List[str] = []
        if progress_file and progress_file.exists():
            try:
                saved = _json.loads(progress_file.read_text(encoding="utf-8"))
                saved_translations = saved.get("translations", [])
                if saved_translations and len(saved_translations) < len(texts):
                    # Clamp: defensive against corrupted progress file claiming more than we have
                    resume_from = min(len(saved_translations), len(texts))
                    results = list(saved_translations)
                    logger.info("translate_batch: resuming from segment %d/%d", resume_from, len(texts))
                elif len(saved_translations) == len(texts):
                    logger.info("translate_batch: progress file complete, skipping translation pass")
                    return list(saved_translations)
            except Exception:
                logger.warning("translate_batch: could not read progress file, starting fresh")

        # Adaptive chunk sizing: adjust max segments per chunk based on content
        # Short segments (lyrics, quick dialogue) → more per chunk (up to 50)
        # Long segments (narration, descriptions) → fewer per chunk (down to 10)
        avg_len = sum(len(t) for t in texts) / len(texts) if texts else 50
        if avg_len < 30:
            max_segments_per_chunk = 50
        elif avg_len < 60:
            max_segments_per_chunk = 30
        elif avg_len < 120:
            max_segments_per_chunk = 20
        else:
            max_segments_per_chunk = 10
        logger.info("translate_batch: adaptive chunking: avg_seg_len=%.0f max_segments=%d",
                     avg_len, max_segments_per_chunk)

        def make_chunks(texts_list, max_chars_local):
            chunks: List[Tuple[int, List[str]]] = []
            cur: List[str] = []
            cur_start = 0
            cur_len = 0
            for i, t in enumerate(texts_list):
                if cur and (cur_len + len(t) > max_chars_local or len(cur) >= max_segments_per_chunk):
                    chunks.append((cur_start, cur))
                    cur = []
                    cur_start = i
                    cur_len = 0
                cur.append(t)
                cur_len += len(t)
            if cur:
                chunks.append((cur_start, cur))
            return chunks

        chunks = make_chunks(texts, max_chars)
        if not results:
            results = []

        # Prepare work items, skipping already-translated chunks
        work_items: List[Tuple[int, int, List[str]]] = []  # (chunk_idx, chunk_start, chunk)
        for chunk_idx, (chunk_start, chunk) in enumerate(chunks):
            chunk_end = chunk_start + len(chunk)
            if chunk_end <= resume_from:
                continue
            if chunk_start < resume_from:
                skip_n = resume_from - chunk_start
                chunk = chunk[skip_n:]
                chunk_start = resume_from
            if chunk:
                work_items.append((chunk_idx, chunk_start, chunk))

        # Process chunks in parallel groups
        # Each group of `parallel_chunks` runs concurrently, then results are collected in order
        # Cross-chunk context: first chunk in group gets tail from previous group,
        # others in same group share the same context (acceptable tradeoff for speed)
        prev_chunk_tail: List[str] = []
        if resume_from > 0 and results:
            # Reconstruct tail from already-translated results
            prev_chunk_tail = results[-3:] if len(results) >= 3 else results[:]

        use_parallel = parallel_chunks > 1 and len(work_items) > 1
        if use_parallel:
            actual_workers = min(parallel_chunks, len(work_items))
            logger.info("translate_batch: parallel mode, %d workers (cap=%d), %d chunks",
                         actual_workers, parallel_chunks, len(work_items))

        group_start = 0
        while group_start < len(work_items):
            group = work_items[group_start:group_start + parallel_chunks]
            group_start += len(group)

            if use_parallel and len(group) > 1:
                # Parallel: submit all chunks in group concurrently
                group_tail = prev_chunk_tail  # shared context for this group
                future_map = {}
                with ThreadPoolExecutor(max_workers=len(group)) as pool:
                    for item_idx, (chunk_idx, chunk_start, chunk) in enumerate(group):
                        fut = pool.submit(
                            self._translate_chunk,
                            chunk, chunk_idx, len(chunks), texts, chunk_start, group_tail,
                        )
                        future_map[item_idx] = (fut, chunk)

                    # Collect results in original order
                    for item_idx in range(len(group)):
                        fut, chunk = future_map[item_idx]
                        chunk_results = fut.result()
                        results.extend(chunk_results)
                        if on_progress:
                            on_progress(len(results), len(texts))

                # Update tail from last chunk's translated results (not originals)
                last_group_results = results[-len(group[-1][2]):]
                prev_chunk_tail = last_group_results[-3:] if len(last_group_results) >= 3 else last_group_results[:]
            else:
                # Sequential: single chunk or parallel disabled
                for chunk_idx, chunk_start, chunk in group:
                    logger.info("translate_batch: chunk %d/%d segments=%d", chunk_idx + 1, len(chunks), len(chunk))
                    chunk_results = self._translate_chunk(
                        chunk, chunk_idx, len(chunks), texts, chunk_start, prev_chunk_tail,
                    )
                    results.extend(chunk_results)
                    prev_chunk_tail = chunk_results[-3:] if len(chunk_results) >= 3 else chunk_results[:]
                    if on_progress:
                        on_progress(len(results), len(texts))

            # Save incremental progress after each group
            if progress_file:
                try:
                    progress_file.write_text(
                        _json.dumps({"translations": results}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception as e:
                    logger.warning("translate_batch: failed to save progress: %s", e)

        # Final sweep: catch remaining untranslated segments (up to 3 passes)
        for sweep_pass in range(3):
            untranslated_indices = []
            for i in range(len(results)):
                tr = results[i]
                orig = texts[i]
                if not tr or not tr.strip():
                    untranslated_indices.append(i)
                elif tr.strip() == orig.strip():
                    untranslated_indices.append(i)
                elif seems_untranslated(orig, tr, self.target_lang):
                    untranslated_indices.append(i)
            if not untranslated_indices:
                break
            logger.warning("translate_batch: final sweep pass %d found %d untranslated segments",
                           sweep_pass + 1, len(untranslated_indices))
            for idx in untranslated_indices:
                orig = texts[idx]
                w = self.context_window
                prev_txts = [texts[j] for j in range(max(0, idx - w), idx)]
                next_txts = [texts[j] for j in range(idx + 1, min(len(texts), idx + 1 + w))]
                # Clear cache to force fresh translation
                self._cache.pop(orig.strip(), None)
                retranslated = self.translate(orig, prev_texts=prev_txts, next_texts=next_txts)
                if retranslated and retranslated.strip() != orig.strip() and not seems_untranslated(orig, retranslated, self.target_lang):
                    results[idx] = retranslated
                    logger.info("translate_batch: final sweep seg %d re-translated OK", idx + 1)
                else:
                    logger.warning("translate_batch: final sweep seg %d still untranslated (pass %d)", idx + 1, sweep_pass + 1)
            if on_progress:
                on_progress(len(results), len(texts))

        # Second pass: batch review for quality (chunked, same as translation)
        if self.two_pass and results:
            logger.info("translate_batch: starting batch review pass (%d segments)", len(results))
            if on_phase:
                on_phase("reviewing")
            reviewed: List[str] = []
            review_chunks_orig: List[List[str]] = []
            review_chunks_trans: List[List[str]] = []
            current_orig: List[str] = []
            current_trans: List[str] = []
            current_len = 0
            for orig, trans in zip(texts, results):
                seg_len = len(orig) + len(trans)
                if current_orig and current_len + seg_len > max_chars:
                    review_chunks_orig.append(current_orig)
                    review_chunks_trans.append(current_trans)
                    current_orig = []
                    current_trans = []
                    current_len = 0
                current_orig.append(orig)
                current_trans.append(trans)
                current_len += seg_len
            if current_orig:
                review_chunks_orig.append(current_orig)
                review_chunks_trans.append(current_trans)

            total_review_chunks = len(review_chunks_orig)
            done_count = 0
            for ci, (ch_orig, ch_trans) in enumerate(zip(review_chunks_orig, review_chunks_trans)):
                chunk_reviewed = self._review_chunk(ch_orig, ch_trans, ci, total_review_chunks)
                reviewed.extend(chunk_reviewed)
                done_count += len(chunk_reviewed)
                if on_progress:
                    on_progress(done_count, len(texts))
            logger.info("translate_batch: batch review pass done (%d chunks)", total_review_chunks)
            return reviewed

        return results


def parse_glossary(raw: str) -> Dict[str, str]:
    """Parse glossary from 'key=value' lines (one per line or comma-separated)."""
    glossary: Dict[str, str] = {}
    for line in raw.replace(",", "\n").split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            glossary[key] = value
    return glossary


def validate_reading_speed(blocks: List[SrtBlock], translated_texts: List[str],
                           translator: "Translator",
                           max_cps: float = 25.0) -> List[str]:
    """Check and fix subtitles that exceed max characters per second.

    Returns updated translated_texts with overly fast subtitles shortened.
    """
    fixed = list(translated_texts)
    shortened_count = 0
    for i, (block, text) in enumerate(zip(blocks, fixed)):
        dur = duration_seconds(block.timecode)
        if dur <= 0:
            continue
        char_count = len(text.replace("\n", ""))
        cps = char_count / dur
        if cps > max_cps and char_count > 10:
            target_chars = int(dur * max_cps)
            logger.info("CPS check: block %d cps=%.1f (max=%.1f), %d chars in %.1fs, shortening to ~%d",
                        block.index, cps, max_cps, char_count, dur, target_chars)
            shortened = translator._shorten(text, block.text())
            if len(shortened.replace("\n", "")) < char_count:
                fixed[i] = shortened
                shortened_count += 1
    if shortened_count:
        logger.info("CPS validation: shortened %d/%d subtitles", shortened_count, len(blocks))
    return fixed


def translate_srt(input_path: Path, output_path: Path, target_lang: str = "Russian",
                  model: str = "gemma4:e12b",
                  context: str = "", source_lang: str = "",
                  two_pass: bool = False, review_model: str = "",
                  chunk_size: int = 1000,
                  glossary: Optional[Dict[str, str]] = None,
                  context_window: int = 3,
                  genre: str = "",
                  max_cps: float = 0,
                  context_analysis: bool = True,
                  qe: bool = True,
                  auto_glossary: bool = True,
                  tm_path: Optional[Path] = None) -> None:
    """Переводит SRT файл."""
    print(f"📖 Читаю: {input_path}")
    text, encoding = read_srt_file(input_path)
    blocks = parse_srt(text)
    total = len(blocks)
    print(f"   Субтитров: {total}")

    translator = Translator(model, target_lang, context=context, source_lang=source_lang,
                            two_pass=two_pass, review_model=review_model,
                            glossary=glossary, context_window=context_window,
                            genre=genre, tm_path=tm_path)

    texts = [b.text() for b in blocks]

    # Auto-glossary generation (Phase 13)
    if auto_glossary:
        print(f"📝 Генерация глоссария...")
        auto_gloss = translator.generate_glossary(texts)
        if auto_gloss:
            print(f"   Найдено терминов: {len(auto_gloss)}")
            for src, tgt in list(auto_gloss.items())[:10]:
                print(f"   {src} → {tgt}")
            # Merge: user glossary takes priority over auto-generated
            merged = dict(auto_gloss)
            merged.update(translator.glossary)
            translator.glossary = merged

    # Pre-translation context analysis (Phase 10)
    if context_analysis:
        print(f"🔍 Анализ контента...")
        analysis = translator.analyze_context(texts)
        if analysis:
            print(f"   {analysis[:200]}...")

    # Progress file for crash recovery (CLI only)
    progress_path = input_path.with_suffix(".progress.json")
    if progress_path.exists():
        print(f"📎 Найден файл прогресса, продолжаю с места остановки...")

    print(f"🔄 Перевод...")
    cli_phase = "translating"
    cli_t0 = time.time()

    def cli_progress(done: int, total_count: int):
        pct = done / total_count * 100 if total_count else 100
        bar_len = 30
        filled = int(bar_len * done / total_count) if total_count else bar_len
        bar = "█" * filled + "░" * (bar_len - filled)
        label = "Проверка" if cli_phase == "reviewing" else "Перевод"
        elapsed = int(time.time() - cli_t0)
        m, s = divmod(elapsed, 60)
        t_str = f"{m}m {s}s" if m else f"{s}s"
        print(f"   {label}: [{bar}] {done}/{total_count} ({pct:.1f}%) [{t_str}]", end="\r")

    def cli_on_phase(phase: str):
        nonlocal cli_phase
        cli_phase = phase
        if phase == "reviewing":
            print()
            print(f"🔍 Проверка перевода (проход 2/2)...")

    translated_texts = translator.translate_batch(texts, max_chars=chunk_size,
                                                  on_progress=cli_progress,
                                                  on_phase=cli_on_phase,
                                                  progress_file=progress_path)

    # Quality estimation with auto-retranslate (Phase 11)
    if qe:
        print()
        print(f"📊 Оценка качества перевода...")
        scores = translator.estimate_quality(texts, translated_texts)
        weak_count = sum(1 for s in scores if s < 3)
        if weak_count > 0:
            print(f"   Слабых сегментов: {weak_count}/{len(scores)}, переперевод...")
            translated_texts = translator.retranslate_weak(texts, translated_texts, scores)
        else:
            print(f"   Все сегменты OK (мин. оценка: {min(scores) if scores else 'N/A'})")

    # Validate reading speed (CPS) and shorten if needed
    if max_cps > 0:
        translated_texts = validate_reading_speed(blocks, translated_texts, translator, max_cps)

    translated_blocks: List[SrtBlock] = []
    for block, translated_text in zip(blocks, translated_texts):
        translated_lines = tuple(translated_text.split("\n"))
        translated_blocks.append(SrtBlock(
            index=block.index,
            timecode=block.timecode,
            lines=translated_lines,
        ))

    print()
    print(f"💾 Сохраняю: {output_path}")
    write_srt(translated_blocks, output_path, "utf-8")

    # Clean up progress file on success
    if progress_path.exists():
        try:
            progress_path.unlink()
        except Exception:
            pass

    total_elapsed = int(time.time() - cli_t0)
    m, s = divmod(total_elapsed, 60)
    t_total = f"{m}m {s}s" if m else f"{s}s"
    print(f"✅ Готово! ({t_total})")


def main():
    parser = argparse.ArgumentParser(
        description="🎬 Переводчик субтитров (Ollama + Translating Gemma)"
    )
    parser.add_argument("input", type=Path, help="Входной SRT файл")
    parser.add_argument("--out", "-o", type=Path, default=None, help="Выходной файл")
    parser.add_argument("--lang", "-l", type=str, default="Russian", help="Целевой язык")
    parser.add_argument("--model", "-m", type=str, default="gemma4:e12b", help="Модель Ollama для перевода (рекомендуется: gemma4:e12b, qwen3.5:8b, hunyuan-mt:7b)")
    parser.add_argument("--chunk-size", type=int, default=1000, help="Макс. символов в одном запросе к модели (по умолчанию 1000)")
    parser.add_argument("--context", "-c", type=str, default="", help="Контекст для перевода (например: 'Сериал о больнице с медицинской терминологией')")
    parser.add_argument("--source-lang", "-s", type=str, default="", help="Исходный язык (например: English). По умолчанию — автоопределение")
    parser.add_argument("--two-pass", action="store_true", help="Двухпроходный перевод: translate → review")
    parser.add_argument("--review-model", type=str, default="", help="Модель для review-прохода (по умолчанию — та же)")
    parser.add_argument("--glossary", "-g", type=str, default="",
                        help="Глоссарий: 'Tony Stark=Тони Старк, SHIELD=Щ.И.Т.' или путь к файлу (key=value на каждой строке)")
    parser.add_argument("--context-window", type=int, default=3,
                        help="Количество соседних субтитров для контекста (по умолчанию 3)")
    parser.add_argument("--genre", type=str, default="",
                        choices=["", "comedy", "drama", "anime", "documentary", "action", "horror"],
                        help="Жанр контента — адаптирует стиль перевода")
    parser.add_argument("--max-cps", type=float, default=0,
                        help="Макс. символов в секунду (0 = без проверки, по умолчанию 0 — отключено)")
    parser.add_argument("--aux-model", type=str, default="",
                        help="Вспомогательная модель для анализа/глоссария/QE (по умолчанию: gemma3:12b)")
    parser.add_argument("--no-context-analysis", action="store_true",
                        help="Отключить анализ контента перед переводом")
    parser.add_argument("--no-qe", action="store_true",
                        help="Отключить оценку качества после перевода")
    parser.add_argument("--no-auto-glossary", action="store_true",
                        help="Отключить авто-генерацию глоссария")
    parser.add_argument("--tm", type=Path, default=None,
                        help="Путь к SQLite Translation Memory (кэш между сеансами). "
                             "По умолчанию: ~/.cache/ollama-subtitle-translator/tm.db")
    parser.add_argument("--no-tm", action="store_true",
                        help="Отключить Translation Memory")

    args = parser.parse_args()
    
    if not args.input.exists():
        print(f"❌ Файл не найден: {args.input}")
        sys.exit(1)
    
    lang_key = args.lang.lower()
    lang_code = LANGUAGES.get(lang_key, lang_key[:2].lower())
    
    output_path = args.out
    if output_path is None:
        stem = args.input.stem
        output_path = args.input.with_name(f"{stem}.{lang_code}.srt")
    
    # Parse glossary: either inline string or path to file
    glossary: Dict[str, str] = {}
    if args.glossary:
        glossary_path = Path(args.glossary)
        if glossary_path.exists():
            glossary = parse_glossary(glossary_path.read_text(encoding="utf-8"))
        else:
            glossary = parse_glossary(args.glossary)
        if glossary:
            print(f"📖 Глоссарий: {len(glossary)} терминов")

    tm_path: Optional[Path] = None
    if not args.no_tm:
        tm_path = args.tm or Path.home() / ".cache" / "ollama-subtitle-translator" / "tm.db"

    translate_srt(args.input, output_path, args.lang, args.model, args.context,
                  args.source_lang, args.two_pass, args.review_model, args.chunk_size,
                  glossary=glossary, context_window=args.context_window,
                  genre=args.genre, max_cps=args.max_cps,
                  context_analysis=not args.no_context_analysis,
                  qe=not args.no_qe,
                  auto_glossary=not args.no_auto_glossary,
                  tm_path=tm_path)


if __name__ == "__main__":
    main()
