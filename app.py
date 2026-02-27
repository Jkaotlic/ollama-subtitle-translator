#!/usr/bin/env python3
"""
🎬 Веб-интерфейс переводчика субтитров (Ollama + Translating Gemma)
"""

import os
import uuid
import threading
import time
import signal
import sys
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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

# Пул воркеров для фоновых переводов (можно настроить через env)
executor = ThreadPoolExecutor(max_workers=int(os.environ.get("MAX_WORKERS", "3")))
SHUTDOWN_TIMEOUT = int(os.environ.get("SHUTDOWN_TIMEOUT", "30"))

# Cleanup/TTL settings (seconds)
FILE_TTL = int(os.environ.get("FILE_TTL", str(60 * 60 * 24)))  # default 1 day
TASK_TTL = int(os.environ.get("TASK_TTL", str(60 * 60 * 24)))  # default 1 day
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", str(60 * 10)))  # default 10 minutes

LANGUAGES = {
    "Russian": "ru", "English": "en", "Chinese": "zh", "Japanese": "ja",
    "Korean": "ko", "German": "de", "French": "fr", "Spanish": "es",
    "Italian": "it", "Portuguese": "pt", "Turkish": "tr", "Arabic": "ar",
    "Ukrainian": "uk", "Polish": "pl", "Dutch": "nl", "Vietnamese": "vi",
}

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")


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
            for tid, t in list(tasks.items()):
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
                     review_model: str = ""):
    """Фоновый worker для перевода."""
    from translate_srt import Translator, read_srt_file, parse_srt, write_srt, SrtBlock

    t0 = time.time()
    try:
        logger.info("task=%s action=start model=%s lang=%s", task_id, model, target_lang)
        tasks[task_id]["status"] = "running"

        # Читаем и парсим SRT (единая логика из translate_srt.py)
        text, encoding = read_srt_file(input_path)
        blocks = parse_srt(text)
        tasks[task_id]["total"] = len(blocks)

        # Runtime-настройки из UI
        temp = tasks.get(task_id, {}).get("temperature", 0.0)
        chunk_size = tasks.get(task_id, {}).get("chunk_size", 2000)

        translator = Translator(
            model=model, target_lang=target_lang, ollama_url=OLLAMA_URL,
            context=context, temperature=temp, source_lang=source_lang,
            two_pass=two_pass, review_model=review_model,
        )

        texts = [b.text() for b in blocks]

        # Progress callback — обновляет задачу в реальном времени
        def update_progress(done: int, total: int):
            tasks[task_id]["current"] = done

        def update_phase(phase: str):
            tasks[task_id]["phase"] = phase
            if phase == "reviewing":
                tasks[task_id]["current"] = 0  # reset progress for review pass

        tasks[task_id]["phase"] = "translating"
        translated_texts = translator.translate_batch(
            texts, max_chars=int(chunk_size), on_progress=update_progress,
            on_phase=update_phase,
        )

        # Собираем результат
        translated_blocks = []
        for block, translated_text in zip(blocks, translated_texts):
            translated_blocks.append(SrtBlock(
                index=block.index,
                timecode=block.timecode,
                lines=tuple(translated_text.split("\n")),
            ))
        tasks[task_id]["current"] = len(translated_blocks)

        # Сохраняем (единая логика из translate_srt.py)
        write_srt(translated_blocks, output_path, "utf-8")

        tasks[task_id]["output_file"] = str(output_path)
        tasks[task_id]["completed_at"] = time.time()
        tasks[task_id]["status"] = "done"  # set last to avoid race with /progress poll
        elapsed = time.time() - t0
        logger.info("task=%s action=done blocks=%d elapsed=%.1fs", task_id, len(translated_blocks), elapsed)

        # Auto-save next to video if save_dir is set
        save_dir = tasks[task_id].get("save_dir", "")
        if save_dir:
            try:
                import shutil
                dest = Path(save_dir) / tasks[task_id]["output_name"]
                shutil.copy2(str(output_path), str(dest))
                tasks[task_id]["saved_to"] = str(dest)
                logger.info("task=%s action=auto_save dest=%s", task_id, dest)
            except Exception as copy_err:
                logger.warning("task=%s action=auto_save_failed error=%s", task_id, copy_err)

    except Exception as e:
        elapsed = time.time() - t0
        logger.exception("task=%s action=error elapsed=%.1fs error=%s", task_id, elapsed, e)
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = str(e)
        tasks[task_id]["completed_at"] = time.time()
    finally:
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
    
    target_lang = request.form.get("lang", "Russian")
    model = request.form.get("model", "translategemma:4b")
    context = request.form.get("context", "")
    source_lang = request.form.get("source_lang", "")
    two_pass = request.form.get("two_pass", "") == "on"
    review_model = request.form.get("review_model", "")

    task_id = str(uuid.uuid4())
    input_path = UPLOAD_DIR / f"{task_id}_input.srt"
    
    lang_code = LANGUAGES.get(target_lang, "ru")
    output_name = Path(file.filename).stem + f".{lang_code}.srt"
    output_path = UPLOAD_DIR / f"{task_id}_{output_name}"
    
    file.save(input_path)
    logger.info("task=%s action=upload file=%s lang=%s model=%s", task_id, file.filename, target_lang, model)
    
    # Pass through temperature and chunk_size from UI
    temperature = request.form.get("temperature")
    chunk_size = request.form.get("chunk_size")
    save_dir = request.form.get("save_dir", "").strip()

    tasks[task_id] = {
        "status": "starting",
        "current": 0,
        "total": 0,
        "output_name": output_name,
        "save_dir": save_dir,
        "created_at": time.time(),
        "temperature": float(temperature) if temperature is not None and temperature != "" else 0.0,
        "chunk_size": int(chunk_size) if chunk_size is not None and chunk_size != "" else 2000,
        "two_pass_enabled": two_pass,
    }

    # Запускаем фоновую задачу в пуле воркеров
    future = executor.submit(translate_worker, task_id, input_path, output_path, target_lang, model, context, source_lang, two_pass, review_model)
    tasks[task_id]["future"] = future
    
    return jsonify({"task_id": task_id})


@app.route("/progress/<task_id>")
def progress(task_id):
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
    model = data.get("model", "translategemma:4b")
    context = data.get("context", "")
    source_lang = data.get("source_lang", "")
    two_pass = data.get("two_pass", False)
    review_model = data.get("review_model", "")
    temperature = data.get("temperature")
    chunk_size = data.get("chunk_size")
    original_name = data.get("original_name", "").strip()

    if not video_path:
        return jsonify({"error": "Video path is required"}), 400
    if sub_index is None:
        return jsonify({"error": "Subtitle track index is required"}), 400

    task_id = str(uuid.uuid4())

    # Extract subtitle to temp SRT
    extracted_srt = UPLOAD_DIR / f"{task_id}_extracted.srt"

    try:
        from video_utils import extract_subtitle_track, resolve_video_path
        resolved = resolve_video_path(video_path)
        extract_subtitle_track(resolved, int(sub_index), str(extracted_srt))
    except Exception as e:
        logger.exception("task=%s action=extract_failed error=%s", task_id, e)
        return jsonify({"error": f"Subtitle extraction failed: {e}"}), 500

    # Use original_name if provided (uploaded file), otherwise use video_path stem
    video_stem = Path(original_name).stem if original_name else Path(video_path).stem
    lang_code = LANGUAGES.get(target_lang, "ru")
    output_name = f"{video_stem}.{lang_code}.srt"
    output_path = UPLOAD_DIR / f"{task_id}_{output_name}"

    # Determine save directory: prefer UI-provided, fallback to next-to-video
    save_dir = data.get("save_dir", "").strip()
    if not save_dir:
        video_dir = str(Path(video_path).parent)
        if not video_dir.startswith(str(UPLOAD_DIR)):
            save_dir = video_dir

    tasks[task_id] = {
        "status": "starting",
        "current": 0,
        "total": 0,
        "output_name": output_name,
        "save_dir": save_dir,
        "created_at": time.time(),
        "temperature": float(temperature) if temperature is not None and temperature != "" else 0.0,
        "chunk_size": int(chunk_size) if chunk_size is not None and chunk_size != "" else 2000,
        "two_pass_enabled": two_pass,
    }

    logger.info("task=%s action=extract_and_translate video=%s sub_index=%s lang=%s model=%s",
                task_id, video_path, sub_index, target_lang, model)

    future = executor.submit(
        translate_worker, task_id, extracted_srt, output_path,
        target_lang, model, context, source_lang, two_pass, review_model,
    )
    tasks[task_id]["future"] = future

    return jsonify({"task_id": task_id})


@app.route("/download/<task_id>")
def download(task_id):
    if task_id not in tasks:
        return jsonify({"error": "Задача не найдена"}), 404
    
    task = tasks[task_id]
    if task["status"] != "done":
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
