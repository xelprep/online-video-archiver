import contextlib
import functools
import hashlib
import logging
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from yt_dlp import YoutubeDL

APP_VERSION = "1.5.0"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
VIDEO_DIR = DATA_DIR / "videos"
DB_PATH = DATA_DIR / "archive.db"
# Env vars provide *initial defaults only*; once the settings table holds a
# value, it is the source of truth at runtime (B3).
LOG_LEVEL_DEFAULT = os.getenv("LOG_LEVEL", "INFO").upper()
# How often the worker re-checks the paused flag while paused.
PAUSE_POLL_SECONDS = 30
# Web UI password (B3/C7). Required: the app refuses to start without it.
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
# Login throttling (in-memory; resets on restart).
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60
TZ = timezone.utc

logging.basicConfig(
    level=LOG_LEVEL_DEFAULT,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("yt-archiver")


def _env_int(name, default):
    # A10: a malformed env value must not crash startup — fall back to the
    # default and warn instead of raising at import time.
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Ignoring invalid integer for %s=%r; using %d", name, raw, default)
        return default


POLL_MINUTES_DEFAULT = max(1, _env_int("POLL_MINUTES", 30))

app = Flask(__name__)
app.config.update(
    # Derived from the password so sessions survive restarts, and changing the
    # password invalidates every existing session.
    SECRET_KEY=hashlib.sha256(WEB_PASSWORD.encode("utf-8")).hexdigest(),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
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
        rows = conn.execute(
            """SELECT c.url, c.name, c.latest_n, c.added_at,
                      (SELECT COUNT(*) FROM videos v WHERE v.channel_url = c.url) AS video_count
               FROM channels c ORDER BY c.url"""
        ).fetchall()
    return [dict(r) for r in rows]


def now_iso():
    return datetime.now(TZ).isoformat()


# ---------------------------------------------------------------------------
# Web UI (Phase 2): auth, CSRF, settings & channels APIs
# ---------------------------------------------------------------------------

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
CHANNEL_URL_RE = re.compile(
    r"^https://(?:www\.)?youtube\.com/(?:@[^/]+|channel/UC[^/]+|c/[^/]+|user/[^/]+)$"
)

login_failures = {}
login_failures_lock = threading.Lock()


def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("authed"):
            return f(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "authentication required"}), 401
        return redirect(url_for("login"))
    return wrapper


def require_csrf(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf") or ""
        if not session.get("csrf") or not secrets.compare_digest(token, session["csrf"]):
            return jsonify({"error": "missing or invalid CSRF token"}), 403
        return f(*args, **kwargs)
    return wrapper


def _login_locked_out(ip):
    with login_failures_lock:
        entry = login_failures.get(ip)
        return bool(entry and entry[1] > time.time())


def _record_login_failure(ip):
    with login_failures_lock:
        count, _ = login_failures.get(ip, (0, 0.0))
        count += 1
        lockout = time.time() + LOGIN_LOCKOUT_SECONDS if count >= LOGIN_MAX_FAILURES else 0.0
        login_failures[ip] = (count, lockout)
    if count >= LOGIN_MAX_FAILURES:
        log.warning("Login throttled for %s after %d failed attempts", ip, count)


def get_settings():
    try:
        poll_minutes = int(get_setting("poll_minutes", str(POLL_MINUTES_DEFAULT)))
    except (TypeError, ValueError):
        poll_minutes = POLL_MINUTES_DEFAULT
    return {
        "poll_minutes": poll_minutes,
        "log_level": get_setting("log_level", "INFO"),
        "paused": is_paused(),
    }


def normalize_channel_url(raw):
    # Accepts @handle, youtube.com/@handle, youtube.com/channel/UC... and the
    # like; returns the canonical https URL, or None if not a channel URL.
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("@"):
        s = "https://www.youtube.com/" + s
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    s = re.sub(r"^http://", "https://", s, flags=re.I).rstrip("/")
    return s if CHANNEL_URL_RE.match(s) else None


def parse_latest_n(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def safe_filename(value, max_len=180):
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r'[<>:"|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    value = re.sub(r"[. ]+$", "", value)
    return value[:max_len] or "untitled"


def output_template():
    # yt-dlp performs the final extension substitution after merging/remuxing.
    # Files live in a per-channel subfolder; the channel name is not part of
    # the file name itself.
    return str(VIDEO_DIR / "%(uploader)s" / "%(upload_date)s-%(title)s.%(ext)s")


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
        # check_formats probes each format by writing a temp file to the "temp"
        # output path, which defaults to the cwd. The container runs
        # unprivileged with cwd=/app (read-only), so pin temp files to a
        # writable dir under DATA_DIR.
        "paths": {"temp": str(DATA_DIR / "tmp")},
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


# yt-dlp availability values for videos that require authentication (channel
# membership or YouTube Premium). They appear in a channel's listing but
# cannot be downloaded without cookies, so they must not consume slots in
# the channel's latest-N count.
RESTRICTED_AVAILABILITY = {"subscriber_only", "premium_only"}


def list_channel(channel_url, n):
    # Target the channel's uploads tab (…/videos). Extracting the bare channel
    # URL returns the channel's *tabs* (Videos / Shorts / Live / …) as entries —
    # each carrying the channel ID, not a video ID — so downloads would fail
    # with "This video is unavailable". The uploads tab yields the real videos.
    uploads_url = channel_url.rstrip("/") + "/videos"
    # Members-only / premium-only videos show up in the listing (yt-dlp tags
    # them with an availability badge) but cannot be downloaded without
    # auth. They must not consume slots in the latest-N count, so over-fetch —
    # doubling until enough downloadable entries are found or the listing is
    # exhausted — then drop them before truncating to N.
    opts = ydl_opts(download=False)
    opts["extract_flat"] = True
    cap = max(n * 4, 100)
    while True:
        opts["playlistend"] = cap
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(uploads_url, download=False)
        entries = [] if not info else [e for e in (info.get("entries") or []) if e]
        downloadable = [
            e for e in entries
            if e.get("availability") not in RESTRICTED_AVAILABILITY
        ]
        if len(downloadable) >= n or len(entries) <= cap or cap >= 5000:
            break
        cap = min(cap * 2, 5000)
    result = []
    for e in downloadable[:n]:
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
    now = now_iso()
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


def download_video(channel_url, video_id, info=None):
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    if info is None:
        info = get_full_info(video_url)
    if not info:
        raise RuntimeError(f"Could not extract metadata for {video_id}")

    uploader = safe_filename(info.get("channel") or info.get("uploader") or "UnknownChannel")
    title = safe_filename(info.get("title") or video_id)
    upload_date = info.get("upload_date") or "unknown-date"
    # Videos live in a per-channel subfolder; the channel name is not part of
    # the file name itself. The video ID is part of the name so two videos can
    # never collide.
    channel_dir = VIDEO_DIR / uploader
    channel_dir.mkdir(parents=True, exist_ok=True)
    final_name = f"{upload_date}-{title}-{video_id}.mp4"
    final_path = channel_dir / final_name

    if final_path.exists():
        record_video(channel_url, info, final_path)
        return final_path

    tmp_template = str(channel_dir / f".{upload_date}-{title}-{video_id}.%(ext)s")
    opts = ydl_opts(download=True, outtmpl=tmp_template)
    opts["extract_flat"] = False
    opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([video_url])
    except Exception:
        # A10: a failed download can leave an orphaned hidden temp file; remove
        # it (any extension) before propagating the error.
        for leftover in channel_dir.glob(f".*-{video_id}.*"):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    candidates = list(channel_dir.glob(f".*-{video_id}.mp4"))
    if not candidates:
        # yt-dlp may have changed the exact temporary name; locate a recent mp4
        # matching the video title/date and use it if there is exactly one.
        candidates = [p for p in channel_dir.glob("*.mp4") if video_id in p.name]
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
    # C1: protected videos are kept in addition to the N most recent and do not
    # count toward N, so the N-most-recent selection considers only non-protected
    # rows. Protected rows are never selected, hence never deleted.
    with db() as conn:
        rows = conn.execute(
            """SELECT id, filepath FROM videos
               WHERE channel_url=? AND protected=0
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


def is_restricted_error(exc):
    # Auth-gated videos (members-only / premium-only) fail with a
    # server-provided message ("Join this channel to get access to
    # members-only content…"). Match on its stable keywords so such videos
    # can be skipped with a clean warning instead of a full traceback.
    msg = str(exc).lower()
    return "members-only" in msg or "join this channel" in msg or "premium" in msg


def sync_channel(channel_url, latest_n):
    log.info("Checking %s (latest_n=%d)", channel_url, latest_n)
    entries = list_channel(channel_url, latest_n)
    # If the channel was added without a display name, learn it from metadata.
    if entries:
        resolved = entries[0].get("channel")
        if resolved:
            with db() as conn:
                row = conn.execute("SELECT name FROM channels WHERE url=?", (channel_url,)).fetchone()
                if row is not None and not row["name"]:
                    conn.execute("UPDATE channels SET name=? WHERE url=?", (resolved, channel_url))
    # Process oldest -> newest so an interrupted initial sync leaves the newest
    # videos to be picked up on the next cycle.
    for entry in reversed(entries):
        if existing(channel_url, entry["id"]):
            continue
        try:
            path = download_video(channel_url, entry["id"])
            log.info("Downloaded: %s", path.name)
        except Exception as exc:
            if is_restricted_error(exc):
                # Auth-gated video: expected and permanent, so skip it with a
                # clean warning instead of a full traceback.
                log.warning(
                    "Skipping %s (%s): not publicly available (members-only or premium-only)",
                    entry["id"], entry["title"],
                )
            else:
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


# ---------------------------------------------------------------------------
# On-demand download by URL/ID (B8)
# ---------------------------------------------------------------------------

# In-memory registry of in-flight on-demand downloads (lost on restart, which is
# fine — a completed download is persisted in the DB and on disk).
download_tasks = {}
download_tasks_lock = threading.Lock()


def extract_video_id(url_or_id):
    # Accepts a bare video ID or any common YouTube URL form.
    s = (url_or_id or "").strip()
    if not s:
        return None
    if "youtu" in s:
        m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{6,20})", s)
        return m.group(1) if m else None
    return s if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", s) else None


def resolve_or_add_channel(info):
    # Derive the posting channel from the video's metadata. If it is not already
    # in the master list, add it at latest_n=0 (tracked/visible but not actively
    # synced). Returns the channel URL to associate the video with, or None.
    name = info.get("channel") or info.get("uploader") or None
    channel_id = info.get("channel_id") or info.get("uploader_id")
    url = (info.get("uploader_url") or "").strip() or None
    if not url and channel_id:
        url = f"https://www.youtube.com/channel/{channel_id}"
    if not url:
        return None
    url = url.rstrip("/")
    with db() as conn:
        row = conn.execute("SELECT url FROM channels WHERE url=?", (url,)).fetchone()
        if row is not None:
            return row["url"]
        if name:
            # Guard against a duplicate created via a different URL form (e.g. the
            # user added @handle while we derived channel/UC...).
            dup = conn.execute("SELECT url FROM channels WHERE name=?", (name,)).fetchone()
            if dup is not None:
                return dup["url"]
        conn.execute(
            "INSERT INTO channels (url, name, latest_n, added_at) VALUES (?, ?, 0, ?)",
            (url, name, now_iso()),
        )
    return url


def download_by_url(url_or_id):
    # Download a specific video (B8). It is protected by default (C2) and its
    # channel is auto-tracked at N=0 if not already present.
    video_id = extract_video_id(url_or_id)
    if not video_id:
        raise ValueError("Could not parse a YouTube video ID from the input")
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    info = get_full_info(video_url)
    if not info:
        raise RuntimeError(f"Could not extract metadata for {video_id}")
    channel_url = resolve_or_add_channel(info) or (
        f"unknown:{info.get('channel') or info.get('uploader') or video_id}"
    )
    path = download_video(channel_url, video_id, info=info)
    # video_id is globally unique on YouTube, so this is a safe, unambiguous key.
    with db() as conn:
        conn.execute("UPDATE videos SET protected=1 WHERE video_id=?", (video_id,))
    return path


def start_download_task(url_or_id):
    task_id = secrets.token_hex(8)
    task = {"id": task_id, "status": "running", "video_id": None,
            "message": "Starting…", "error": None}
    with download_tasks_lock:
        download_tasks[task_id] = task
    threading.Thread(target=_run_download_task, args=(task_id, url_or_id),
                     daemon=True, name=f"download-{task_id}").start()
    return task_id


def _run_download_task(task_id, url_or_id):
    def update(**fields):
        with download_tasks_lock:
            download_tasks[task_id].update(fields)
    try:
        video_id = extract_video_id(url_or_id)
        update(video_id=video_id, message=f"Fetching metadata for {video_id}…")
        download_by_url(url_or_id)
        update(status="done", message="Download complete")
    except Exception as exc:
        log.exception("On-demand download failed: %s", url_or_id)
        update(status="error", error=str(exc), message="Failed: " + str(exc))


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


# ---------------------------------------------------------------------------
# Web UI routes (B3/B6). /health stays public (C4); everything else is gated.
# ---------------------------------------------------------------------------

@app.get("/")
@require_auth
def index():
    session.setdefault("csrf", secrets.token_hex(16))
    return render_template("index.html", version=APP_VERSION, csrf_token=session["csrf"])


@app.get("/login")
def login():
    if session.get("authed"):
        return redirect(url_for("index"))
    return render_template("login.html", version=APP_VERSION, error=None)


@app.post("/login")
def login_post():
    if session.get("authed"):
        return redirect(url_for("index"))
    ip = request.remote_addr or "?"
    if _login_locked_out(ip):
        return render_template("login.html", version=APP_VERSION,
                               error="Too many failed attempts — try again in a minute."), 429
    password = request.form.get("password", "")
    if password and secrets.compare_digest(password, WEB_PASSWORD):
        session["authed"] = True
        session.permanent = True
        session["csrf"] = secrets.token_hex(16)
        with login_failures_lock:
            login_failures.pop(ip, None)
        return redirect(url_for("index"))
    _record_login_failure(ip)
    return render_template("login.html", version=APP_VERSION, error="Incorrect password."), 401


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/api/settings")
@require_auth
def api_get_settings():
    return jsonify(get_settings())


@app.put("/api/settings")
@require_auth
@require_csrf
def api_update_settings():
    data = request.get_json(silent=True) or {}
    errors = {}
    if "poll_minutes" in data:
        try:
            pm = int(data["poll_minutes"])
        except (TypeError, ValueError):
            pm = None
            errors["poll_minutes"] = "must be an integer"
        if pm is not None and not 1 <= pm <= 1440:
            pm = None
            errors["poll_minutes"] = "must be between 1 and 1440"
        if pm is not None:
            set_setting("poll_minutes", pm)
    if "log_level" in data:
        level = str(data["log_level"]).upper()
        if level not in VALID_LOG_LEVELS:
            errors["log_level"] = "must be one of: " + ", ".join(VALID_LOG_LEVELS)
        else:
            set_setting("log_level", level)
            apply_log_level()
    if "paused" in data:
        set_setting("paused", "1" if data["paused"] else "0")
    if errors:
        return jsonify({"error": errors}), 400
    return jsonify(get_settings())


@app.get("/api/channels")
@require_auth
def api_list_channels():
    return jsonify(load_channels())


@app.post("/api/channels")
@require_auth
@require_csrf
def api_add_channel():
    data = request.get_json(silent=True) or {}
    url = normalize_channel_url(data.get("url"))
    if not url:
        return jsonify({"error": {"url": "Not a valid YouTube channel URL. Use @handle, "
                                         "https://www.youtube.com/@handle, or "
                                         "https://www.youtube.com/channel/UC..."}}), 400
    if data.get("latest_n") in (None, ""):
        return jsonify({"error": {"latest_n": "Required — how many of the most recent videos "
                                              "to keep (0 = keep only protected videos)"}}), 400
    n = parse_latest_n(data["latest_n"])
    if n is None:
        return jsonify({"error": {"latest_n": "Must be an integer >= 0"}}), 400
    name = (data.get("name") or "").strip() or None
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO channels (url, name, latest_n, added_at) VALUES (?, ?, ?, ?)",
            (url, name, n, now_iso()),
        )
    if cur.rowcount == 0:
        return jsonify({"error": {"url": "Channel already exists"}}), 409
    return jsonify(load_channels()), 201


@app.put("/api/channels")
@require_auth
@require_csrf
def api_update_channel():
    data = request.get_json(silent=True) or {}
    url = normalize_channel_url(data.get("url"))
    if not url:
        return jsonify({"error": {"url": "Not a valid YouTube channel URL"}}), 400
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM channels WHERE url=?", (url,)).fetchone()
    if exists is None:
        return jsonify({"error": {"url": "Channel not found"}}), 404
    sets, params = [], []
    if "name" in data:
        sets.append("name=?")
        params.append((data.get("name") or "").strip() or None)
    if "latest_n" in data:
        n = parse_latest_n(data["latest_n"])
        if n is None:
            return jsonify({"error": {"latest_n": "Must be an integer >= 0"}}), 400
        sets.append("latest_n=?")
        params.append(n)
    if not sets:
        return jsonify({"error": "Nothing to update"}), 400
    with db() as conn:
        conn.execute(f"UPDATE channels SET {', '.join(sets)} WHERE url=?", [*params, url])
    return jsonify(load_channels())


@app.delete("/api/channels")
@require_auth
@require_csrf
def api_delete_channel():
    data = request.get_json(silent=True) or {}
    url = normalize_channel_url(data.get("url"))
    if not url:
        return jsonify({"error": {"url": "Not a valid YouTube channel URL"}}), 400
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM channels WHERE url=?", (url,)).fetchone()
        if exists is None:
            return jsonify({"error": {"url": "Channel not found"}}), 404
        rows = conn.execute("SELECT filepath FROM videos WHERE channel_url=?", (url,)).fetchall()
    # Move the channel's files into an orphaned folder (kept on disk, no longer
    # referenced by the app), then drop the DB rows.
    orphaned_dir = VIDEO_DIR / "orphaned"
    orphaned_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%dT%H%M%SZ")
    for row in rows:
        src = Path(row["filepath"])
        if not src.exists():
            continue
        target = orphaned_dir / f"{src.stem}-orphaned-{stamp}{src.suffix}"
        k = 2
        while target.exists():
            target = orphaned_dir / f"{src.stem}-orphaned-{stamp}-{k}{src.suffix}"
            k += 1
        shutil.move(str(src), str(target))
        log.info("Orphaned %s -> %s", src.name, target.name)
    with db() as conn:
        conn.execute("DELETE FROM videos WHERE channel_url=?", (url,))
        conn.execute("DELETE FROM channels WHERE url=?", (url,))
    log.info("Deleted channel %s (%d videos orphaned)", url, len(rows))
    return jsonify(load_channels())


@app.get("/api/videos")
@require_auth
def api_videos():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, channel_name, video_id, title, upload_date, filepath, downloaded_at, protected FROM videos ORDER BY upload_date DESC, downloaded_at DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.patch("/api/videos")
@require_auth
@require_csrf
def api_update_video():
    # B7: toggle a video's "protect" flag. Protected videos are exempt from
    # retention pruning (see enforce_retention / C1).
    data = request.get_json(silent=True) or {}
    vid = data.get("id")
    if vid is None:
        return jsonify({"error": "id is required"}), 400
    if "protected" not in data:
        return jsonify({"error": "nothing to update"}), 400
    protected = 1 if data["protected"] else 0
    with db() as conn:
        cur = conn.execute("UPDATE videos SET protected=? WHERE id=?", (protected, vid))
    if cur.rowcount == 0:
        return jsonify({"error": "video not found"}), 404
    return jsonify({"ok": True, "id": vid, "protected": bool(protected)})


def resolve_video_path(video_id):
    # Look up the stored file path for a video and verify it lives inside the
    # video directory, so file serving can never escape the data dir (B9/B13).
    with db() as conn:
        row = conn.execute("SELECT filepath FROM videos WHERE id=?", (video_id,)).fetchone()
    if row is None:
        return None
    path = Path(row["filepath"]).resolve()
    try:
        path.relative_to(VIDEO_DIR.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path


@app.get("/api/videos/<int:video_id>/file")
@require_auth
def api_video_file(video_id):
    # B9 (inline, for the in-UI player) and B13 (attachment, to save to the
    # user's machine) share this range-capable serving path; only the
    # Content-Disposition differs. send_file(conditional=True) answers Range
    # headers with HTTP 206 so seeking works.
    path = resolve_video_path(video_id)
    if path is None:
        return jsonify({"error": "video file not found"}), 404
    as_attachment = request.args.get("disposition") == "attachment"
    return send_file(
        path,
        mimetype="video/mp4",
        as_attachment=as_attachment,
        download_name=path.name if as_attachment else None,
        conditional=True,
    )


@app.post("/api/videos/download")
@require_auth
@require_csrf
def api_start_download():
    # B8: kick off an on-demand download in the background; the client polls
    # /api/videos/download/<task_id> for progress.
    data = request.get_json(silent=True) or {}
    url_or_id = (data.get("url") or "").strip()
    if not url_or_id:
        return jsonify({"error": {"url": "Provide a YouTube video URL or ID"}}), 400
    if not extract_video_id(url_or_id):
        return jsonify({"error": {"url": "Could not parse a YouTube video URL or ID"}}), 400
    task_id = start_download_task(url_or_id)
    return jsonify({"task_id": task_id}), 202


@app.get("/api/videos/download/<task_id>")
@require_auth
def api_download_status(task_id):
    with download_tasks_lock:
        task = download_tasks.get(task_id)
    if task is None:
        return jsonify({"error": "unknown task"}), 404
    return jsonify(task)


if __name__ == "__main__":
    if not WEB_PASSWORD:
        log.critical(
            "WEB_PASSWORD is not set. The web UI password is required — set the "
            "WEB_PASSWORD environment variable (e.g. in docker-compose.yml) and restart."
        )
        sys.exit(1)
    init_db()
    seed_settings()
    apply_log_level()
    # A3: start the worker *before* the HTTP server. Its first action is an
    # immediate sync, which now runs in the background so /health is reachable
    # during the (potentially long) initial sync instead of only after it.
    # While paused — the first-run default — that initial sync is a no-op.
    threading.Thread(target=worker, daemon=True, name="sync-worker").start()
    # Fixed port: remap via Docker (ports: "8090:8080"), not via env.
    app.run(host="0.0.0.0", port=8080)
