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
from typing import List, Tuple

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


def protect_tags(text: str) -> Tuple[str, List[str]]:
    """Защищает теги от перевода."""
    tags: List[str] = []
    
    def replacer(match):
        tags.append(match.group(0))
        return f"⟨{len(tags)-1}⟩"
    
    protected = TAG_RE.sub(replacer, text)
    return protected, tags


def restore_tags(text: str, tags: List[str]) -> str:
    """Восстанавливает теги из плейсхолдеров."""
    for i, tag in enumerate(tags):
        text = text.replace(f"⟨{i}⟩", tag)
    return text


class Translator:
    """Переводчик через Ollama + Translating Gemma"""
    
    def __init__(self, model: str = "translategemma:4b", target_lang: str = "Russian", 
                 ollama_url: str = "http://127.0.0.1:11434"):
        print(f"🔄 Подключение к Ollama ({model})...")
        self.model = model
        self.target_lang = target_lang
        self.base_url = ollama_url
        
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
        print("✅ Подключено!")
    
    def translate(self, text: str) -> str:
        """Переводит текст."""
        if not text.strip():
            return text
        
        protected_text, tags = protect_tags(text)
        
        prompt = f"Translate the following segment into {self.target_lang}, without additional explanation.\n\n{protected_text}"
        
        response = requests.post(
            f"{self.base_url}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
            timeout=120
        )
        
        if response.status_code != 200:
            print(f"❌ Ошибка: {response.text}")
            return text
        
        translated = response.json().get("response", "").strip()
        return restore_tags(translated, tags)


def translate_srt(input_path: Path, output_path: Path, target_lang: str = "Russian",
                  model: str = "translategemma:4b", batch_size: int = 10) -> None:
    """Переводит SRT файл."""
    print(f"📖 Читаю: {input_path}")
    text, encoding = read_srt_file(input_path)
    blocks = parse_srt(text)
    print(f"   Субтитров: {len(blocks)}")
    
    translator = Translator(model, target_lang)
    
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
    
    translate_srt(args.input, output_path, args.lang, args.model, args.batch)


if __name__ == "__main__":
    main()
