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
import json
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
    """Читает файл с автоопределением кодировки."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1251"), "cp1251"


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


class Translator:
    """Переводчик через Ollama + Translating Gemma"""
    
    def __init__(self, model: str = "translategemma:4b", target_lang: str = "Russian", 
                 ollama_url: str = "http://127.0.0.1:11434", context: str = "", temperature: float = 0.0):
        print(f"🔄 Подключение к Ollama ({model})...")
        self.model = model
        self.target_lang = target_lang
        self.base_url = ollama_url
        self.context = context
        self.temperature = float(temperature)
        
        # Проверяем Ollama
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if resp.status_code != 200:
                raise Exception("Ollama не отвечает")
            models = [m["name"] for m in resp.json().get("models", [])]
            if not any(model in m for m in models):
                print(f"⚠️  Модель {model} не найдена. Доступные: {models}")
                print(f"   Запустите: ollama pull {model}")
                sys.exit(1)
        except requests.exceptions.ConnectionError:
            print("❌ Ollama не запущен!")
            print("   Запустите: ollama serve")
            sys.exit(1)
        
        print(f"   Целевой язык: {target_lang}")
        if context:
            print(f"   Контекст: {context[:60]}{'...' if len(context) > 60 else ''}")
        print("✅ Подключено!")
    
    def translate(self, text: str) -> str:
        """Переводит текст."""
        if not text.strip():
            return text

        logger.debug("translate: input length=%d chars", len(text))
        protected_text, tags = protect_tags(text)

        # Формируем промпт с учётом контекста
        if self.context and self.context.strip():
            prompt = f"Context: {self.context.strip()}\n\nTranslate the following segment into {self.target_lang}, keeping the context in mind. Provide only the translation without additional explanation.\n\n{protected_text}"
        else:
            prompt = f"Translate the following segment into {self.target_lang}, without additional explanation.\n\n{protected_text}"

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

        return restore_tags(translated, tags)

    def translate_batch(self, texts: List[str], max_chars: int = 2000) -> List[str]:
        """Translate a list of texts as a single request (or multiple chunked requests).

        Returns list of translated strings in the same order.
        """
        if not texts:
            return []

        # Helper to chunk by character length
        def make_chunks(texts_list, max_chars_local):
            chunks = []
            cur = []
            cur_len = 0
            for t in texts_list:
                if cur and cur_len + len(t) > max_chars_local:
                    chunks.append(cur)
                    cur = []
                    cur_len = 0
                cur.append(t)
                cur_len += len(t)
            if cur:
                chunks.append(cur)
            return chunks

        chunks = make_chunks(texts, max_chars)
        results: List[str] = []

        for chunk_idx, chunk in enumerate(chunks):
            logger.info("translate_batch: chunk %d/%d segments=%d", chunk_idx + 1, len(chunks), len(chunk))
            # Protect tags per segment
            protected_list = []
            tags_list: List[Dict[str, str]] = []
            for seg in chunk:
                p, tags = protect_tags(seg)
                protected_list.append(p)
                tags_list.append(tags)

            # Build prompt requiring strict JSON output
            segments_payload = "\n".join([f"<<<SEG>>>{s}<<<ENDSEG>>>" for s in protected_list])
            prompt = (
                f"Translate the following segments into {self.target_lang}.\n"
                "Preserve placeholders exactly (e.g. __TAG_xxx__).\n"
                "Return ONLY a JSON object with key \"segments\" that is an array of strings in the same order.\n\n"
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

            # Try to parse JSON from response
            translated_list: List[str] = []
            try:
                data = resp.json()
                translated_list = data.get("segments") or data.get("segments")
            except Exception:
                # Try to extract JSON substring
                txt = resp.text
                try:
                    start = txt.index("{")
                    end = txt.rindex("}")
                    data = json.loads(txt[start:end+1])
                    translated_list = data.get("segments", [])
                except Exception:
                    translated_list = []

            if not translated_list or len(translated_list) != len(chunk):
                # If model didn't follow JSON contract, fallback to splitting by markers
                txt = resp.text
                # crude split: try to split by <<<ENDSEG>>>
                parts = [p.strip() for p in txt.split("<<<ENDSEG>>>") if p.strip()]
                # remove potential opening markers
                cleaned = []
                for p in parts:
                    cleaned.append(p.replace("<<<SEG>>>", "").strip())
                # pad or trim
                while len(cleaned) < len(chunk):
                    cleaned.append("")
                translated_list = cleaned[:len(chunk)]

            # Restore tags
            for translated, tags in zip(translated_list, tags_list):
                restored = restore_tags(translated, tags)
                results.append(restored)

        return results


def translate_srt(input_path: Path, output_path: Path, target_lang: str = "Russian",
                  model: str = "translategemma:4b", batch_size: int = 10, context: str = "") -> None:
    """Переводит SRT файл."""
    print(f"📖 Читаю: {input_path}")
    text, encoding = read_srt_file(input_path)
    blocks = parse_srt(text)
    print(f"   Субтитров: {len(blocks)}")
    
    translator = Translator(model, target_lang, context=context)
    
    print(f"🔄 Перевод...")
    translated_blocks: List[SrtBlock] = []
    total = len(blocks)
    
    for i, block in enumerate(blocks):
        translated_text = translator.translate(block.text())
        translated_lines = tuple(translated_text.split("\n"))
        translated_blocks.append(SrtBlock(
            index=block.index,
            timecode=block.timecode,
            lines=translated_lines
        ))
        
        # Прогресс
        if (i + 1) % batch_size == 0 or i == total - 1:
            pct = (i + 1) / total * 100
            bar_len = 30
            filled = int(bar_len * (i + 1) / total)
            bar = "█" * filled + "░" * (bar_len - filled)
            print(f"   [{bar}] {i+1}/{total} ({pct:.1f}%)", end="\r")
    
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
    parser.add_argument("--batch", "-b", type=int, default=10, help="Размер батча для прогресса")
    parser.add_argument("--context", "-c", type=str, default="", help="Контекст для перевода (например: 'Сериал о больнице с медицинской терминологией')")
    
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
    
    translate_srt(args.input, output_path, args.lang, args.model, args.batch, args.context)


if __name__ == "__main__":
    main()
