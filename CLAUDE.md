# CLAUDE.md — Ollama Subtitle Translator

## Project Overview
Веб-приложение + CLI для перевода субтитров (.srt) через локальный Ollama.
Основная модель — TranslateGemma (translation-only), вспомогательная — qwen3.5:4b (анализ, глоссарий, QE).

## Architecture

```
app.py              — Flask web server, REST API, SSE streaming, task management
translate_srt.py    — Core: Translator class, batch/single translation, retry, cache
video_utils.py      — FFmpeg wrapper: probe/extract subtitles from video
templates/index.html — SPA web UI (dark theme, SSE progress, model management)
```

## Key Classes & Functions

### translate_srt.py
- `Translator` — main translation engine
  - `translate()` — single segment with context window, cache, tag protection
  - `translate_batch()` — chunked batch translation with 3-tier fallback (JSON → SEP → per-segment)
  - `review()` — second pass quality check
  - `analyze_context()` — pre-translation content analysis (uses aux_model)
  - `generate_glossary()` — auto-detect proper names (uses aux_model)
  - `estimate_quality()` — rate translations 1-5 (uses aux_model)
  - `retranslate_weak()` — re-translate segments with score < 3
  - `_call_llm()` — send chat request to Ollama (non-streaming)
  - `_call_llm_stream()` — streaming chat request
  - `_cache_lookup()` — exact + fuzzy (0.9 threshold) cache
  - `_unload_model()` — free VRAM via keep_alive=0
- `post_with_retry()` — HTTP POST with exponential backoff (3 attempts)
- `parse_srt()` / `write_srt()` — SRT parsing/writing
- `protect_tags()` / `restore_tags()` — preserve HTML/ASS tags during translation
- `validate_translation()` — quality checks (empty, identical, too long)
- `validate_reading_speed()` — CPS check with auto-shorten

### app.py
- `translate_worker()` — background translation pipeline:
  1. auto_glossary → 2. context_analysis → 3. translate_batch → 4. quality_check → 5. CPS validation
- Endpoints: `/translate`, `/extract_and_translate`, `/progress/<id>`, `/stream_progress/<id>`, `/download/<id>`
- Model mgmt: `/check_model`, `/pull_model`
- Video: `/upload_video`, `/probe_video`, `/check_ffmpeg`, `/install_ffmpeg`

### video_utils.py
- `probe_subtitle_tracks()` — ffprobe JSON parsing
- `extract_subtitle_track()` — ffmpeg -map extraction
- `ensure_ffmpeg()` — auto-download ffmpeg binaries

## Translation Pipeline (translate_batch)

1. Chunk texts by `max_chars` (default 2000 from UI, 1000 CLI)
2. For each chunk:
   - Protect tags → build JSON input → send to LLM
   - Parse response: try JSON → try |||SEP||| → fallback per-segment
   - Validate each translation, retry bad ones
   - Cross-chunk context: pass tail of previous chunk
3. Optional two-pass review (per-segment)
4. Save incremental progress to file

## Fixed Issues (2026-03-08)

- `post_with_retry()`: extended to 13 attempts with 60s max backoff for ConnectionError
- `requests.Session` for connection pooling (reuses TCP connections)
- `_translate_chunk()`: retry failed chunks (3 batch retries + per-segment fallback)
- Parallel chunk processing via ThreadPoolExecutor (`parallel_chunks=2` default)

## Remaining Issues & Bottlenecks

- ~~`validate_translation` doesn't check target language~~ — FIXED: now accepts optional `target_lang` param
- **Sequential review pass** — two_pass reviews one segment at a time
- **Model switching overhead** — unload/reload between main and aux model
- ~~**Fuzzy cache is O(n)**~~ — FIXED: length pre-filter skips entries that can't match
- `_call_llm` always uses `stream: False` — long wait for large chunks
- ~~No adaptive chunk sizing~~ — FIXED: auto-adjusts segments per chunk based on avg segment length
- ~~**Tag leak**~~ — FIXED: `restore_tags()` now cleans up any remaining `__TAG_*` placeholders
- ~~**ASS tags inconsistent**~~ — FIXED: `{\an8}` tags stripped before translation, restored after
- ~~**Content shift in batch**~~ — FIXED: alignment validation detects shifted segments, falls back to per-segment

## Environment Variables

```
PORT=8847            OLLAMA_URL=http://127.0.0.1:11434
MAX_WORKERS=3        FILE_TTL=86400        TASK_TTL=86400
CLEANUP_INTERVAL=600 SHUTDOWN_TIMEOUT=30   LOG_LEVEL=INFO
OLLAMA_NUM_PARALLEL=2  OLLAMA_MAX_LOADED_MODELS=1
```

## Testing

```bash
python -m pytest tests/ -v          # 124 tests
python -m pytest tests/ -x -q       # quick, stop on first failure
```

Key test files:
- `tests/test_parse_srt.py` — 60+ tests: parsing, retry, batch, cache, glossary, QE, genre
- `tests/test_video_utils.py` — 13+ tests: ffmpeg, probe, extract

Tests mock `requests.Session` via `_MockSession` + `_patch_session(monkeypatch)` helper.

Tests use `unittest.mock.patch` to mock `requests.post` for Ollama calls.
Test convention: `test_<feature>_<scenario>` methods in `unittest.TestCase` classes.

## Coding Conventions

- Language: Python 3.9+, type hints, dataclasses
- Logging: `logging.getLogger("translate_srt")` / `logging.getLogger("srt-translator")`
- Log format: `key=value` structured logging (e.g., `task=%s action=start model=%s`)
- Error handling: graceful degradation, return original text on failure
- Imports: stdlib → third-party → local (inside functions for circular avoidance)
- No async — all synchronous with ThreadPoolExecutor for parallelism
- UI: single-file SPA in `templates/index.html`, vanilla JS, CSS variables for theming

## Phases Implemented (1-13)

1. Glossary support  2. Chat API  3. JSON batch output  4. Context window (1-10)
5. Fuzzy cache (90%)  6. Crash recovery  7. Genre prompts  8. Length validation
9. CPS validation  10. Context analysis  11. Quality estimation + retranslate
12. SSE streaming  13. Auto-glossary

## Common Development Tasks

- Add new genre: add to `GENRE_PROMPTS` dict in translate_srt.py + UI dropdown in index.html
- Add new language: add to `LANGUAGES` dict in both translate_srt.py and app.py
- Change retry behavior: modify `post_with_retry()` params or `_call_llm()` calls
- Add new translation phase: add to `translate_worker()` in app.py between existing phases
