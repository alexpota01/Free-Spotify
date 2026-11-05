"""Free Spotify: Flask library + YouTube Music search + embedded player UI.

Keep port 5000 private in Codespaces. This is a development app without user accounts.
Online results are played through the official, visible YouTube IFrame player.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, render_template, request, send_from_directory

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "uploads"
DATABASE = BASE / "music.db"
UPLOADS.mkdir(exist_ok=True)

ALLOWED = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 150 * 1024 * 1024


def connect():
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    # Preserves the original Stage 2 database and uploads.
    with closing(connect()) as db:
        with db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS songs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    favorite INTEGER NOT NULL DEFAULT 0
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS online_favorites (
                    video_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL DEFAULT '',
                    cover TEXT NOT NULL DEFAULT ''
                )
            """)


@app.after_request
def set_headers(response):
    # YouTube requires client identity; don't suppress cross-origin Referer.
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/songs")
def songs():
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM songs ORDER BY id DESC").fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/upload")
def upload():
    files = request.files.getlist("files")
    added = 0
    with closing(connect()) as db:
        with db:
            for file in files:
                if not file.filename:
                    continue
                suffix = Path(file.filename).suffix.lower()
                if suffix not in ALLOWED:
                    continue
                stored_filename = uuid4().hex + suffix
                title = Path(file.filename).stem[:200]
                file.save(UPLOADS / stored_filename)
                db.execute(
                    "INSERT INTO songs (title, filename) VALUES (?, ?)",
                    (title, stored_filename),
                )
                added += 1
    if not added:
        return jsonify(error="No supported audio files were uploaded"), 400
    return jsonify(added=added)


@app.get("/media/<filename>")
def media(filename):
    return send_from_directory(UPLOADS, filename, conditional=True)


@app.post("/api/songs/<int:song_id>/favorite")
def local_favorite(song_id):
    with closing(connect()) as db:
        with db:
            result = db.execute("""
                UPDATE songs
                SET favorite = CASE favorite WHEN 1 THEN 0 ELSE 1 END
                WHERE id = ?
            """, (song_id,))
            if not result.rowcount:
                return jsonify(error="Song not found"), 404
    return jsonify(success=True)


@app.delete("/api/songs/<int:song_id>")
def delete_local_song(song_id):
    with closing(connect()) as db:
        with db:
            song = db.execute(
                "SELECT filename FROM songs WHERE id = ?", (song_id,)
            ).fetchone()
            if song is None:
                return jsonify(error="Song not found"), 404
            db.execute("DELETE FROM songs WHERE id = ?", (song_id,))
    (UPLOADS / song["filename"]).unlink(missing_ok=True)
    return jsonify(success=True)


@app.get("/api/online-search")
def online_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    if len(query) > 150:
        return jsonify(error="Search must be 150 characters or less"), 400

    try:
        from ytmusicapi import YTMusic

        results = YTMusic().search(query, filter="songs", limit=20)
        tracks = []
        seen = set()
        for item in results:
            video_id = item.get("videoId")
            if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
                continue
            if video_id in seen:
                continue
            seen.add(video_id)
            artists = item.get("artists") or []
            thumbnails = item.get("thumbnails") or []
            artist = ", ".join(
                part.get("name", "") for part in artists if isinstance(part, dict)
            )
            cover = thumbnails[-1].get("url", "") if thumbnails else ""
            tracks.append({
                "id": video_id,
                "title": str(item.get("title") or "Untitled"),
                "artist": artist or "YouTube Music",
                "cover": cover,
            })
        return jsonify(tracks)
    except Exception:
        app.logger.exception("YouTube Music search failed")
        return jsonify(error="Search failed; check the Flask terminal"), 502


@app.get("/api/online-favorites")
def get_online_favorites():
    with closing(connect()) as db:
        rows = db.execute("""
            SELECT video_id AS id, title, artist, cover
            FROM online_favorites ORDER BY rowid DESC
        """).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/online-favorites")
def save_online_favorite():
    data = request.get_json(silent=True) or {}
    video_id = data.get("id", "")
    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
        return jsonify(error="Invalid video ID"), 400

    title = str(data.get("title") or "Untitled")[:200]
    artist = str(data.get("artist") or "")[:200]
    cover = str(data.get("cover") or "")[:1500]
    # Only retain HTTPS cover URLs, never arbitrary schemes.
    if not cover.startswith("https://"):
        cover = ""

    with closing(connect()) as db:
        with db:
            db.execute("""
                INSERT INTO online_favorites (video_id, title, artist, cover)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    title = excluded.title,
                    artist = excluded.artist,
                    cover = excluded.cover
            """, (video_id, title, artist, cover))
    return jsonify(success=True)


@app.delete("/api/online-favorites/<video_id>")
def remove_online_favorite(video_id):
    if not VIDEO_ID.fullmatch(video_id):
        return jsonify(error="Invalid video ID"), 400
    with closing(connect()) as db:
        with db:
            db.execute("DELETE FROM online_favorites WHERE video_id = ?", (video_id,))
    return jsonify(success=True)


@app.errorhandler(413)
def file_too_large(error):
    return jsonify(error="Upload exceeds the 150 MB limit"), 413


initialize_database()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
