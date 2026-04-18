<div align="center">

# 🎬 Ollama Subtitle Translator

**Fully-offline subtitle translator powered by modern 2026 LLMs running locally via [Ollama](https://ollama.com).**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg)](docker-compose.yml)
[![Tests](https://img.shields.io/badge/tests-126%20passing-brightgreen.svg)](tests/)
[![Models](https://img.shields.io/badge/Gemma%204-e12b-blueviolet.svg)](https://ollama.com/library/gemma4)

**🌍 [English](README.md) · [Русский](README.ru.md)**

</div>

---

> No API keys. No cloud. No data leaks. Everything runs on your machine — your subtitles never leave your computer.
>
> Translate `.srt` files and extract subtitles from videos (MKV / MP4 / AVI / MOV / WebM) between **17 languages** using top-tier 2026 open-weight models: **Gemma 4**, **Qwen 3.5**, **Hunyuan-MT**, **Llama 4 Scout**.

## ✨ What makes it different

- **🧠 Smart Quality Estimation** — LLM-as-judge scores every ambiguous segment 1-5, re-translates weak ones automatically
- **💾 Persistent Translation Memory** — SQLite cache survives across sessions. Series episodes translate **30-50% faster** from episode 2 onwards
- **📚 Glossary Enforcement** — Unicode-aware mechanical substitution catches cases where the LLM ignored your glossary (works for Cyrillic, CJK, Arabic)
- **🎯 Context Analysis** — analyzes entire subtitle file before translation to understand characters, themes, tone
- **🎭 Genre Presets** — comedy, drama, anime, documentary, action, horror — each with tailored translation instructions
- **⚡ Parallel Chunks** — multiple chunks translated concurrently with preserved cross-chunk coherence
- **🔄 Crash Recovery** — resumable progress file, won't lose work if Ollama crashes mid-translation
- **🎬 Video Support** — probe + extract embedded subtitle tracks via FFmpeg, auto-download on Windows
- **🌐 Modern Web UI** — dark theme, drag-and-drop, SSE progress streaming, model management with one-click download

---

## 🚀 Quick Start

### 1. Install Ollama

[Download from ollama.com/download](https://ollama.com/download), then:

```bash
ollama serve
ollama pull gemma4:e12b      # Main translation model
ollama pull qwen3.5:8b        # Auxiliary (glossary, QE, analysis)
```

### 2. Install & Run

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:8847
```

**Or CLI:**

```bash
python translate_srt.py movie.srt                   # EN → RU
python translate_srt.py movie.srt -l Japanese       # EN → JP
python translate_srt.py series.srt --genre drama    # With genre preset
```

---

## 🤖 Recommended Models (2026)

| Model | Size | VRAM | Best for | Ollama tag |
|---|---|---|---|---|
| **Gemma 4 12B** ⭐ | 8 GB | ~16 GB | General use, recommended default | `gemma4:e12b` |
| **Gemma 4 4B** | 3 GB | ~8 GB | Weaker hardware, still 140 languages | `gemma4:e4b` |
| **Gemma 4 27B** | 17 GB | ~24 GB | Maximum Gemma quality | `gemma4:e27b` |
| **Qwen 3.5 8B** | 5 GB | ~12 GB | Excellent for CJK, 201 languages | `qwen3.5:8b` |
| **Qwen 3.5 32B** | 20 GB | ~24 GB | Top-tier for complex content, sarcasm, idioms | `qwen3.5:32b` |
| **Hunyuan-MT 7B** | 4 GB | ~10 GB | Translation-specialized (Tencent) | `hunyuan-mt:7b` |
| **Llama 4 Scout** | big | server-grade | **10M context** — whole movie in a single prompt | `llama4:scout` |

Legacy narrow translation models (`translategemma`, `nllb`, `alma`, `tower-*`) are auto-detected and routed through an auxiliary model for glossary/QE tasks.

---

## ⚡ Performance Expectations

Rough translation time for a 90-min movie (~1500 subtitles) with **Gemma 4 12B**:

| Hardware | Full pipeline | Subtitles only (`--no-qe`) |
|---|---|---|
| RTX 4090 / M2 Max+ | **5-8 min** | 3-5 min |
| RTX 4070 / 3080 / M2 Pro | **8-15 min** | 5-10 min |
| RTX 3060 12GB / M1 Pro | **15-25 min** | 10-15 min |
| CPU only | **30-60 min** | 20-40 min |

**Translation Memory bonus**: For TV series, episode 1 takes full time; episodes 2+ hit the cache for recurring phrases, names, and dialogue patterns — **30-50% speedup**.

For short clips (~400 subtitles / 24 min episode): multiply by ~0.25-0.3.

---

## 🌐 Supported Languages

Russian · English · Chinese · Japanese · Korean · German · French · Spanish · Italian · Portuguese · Turkish · Arabic · Ukrainian · Polish · Dutch · Vietnamese · Thai

Source language is auto-detected, or you can specify it explicitly with `-s <language>`.

---

## 🖥️ Web Interface

Navigate to `http://localhost:8847`. Features:

- **Drag & drop** `.srt` files or extract from videos
- **Model selector** — visual cards with status (ready / not installed / downloading), click to install
- **Genre presets** — comedy, drama, anime, documentary, action, horror
- **Auto-glossary** — LLM identifies character names, places, recurring terms
- **Context analysis** — pre-translation pass for consistent tone and style
- **Two-pass mode** — translate + review for maximum quality
- **Real-time progress** via SSE streaming
- **Auto-save** to any folder next to your video
- **Quality estimation** with auto-retranslate of weak segments

### Video subtitle extraction

1. Switch to **"Извлечь из видео"** tab
2. Select video file (supports network / NAS paths)
3. Click **"Сканировать"** — lists embedded subtitle tracks
4. Pick the track, click **"Перевести"**

> Requires **ffmpeg**. On Windows, the app auto-downloads it on first use. macOS: `brew install ffmpeg`. Linux: `apt install ffmpeg`.

---

## 💻 CLI Reference

```bash
python translate_srt.py <file.srt> [options]
```

| Flag | Description | Default |
|---|---|---|
| `-l`, `--lang` | Target language | `Russian` |
| `-s`, `--source-lang` | Source language (auto-detect if omitted) | — |
| `-o`, `--out` | Output file | `<input>.<code>.srt` |
| `-m`, `--model` | Ollama model | `gemma4:e12b` |
| `-c`, `--context` | Free-form context hint | — |
| `-g`, `--glossary` | Glossary: `"Tony=Тони, SHIELD=Щ.И.Т."` or path to file | — |
| `--genre` | `comedy` / `drama` / `anime` / `documentary` / `action` / `horror` | — |
| `--chunk-size` | Max chars per batch request | `1000` |
| `--context-window` | Neighbor subtitles for coherence | `3` |
| `--max-cps` | Max chars per second (reading speed check) | `0` (off) |
| `--two-pass` | Translate + review pass | off |
| `--review-model` | Model for review pass | same as `--model` |
| `--aux-model` | Auxiliary model for analysis / glossary / QE | `qwen3.5:8b` |
| `--no-context-analysis` | Skip pre-translation analysis | — |
| `--no-qe` | Skip quality estimation + retranslate | — |
| `--no-auto-glossary` | Skip auto-glossary generation | — |
| `--tm` | Path to SQLite Translation Memory | `~/.cache/ollama-subtitle-translator/tm.db` |
| `--no-tm` | Disable Translation Memory | — |

---

## 🐳 Docker

```bash
docker-compose up --build
# → http://localhost:8847
```

To translate subtitles embedded in videos, mount your media folder:

```bash
VIDEO_HOST_DIR=/path/to/media docker-compose up --build
```

More: [Docker guide](docs/README_DOCKER.md) · [Portainer deployment](docs/PORTAINER_DEPLOY.md)

---

## ⚙️ Configuration (Environment Variables)

| Variable | Description | Default |
|---|---|---|
| `OLLAMA_URL` | Ollama API URL | `http://127.0.0.1:11434` |
| `PORT` | Web server port | `8847` |
| `MAX_WORKERS` | Parallel translation tasks | `3` |
| `UPLOAD_DIR` | Temp file storage | System temp |
| `FILE_TTL` | File retention (sec) | `86400` (1 day) |
| `TASK_TTL` | Task retention (sec) | `86400` (1 day) |
| `CLEANUP_INTERVAL` | Cleanup worker interval (sec) | `600` (10 min) |
| `SHUTDOWN_TIMEOUT` | Graceful shutdown timeout (sec) | `30` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `VIDEO_DIR` | Allowed video root (Docker only, path-traversal protection) | — |

---

## 🏗️ Architecture

```text
app.py                 — Flask server, REST API, SSE streaming, task manager (thread-safe)
translate_srt.py       — Translator class, TranslationMemory (SQLite), batch pipeline, CLI
video_utils.py         — FFmpeg wrapper with secure path resolution
templates/index.html   — Single-file SPA (dark theme, drag-and-drop, model cards)
analysis/              — Audit reports (security, bugs, deps, dead-code, architecture)
tests/                 — 126 unit tests (pytest)
```

### Translation Pipeline

```
SRT parse → auto-glossary → context analysis → batch translate (parallel chunks)
  ↓
QE (heuristic + LLM-as-judge) → retranslate weak (score < 3) → CPS validation → save
```

Three-tier batch fallback: JSON response → `|||SEP|||` delimiter → per-segment. Alignment check detects content shift and triggers fallback.

### Security

After the 2026-04 audit, the codebase has hardened:

- **Path-traversal protection** on `save_dir` and `resolve_video_path` (allow-list + `relative_to()`)
- **Per-endpoint size limits** (100 MB for SRT)
- **Security headers** (X-Content-Type-Options, X-Frame-Options, Referrer-Policy)
- **Sanitized filenames** (no `/`, `\`, `:`, etc. in user-controlled video_stem)
- **Docker runs as non-root** (`appuser`, uid 1000)
- **Thread-safe `tasks` dict** (RLock on all mutations, snapshot reads)

Full findings in [analysis/00-summary.md](analysis/00-summary.md).

---

## 💾 Translation Memory

SQLite-backed persistent cache keyed by `(source_text, target_lang, model)`. Automatically deduplicates work across sessions.

**Why it matters:**
- Translating a 10-episode series? Episodes 2-10 are 30-50% faster thanks to cached character names and recurring phrases.
- Re-running on the same file after tweaking settings? Near-instant on cached segments.
- Shared across all translations on your machine (`~/.cache/ollama-subtitle-translator/tm.db`).

Disable with `--no-tm`. Auto-prunes to 100k entries (keeps most-used).

---

## 🧪 Testing

```bash
pip install -r requirements-dev.txt
pytest -q
# → 126 passed in ~30s
```

Coverage: SRT parsing (UTF-8, BOM, CP1251, edge cases), tag protection, HTTP retry logic, Ollama mocks, batch pipeline with chunking, `write_srt` round-trip, Translator init, FFmpeg mocks, path-traversal defenses, glossary enforcement, TranslationMemory persistence, LLM-as-judge parsing.

---

## 💡 Tips

- **Genre preset** matters a lot — `--genre anime` keeps honorifics, `--genre documentary` enforces formal tone.
- **Glossary** is enforced mechanically after translation — character names will never leak untranslated.
- **Temperature 0** for precise / technical content. **0.3–0.5** for creative / dramatic material.
- **Chunk size** of 800-1200 chars is optimal. Higher = better coherence, lower = faster recovery from bad responses.
- For **TV series**, keep the same `tm_path` across episodes for max cache benefit.
- **Two-pass mode** (`--two-pass`) roughly doubles time but catches subtle errors.
- Use `--no-qe` to skip LLM-as-judge and save ~20-30% time on draft translations.

---

## 📜 License

[MIT](LICENSE) © 2026

---

<div align="center">

**Made with ❤️ for people who watch in too many languages**

[Report bug](https://github.com/Jkaotlic/ollama-subtitle-translator/issues) · [Request feature](https://github.com/Jkaotlic/ollama-subtitle-translator/issues)

</div>
