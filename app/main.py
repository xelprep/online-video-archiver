import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from flask import Flask, jsonify
from yt_dlp import YoutubeDL

APP_VERSION = "1.1.0"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
VIDEO_DIR = DATA_DIR / "videos"
DB_PATH = DATA_DIR / "archive.db"
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/channels.yml"))
POLL_MINUTES = max(1, int(os.getenv("POLL_MINUTES", "30")))
DEFAULT_N = max(1, int(os.getenv("LATEST_N", "10")))
TZ = timezone.utc

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("yt-archiver")

app = Flask(__name__)
lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_url TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            video_id TEXT NOT NULL,
            title TEXT NOT NULL,
            upload_date TEXT,
            filepath TEXT NOT NULL,
            downloaded_at TEXT NOT NULL,
            UNIQUE(channel_url, video_id)
        );
        CREATE INDEX IF NOT EXISTS idx_videos_channel_date
          ON videos(channel_url, upload_date, downloaded_at);
        """)


def load_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    channels = cfg.get("channels", [])
    if not isinstance(channels, list):
        raise ValueError("channels must be a list")
    normalized = []
    for item in channels:
        if isinstance(item, str):
            normalized.append({"url": item, "latest_n": DEFAULT_N})
        elif isinstance(item, dict) and item.get("url"):
            normalized.append({
                "url": str(item["url"]).strip(),
                "latest_n": max(1, int(item.get("latest_n", DEFAULT_N))),
            })
        else:
            raise ValueError(f"Invalid channel entry: {item!r}")
    return normalized


def safe_filename(value, max_len=180):
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r'[<>:"|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    value = re.sub(r"[. ]+$", "", value)
    return value[:max_len] or "untitled"


def output_template():
    # yt-dlp performs the final extension substitution after merging/remuxing.
    return str(VIDEO_DIR / "%(uploader)s-%(title)s-%(upload_date)s.%(ext)s")


def ydl_opts(download=False, outtmpl=None):
    # Apple-TV-friendly selection: never exceed 1080p and prefer H.264/AVC
    # over HEVC. The height constraint is applied to the source video stream,
    # so yt-dlp never downloads 4K/1440p merely to discard or transcode it.
    # Separate video/audio streams are muxed into MP4 by FFmpeg without re-encoding.
    fmt = (
        "(bv*[height<=1080][ext=mp4][vcodec^=avc1]+ba[ext=m4a][acodec^=mp4a])"
        "/(bv*[height<=1080][ext=mp4][vcodec^=hvc1]+ba[ext=m4a][acodec^=mp4a])"
        "/(b[height<=1080][ext=mp4][vcodec^=avc1][acodec^=mp4a])"
        "/(b[height<=1080][ext=mp4][vcodec^=hvc1][acodec^=mp4a])"
    )
    opts = {
        "quiet": not download,
        "no_warnings": False,
        "ignoreerrors": False,
        "noplaylist": False,
        "extract_flat": True,
        "skip_download": not download,
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": outtmpl or output_template(),
        "restrictfilenames": False,
        "windowsfilenames": True,
        "overwrites": False,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "check_formats": True,
        "postprocessors": [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
        ],
    }
    return opts


def list_channel(channel_url, n):
    # Use the channel's uploads playlist and only inspect enough recent entries.
    opts = ydl_opts(download=False)
    opts.update({"playlistend": n, "extract_flat": True})
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
    entries = [] if not info else [e for e in (info.get("entries") or []) if e]
    result = []
    for e in entries[:n]:
        video_id = e.get("id")
        if not video_id:
            continue
        result.append({
            "id": video_id,
            "title": e.get("title") or video_id,
            "url": e.get("url") or f"https://www.youtube.com/watch?v={video_id}",
            "upload_date": e.get("upload_date"),
            "channel": e.get("channel") or e.get("uploader"),
        })
    return result


def get_full_info(video_url):
    opts = ydl_opts(download=False)
    opts["extract_flat"] = False
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(video_url, download=False)


def existing(channel_url, video_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE channel_url=? AND video_id=?",
            (channel_url, video_id),
        ).fetchone()
    return row


def find_downloaded_path(channel_url, video_id):
    with db() as conn:
        row = conn.execute(
            "SELECT filepath FROM videos WHERE channel_url=? AND video_id=?",
            (channel_url, video_id),
        ).fetchone()
    if row and Path(row["filepath"]).exists():
        return Path(row["filepath"])
    return None


def record_video(channel_url, info, filepath):
    now = datetime.now(TZ).isoformat()
    upload_date = info.get("upload_date") or ""
    channel_name = info.get("channel") or info.get("uploader") or "UnknownChannel"
    title = info.get("title") or info.get("id") or "Untitled"
    with db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO videos
               (channel_url, channel_name, video_id, title, upload_date, filepath, downloaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (channel_url, channel_name, info["id"], title, upload_date, str(filepath), now),
        )


def locate_downloaded_file(video_id):
    matches = list(VIDEO_DIR.glob(f"*{video_id}*.mp4"))
    return matches[0] if matches else None


def download_video(channel_url, video_id):
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    info = get_full_info(video_url)
    if not info:
        raise RuntimeError(f"Could not extract metadata for {video_id}")

    uploader = safe_filename(info.get("channel") or info.get("uploader") or "UnknownChannel")
    title = safe_filename(info.get("title") or video_id)
    upload_date = info.get("upload_date") or "unknown-date"
    # Include the ID in a temporary/output filename to prevent collisions. It is
    # removed from the final user-facing filename below.
    final_name = f"{uploader}-{upload_date}-{title}.mp4"
    final_path = VIDEO_DIR / final_name

    if final_path.exists():
        record_video(channel_url, info, final_path)
        return final_path

    tmp_template = str(VIDEO_DIR / f".{safe_filename(uploader)}-{safe_filename(title)}-{upload_date}-{video_id}.%(ext)s")
    opts = ydl_opts(download=True, outtmpl=tmp_template)
    opts["extract_flat"] = False
    opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    with YoutubeDL(opts) as ydl:
        ydl.download([video_url])

    candidates = list(VIDEO_DIR.glob(f".*-{video_id}.mp4"))
    if not candidates:
        # yt-dlp may have changed the exact temporary name; locate a recent mp4
        # matching the video title/date and use it if there is exactly one.
        candidates = [p for p in VIDEO_DIR.glob("*.mp4") if video_id in p.name]
    if not candidates:
        raise RuntimeError(f"Download completed but output file for {video_id} was not found")

    source = candidates[0]
    if final_path.exists():
        source.unlink(missing_ok=True)
    else:
        source.replace(final_path)
    record_video(channel_url, info, final_path)
    return final_path


def enforce_retention(channel_url, latest_n):
    with db() as conn:
        rows = conn.execute(
            """SELECT id, filepath FROM videos
               WHERE channel_url=?
               ORDER BY CASE WHEN upload_date='' THEN 1 ELSE 0 END,
                        upload_date DESC, downloaded_at DESC""",
            (channel_url,),
        ).fetchall()
    for row in rows[latest_n:]:
        path = Path(row["filepath"])
        try:
            path.unlink(missing_ok=True)
        finally:
            with db() as conn:
                conn.execute("DELETE FROM videos WHERE id=?", (row["id"],))
        log.info("Retention: deleted %s", path)


def sync_channel(channel_url, latest_n):
    log.info("Checking %s (latest_n=%d)", channel_url, latest_n)
    entries = list_channel(channel_url, latest_n)
    # Process oldest -> newest so an interrupted initial sync leaves the newest
    # videos to be picked up on the next cycle.
    for entry in reversed(entries):
        if existing(channel_url, entry["id"]):
            continue
        try:
            path = download_video(channel_url, entry["id"])
            log.info("Downloaded: %s", path.name)
        except Exception:
            log.exception("Failed downloading %s (%s)", entry["id"], entry["title"])
    enforce_retention(channel_url, latest_n)


def sync_all():
    if not lock.acquire(blocking=False):
        log.info("Sync already running; skipping overlapping cycle")
        return
    try:
        channels = load_config()
        for ch in channels:
            try:
                sync_channel(ch["url"], ch["latest_n"])
            except Exception:
                log.exception("Channel sync failed: %s", ch["url"])
    finally:
        lock.release()


def worker():
    while True:
        started = time.monotonic()
        try:
            sync_all()
        except Exception:
            log.exception("Sync cycle failed")
        elapsed = time.monotonic() - started
        sleep_for = max(5, POLL_MINUTES * 60 - elapsed)
        time.sleep(sleep_for)


@app.get("/health")
def health():
    try:
        channels = load_config()
        with db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        return jsonify({"status": "ok", "version": APP_VERSION, "channels": len(channels), "tracked_videos": count})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.get("/api/videos")
def api_videos():
    with db() as conn:
        rows = conn.execute(
            "SELECT channel_name, video_id, title, upload_date, filepath, downloaded_at FROM videos ORDER BY upload_date DESC, downloaded_at DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    init_db()
    # Run one synchronization immediately, then continue in the background.
    sync_all()
    threading.Thread(target=worker, daemon=True, name="sync-worker").start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
