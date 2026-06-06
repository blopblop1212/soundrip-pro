"""
SoundRip Pro — Backend Python
Requires: pip install flask flask-cors yt-dlp gunicorn
Requires: ffmpeg installed on the system (apt install ffmpeg)
"""

import os
import re
import uuid
import threading
import time
from pathlib import Path

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

OUTPUT_DIR = Path("./downloads")
OUTPUT_DIR.mkdir(exist_ok=True)

jobs = {}


# ─────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────

def clean_filename(title: str) -> str:
    safe = re.sub(r'[\\/*?:"<>|]', '', title)
    safe = safe.strip().replace(' ', '_')
    return safe[:100]


def delete_file_after_delay(filepath: str, delay: int = 300):
    def _delete():
        time.sleep(delay)
        try:
            os.remove(filepath)
        except FileNotFoundError:
            pass
    threading.Thread(target=_delete, daemon=True).start()


# ─────────────────────────────────────────────
# Front-end
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    """Sert le front-end (index.html) directement."""
    return send_file("index.html")


# ─────────────────────────────────────────────
# API
# ─────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": "1.0.0"})


@app.route("/api/info", methods=["POST"])
def get_video_info():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL manquante"}), 400

    ydl_opts = {"quiet": True, "skip_download": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return jsonify({
            "id": info.get("id"),
            "title": info.get("title"),
            "channel": info.get("uploader"),
            "duration": info.get("duration"),
            "views": info.get("view_count"),
            "thumbnail": info.get("thumbnail"),
        })
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Vidéo inaccessible : {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": f"Erreur : {str(e)}"}), 500


@app.route("/api/convert", methods=["POST"])
def convert_video():
    data = request.get_json()
    url     = data.get("url", "").strip()
    quality = data.get("quality", "192")
    fmt     = data.get("format", "mp3").lower()

    if not url:
        return jsonify({"error": "URL manquante"}), 400

    SUPPORTED = {"mp3", "aac", "flac", "wav", "ogg"}
    if fmt not in SUPPORTED:
        return jsonify({"error": f"Format non supporté : {fmt}"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "progress": 0, "filename": None, "filepath": None, "error": None}

    threading.Thread(target=_run_conversion, args=(job_id, url, quality, fmt), daemon=True).start()
    return jsonify({"job_id": job_id})


def _run_conversion(job_id, url, quality, fmt):
    job = jobs[job_id]
    job["status"] = "running"
    output_path = OUTPUT_DIR / f"{job_id}.%(ext)s"

    def progress_hook(d):
        if d["status"] == "downloading":
            total   = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
            current = d.get("downloaded_bytes", 0)
            job["progress"] = int((current / total) * 70)
        elif d["status"] == "finished":
            job["progress"] = 80

    codec_map = {"mp3": "mp3", "aac": "aac", "flac": "flac", "wav": "pcm_s16le", "ogg": "libvorbis"}

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_path),
        "noplaylist": True,
        "quiet": True,
        "progress_hooks": [progress_hook],
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": codec_map[fmt], "preferredquality": quality}],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "audio")

        final_path = OUTPUT_DIR / f"{job_id}.{fmt}"
        if not final_path.exists():
            candidates = list(OUTPUT_DIR.glob(f"{job_id}.*"))
            if not candidates:
                raise FileNotFoundError("Fichier converti introuvable.")
            final_path = candidates[0]

        clean_name = f"{clean_filename(title)}_{quality}kbps.{fmt}"
        job["status"]   = "done"
        job["progress"] = 100
        job["filename"] = clean_name
        job["filepath"] = str(final_path)
        delete_file_after_delay(str(final_path), delay=300)

    except yt_dlp.utils.DownloadError as e:
        job["status"] = "error"
        job["error"]  = f"Erreur de téléchargement : {str(e)}"
    except Exception as e:
        job["status"] = "error"
        job["error"]  = f"Erreur : {str(e)}"


@app.route("/api/status/<job_id>", methods=["GET"])
def get_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job introuvable"}), 404
    return jsonify({"status": job["status"], "progress": job["progress"], "filename": job["filename"], "error": job["error"]})


@app.route("/api/download/<job_id>", methods=["GET"])
def download_file(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job introuvable"}), 404
    if job["status"] != "done":
        return jsonify({"error": "Conversion non terminée"}), 400
    filepath = job.get("filepath")
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "Fichier expiré"}), 404
    return send_file(filepath, as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
