#!/usr/bin/env python3
"""
🎬 Веб-интерфейс переводчика субтитров (Ollama + Translating Gemma)
"""

import os
import re
import uuid
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
import requests
import tempfile

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

# На Windows используем tempfile для корректного пути
default_upload_dir = Path(tempfile.gettempdir()) / "srt_translator"
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(default_upload_dir)))
UPLOAD_DIR.mkdir(exist_ok=True)

tasks = {}

LANGUAGES = {
    "Russian": "ru", "English": "en", "Chinese": "zh", "Japanese": "ja",
    "Korean": "ko", "German": "de", "French": "fr", "Spanish": "es",
    "Italian": "it", "Portuguese": "pt", "Turkish": "tr", "Arabic": "ar",
    "Ukrainian": "uk", "Polish": "pl", "Dutch": "nl", "Vietnamese": "vi",
}

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}.*$")


def translate_worker(task_id: str, input_path: Path, output_path: Path, 
                     target_lang: str, model: str):
    """Фоновый worker для перевода."""
    try:
        tasks[task_id]["status"] = "running"
        
        # Читаем файл
        raw = input_path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            text = raw.decode("utf-8-sig")
        else:
            try:
                text = raw.decode("utf-8")
            except:
                text = raw.decode("cp1251")
        
        # Парсим SRT
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        
        blocks = []
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
            
            text_lines = []
            while i < n and lines[i].strip() != "":
                text_lines.append(lines[i])
                i += 1
            
            blocks.append({"index": index, "timecode": timecode, "lines": text_lines})
            i += 1
        
        tasks[task_id]["total"] = len(blocks)
        
        # Переводим
        translated_blocks = []
        for idx, block in enumerate(blocks):
            original_text = "\n".join(block["lines"])
            
            if original_text.strip():
                prompt = f"Translate the following segment into {target_lang}, without additional explanation.\n\n{original_text}"
                
                try:
                    resp = requests.post(
                        f"{OLLAMA_URL}/api/generate",
                        json={"model": model, "prompt": prompt, "stream": False},
                        timeout=120
                    )
                    
                    if resp.status_code == 200:
                        translated = resp.json().get("response", "").strip()
                    else:
                        translated = original_text
                except:
                    translated = original_text
            else:
                translated = original_text
            
            translated_blocks.append({
                "index": block["index"],
                "timecode": block["timecode"],
                "lines": translated.split("\n")
            })
            
            tasks[task_id]["current"] = idx + 1
        
        # Сохраняем
        out_lines = []
        for b in translated_blocks:
            out_lines.append(str(b["index"]))
            out_lines.append(b["timecode"])
            out_lines.extend(b["lines"])
            out_lines.append("")
        
        output_path.write_text("\n".join(out_lines).rstrip("\n") + "\n", encoding="utf-8")
        
        tasks[task_id]["status"] = "done"
        tasks[task_id]["output_file"] = str(output_path)
        
    except Exception as e:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = str(e)


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
    
    task_id = str(uuid.uuid4())
    input_path = UPLOAD_DIR / f"{task_id}_input.srt"
    
    lang_code = LANGUAGES.get(target_lang, "ru")
    output_name = Path(file.filename).stem + f".{lang_code}.srt"
    output_path = UPLOAD_DIR / f"{task_id}_{output_name}"
    
    file.save(input_path)
    
    tasks[task_id] = {
        "status": "starting",
        "current": 0,
        "total": 0,
        "output_name": output_name
    }
    
    thread = threading.Thread(
        target=translate_worker,
        args=(task_id, input_path, output_path, target_lang, model)
    )
    thread.start()
    
    return jsonify({"task_id": task_id})


@app.route("/progress/<task_id>")
def progress(task_id):
    if task_id not in tasks:
        return jsonify({"error": "Задача не найдена"}), 404
    return jsonify(tasks[task_id])


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
    app.run(host="0.0.0.0", port=port, debug=True)
