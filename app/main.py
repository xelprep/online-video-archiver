import contextlib
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify
from yt_dlp import YoutubeDL

APP_VERSION = "1.2.0"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
VIDEO_DIR = DATA_DIR / "videos"
DB_PATH = DATA_DIR / "archive.db"
# Env vars provide *initial defaults only*; once the settings table holds a
# value, it is the source of truth at runtime (B3).
POLL_MINUTES_DEFAULT = max(1, int(os.getenv("POLL_MINUTES", "30")))
LOG_LEVEL_DEFAULT = os.getenv("LOG_LEVEL", "INFO").upper()
# How often the worker re-checks the paused flag while paused.
PAUSE_POLL_SECONDS = 30
TZ = timezone.utc

logging.basicConfig(
    level=LOG_LEVEL_DEFAULT,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("yt-archiver")

app = Flask(__name__)
lock = threading.Lock()


@contextlib.contextmanager
def db():
    # The sqlite3 connection context manager commits/rolls back but does not
    # close; wrap it so the connection is always closed on exit.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


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
            protected INTEGER NOT NULL DEFAULT 0,
            UNIQUE(channel_url, video_id)
        );
        CREATE INDEX IF NOT EXISTS idx_videos_channel_date
          ON videos(channel_url, upload_date, downloaded_at);
        CREATE TABLE IF NOT EXISTS channels (
            url TEXT PRIMARY KEY,
            name TEXT,
            latest_n INTEGER NOT NULL,
            added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        migrate(conn)


def migrate(conn):
    # One-time upgrades for databases created by older versions.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    if "protected" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN protected INTEGER NOT NULL DEFAULT 0")


def get_setting(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def seed_settings():
    # Insert defaults only for keys that do not exist yet, so env-provided
    # initial values never overwrite a user's later changes (B3).
    defaults = {
        "paused": "1",  # B5: a fresh deploy starts paused
        "poll_minutes": str(POLL_MINUTES_DEFAULT),
        "log_level": LOG_LEVEL_DEFAULT,
    }
    with db() as conn:
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))


def is_paused():
    return get_setting("paused", "1") == "1"


def apply_log_level():
    try:
        logging.getLogger().setLevel(str(get_setting("log_level", "INFO")).upper())
    except ValueError:
        pass


def load_channels():
    with db() as conn:
        rows = conn.execute("SELECT url, name, latest_n FROM channels ORDER BY url").fetchall()
    return [dict(r) for r in rows]


def safe_filename(value, max_len=180):
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r'[<>:"|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    value = re.sub(r"[. ]+$", "", value)
    return value[:max_len] or "untitled"


def output_template():
    # yt-dlp performs the final extension substitution after merging/remuxing.
    return str(VIDEO_DIR / "%(uploader)s-%(upload_date)s-%(title)s.%(ext)s")


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


def download_video(channel_url, video_id):
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    info = get_full_info(video_url)
    if not info:
        raise RuntimeError(f"Could not extract metadata for {video_id}")

    uploader = safe_filename(info.get("channel") or info.get("uploader") or "UnknownChannel")
    title = safe_filename(info.get("title") or video_id)
    upload_date = info.get("upload_date") or "unknown-date"
    # The video ID is part of the final name so two videos can never collide.
    final_name = f"{uploader}-{upload_date}-{title}-{video_id}.mp4"
    final_path = VIDEO_DIR / final_name

    if final_path.exists():
        record_video(channel_url, info, final_path)
        return final_path

    tmp_template = str(VIDEO_DIR / f".{uploader}-{upload_date}-{title}-{video_id}.%(ext)s")
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
    if is_paused():
        log.info("Archiver is paused; skipping sync")
        return
    if not lock.acquire(blocking=False):
        log.info("Sync already running; skipping overlapping cycle")
        return
    try:
        for ch in load_channels():
            if ch["latest_n"] <= 0:
                # C8: channels with N=0 are skipped during the scheduled poll.
                continue
            try:
                sync_channel(ch["url"], ch["latest_n"])
            except Exception:
                log.exception("Channel sync failed: %s", ch["url"])
    finally:
        lock.release()


def worker():
    while True:
        paused = is_paused()
        apply_log_level()
        started = time.monotonic()
        try:
            sync_all()
        except Exception:
            log.exception("Sync cycle failed")
        elapsed = time.monotonic() - started
        if paused:
            # Re-check the flag frequently so an unpause takes effect quickly.
            time.sleep(PAUSE_POLL_SECONDS)
            continue
        try:
            poll_minutes = max(1, int(get_setting("poll_minutes", "30")))
        except (TypeError, ValueError):
            poll_minutes = POLL_MINUTES_DEFAULT
        time.sleep(max(5, poll_minutes * 60 - elapsed))


@app.get("/health")
def health():
    try:
        with db() as conn:
            channels = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
            videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        return jsonify({
            "status": "ok",
            "version": APP_VERSION,
            "channels": channels,
            "tracked_videos": videos,
            "paused": is_paused(),
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.get("/api/videos")
def api_videos():
    with db() as conn:
        rows = conn.execute(
            "SELECT channel_name, video_id, title, upload_date, filepath, downloaded_at, protected FROM videos ORDER BY upload_date DESC, downloaded_at DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    init_db()
    seed_settings()
    apply_log_level()
    # Run one synchronization immediately, then continue in the background.
    # (While paused — the first-run default — this is a no-op.)
    sync_all()
    threading.Thread(target=worker, daemon=True, name="sync-worker").start()
    # Fixed port: remap via Docker (ports: "8090:8080"), not via env.
    app.run(host="0.0.0.0", port=8080)
