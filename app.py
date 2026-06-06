from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os, re, uuid, threading, time
from pathlib import Path

app = Flask(__name__)
CORS(app)

OUTPUT_DIR = Path("./downloads")
OUTPUT_DIR.mkdir(exist_ok=True)

jobs = {}


# ── Utilitaires ───────────────────────────────────────────────────────────────

def clean_filename(title):
    safe = re.sub(r'[\\/*?:"<>|]', '', title).strip().replace(' ', '_')
    return safe[:100]

def auto_delete(filepath, delay=300):
    def _del():
        time.sleep(delay)
        try: os.remove(filepath)
        except: pass
    threading.Thread(target=_del, daemon=True).start()


# ── Front-end ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file("index.html")


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/info", methods=["POST"])
def info():
    url = request.get_json().get("url", "").strip()
    if not url:
        return jsonify({"error": "URL manquante"}), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
            d = ydl.extract_info(url, download=False)
        return jsonify({
            "id":        d.get("id"),
            "title":     d.get("title"),
            "channel":   d.get("uploader"),
            "duration":  d.get("duration"),
            "views":     d.get("view_count"),
            "thumbnail": d.get("thumbnail"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/convert", methods=["POST"])
def convert():
    body    = request.get_json()
    url     = body.get("url", "").strip()
    quality = body.get("quality", "192")
    fmt     = body.get("format", "mp3").lower()

    if not url:
        return jsonify({"error": "URL manquante"}), 400
    if fmt not in {"mp3", "aac", "flac", "wav", "ogg"}:
        return jsonify({"error": "Format non supporté"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "progress": 0, "filename": None, "filepath": None, "error": None}
    threading.Thread(target=_convert, args=(job_id, url, quality, fmt), daemon=True).start()
    return jsonify({"job_id": job_id})


def _convert(job_id, url, quality, fmt):
    job = jobs[job_id]
    job["status"] = "running"

    codec = {"mp3": "mp3", "aac": "aac", "flac": "flac", "wav": "pcm_s16le", "ogg": "libvorbis"}

    def hook(d):
        if d["status"] == "downloading":
            total   = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
            job["progress"] = int(d.get("downloaded_bytes", 0) / total * 70)
        elif d["status"] == "finished":
            job["progress"] = 80

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(OUTPUT_DIR / f"{job_id}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "progress_hooks": [hook],
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": codec[fmt], "preferredquality": quality}],
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "audio")

        final = OUTPUT_DIR / f"{job_id}.{fmt}"
        if not final.exists():
            candidates = list(OUTPUT_DIR.glob(f"{job_id}.*"))
            if not candidates:
                raise FileNotFoundError("Fichier introuvable après conversion")
            final = candidates[0]

        job.update({
            "status":   "done",
            "progress": 100,
            "filename": f"{clean_filename(title)}_{quality}kbps.{fmt}",
            "filepath": str(final),
        })
        auto_delete(str(final))

    except Exception as e:
        job.update({"status": "error", "error": str(e)})


@app.route("/api/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job introuvable"}), 404
    return jsonify({"status": job["status"], "progress": job["progress"],
                    "filename": job["filename"], "error": job["error"]})


@app.route("/api/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job introuvable"}), 404
    if job["status"] != "done":
        return jsonify({"error": "Pas encore prêt"}), 400
    path = job.get("filepath")
    if not path or not os.path.exists(path):
        return jsonify({"error": "Fichier expiré"}), 404
    return send_file(path, as_attachment=True, download_name=job["filename"])


# ── Lancement ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
