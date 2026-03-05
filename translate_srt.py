#!/usr/bin/env python3
"""
🎬 Переводчик субтитров (.srt) через Ollama
Использует модель Translating Gemma (Google) — специализированная модель для перевода.

Поддерживает множество языков: русский, английский, китайский, японский, корейский,
немецкий, французский, испанский, итальянский, португальский и др.

Установка:
  1. Установить Ollama: https://ollama.com/download
  2. ollama pull translategemma:4b
  3. pip install requests

Примеры:
  python translate_srt.py movie.srt                    # EN→RU (по умолчанию)
  python translate_srt.py movie.srt -l Japanese        # EN→JP
  python translate_srt.py movie.srt -l German -o de.srt
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional, Dict
import time
import uuid
import logging

logger = logging.getLogger("translate_srt")

try:
    import requests
except ImportError:
    print("❌ Не установлен requests: pip install requests")
    sys.exit(1)


# Поддерживаемые языки и их коды для имени файла
LANGUAGES = {
    "russian": "ru", "ru": "ru", "русский": "ru",
    "english": "en", "en": "en", "английский": "en",
    "chinese": "zh", "zh": "zh", "китайский": "zh",
    "japanese": "ja", "ja": "ja", "японский": "ja",
    "korean": "ko", "ko": "ko", "корейский": "ko",
    "german": "de", "de": "de", "немецкий": "de",
    "french": "fr", "fr": "fr", "французский": "fr",
    "spanish": "es", "es": "es", "испанский": "es",
    "italian": "it", "it": "it", "итальянский": "it",
    "portuguese": "pt", "pt": "pt", "португальский": "pt",
    "turkish": "tr", "tr": "tr", "турецкий": "tr",
    "arabic": "ar", "ar": "ar", "арабский": "ar",
    "thai": "th", "th": "th", "тайский": "th",
    "vietnamese": "vi", "vi": "vi", "вьетнамский": "vi",
    "polish": "pl", "pl": "pl", "польский": "pl",
    "dutch": "nl", "nl": "nl", "голландский": "nl",
    "ukrainian": "uk", "uk": "uk", "украинский": "uk",
}

# Регулярка для таймкодов SRT
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}.*$")
# Регулярка для HTML-тегов (сохраняем их)
TAG_RE = re.compile(r"(<[^>]+>|{\\\w+\d*})")


def post_with_retry(url: str, json: dict, timeout: int = 120, attempts: int = 3, backoff: float = 1.0) -> Optional[requests.Response]:
    """POST with simple exponential backoff retry. Returns Response or None."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        t0 = time.time()
        try:
            resp = requests.post(url, json=json, timeout=timeout)
            elapsed = time.time() - t0
            logger.info("POST %s attempt=%d status=%d elapsed=%.2fs", url, attempt, resp.status_code, elapsed)
            return resp
        except requests.RequestException as e:
            elapsed = time.time() - t0
            last_exc = e
            sleep = backoff * (2 ** (attempt - 1))
            logger.warning("POST %s attempt=%d failed (%.2fs): %s — retrying in %.1fs", url, attempt, elapsed, e, sleep)
            time.sleep(sleep)
    logger.error("post_with_retry exhausted %d attempts to %s: %s", attempts, url, last_exc)
    return None


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
    return text


def validate_translation(original: str, translated: str) -> bool:
    """Check if translated text looks reasonable compared to original.

    Returns True if the translation passes basic quality checks.
    """
    if not translated or not translated.strip():
        return False
    # Translation is identical to source (model didn't translate)
    if translated.strip() == original.strip():
        return False
    # Translation is absurdly longer than original (likely hallucination)
    if len(translated) > len(original) * 5 and len(original) > 10:
        return False
    # Translation is just punctuation or whitespace
    stripped = re.sub(r'[\s\W]+', '', translated)
    if not stripped:
        return False
    return True


class Translator:
    """Переводчик через Ollama + Translating Gemma"""

    def __init__(self, model: str = "translategemma:4b", target_lang: str = "Russian",
                 ollama_url: str = "http://127.0.0.1:11434", context: str = "",
                 temperature: float = 0.0, source_lang: str = "",
                 two_pass: bool = False, review_model: str = ""):
        self.model = model
        self.target_lang = target_lang
        self.source_lang = source_lang  # пустая строка = автоопределение
        self.base_url = ollama_url
        self.context = context
        self.temperature = float(temperature)

        self.two_pass = two_pass
        self.review_model = review_model or model  # по умолчанию та же модель
        self._cache: Dict[str, str] = {}
        self._cache_hits = 0

        # Quick connectivity check (model availability already verified by web UI)
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=3)
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
        except requests.exceptions.ConnectionError:
            raise RuntimeError("Ollama не запущен! Запустите: ollama serve")

        logger.info("Translator ready: model=%s lang=%s two_pass=%s", model, target_lang, two_pass)

    def translate(self, text: str, prev_text: str = "", next_text: str = "") -> str:
        """Переводит текст с учётом соседних субтитров для связности."""
        if not text.strip():
            return text

        # Cache lookup — повторяющиеся фразы переводятся одинаково
        cache_key = text.strip()
        if cache_key in self._cache:
            self._cache_hits += 1
            logger.debug("translate: cache hit #%d for '%s'", self._cache_hits, cache_key[:40])
            return self._cache[cache_key]

        logger.debug("translate: input length=%d chars", len(text))
        protected_text, tags = protect_tags(text)

        # Формируем промпт с учётом контекста
        parts: List[str] = []
        if self.context and self.context.strip():
            parts.append(f"Context: {self.context.strip()}")

        # Sliding window: соседние субтитры дают модели понимание диалога
        if prev_text or next_text:
            parts.append("Surrounding subtitles for reference (do NOT translate these):")
            if prev_text:
                parts.append(f"[BEFORE]: {prev_text}")
            if next_text:
                parts.append(f"[AFTER]: {next_text}")
            parts.append("")

        from_part = f" from {self.source_lang}" if self.source_lang else ""
        parts.append(
            f"Translate the following subtitle{from_part} into {self.target_lang}. "
            "Keep it concise for subtitles. Provide only the translation, nothing else."
        )
        parts.append(f"\n{protected_text}")
        prompt = "\n".join(parts)

        payload = {"model": self.model, "prompt": prompt, "stream": False}
        # include temperature if applicable
        if self.temperature is not None:
            payload["temperature"] = float(self.temperature)

        resp = post_with_retry(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=120,
            attempts=3,
            backoff=1.0,
        )

        if resp is None:
            logger.error("Ollama request failed after retries: model=%s url=%s", self.model, f"{self.base_url}/api/generate")
            return text

        if resp.status_code != 200:
            body = resp.text if hasattr(resp, "text") else ""
            logger.warning("Ollama non-200: status=%d model=%s body=%.200s", resp.status_code, self.model, body)
            return text

        try:
            data = resp.json()
            translated = data.get("response", "").strip()
            logger.debug("translate: model=%s input_len=%d output_len=%d", self.model, len(text), len(translated))
        except Exception:
            try:
                body = resp.text
            except Exception:
                body = ""
            logger.exception("Failed to parse Ollama JSON: model=%s body=%.500s", self.model, body)
            translated = body.strip()

        # Validate quality; retry once if suspicious
        if not validate_translation(text, translated):
            logger.warning("translate: validation failed (original=%d chars, translated=%d chars), retrying once",
                           len(text), len(translated))
            retry_resp = post_with_retry(
                f"{self.base_url}/api/generate", json=payload, timeout=120, attempts=1, backoff=0,
            )
            if retry_resp and retry_resp.status_code == 200:
                try:
                    translated = retry_resp.json().get("response", "").strip()
                except Exception:
                    pass
            # If still bad, return original to avoid garbage in output
            if not validate_translation(text, translated):
                logger.warning("translate: retry also failed validation, returning original text")
                return text

        result = restore_tags(translated, tags)
        self._cache[cache_key] = result
        return result

    def review(self, original: str, translated: str,
               prev_original: str = "", prev_translated: str = "",
               next_original: str = "") -> str:
        """Второй проход: проверяет и правит перевод с учётом контекста.

        Возвращает исправленный перевод или оригинальный, если модель
        решила, что правок не нужно.
        """
        if not translated.strip() or not original.strip():
            return translated

        parts: List[str] = []
        if self.context and self.context.strip():
            parts.append(f"Context: {self.context.strip()}")

        # Окружающие субтитры для понимания диалога
        if prev_original or next_original:
            parts.append("Surrounding subtitles for reference:")
            if prev_original:
                line = f"[BEFORE]: {prev_original}"
                if prev_translated:
                    line += f" → {prev_translated}"
                parts.append(line)
            if next_original:
                parts.append(f"[AFTER]: {next_original}")
            parts.append("")

        from_part = f" from {self.source_lang}" if self.source_lang else ""
        parts.append(
            f"You are reviewing a subtitle translation{from_part} into {self.target_lang}.\n"
            "Check the translation for:\n"
            "- Accuracy: does it convey the original meaning?\n"
            "- Natural flow: does it sound natural in the target language?\n"
            "- Consistency with surrounding subtitles\n"
            "- Conciseness: subtitles must be short (max ~42 chars per line)\n\n"
            f"Original: {original}\n"
            f"Translation: {translated}\n\n"
            "If the translation is good, output it exactly as-is.\n"
            "If it needs fixes, output ONLY the corrected translation, nothing else."
        )
        prompt = "\n".join(parts)

        payload = {"model": self.review_model, "prompt": prompt, "stream": False}
        if self.temperature is not None:
            payload["temperature"] = float(self.temperature)

        resp = post_with_retry(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=120,
            attempts=2,
            backoff=1.0,
        )

        if resp is None or resp.status_code != 200:
            logger.warning("review: request failed, keeping original translation")
            return translated

        try:
            reviewed = resp.json().get("response", "").strip()
        except Exception:
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

    def translate_batch(self, texts: List[str], max_chars: int = 1000,
                        on_progress: Optional["callable"] = None,
                        on_phase: Optional["callable"] = None) -> List[str]:
        """Translate a list of texts as a single request (or multiple chunked requests).

        Uses sliding window context for per-segment fallback to maintain dialogue coherence.
        on_progress(done, total) is called after each chunk completes.
        on_phase(phase_name) is called when the processing phase changes.
        Returns list of translated strings in the same order.
        """
        if not texts:
            return []

        # Helper to chunk by character length, returning (start_index, chunk_texts)
        def make_chunks(texts_list, max_chars_local):
            chunks: List[Tuple[int, List[str]]] = []
            cur: List[str] = []
            cur_start = 0
            cur_len = 0
            for i, t in enumerate(texts_list):
                if cur and cur_len + len(t) > max_chars_local:
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
        results: List[str] = []
        prev_chunk_tail: List[str] = []  # last segments of previous chunk for cross-chunk context

        for chunk_idx, (chunk_start, chunk) in enumerate(chunks):
            logger.info("translate_batch: chunk %d/%d segments=%d", chunk_idx + 1, len(chunks), len(chunk))
            # Protect tags per segment
            protected_list = []
            tags_list: List[Dict[str, str]] = []
            for seg in chunk:
                p, tags = protect_tags(seg)
                protected_list.append(p)
                tags_list.append(tags)

            # Build prompt with delimiter-based output format and numbered segments
            numbered = [f"[{i+1}] {seg}" for i, seg in enumerate(protected_list)]
            segments_payload = "\n|||SEP|||\n".join(numbered)

            context_line = ""
            if self.context and self.context.strip():
                context_line = f"Context: {self.context.strip()}\n\n"

            # Cross-chunk context: show tail of previous chunk so the model keeps coherence
            prev_context = ""
            if prev_chunk_tail:
                prev_lines = "\n".join(prev_chunk_tail)
                prev_context = (
                    "Previous subtitles for reference (do NOT translate these, they are already translated):\n"
                    f"{prev_lines}\n\n"
                )

            from_part = f" from {self.source_lang}" if self.source_lang else ""
            prompt = (
                f"{context_line}"
                f"{prev_context}"
                f"Translate each segment below{from_part} into {self.target_lang}.\n"
                "Keep translations concise — suitable for subtitles (max ~42 characters per line).\n"
                "Preserve any placeholders exactly (e.g. __TAG_xxx__).\n"
                "Each segment is numbered [1], [2], etc. — translate them in the same order.\n"
                "Separate translated segments with |||SEP||| on its own line.\n"
                "Output ONLY the translations (without numbers), nothing else.\n\n"
                f"{segments_payload}"
            )

            payload = {"model": self.model, "prompt": prompt, "stream": False}
            if self.temperature is not None:
                payload["temperature"] = float(self.temperature)

            resp = post_with_retry(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=180,
                attempts=3,
                backoff=1.0,
            )

            if resp is None:
                logger.error("Ollama batch failed: model=%s chunk=%d/%d", self.model, chunk_idx + 1, len(chunks))
                results.extend(chunk)
                continue

            if resp.status_code != 200:
                body = resp.text if hasattr(resp, "text") else ""
                logger.warning("Ollama batch non-200: status=%d model=%s chunk=%d/%d body=%.200s",
                               resp.status_code, self.model, chunk_idx + 1, len(chunks), body)
                results.extend(chunk)
                continue

            # Parse delimiter-based response
            translated_list: List[str] = []
            try:
                data = resp.json()
                model_response = data.get("response", "")
                logger.debug("translate_batch: raw model response (first 500 chars): %.500s", model_response)

                # Split by delimiter
                parts = [p.strip() for p in model_response.split("|||SEP|||")]
                # Remove empty leading/trailing parts
                parts = [p for p in parts if p]
                # Strip leading segment numbers like [1], [2] etc. that model may echo back
                parts = [re.sub(r"^\[\d+\]\s*", "", p) for p in parts]

                if len(parts) == len(chunk):
                    translated_list = parts
                else:
                    translated_list = []
            except Exception:
                translated_list = []

            # Validate each translated segment in the batch
            if translated_list and len(translated_list) == len(chunk):
                bad_count = sum(1 for orig, tr in zip(chunk, translated_list) if not validate_translation(orig, tr))
                if bad_count > len(chunk) * 0.5:
                    logger.warning("translate_batch: chunk %d/%d has %d/%d bad translations, falling back",
                                   chunk_idx + 1, len(chunks), bad_count, len(chunk))
                    translated_list = []  # force fallback
                elif bad_count > 0:
                    logger.info("translate_batch: chunk %d/%d has %d/%d suspicious translations",
                                chunk_idx + 1, len(chunks), bad_count, len(chunk))

            if translated_list and len(translated_list) == len(chunk):
                logger.info("translate_batch: chunk %d/%d parsed %d segments OK", chunk_idx + 1, len(chunks), len(translated_list))
                # Restore tags (batch path — tags were protected before sending to model)
                for translated, tags in zip(translated_list, tags_list):
                    restored = restore_tags(translated, tags)
                    results.append(restored)
            else:
                # Delimiter parsing failed or wrong count — fallback to one-by-one with sliding window
                logger.warning("translate_batch: chunk %d/%d delimiter parse failed (got %d, expected %d), falling back to per-segment translation",
                               chunk_idx + 1, len(chunks), len(translated_list) if translated_list else 0, len(chunk))
                for seg_idx, seg in enumerate(chunk):
                    # Sliding window: pass neighboring subtitles for coherence
                    global_idx = chunk_start + seg_idx
                    prev_text = texts[global_idx - 1] if global_idx > 0 else ""
                    next_text = texts[global_idx + 1] if global_idx < len(texts) - 1 else ""
                    # self.translate() handles protect/restore tags internally
                    translated = self.translate(seg, prev_text=prev_text, next_text=next_text)
                    results.append(translated)
                    logger.debug("translate_batch fallback: seg %d/%d done", seg_idx + 1, len(chunk))

            # Save tail of current chunk for cross-chunk context in next iteration
            prev_chunk_tail = chunk[-3:] if len(chunk) >= 3 else chunk[:]

            if on_progress:
                on_progress(len(results), len(texts))

        # Second pass: review each translation for quality
        if self.two_pass and results:
            logger.info("translate_batch: starting review pass (%d segments)", len(results))
            if on_phase:
                on_phase("reviewing")
            reviewed: List[str] = []
            for i, (orig, trans) in enumerate(zip(texts, results)):
                prev_orig = texts[i - 1] if i > 0 else ""
                prev_trans = results[i - 1] if i > 0 else ""
                next_orig = texts[i + 1] if i < len(texts) - 1 else ""
                corrected = self.review(
                    orig, trans,
                    prev_original=prev_orig,
                    prev_translated=prev_trans,
                    next_original=next_orig,
                )
                reviewed.append(corrected)
                if on_progress:
                    on_progress(i + 1, len(texts))
            logger.info("translate_batch: review pass done")
            return reviewed

        return results


def translate_srt(input_path: Path, output_path: Path, target_lang: str = "Russian",
                  model: str = "translategemma:4b",
                  context: str = "", source_lang: str = "",
                  two_pass: bool = False, review_model: str = "",
                  chunk_size: int = 1000) -> None:
    """Переводит SRT файл."""
    print(f"📖 Читаю: {input_path}")
    text, encoding = read_srt_file(input_path)
    blocks = parse_srt(text)
    total = len(blocks)
    print(f"   Субтитров: {total}")

    translator = Translator(model, target_lang, context=context, source_lang=source_lang,
                            two_pass=two_pass, review_model=review_model)

    print(f"🔄 Перевод...")
    texts = [b.text() for b in blocks]
    cli_phase = "translating"

    def cli_progress(done: int, total_count: int):
        pct = done / total_count * 100 if total_count else 100
        bar_len = 30
        filled = int(bar_len * done / total_count) if total_count else bar_len
        bar = "█" * filled + "░" * (bar_len - filled)
        label = "Проверка" if cli_phase == "reviewing" else "Перевод"
        print(f"   {label}: [{bar}] {done}/{total_count} ({pct:.1f}%)", end="\r")

    def cli_on_phase(phase: str):
        nonlocal cli_phase
        cli_phase = phase
        if phase == "reviewing":
            print()
            print(f"🔍 Проверка перевода (проход 2/2)...")

    translated_texts = translator.translate_batch(texts, max_chars=chunk_size,
                                                  on_progress=cli_progress,
                                                  on_phase=cli_on_phase)

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
    print("✅ Готово!")


def main():
    parser = argparse.ArgumentParser(
        description="🎬 Переводчик субтитров (Ollama + Translating Gemma)"
    )
    parser.add_argument("input", type=Path, help="Входной SRT файл")
    parser.add_argument("--out", "-o", type=Path, default=None, help="Выходной файл")
    parser.add_argument("--lang", "-l", type=str, default="Russian", help="Целевой язык")
    parser.add_argument("--model", "-m", type=str, default="translategemma:4b", help="Модель Ollama")
    parser.add_argument("--chunk-size", type=int, default=1000, help="Макс. символов в одном запросе к модели (по умолчанию 1000)")
    parser.add_argument("--context", "-c", type=str, default="", help="Контекст для перевода (например: 'Сериал о больнице с медицинской терминологией')")
    parser.add_argument("--source-lang", "-s", type=str, default="", help="Исходный язык (например: English). По умолчанию — автоопределение")
    parser.add_argument("--two-pass", action="store_true", help="Двухпроходный перевод: translate → review")
    parser.add_argument("--review-model", type=str, default="", help="Модель для review-прохода (по умолчанию — та же)")

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
    
    translate_srt(args.input, output_path, args.lang, args.model, args.context,
                  args.source_lang, args.two_pass, args.review_model, args.chunk_size)


if __name__ == "__main__":
    main()
