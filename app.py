#!/usr/bin/env python3
"""
🎬 Веб-интерфейс переводчика субтитров (Ollama + Translating Gemma)
"""

import os
import re
import uuid
import threading
import time
import signal
import sys
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from flask import Flask, render_template, request, jsonify, send_file, Response, stream_with_context
import requests
import logging
import tempfile

app = Flask(__name__)

# Configure structured logging
LOG_FORMAT = os.environ.get(
    "LOG_FORMAT",
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format=LOG_FORMAT)
logger = logging.getLogger("srt-translator")
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024 * 1024  # 50GB (video files)

# На Windows используем tempfile для корректного пути
default_upload_dir = Path(tempfile.gettempdir()) / "srt_translator"
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(default_upload_dir)))
UPLOAD_DIR.mkdir(exist_ok=True)

tasks = {}
tasks_lock = threading.RLock()

# Пул воркеров для фоновых переводов (можно настроить через env)
executor = ThreadPoolExecutor(max_workers=int(os.environ.get("MAX_WORKERS", "3")))
SHUTDOWN_TIMEOUT = int(os.environ.get("SHUTDOWN_TIMEOUT", "30"))

# Per-endpoint size limit (100 MB for SRT files)
MAX_SRT_SIZE = 100 * 1024 * 1024

# Invalid characters for filenames (Windows + POSIX)
_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_stem(stem: str, fallback: str = "subtitle") -> str:
    """Strip path separators and special chars from a filename stem."""
    cleaned = _INVALID_FILENAME_RE.sub('_', stem or '').strip(' .')
    return cleaned or fallback


def _safe_base_dirs(extra: Optional[Path] = None) -> list:
    """Allowed base directories for auto-save. Resolved absolute paths."""
    bases: list = []
    try:
        bases.append(UPLOAD_DIR.resolve())
    except Exception:
        pass
    home = Path.home()
    for sub in ("Downloads", "Videos", "Movies", "Desktop"):
        cand = home / sub
        try:
            if cand.exists():
                bases.append(cand.resolve())
        except Exception:
            continue
    if extra is not None:
        try:
            bases.append(Path(extra).resolve())
        except Exception:
            pass
    return bases


def _is_within(child: Path, parent: Path) -> bool:
    """Return True if `child` is equal to or inside `parent` (after resolve)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_save_dir(save_dir: str, extra_base: Optional[Path] = None) -> Optional[Path]:
    """Validate a user-provided save directory.

    Rejects paths that contain `..` or resolve outside of an allow-list of
    safe base directories (UPLOAD_DIR, ~/Downloads, ~/Videos, ~/Movies,
    ~/Desktop, optionally the parent dir of a validated video file).

    Returns the resolved Path on success, None if invalid or empty.
    """
    if not save_dir:
        return None
    raw = str(save_dir).strip()
    if not raw:
        return None
    # Reject explicit parent-references
    if ".." in Path(raw).parts:
        return None
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, ValueError):
        return None
    for base in _safe_base_dirs(extra_base):
        if _is_within(resolved, base):
            return resolved
    return None

# Cleanup/TTL settings (seconds)
FILE_TTL = int(os.environ.get("FILE_TTL", str(60 * 60 * 24)))  # default 1 day
TASK_TTL = int(os.environ.get("TASK_TTL", str(60 * 60 * 24)))  # default 1 day
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", str(60 * 10)))  # default 10 minutes

# Единый источник языков — импортируем display→code из translate_srt (ARCH-02)
from translate_srt import SUPPORTED_LANGUAGES as LANGUAGES  # noqa: E402

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")


@app.after_request
def _security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    return response


def cleanup_worker():
    """Background worker that removes old files and prunes old tasks."""
    while True:
        try:
            now = time.time()
            # remove old files
            for p in list(UPLOAD_DIR.iterdir()):
                try:
                    mtime = p.stat().st_mtime
                except Exception:
                    continue
                if now - mtime > FILE_TTL:
                    try:
                        p.unlink()
                    except Exception:
                        pass

            # prune tasks
            to_remove = []
            with tasks_lock:
                snapshot = list(tasks.items())
            for tid, t in snapshot:
                created = t.get("created_at", 0)
                completed = t.get("completed_at")
                if completed and (now - completed > TASK_TTL):
                    out = t.get("output_file")
                    if out:
                        try:
                            Path(out).unlink()
                        except Exception:
                            pass
                    to_remove.append(tid)
                elif not completed and (now - created > TASK_TTL * 2):
                    # stale
                    to_remove.append(tid)

            if to_remove:
                with tasks_lock:
                    for tid in to_remove:
                        tasks.pop(tid, None)

        except Exception:
            pass

        time.sleep(CLEANUP_INTERVAL)

# start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
cleanup_thread.start()


def translate_worker(task_id: str, input_path: Path, output_path: Path,
                     target_lang: str, model: str, context: str = "",
                     source_lang: str = "", two_pass: bool = False,
                     review_model: str = "",
                     glossary: dict = None,
                     genre: str = "",
                     context_analysis: bool = True,
                     qe: bool = True,
                     auto_glossary: bool = True):
    """Фоновый worker для перевода."""
    from translate_srt import Translator, read_srt_file, parse_srt, write_srt, SrtBlock

    t0 = time.time()
    try:
        logger.info("task=%s action=start model=%s lang=%s", task_id, model, target_lang)
        with tasks_lock:
            tasks[task_id]["status"] = "running"
            tasks[task_id]["started_at"] = t0

        # Читаем и парсим SRT (единая логика из translate_srt.py)
        text, encoding = read_srt_file(input_path)
        blocks = parse_srt(text)

        # Empty SRT — fail early with a clear error
        if len(blocks) == 0:
            with tasks_lock:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["error"] = "SRT файл не содержит субтитров"
                tasks[task_id]["completed_at"] = time.time()
            logger.warning("task=%s action=error reason=empty_srt", task_id)
            return

        with tasks_lock:
            tasks[task_id]["total"] = len(blocks)
            # Snapshot UI-provided runtime options
            task_snapshot = dict(tasks.get(task_id, {}))

        temp = task_snapshot.get("temperature", 0.0)
        chunk_size = task_snapshot.get("chunk_size", 1000)
        context_window = task_snapshot.get("context_window", 3)

        tm_path = UPLOAD_DIR / "translation_memory.db"
        translator = Translator(
            model=model, target_lang=target_lang, ollama_url=OLLAMA_URL,
            context=context, temperature=temp, source_lang=source_lang,
            two_pass=two_pass, review_model=review_model,
            glossary=glossary, context_window=int(context_window),
            genre=genre, tm_path=tm_path,
        )

        max_cps = task_snapshot.get("max_cps", 0)

        texts = [b.text() for b in blocks]

        # Auto-glossary generation (Phase 13)
        if auto_glossary:
            with tasks_lock:
                tasks[task_id]["phase"] = "glossary"
            auto_gloss = translator.generate_glossary(texts)
            if auto_gloss:
                merged = dict(auto_gloss)
                merged.update(translator.glossary)
                translator.glossary = merged
                with tasks_lock:
                    tasks[task_id]["auto_glossary"] = auto_gloss

        # Pre-translation context analysis (Phase 10)
        if context_analysis:
            with tasks_lock:
                tasks[task_id]["phase"] = "analyzing"
            analysis = translator.analyze_context(texts)
            if analysis:
                with tasks_lock:
                    tasks[task_id]["context_analysis_result"] = analysis[:500]

        # Progress callback — обновляет задачу в реальном времени
        def update_progress(done: int, total: int):
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id]["current"] = done

        def update_phase(phase: str):
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id]["phase"] = phase
                    if phase == "reviewing":
                        tasks[task_id]["current"] = 0  # reset progress for review pass

        with tasks_lock:
            tasks[task_id]["phase"] = "translating"
        translated_texts = translator.translate_batch(
            texts, max_chars=int(chunk_size), on_progress=update_progress,
            on_phase=update_phase,
        )

        # Quality estimation with auto-retranslate (Phase 11)
        if qe:
            with tasks_lock:
                tasks[task_id]["phase"] = "quality_check"
                tasks[task_id]["current"] = 0
            scores = translator.estimate_quality(texts, translated_texts)
            weak_count = sum(1 for s in scores if s < 3)
            with tasks_lock:
                tasks[task_id]["qe_weak_count"] = weak_count
            if weak_count > 0:
                translated_texts = translator.retranslate_weak(texts, translated_texts, scores)

        # Validate reading speed (CPS)
        if max_cps > 0:
            from translate_srt import validate_reading_speed
            translated_texts = validate_reading_speed(blocks, translated_texts, translator, max_cps)

        # Собираем результат
        translated_blocks = []
        for block, translated_text in zip(blocks, translated_texts):
            translated_blocks.append(SrtBlock(
                index=block.index,
                timecode=block.timecode,
                lines=tuple(translated_text.split("\n")),
            ))
        with tasks_lock:
            tasks[task_id]["current"] = len(translated_blocks)

        # Сохраняем (единая логика из translate_srt.py)
        write_srt(translated_blocks, output_path, "utf-8")

        with tasks_lock:
            tasks[task_id]["output_file"] = str(output_path)
            tasks[task_id]["completed_at"] = time.time()
            tasks[task_id]["status"] = "done"  # set last to avoid race with /progress poll
            save_dir = tasks[task_id].get("save_dir", "")
            output_name = tasks[task_id].get("output_name", "")
        elapsed = time.time() - t0
        logger.info("task=%s action=done blocks=%d elapsed=%.1fs", task_id, len(translated_blocks), elapsed)

        # Auto-save next to video if save_dir is set (already validated at submit time)
        if save_dir:
            safe_dir = _validate_save_dir(save_dir)
            if safe_dir is None:
                logger.warning("task=%s rejected save_dir=%s", task_id, save_dir)
            else:
                try:
                    import shutil
                    dest = safe_dir / output_name
                    shutil.copy2(str(output_path), str(dest))
                    with tasks_lock:
                        tasks[task_id]["saved_to"] = str(dest)
                    logger.info("task=%s action=auto_save dest=%s", task_id, dest)
                except Exception as copy_err:
                    logger.warning("task=%s action=auto_save_failed error=%s", task_id, copy_err)

    except Exception as e:
        elapsed = time.time() - t0
        logger.exception("task=%s action=error elapsed=%.1fs error=%s", task_id, elapsed, e)
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["error"] = str(e)
                tasks[task_id]["completed_at"] = time.time()
    finally:
        # Release TM and HTTP session to avoid file-handle leak across many tasks
        try:
            if 'translator' in locals() and translator is not None:
                translator.close()
        except Exception:
            pass
        with tasks_lock:
            final_status = tasks.get(task_id, {}).get("status", "unknown")
        if final_status != "done":
            logger.info("task=%s action=final status=%s", task_id, final_status)


@app.route("/")
def index():
    return render_template("index.html", languages=list(LANGUAGES.keys()))


@app.route("/translate", methods=["POST"])
def translate():
    if "file" not in request.files:
        return jsonify({"error": "Файл не выбран"}), 400
    
    file = request.files["file"]
    if not file.filename.endswith(".srt"):
        return jsonify({"error": "Только .srt файлы"}), 400

    # Per-endpoint size check: 100 MB for SRT
    try:
        file.seek(0, 2)
        size = file.tell()
        file.seek(0)
    except Exception:
        size = 0
    if size > MAX_SRT_SIZE:
        return jsonify({"error": "SRT файл слишком большой (макс 100MB)"}), 413

    target_lang = request.form.get("lang", "Russian")
    model = request.form.get("model", "gemma4:e12b")
    context = request.form.get("context", "")
    source_lang = request.form.get("source_lang", "")
    two_pass = request.form.get("two_pass", "") == "on"
    review_model = request.form.get("review_model", "")
    glossary_raw = request.form.get("glossary", "")
    genre = request.form.get("genre", "")
    context_analysis = request.form.get("context_analysis", "") == "on"
    qe = request.form.get("qe", "") == "on"
    auto_glossary = request.form.get("auto_glossary", "") == "on"

    from translate_srt import parse_glossary
    glossary = parse_glossary(glossary_raw) if glossary_raw.strip() else {}

    task_id = str(uuid.uuid4())
    input_path = UPLOAD_DIR / f"{task_id}_input.srt"

    lang_code = LANGUAGES.get(target_lang, "ru")
    output_name = Path(file.filename).stem + f".{lang_code}.srt"
    output_path = UPLOAD_DIR / f"{task_id}_{output_name}"

    file.save(input_path)
    logger.info("task=%s action=upload file_ext=%s lang=%s model=%s",
                task_id, Path(file.filename).suffix, target_lang, model)

    # Pass through temperature, chunk_size, context_window, max_cps from UI
    temperature = request.form.get("temperature")
    chunk_size = request.form.get("chunk_size")
    context_window = request.form.get("context_window")
    max_cps = request.form.get("max_cps")
    save_dir_raw = request.form.get("save_dir", "").strip()
    safe_save_dir = _validate_save_dir(save_dir_raw) if save_dir_raw else None
    if save_dir_raw and safe_save_dir is None:
        logger.warning("task=%s rejected save_dir=%s", task_id, save_dir_raw)
    save_dir = str(safe_save_dir) if safe_save_dir is not None else ""

    with tasks_lock:
        tasks[task_id] = {
            "status": "starting",
            "current": 0,
            "total": 0,
            "output_name": output_name,
            "save_dir": save_dir,
            "created_at": time.time(),
            "temperature": float(temperature) if temperature is not None and temperature != "" else 0.0,
            "chunk_size": int(chunk_size) if chunk_size is not None and chunk_size != "" else 2000,
            "context_window": int(context_window) if context_window is not None and context_window != "" else 3,
            "max_cps": float(max_cps) if max_cps is not None and max_cps != "" else 0,
            "two_pass_enabled": two_pass,
        }

    # Запускаем фоновую задачу в пуле воркеров
    future = executor.submit(translate_worker, task_id, input_path, output_path,
                             target_lang, model, context, source_lang, two_pass, review_model,
                             glossary=glossary, genre=genre,
                             context_analysis=context_analysis, qe=qe,
                             auto_glossary=auto_glossary)
    with tasks_lock:
        tasks[task_id]["future"] = future

    return jsonify({"task_id": task_id})


@app.route("/progress/<task_id>")
def progress(task_id):
    with tasks_lock:
        if task_id not in tasks:
            logger.warning("task=%s action=progress error=not_found", task_id)
            return jsonify({"error": "Задача не найдена"}), 404
        # Exclude non-serializable fields (e.g. Future) from JSON response
        safe = {k: v for k, v in tasks[task_id].items() if k != "future"}
    return jsonify(safe)


@app.route("/check_model", methods=["POST"])
def check_model():
    """Проверяет наличие модели в Ollama."""
    model_name = request.json.get("model", "")
    if not model_name:
        return jsonify({"exists": False, "error": "Имя модели не указано"}), 400
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        if resp.status_code != 200:
            return jsonify({"exists": False, "error": "Ollama не отвечает"}), 502
        available = [m["name"] for m in resp.json().get("models", [])]
        exists = any(model_name in m for m in available)
        return jsonify({"exists": exists, "available": available})
    except requests.exceptions.ConnectionError:
        return jsonify({"exists": False, "error": "Ollama не запущен"}), 502


@app.route("/pull_model", methods=["POST"])
def pull_model():
    """Скачивает модель через Ollama API, стримит прогресс как SSE."""
    model_name = request.json.get("model", "")
    if not model_name:
        return jsonify({"error": "Имя модели не указано"}), 400

    def generate():
        try:
            with requests.post(
                f"{OLLAMA_URL}/api/pull",
                json={"name": model_name},
                stream=True,
                timeout=600,
            ) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    import json as _json
                    data = _json.loads(line)
                    status = data.get("status", "")
                    total = data.get("total", 0)
                    completed = data.get("completed", 0)
                    pct = int(completed / total * 100) if total else 0
                    yield f"data: {_json.dumps({'status': status, 'pct': pct, 'total': total, 'completed': completed})}\n\n"
                yield f"data: {_json.dumps({'status': 'done', 'pct': 100})}\n\n"
        except Exception as e:
            import json as _json
            yield f"data: {_json.dumps({'status': 'error', 'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


def _tm_db_path() -> Path:
    """Path to the default Translation Memory database used by the web worker."""
    return UPLOAD_DIR / "translation_memory.db"


@app.route("/tm/stats", methods=["GET"])
def tm_stats():
    """Return Translation Memory stats: number of entries and file size on disk."""
    from translate_srt import TranslationMemory
    db_path = _tm_db_path()
    if not db_path.exists():
        return jsonify({"entries": 0, "size_bytes": 0})
    try:
        tm = TranslationMemory(db_path)
        stats = tm.stats()
        tm.close()
        size_bytes = db_path.stat().st_size
        return jsonify({
            "entries": stats["entries"],
            "size_bytes": size_bytes,
        })
    except Exception as e:
        logger.warning("tm_stats failed: %s", e)
        return jsonify({"entries": 0, "size_bytes": 0, "error": "tm_unavailable"}), 200


@app.route("/tm/clear", methods=["POST"])
def tm_clear():
    """Clear all entries from Translation Memory."""
    from translate_srt import TranslationMemory
    db_path = _tm_db_path()
    if not db_path.exists():
        return jsonify({"ok": True, "cleared": 0})
    try:
        tm = TranslationMemory(db_path)
        cleared = tm.clear()
        tm.close()
        return jsonify({"ok": True, "cleared": cleared})
    except Exception as e:
        logger.exception("tm_clear failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/check_ffmpeg", methods=["POST"])
def check_ffmpeg():
    """Check if ffmpeg/ffprobe are installed."""
    from video_utils import check_ffmpeg_available
    available = check_ffmpeg_available()
    return jsonify({"available": available})


@app.route("/install_ffmpeg", methods=["POST"])
def install_ffmpeg():
    """Auto-download ffmpeg/ffprobe binaries."""
    from video_utils import ensure_ffmpeg
    ok = ensure_ffmpeg()
    return jsonify({"success": ok})


@app.route("/upload_video", methods=["POST"])
def upload_video():
    """Upload a video file and return its server-side path."""
    from video_utils import SUPPORTED_VIDEO_EXTENSIONS
    if "file" not in request.files:
        return jsonify({"error": "Файл не выбран"}), 400

    file = request.files["file"]
    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_VIDEO_EXTENSIONS:
        return jsonify({"error": f"Неподдерживаемый формат: {ext}"}), 400

    # Save to temp directory, preserving original name for clarity
    safe_name = f"{uuid.uuid4()}_{Path(file.filename).name}"
    dest = UPLOAD_DIR / safe_name
    file.save(dest)
    logger.info("video uploaded: %s (%d bytes)", dest, dest.stat().st_size)
    return jsonify({"path": str(dest)})


@app.route("/probe_video", methods=["POST"])
def probe_video():
    """Probe a video file for embedded subtitle tracks."""
    data = request.get_json(silent=True) or {}
    video_path = data.get("path", "").strip()

    if not video_path:
        return jsonify({"error": "Video path is required"}), 400

    try:
        from video_utils import probe_subtitle_tracks, resolve_video_path, format_track_label
        resolved = resolve_video_path(video_path)
        tracks = probe_subtitle_tracks(resolved)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not tracks:
        return jsonify({"error": "No subtitle tracks found", "tracks": []}), 200

    return jsonify({
        "tracks": [
            {
                "index": t.index,
                "sub_index": t.sub_index,
                "codec_name": t.codec_name,
                "language": t.language,
                "title": t.title,
                "is_text": t.is_text,
                "is_image": t.is_image,
                "label": format_track_label(t),
            }
            for t in tracks
        ]
    })


@app.route("/extract_and_translate", methods=["POST"])
def extract_and_translate():
    """Extract subtitle track from video and start translation."""
    data = request.get_json(silent=True) or {}

    video_path = data.get("path", "").strip()
    sub_index = data.get("sub_index")
    target_lang = data.get("lang", "Russian")
    model = data.get("model", "gemma4:e12b")
    context = data.get("context", "")
    source_lang = data.get("source_lang", "")
    two_pass = data.get("two_pass", False)
    review_model = data.get("review_model", "")
    temperature = data.get("temperature")
    chunk_size = data.get("chunk_size")
    context_window = data.get("context_window")
    glossary_raw = data.get("glossary", "")
    genre = data.get("genre", "")
    context_analysis = data.get("context_analysis", False)
    qe = data.get("qe", False)
    auto_glossary = data.get("auto_glossary", False)

    from translate_srt import parse_glossary
    glossary = parse_glossary(glossary_raw) if glossary_raw.strip() else {}
    original_name = data.get("original_name", "").strip()

    if not video_path:
        return jsonify({"error": "Video path is required"}), 400
    if sub_index is None:
        return jsonify({"error": "Subtitle track index is required"}), 400

    # Validate sub_index: must be a small non-negative integer
    try:
        sub_index_int = int(sub_index)
        if sub_index_int < 0 or sub_index_int > 100:
            return jsonify({"error": "Invalid subtitle index range"}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid subtitle index"}), 400

    task_id = str(uuid.uuid4())

    # Extract subtitle to temp SRT
    extracted_srt = UPLOAD_DIR / f"{task_id}_extracted.srt"

    try:
        from video_utils import extract_subtitle_track, resolve_video_path
        resolved = resolve_video_path(video_path)
        extract_subtitle_track(resolved, sub_index_int, str(extracted_srt))
    except Exception as e:
        logger.exception("task=%s action=extract_failed error=%s", task_id, e)
        return jsonify({"error": "Subtitle extraction failed"}), 500

    # Use original_name if provided (uploaded file), otherwise use video_path stem.
    # Sanitize the stem so it cannot contain path separators / special chars.
    raw_stem = Path(original_name).stem if original_name else Path(video_path).stem
    video_stem = _sanitize_stem(raw_stem)
    lang_code = LANGUAGES.get(target_lang, "ru")
    output_name = f"{video_stem}.{lang_code}.srt"
    output_path = UPLOAD_DIR / f"{task_id}_{output_name}"

    # Determine save directory: prefer UI-provided, fallback to next-to-video.
    # Both options must pass the allow-list validation.
    save_dir_raw = data.get("save_dir", "").strip()
    # Allow the parent directory of the resolved video as an additional base.
    try:
        video_parent = Path(resolved).parent
    except Exception:
        video_parent = None

    safe_save_dir = _validate_save_dir(save_dir_raw, extra_base=video_parent) if save_dir_raw else None
    if save_dir_raw and safe_save_dir is None:
        logger.warning("task=%s rejected save_dir=%s", task_id, save_dir_raw)

    if safe_save_dir is None and not save_dir_raw:
        # Fallback: save next to the video file, but only if it's not inside UPLOAD_DIR
        try:
            video_dir = Path(resolved).parent
            if not _is_within(video_dir.resolve(), UPLOAD_DIR.resolve()):
                safe_save_dir = _validate_save_dir(str(video_dir), extra_base=video_parent)
        except Exception:
            safe_save_dir = None

    save_dir = str(safe_save_dir) if safe_save_dir is not None else ""

    max_cps = data.get("max_cps")

    with tasks_lock:
        tasks[task_id] = {
            "status": "starting",
            "current": 0,
            "total": 0,
            "output_name": output_name,
            "save_dir": save_dir,
            "created_at": time.time(),
            "temperature": float(temperature) if temperature is not None and temperature != "" else 0.0,
            "chunk_size": int(chunk_size) if chunk_size is not None and chunk_size != "" else 2000,
            "context_window": int(context_window) if context_window is not None and context_window != "" else 3,
            "max_cps": float(max_cps) if max_cps is not None and max_cps != "" else 0,
            "two_pass_enabled": two_pass,
        }

    logger.info("task=%s action=extract_and_translate sub_index=%s lang=%s model=%s",
                task_id, sub_index_int, target_lang, model)

    future = executor.submit(
        translate_worker, task_id, extracted_srt, output_path,
        target_lang, model, context, source_lang, two_pass, review_model,
        glossary=glossary, genre=genre,
        context_analysis=context_analysis, qe=qe, auto_glossary=auto_glossary,
    )
    with tasks_lock:
        tasks[task_id]["future"] = future

    return jsonify({"task_id": task_id})


@app.route("/stream_progress/<task_id>")
def stream_progress(task_id):
    """SSE endpoint for real-time translation progress streaming."""
    import json as _json

    with tasks_lock:
        if task_id not in tasks:
            return jsonify({"error": "Task not found"}), 404

    def generate():
        last_current = -1
        last_phase = ""
        while True:
            with tasks_lock:
                task = dict(tasks[task_id]) if task_id in tasks else None
            if not task:
                yield f"data: {_json.dumps({'status': 'error', 'error': 'Task not found'})}\n\n"
                break

            status = task.get("status", "starting")
            current = task.get("current", 0)
            total = task.get("total", 0)
            phase = task.get("phase", "")

            # Only send updates when something changes
            if current != last_current or phase != last_phase:
                last_current = current
                last_phase = phase
                event = {
                    "status": status,
                    "current": current,
                    "total": total,
                    "phase": phase,
                }
                # Include extra info if available
                if "context_analysis_result" in task:
                    event["context_analysis"] = task["context_analysis_result"]
                if "auto_glossary" in task:
                    event["auto_glossary"] = task["auto_glossary"]
                if "qe_weak_count" in task:
                    event["qe_weak_count"] = task["qe_weak_count"]
                if "current_segment" in task:
                    event["current_segment"] = task["current_segment"]

                yield f"data: {_json.dumps(event, ensure_ascii=False)}\n\n"

            if status in ("done", "error"):
                final = {"status": status}
                if status == "error":
                    final["error"] = task.get("error", "Unknown error")
                if status == "done":
                    final["output_name"] = task.get("output_name", "")
                yield f"data: {_json.dumps(final, ensure_ascii=False)}\n\n"
                break

            time.sleep(0.5)

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/download/<task_id>")
def download(task_id):
    with tasks_lock:
        if task_id not in tasks:
            return jsonify({"error": "Задача не найдена"}), 404
        task = dict(tasks[task_id])

    if task.get("status") != "done":
        return jsonify({"error": "Перевод не завершён"}), 400

    return send_file(
        task["output_file"],
        as_attachment=True,
        download_name=task["output_name"]
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8847))
    print(f"🌐 Запуск на http://localhost:{port}")
    # Register signal handlers for graceful shutdown
    def _shutdown_handler(signum, frame):
        logger.info("Shutdown signal received: %s", signum)
        try:
            # stop accepting new tasks
            executor.shutdown(wait=False)
        except Exception:
            pass

        # collect futures to wait on
        with tasks_lock:
            futures = [t.get("future") for t in tasks.values() if t.get("future") is not None]
        pending = [f for f in futures if f is not None and not f.done()]
        if pending:
            logger.info("Waiting up to %s seconds for %d running tasks", SHUTDOWN_TIMEOUT, len(pending))
            try:
                done, not_done = concurrent.futures.wait(pending, timeout=SHUTDOWN_TIMEOUT)
            except Exception:
                not_done = pending
            # cancel remaining
            for f in not_done:
                try:
                    f.cancel()
                except Exception:
                    pass

        logger.info("Exiting")
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)

    try:
        signal.signal(signal.SIGINT, _shutdown_handler)
        signal.signal(signal.SIGTERM, _shutdown_handler)
    except Exception:
        # signals may not be available on some platforms
        pass

    debug = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
    try:
        app.run(host="0.0.0.0", port=port, debug=debug)
    except KeyboardInterrupt:
        _shutdown_handler(None, None)
