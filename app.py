"""
SoundRip Pro — Backend Python
Requires: pip install flask flask-cors yt-dlp
Requires: ffmpeg installed on the system (apt install ffmpeg / brew install ffmpeg)
"""

import os
import re
import uuid
import threading
import time
from pathlib import Path

from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)  # Autorise les requêtes depuis le front-end

# Dossier temporaire pour les fichiers convertis
OUTPUT_DIR = Path("./downloads")
OUTPUT_DIR.mkdir(exist_ok=True)

# Stockage en mémoire des jobs en cours
jobs = {}  # job_id -> { status, progress, filename, error }


# ─────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────

def clean_filename(title: str) -> str:
    """Nettoie le titre pour en faire un nom de fichier valide."""
    safe = re.sub(r'[\\/*?:"<>|]', '', title)
    safe = safe.strip().replace(' ', '_')
    return safe[:100]  # Limite la longueur


def delete_file_after_delay(filepath: str, delay: int = 300):
    """Supprime le fichier après un délai (défaut : 5 minutes)."""
    def _delete():
        time.sleep(delay)
        try:
            os.remove(filepath)
            print(f"[cleanup] Supprimé : {filepath}")
        except FileNotFoundError:
            pass
    threading.Thread(target=_delete, daemon=True).start()


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route("/api/info", methods=["POST"])
def get_video_info():
    """
    Récupère les métadonnées d'une vidéo YouTube.
    Body JSON : { "url": "https://youtube.com/watch?v=..." }
    """
    data = request.get_json()
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL manquante"}), 400

    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        return jsonify({
            "id": info.get("id"),
            "title": info.get("title"),
            "channel": info.get("uploader"),
            "duration": info.get("duration"),       # en secondes
            "views": info.get("view_count"),
            "thumbnail": info.get("thumbnail"),
            "description": (info.get("description") or "")[:300],
        })

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Vidéo inaccessible : {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": f"Erreur inattendue : {str(e)}"}), 500


@app.route("/api/convert", methods=["POST"])
def convert_video():
    """
    Lance la conversion d'une vidéo YouTube en audio.
    Body JSON :
    {
        "url": "https://youtube.com/watch?v=...",
        "quality": "192",     // kbps : 96 | 128 | 192 | 256 | 320
        "format": "mp3"       // mp3 | aac | flac | wav | ogg
    }
    Retourne immédiatement un job_id, puis le front-end poll /api/status/<job_id>
    """
    data = request.get_json()
    url     = data.get("url", "").strip()
    quality = data.get("quality", "192")
    fmt     = data.get("format", "mp3").lower()

    if not url:
        return jsonify({"error": "URL manquante"}), 400

    # Formats supportés
    SUPPORTED_FORMATS = {"mp3", "aac", "flac", "wav", "ogg"}
    if fmt not in SUPPORTED_FORMATS:
        return jsonify({"error": f"Format non supporté. Choisissez parmi : {', '.join(SUPPORTED_FORMATS)}"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "pending",
        "progress": 0,
        "filename": None,
        "filepath": None,
        "error": None,
    }

    # Lancement de la conversion en arrière-plan
    thread = threading.Thread(
        target=_run_conversion,
        args=(job_id, url, quality, fmt),
        daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id})


def _run_conversion(job_id: str, url: str, quality: str, fmt: str):
    """Tâche de conversion exécutée dans un thread séparé."""
    job = jobs[job_id]
    job["status"] = "running"

    output_path = OUTPUT_DIR / f"{job_id}.%(ext)s"

    def progress_hook(d):
        if d["status"] == "downloading":
            total   = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
            current = d.get("downloaded_bytes", 0)
            # La progression de téléchargement représente 0→70%
            job["progress"] = int((current / total) * 70)
        elif d["status"] == "finished":
            job["progress"] = 80  # Conversion ffmpeg en cours

    # Mapping format → codec ffmpeg
    codec_map = {
        "mp3":  "mp3",
        "aac":  "aac",
        "flac": "flac",
        "wav":  "pcm_s16le",
        "ogg":  "libvorbis",
    }

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_path),
        "noplaylist": True,
        "quiet": True,
        "progress_hooks": [progress_hook],
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec_map[fmt],
                "preferredquality": quality,
            }
        ],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "audio")

        # Trouver le fichier généré
        final_path = OUTPUT_DIR / f"{job_id}.{fmt}"
        if not final_path.exists():
            # Chercher avec une extension proche (ex: .m4a pour aac)
            candidates = list(OUTPUT_DIR.glob(f"{job_id}.*"))
            if not candidates:
                raise FileNotFoundError("Fichier converti introuvable.")
            final_path = candidates[0]

        clean_name = f"{clean_filename(title)}_{quality}kbps.{fmt}"

        job["status"]   = "done"
        job["progress"] = 100
        job["filename"] = clean_name
        job["filepath"] = str(final_path)

        # Auto-suppression après 5 minutes
        delete_file_after_delay(str(final_path), delay=300)

    except yt_dlp.utils.DownloadError as e:
        job["status"] = "error"
        job["error"]  = f"Erreur de téléchargement : {str(e)}"
    except Exception as e:
        job["status"] = "error"
        job["error"]  = f"Erreur : {str(e)}"


@app.route("/api/status/<job_id>", methods=["GET"])
def get_status(job_id: str):
    """
    Retourne l'état d'un job de conversion.
    Réponse :
    {
        "status": "pending" | "running" | "done" | "error",
        "progress": 0-100,
        "filename": "titre_192kbps.mp3",   // quand done
        "error": "..."                      // quand error
    }
    """
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job introuvable"}), 404

    return jsonify({
        "status":   job["status"],
        "progress": job["progress"],
        "filename": job["filename"],
        "error":    job["error"],
    })


@app.route("/api/download/<job_id>", methods=["GET"])
def download_file(job_id: str):
    """Télécharge le fichier audio converti."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job introuvable"}), 404

    if job["status"] != "done":
        return jsonify({"error": "Conversion non terminée"}), 400

    filepath = job.get("filepath")
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "Fichier non disponible (expiré ?)"}), 404

    return send_file(
        filepath,
        as_attachment=True,
        download_name=job["filename"],
    )


@app.route("/api/health", methods=["GET"])
def health():
    """Vérification que le serveur tourne."""
    return jsonify({"status": "ok", "version": "1.0.0"})


# ─────────────────────────────────────────────
# Lancement
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("🎵 SoundRip Pro — Backend démarré sur http://localhost:5000")
    app.run(debug=True, host="0.0.0.0", port=5000)
