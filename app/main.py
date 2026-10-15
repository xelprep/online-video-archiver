import concurrent.futures
import contextlib
import functools
import hashlib
import importlib
import json
import logging
import os
import random
import re
import secrets
import shutil
import smtplib
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import make_msgid
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from yt_dlp import YoutubeDL
# The Python API expects an ImpersonateTarget object (not a raw string) for the
# "impersonate" param — yt-dlp's CLI does this conversion itself, but the
# YoutubeDL(params) constructor does not. Same import path the CLI uses.
from yt_dlp.networking.impersonate import ImpersonateTarget

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
# Placeholder/example passwords that must never be used in production. If the
# configured password is one of these, refuse to start — we don't want an
# easily-guessed interface left exposed (C7 hardening).
PLACEHOLDER_PASSWORDS = {"change-me", "changeme", "password", "123456"}
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

# ---------------------------------------------------------------------------
# Anti-detection: a consistent identity, human-like pacing, rate-limit handling
# and TLS/browser impersonation. See dev/PLANNING-antidetect.md for the full
# design and the verified yt-dlp behaviour these settings rely on.
# ---------------------------------------------------------------------------
# A single, common, current desktop-Chrome User-Agent. Used only when TLS
# impersonation is OFF; when impersonation is ON, curl_cffi sends the
# impersonated browser's own UA (which matches the TLS fingerprint), so we must
# not override it — a UA/TLS mismatch is itself a bot signal.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
# Valid values for the tls_impersonate setting. "off" disables impersonation
# (yt-dlp's default urllib backend is used).
TLS_IMPERSONATE_CHOICES = ("off", "chrome", "safari", "firefox")
# Valid values for the rate_limit_action setting.
RATE_LIMIT_ACTIONS = ("abort", "backoff")
# In "backoff" mode, abort the cycle after this many consecutive rate-limits so
# a hard block does not turn into an endless sleep-and-retry loop.
RATE_LIMIT_MAX_CONSECUTIVE = 3

# ---------------------------------------------------------------------------
# Phase 4 — adaptive public-SOCKS5 proxy rotation. When YouTube rate-limits the
# direct egress IP, we reactively build a pool of vetted public SOCKS5 proxies
# (two-phase: quick TLS connect, then a real extract+download) and route
# downloads through them, rotating when a proxy stops working. We probe the
# direct connection periodically and drop back to direct egress once the local
# IP is no longer throttled. See dev/PLANNING-antidetect.md (spike results).
# ---------------------------------------------------------------------------
# ProxyScrape public-proxy API (no auth). The display_proxies JSON endpoint
# returns per-proxy metadata (country, uptime, anonymity) we use to rank.
PROXY_API_URL = "https://api.proxyscrape.com/v4/free-proxy-list/get"
# A small, public, short video used to vet proxies (real extract + download)
# and to probe whether the direct IP has recovered.
PROXY_VET_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
# Where the vetted pool is persisted so a restart does not immediately re-vet.
PROXY_POOL_PATH = DATA_DIR / "proxy_pool.json"
# A persisted pool older than this is considered stale and re-vetted.
PROXY_POOL_MAX_AGE = 24 * 3600
# Parallelism + timeouts for the two-phase vetting sweep.
PROXY_VET_WORKERS_QUICK = 30
PROXY_VET_WORKERS_THOROUGH = 10
PROXY_VET_THOROUGH_CAP = 60
PROXY_QUICK_TIMEOUT = 6
PROXY_THOROUGH_TIMEOUT = 25
# Minimum seconds between vetting sweeps, so an exhausted pool does not trigger
# a full re-sweep on every failed download.
PROXY_VET_COOLDOWN = 300

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
        # Anti-detection defaults (see dev/PLANNING-antidetect.md).
        "user_agent": DEFAULT_USER_AGENT,
        "download_delay_min": "5",
        "download_delay_max": "20",
        "rate_limit_action": "abort",
        "rate_limit_backoff_seconds": "600",
        "tls_impersonate": "chrome",
        # Phase 4: SOCKS5 proxy rotation (see dev/PLANNING-antidetect.md).
        # Off by default; the pool is built reactively only when the local IP
        # is rate-limited. An empty country list means "all countries".
        "proxy_enabled": "0",
        "proxy_countries": "",
        "proxy_timeout_ms": "3000",
        "proxy_vet_limit": "300",
        "proxy_recovery_probe_every": "10",
        "proxy_max_rotations": "5",
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


# ---------------------------------------------------------------------------
# Error email (SMTP): report failures to the user by email. The app is meant to
# run unattended, so a persistent failure should reach the user even when they
# are not watching the logs. STARTTLS is supported (Gmail, port 587).
# ---------------------------------------------------------------------------

SMTP_ENCRYPTION = ("starttls", "ssl", "none")
# A persistent failure (e.g. a channel that keeps erroring) must not email the
# user every poll cycle. Identical errors are throttled to at most one email
# per window.
EMAIL_COOLDOWN_SECONDS = 6 * 3600
email_cooldown = {}
email_cooldown_lock = threading.Lock()


def send_email(subject, body):
    # Send a plain-text email via the configured SMTP server. Returns
    # (ok, message) and never raises — a mail failure must not break the app.
    host = (get_setting("smtp_host") or "").strip()
    if not host:
        return False, "SMTP host is not configured"
    try:
        port = int(get_setting("smtp_port", "587") or 587)
    except (TypeError, ValueError):
        port = 587
    encryption = (get_setting("smtp_encryption", "starttls") or "starttls").lower()
    username = (get_setting("smtp_username") or "").strip()
    password = get_setting("smtp_password") or ""
    sender = (get_setting("smtp_from") or "").strip() or username
    recipient = (get_setting("smtp_to") or "").strip() or username
    if not recipient:
        return False, "No recipient — set the To address (or the username)"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg["Message-ID"] = make_msgid()

    try:
        if encryption == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            if encryption == "starttls":
                server.starttls()
        with server:
            if username:
                server.login(username, password)
            server.sendmail(sender, [recipient], msg.as_string())
        return True, "sent"
    except Exception as exc:
        return False, str(exc)


def report_error(context, exc, force=False):
    # Log the error (with traceback) and, if SMTP is configured, email it to the
    # user. Repeated identical errors are throttled so a persistent failure does
    # not spam the inbox every poll cycle. `force` bypasses the throttle (used
    # for user-initiated actions such as an on-demand download).
    tb = traceback.format_exc()
    log.error("%s: %s%s", context, exc, ("\n" + tb) if tb else "")
    if not (get_setting("smtp_host") or "").strip():
        return
    key = f"{context}|{exc}"
    now = time.time()
    if not force:
        with email_cooldown_lock:
            last = email_cooldown.get(key)
            if last is not None and now - last < EMAIL_COOLDOWN_SECONDS:
                return
            email_cooldown[key] = now
    body = (
        f"{context}\n\n"
        f"{exc}\n\n"
        f"{tb}\n\n"
        f"---\n"
        f"Time: {now_iso()}\n"
        f"Host: {socket.gethostname()}\n"
        f"Version: {APP_VERSION}\n"
    )
    ok, msg = send_email(f"[video-archiver] {context}", body)
    if ok:
        log.info("Error email sent for: %s", context)
    else:
        log.warning("Failed to send error email for %s: %s", context, msg)


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


def dir_size(path):
    # Total size in bytes of all regular files under `path` (recursive).
    p = Path(path)
    if not p.exists():
        return 0
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


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
    try:
        smtp_port = int(get_setting("smtp_port", "587") or 587)
    except (TypeError, ValueError):
        smtp_port = 587
    try:
        download_delay_min = int(get_setting("download_delay_min", "5"))
    except (TypeError, ValueError):
        download_delay_min = 5
    try:
        download_delay_max = int(get_setting("download_delay_max", "20"))
    except (TypeError, ValueError):
        download_delay_max = 20
    try:
        rate_limit_backoff_seconds = int(get_setting("rate_limit_backoff_seconds", "600"))
    except (TypeError, ValueError):
        rate_limit_backoff_seconds = 600
    try:
        proxy_timeout_ms = int(get_setting("proxy_timeout_ms", "3000"))
    except (TypeError, ValueError):
        proxy_timeout_ms = 3000
    try:
        proxy_vet_limit = int(get_setting("proxy_vet_limit", "300"))
    except (TypeError, ValueError):
        proxy_vet_limit = 300
    try:
        proxy_recovery_probe_every = int(get_setting("proxy_recovery_probe_every", "10"))
    except (TypeError, ValueError):
        proxy_recovery_probe_every = 10
    try:
        proxy_max_rotations = int(get_setting("proxy_max_rotations", "5"))
    except (TypeError, ValueError):
        proxy_max_rotations = 5
    return {
        "poll_minutes": poll_minutes,
        "log_level": get_setting("log_level", "INFO"),
        "paused": is_paused(),
        "smtp_host": get_setting("smtp_host", ""),
        "smtp_port": smtp_port,
        "smtp_encryption": get_setting("smtp_encryption", "starttls"),
        "smtp_username": get_setting("smtp_username", ""),
        "smtp_password": get_setting("smtp_password", ""),
        "smtp_from": get_setting("smtp_from", ""),
        "smtp_to": get_setting("smtp_to", ""),
        # Anti-detection (see dev/PLANNING-antidetect.md).
        "user_agent": get_setting("user_agent", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT,
        "download_delay_min": download_delay_min,
        "download_delay_max": download_delay_max,
        "rate_limit_action": get_setting("rate_limit_action", "abort") or "abort",
        "rate_limit_backoff_seconds": rate_limit_backoff_seconds,
        "tls_impersonate": get_setting("tls_impersonate", "chrome") or "chrome",
        # Phase 4: SOCKS5 proxy rotation.
        "proxy_enabled": get_setting("proxy_enabled", "0") == "1",
        "proxy_countries": get_setting("proxy_countries", "") or "",
        "proxy_timeout_ms": proxy_timeout_ms,
        "proxy_vet_limit": proxy_vet_limit,
        "proxy_recovery_probe_every": proxy_recovery_probe_every,
        "proxy_max_rotations": proxy_max_rotations,
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


def safe_filename(value, max_bytes=180):
    # Sanitize a string for use as a single filesystem path component.
    # Truncation is by UTF-8 *bytes* (not characters) so a non-ASCII title can
    # never push a component past the 255-byte per-name limit (APFS/ext4).
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r'[<>:"|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    value = re.sub(r"[. ]+$", "", value)
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes:
        value = encoded[:max_bytes].decode("utf-8", "ignore").rstrip(". ")
    return value or "untitled"


def title_budget(channel_dir, upload_date, video_id):
    # How many UTF-8 bytes the title may use so that (a) the filename
    # {upload_date}-{title}-{video_id}.mp4 stays within the 255-byte per-name
    # limit and (b) the full path stays within PATH_MAX (4096 bytes).
    ud = len(upload_date.encode("utf-8"))
    vid = len(video_id.encode("utf-8"))
    dirb = len(str(channel_dir).encode("utf-8"))
    by_name = 255 - ud - vid - 6          # ud + 1 + title + 1 + vid + 4
    by_path = 4096 - dirb - ud - vid - 7  # dir + 1 + ud + 1 + title + 1 + vid + 4
    return max(1, min(180, by_name, by_path))


def output_template():
    # yt-dlp performs the final extension substitution after merging/remuxing.
    # Files live in a per-channel subfolder; the channel name is not part of
    # the file name itself.
    return str(VIDEO_DIR / "%(uploader)s" / "%(upload_date)s-%(title)s.%(ext)s")


class EgressManager:
    """Chooses the outbound IP used for downloads.

    DIRECT (default): connect directly.
    PROXY: route through a vetted public-SOCKS5 proxy, rotating when a proxy
    stops working. The pool is built *reactively* — only when YouTube
    rate-limits the direct IP — by a two-phase vetting sweep (a quick TLS
    connect, then a real extract+download). We probe the direct connection
    periodically and drop back to DIRECT once the local IP is no longer
    throttled. See dev/PLANNING-antidetect.md for the design and spike results.
    """

    DIRECT = "direct"
    PROXY = "proxy"

    def __init__(self):
        self._lock = threading.Lock()
        self._state = self.DIRECT
        self._pool = []            # [{"ip","port","country","failures"}]
        self._index = 0
        self._consecutive_rotations = 0
        self._downloads_since_probe = 0
        self._last_vet_at = 0.0

    # -- egress decision ---------------------------------------------------
    def current_proxy(self):
        # None => connect directly. A "socks5h://ip:port" URL routes through the
        # active proxy (socks5h = remote DNS, for clean IP masking).
        with self._lock:
            if self._state != self.PROXY or not self._pool:
                return None
            entry = self._pool[self._index % len(self._pool)]
            return f"socks5h://{entry['ip']}:{entry['port']}"

    def on_rate_limited(self):
        # React to a YouTube rate-limit. Returns "proxy" if a proxy is now
        # active (the caller should retry the operation), else "none" (the
        # caller applies the abort/backoff policy).
        if not self._proxy_enabled():
            return "none"
        with self._lock:
            if self._state == self.DIRECT:
                # Reactive trigger: reuse a recent persisted pool if we have
                # one, otherwise build a fresh one.
                pool = self._load_pool()
                if not pool:
                    pool = self._vet_sweep()
                    if pool:
                        self._save_pool()
                if not pool:
                    return "none"
                self._pool = pool
                self._index = 0
                self._state = self.PROXY
                self._consecutive_rotations = 0
                return "proxy"
            # PROXY: rotate to the next proxy; re-vet once every proxy in the
            # pool has been tried.
            if not self._pool:
                return "none"
            self._rotate()
            self._consecutive_rotations += 1
            if self._consecutive_rotations >= len(self._pool):
                pool = self._vet_sweep()
                if not pool:
                    return "none"
                self._pool = pool
                self._index = 0
                self._consecutive_rotations = 0
                self._save_pool()
            return "proxy"

    def on_proxy_failed(self):
        # A proxy error (not a rate-limit): mark the current proxy failed and
        # rotate to the next one.
        with self._lock:
            if self._state != self.PROXY or not self._pool:
                return
            entry = self._pool[self._index % len(self._pool)]
            entry["failures"] = entry.get("failures", 0) + 1
            self._rotate()

    def on_success(self):
        # Record a success. Periodically probe the direct connection; if the
        # local IP is no longer throttled, drop back to DIRECT egress.
        probe = False
        with self._lock:
            self._consecutive_rotations = 0
            self._downloads_since_probe += 1
            if self._state == self.PROXY and self._downloads_since_probe >= self._probe_every():
                self._downloads_since_probe = 0
                probe = True
        if probe and self._probe_direct():
            with self._lock:
                self._state = self.DIRECT
                self._pool = []
                self._index = 0
                self._consecutive_rotations = 0
                self._downloads_since_probe = 0
            self._clear_pool()
            log.info("Direct egress recovered; leaving proxy mode")

    def reset(self):
        # Force direct egress and clear the pool (user-initiated).
        with self._lock:
            self._state = self.DIRECT
            self._pool = []
            self._index = 0
            self._consecutive_rotations = 0
            self._downloads_since_probe = 0
        self._clear_pool()
        log.info("Egress reset to direct")

    def status(self):
        with self._lock:
            current = None
            if self._state == self.PROXY and self._pool:
                e = self._pool[self._index % len(self._pool)]
                current = f"{e['ip']}:{e['port']}"
            return {
                "enabled": self._proxy_enabled(),
                "state": self._state,
                "pool_size": len(self._pool),
                "current_proxy": current,
                "consecutive_rotations": self._consecutive_rotations,
                "last_vet_at": self._last_vet_at or None,
            }

    # -- internals ---------------------------------------------------------
    def _rotate(self):
        if self._pool:
            self._index = (self._index + 1) % len(self._pool)

    def _proxy_enabled(self):
        return get_setting("proxy_enabled", "0") == "1"

    def _probe_every(self):
        try:
            return max(1, int(get_setting("proxy_recovery_probe_every", "10")))
        except (TypeError, ValueError):
            return 10

    def _fetch_proxies(self):
        # Fetch candidate SOCKS5 proxies from ProxyScrape (display_proxies
        # JSON). Returns a list of {"ip","port","country","failures"} dicts,
        # deduped by ip:port. An empty country setting means "all countries".
        countries = (get_setting("proxy_countries", "") or "").strip()
        try:
            timeout_ms = int(get_setting("proxy_timeout_ms", "3000"))
        except (TypeError, ValueError):
            timeout_ms = 3000
        try:
            limit = int(get_setting("proxy_vet_limit", "300"))
        except (TypeError, ValueError):
            limit = 300
        params = {
            "request": "display_proxies",
            "proxy_format": "ipport",
            "format": "json",
            "protocol": "socks5",
            "timeout": str(timeout_ms),
            "limit": str(limit),
        }
        if countries:
            params["country"] = countries
        url = PROXY_API_URL + "?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.warning("Proxy fetch failed: %s", exc)
            return []
        seen = set()
        out = []
        for p in data.get("proxies", []):
            ip = p.get("ip")
            port = p.get("port")
            if not ip or port is None:
                continue
            key = f"{ip}:{port}"
            if key in seen:
                continue
            try:
                port = int(port)
            except (TypeError, ValueError):
                continue
            seen.add(key)
            country = (p.get("ip_data") or {}).get("countryCode", "")
            out.append({"ip": ip, "port": port, "country": country, "failures": 0})
        return out

    def _vet_sweep(self):
        # Two-phase parallel vetting. Returns a list of viable proxy entries,
        # or [] if none. Respects a cooldown so an exhausted pool does not
        # trigger a full re-sweep on every failed download.
        now = time.time()
        if now - self._last_vet_at < PROXY_VET_COOLDOWN:
            return []
        self._last_vet_at = now
        proxies = self._fetch_proxies()
        if not proxies:
            log.warning("Proxy vetting: no proxies fetched")
            return []
        log.info("Proxy vetting: %d candidates", len(proxies))
        passers = self._quick_connect(proxies)
        log.info("Proxy vetting: %d quick-connect passers", len(passers))
        if not passers:
            return []
        viable = self._thorough_vet(passers)
        log.info("Proxy vetting: %d viable proxies", len(viable))
        return viable

    def _quick_connect(self, proxies):
        # Phase A: a parallel TLS connect to youtube.com via each proxy. Any
        # HTTP response (even 403/429) means the proxy can reach YouTube; only
        # connection/proxy errors are failures.
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError:
            log.warning("curl_cffi unavailable; cannot vet proxies")
            return []

        def check(p):
            url = f"socks5h://{p['ip']}:{p['port']}"
            try:
                cffi_requests.get(
                    "https://www.youtube.com",
                    proxies={"http": url, "https": url},
                    timeout=PROXY_QUICK_TIMEOUT,
                    impersonate="chrome",
                )
                return p
            except Exception:
                return None

        passers = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=PROXY_VET_WORKERS_QUICK) as ex:
            for p in ex.map(check, proxies):
                if p is not None:
                    passers.append(p)
        return passers

    def _thorough_vet(self, passers):
        # Phase B: a real extract+download of a small video via each passer.
        # Only proxies that complete the download are viable.
        candidates = passers[:PROXY_VET_THOROUGH_CAP]
        vet_dir = DATA_DIR / "tmp" / "proxy-vet"
        vet_dir.mkdir(parents=True, exist_ok=True)

        def vet(p):
            url = f"socks5h://{p['ip']}:{p['port']}"
            opts = {
                "quiet": True,
                "no_warnings": True,
                "impersonate": ImpersonateTarget.from_str("chrome"),
                "proxy": url,
                "socket_timeout": PROXY_THOROUGH_TIMEOUT,
                "format": "bv*[height<=360]+ba/b[height<=360]",
                "merge_output_format": "mp4",
                "outtmpl": str(vet_dir / "%(title)s.%(ext)s"),
                "paths": {"temp": str(vet_dir)},
            }
            try:
                with YoutubeDL(opts) as ydl:
                    ydl.extract_info(PROXY_VET_VIDEO, download=True)
                return p
            except Exception:
                return None

        viable = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=PROXY_VET_WORKERS_THOROUGH) as ex:
            for p in ex.map(vet, candidates):
                if p is not None:
                    viable.append(p)
        shutil.rmtree(vet_dir, ignore_errors=True)
        return viable

    def _probe_direct(self):
        # A lightweight direct extraction (no proxy). Returns True if the
        # direct IP is no longer rate-limited (i.e., we have recovered).
        opts = {
            "quiet": True,
            "no_warnings": True,
            "impersonate": ImpersonateTarget.from_str("chrome"),
            "socket_timeout": 15,
        }
        try:
            with YoutubeDL(opts) as ydl:
                ydl.extract_info(PROXY_VET_VIDEO, download=False)
            return True
        except Exception as exc:
            # Only a definite rate-limit means "still throttled"; any other
            # error is inconclusive, so stay in proxy mode to be safe.
            return not is_rate_limited_error(exc)

    def _save_pool(self):
        try:
            PROXY_POOL_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {"verified_at": time.time(), "proxies": self._pool}
            PROXY_POOL_PATH.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            log.warning("Failed to save proxy pool: %s", exc)

    def _load_pool(self):
        # Load a persisted pool if it is recent enough. Returns a list of proxy
        # entries, or [] if absent/stale/empty.
        try:
            payload = json.loads(PROXY_POOL_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        verified_at = payload.get("verified_at", 0)
        if time.time() - verified_at > PROXY_POOL_MAX_AGE:
            return []
        proxies = payload.get("proxies", [])
        if not proxies:
            return []
        log.info("Loaded %d persisted proxies (verified %ds ago)",
                 len(proxies), int(time.time() - verified_at))
        return proxies

    def _clear_pool(self):
        try:
            PROXY_POOL_PATH.unlink(missing_ok=True)
        except OSError:
            pass


egress_manager = EgressManager()


def _impersonate_target():
    # TLS/browser impersonation via curl_cffi. "off" (or empty) disables it. A
    # short client name ("chrome", "safari", "firefox") resolves to the newest
    # matching curl_cffi target. Read per call so a settings change applies
    # without a restart. Returns an ImpersonateTarget object (the type the
    # Python API requires) or None when disabled.
    target = (get_setting("tls_impersonate", "chrome") or "").strip().lower()
    if target in ("", "off"):
        return None
    return ImpersonateTarget.from_str(target)


def ydl_opts(download=False, outtmpl=None):
    # Apple-TV-friendly selection: H.264/AVC (avc1) only, never exceeding 1080p.
    # HEVC (hvc1) and VP9/AV1 are deliberately excluded — older Apple TV models
    # do not decode VP9, and the goal is broad compatibility, not max quality.
    # The height constraint is applied to the source video stream, so yt-dlp
    # never downloads 4K/1440p merely to discard or transcode it. Separate
    # video/audio streams are muxed into MP4 by FFmpeg without re-encoding.
    fmt = (
        "(bv*[height<=1080][ext=mp4][vcodec^=avc1]+ba[ext=m4a][acodec^=mp4a])"
        "/(b[height<=1080][ext=mp4][vcodec^=avc1][acodec^=mp4a])"
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
        # Remux (never re-encode) into MP4. -movflags +faststart moves the moov
        # atom to the front of the file so Apple TV can begin playback without
        # first downloading the whole file. No codec args => streams are copied.
        # Extra FFmpeg args are passed via the top-level "postprocessor_args"
        # dict (keyed by "<pp_key>+<exe>"), not per-postprocessor: yt-dlp
        # 2026.8.19 removed the "additional_args" init parameter.
        "postprocessors": [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
        ],
        "postprocessor_args": {
            "videoremuxer+ffmpeg": ["-movflags", "+faststart"]
        },
        # Phase 3: TLS/browser impersonation via curl_cffi. None disables it.
        "impersonate": _impersonate_target(),
    }
    # Phase 4: route through a proxy when the egress manager has one active.
    # (No-op until Phase 4 lands; current_proxy() returns None today.)
    proxy = egress_manager.current_proxy()
    if proxy:
        opts["proxy"] = proxy
    # Phase 1: when TLS impersonation is OFF, pin a consistent, common
    # User-Agent so our requests look like one normal browser rather than
    # yt-dlp's per-process random Chrome. When impersonation is ON we must NOT
    # set a UA: curl_cffi sends the impersonated browser's own UA (matching the
    # TLS fingerprint), and overriding it would create a UA/TLS mismatch —
    # itself a bot signal. (The "user_agent" yt-dlp param is ignored by the
    # Python API; only http_headers overrides the header.)
    if opts["impersonate"] is None:
        opts["http_headers"] = {
            "User-Agent": get_setting("user_agent", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT,
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
    upload_date = info.get("upload_date") or "unknown-date"
    # Videos live in a per-channel subfolder; the channel name is not part of
    # the file name itself. The video ID is part of the name so two videos can
    # never collide.
    channel_dir = VIDEO_DIR / uploader
    channel_dir.mkdir(parents=True, exist_ok=True)
    # Truncate the title to a byte budget so the filename and full path stay
    # within filesystem limits even for very long / non-ASCII titles.
    title = safe_filename(
        info.get("title") or video_id,
        max_bytes=title_budget(channel_dir, upload_date, video_id),
    )
    final_name = f"{upload_date}-{title}-{video_id}.mp4"
    final_path = channel_dir / final_name

    if final_path.exists():
        record_video(channel_url, info, final_path)
        return final_path

    tmp_template = str(channel_dir / f".{upload_date}-{title}-{video_id}.%(ext)s")
    opts = ydl_opts(download=True, outtmpl=tmp_template)
    opts["extract_flat"] = False
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


class RateLimitedError(Exception):
    # Raised when YouTube rate-limits or blocks our requests (HTTP 429,
    # "sign in to confirm your account", etc.). Distinct from a generic
    # download failure so the sync loop can react (backoff/abort) instead of
    # logging a full traceback and moving on.
    pass


def is_rate_limited_error(exc):
    # YouTube signals rate-limiting / bot-detection with specific messages.
    # Match on stable phrases (not a bare "429", which can appear in unrelated
    # contexts) so we can react instead of treating it as an ordinary error.
    msg = str(exc).lower()
    return any(
        phrase in msg
        for phrase in (
            "too many requests",
            "http error 429",
            "sign in to confirm",
            "request is blocked",
            "rate limit",
        )
    )


def is_proxy_error(exc):
    # A proxy-routed request can fail for reasons unrelated to YouTube: the
    # SOCKS tunnel drops, the proxy can't reach the CDN, or curl_cffi reports a
    # transport error. Match on stable phrases so a bad proxy triggers a
    # rotation (on_proxy_failed) rather than being logged as an ordinary
    # download failure.
    msg = str(exc).lower()
    return any(
        phrase in msg
        for phrase in (
            "proxy",
            "socks",
            "tunnel connection failed",
            "over proxy",
            "curl: (7)",
            "curl: (97)",
            "curl: (35)",
        )
    )


def _proxy_max_rotations():
    # Per-operation retry cap so a single listing fetch or download can never
    # loop forever if every proxy in the pool keeps failing.
    try:
        return max(0, int(get_setting("proxy_max_rotations", "5")))
    except (TypeError, ValueError):
        return 5


def _notify_rate_limited(context, channel_url, exc):
    # Send a distinct email when we get rate-limited, throttled so a persistent
    # block does not spam the inbox. Reuses the SMTP error-email path and its
    # cooldown store (keyed separately from ordinary errors).
    if not (get_setting("smtp_host") or "").strip():
        return
    key = f"rate-limited|{context}"
    now = time.time()
    with email_cooldown_lock:
        last = email_cooldown.get(key)
        if last is not None and now - last < EMAIL_COOLDOWN_SECONDS:
            return
        email_cooldown[key] = now
    body = (
        "YouTube rate-limited or blocked the archiver.\n\n"
        f"Context: {context}\n"
        f"Channel: {channel_url}\n"
        f"Detail: {exc}\n\n"
        f"---\n"
        f"Time: {now_iso()}\n"
        f"Host: {socket.gethostname()}\n"
        f"Version: {APP_VERSION}\n"
    )
    ok, msg = send_email("[video-archiver] Rate limited / blocked", body)
    if ok:
        log.info("Rate-limit email sent")
    else:
        log.warning("Failed to send rate-limit email: %s", msg)


def _handle_rate_limit(context, channel_url, exc, consecutive=1):
    # React to a YouTube rate-limit / bot-detection signal. Returns:
    #   "retry"  -> the egress manager rotated to a fresh proxy; retry the same
    #               operation through it.
    #   None     -> a backoff was applied; the caller continues.
    # and raises RateLimitedError to abort the sync cycle (abort mode, or
    # backoff mode after N consecutive limits).
    action = (get_setting("rate_limit_action", "abort") or "abort").lower()
    log.warning(
        "Rate limited / blocked by YouTube during %s for %s: %s",
        context, channel_url, exc,
    )
    _notify_rate_limited(context, channel_url, exc)
    # Phase 4: let the egress manager rotate to a fresh IP if it is active.
    if egress_manager.on_rate_limited() == "proxy":
        log.info("Egress rotated to a proxy; retrying %s", context)
        return "retry"
    if action == "backoff":
        try:
            backoff = max(0, int(get_setting("rate_limit_backoff_seconds", "600")))
        except (TypeError, ValueError):
            backoff = 600
        if consecutive >= RATE_LIMIT_MAX_CONSECUTIVE:
            log.error(
                "Rate limited %d times in a row; aborting sync cycle", consecutive
            )
            raise RateLimitedError(f"Rate limited {consecutive} consecutive times")
        log.info("Backing off %ds before continuing", backoff)
        time.sleep(backoff)
    else:
        raise RateLimitedError(f"Rate limited during {context}")
    return None


def _human_delay():
    # Sleep a random interval between the configured min and max to mimic the
    # irregular cadence of a human, rather than a tight bot loop. Read per call
    # so a settings change applies without a restart.
    try:
        lo = max(0, int(get_setting("download_delay_min", "5")))
        hi = max(0, int(get_setting("download_delay_max", "20")))
    except (TypeError, ValueError):
        lo, hi = 5, 20
    if hi < lo:
        lo, hi = hi, lo
    delay = random.uniform(lo, hi)
    if delay > 0:
        log.debug("Human-like delay: sleeping %.1fs", delay)
        time.sleep(delay)


def sync_channel(channel_url, latest_n):
    log.info("Checking %s (latest_n=%d)", channel_url, latest_n)
    max_rotations = _proxy_max_rotations()
    # Fetch the listing, retrying through a rotated proxy when the current
    # egress is rate-limited or a proxy fails. Bounded so a bad pool can never
    # loop forever.
    rotations = 0
    while True:
        try:
            entries = list_channel(channel_url, latest_n)
            break
        except Exception as exc:
            if is_rate_limited_error(exc):
                # A rate-limit while fetching the listing is a block signal,
                # not a transient per-video error: react (rotate/backoff/abort).
                if _handle_rate_limit("listing", channel_url, exc) == "retry" \
                        and rotations < max_rotations:
                    rotations += 1
                    continue
                raise
            if is_proxy_error(exc):
                # A bad proxy: rotate and retry through the next one.
                egress_manager.on_proxy_failed()
                if egress_manager.current_proxy() and rotations < max_rotations:
                    rotations += 1
                    continue
                raise
            raise
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
    consecutive_rate_limits = 0
    for entry in reversed(entries):
        if existing(channel_url, entry["id"]):
            continue
        # Per-video rotation budget: retry this video through a rotated proxy
        # when the current egress is rate-limited or a proxy fails.
        rotations = 0
        while True:
            try:
                path = download_video(channel_url, entry["id"])
                log.info("Downloaded: %s", path.name)
                consecutive_rate_limits = 0
                # Phase 4: record the success (drives the recovery probe).
                egress_manager.on_success()
                # Phase 1: human-like pause between downloads.
                _human_delay()
                break
            except Exception as exc:
                if is_rate_limited_error(exc):
                    # Check rate-limiting before the restricted check: a block
                    # signal must trigger rotation/backoff/abort, not a generic
                    # error log.
                    consecutive_rate_limits += 1
                    result = _handle_rate_limit(
                        "download", channel_url, exc,
                        consecutive=consecutive_rate_limits,
                    )
                    if result == "retry" and rotations < max_rotations:
                        rotations += 1
                        continue
                    # Backoff applied (move on) or abort (RateLimitedError
                    # already raised inside _handle_rate_limit).
                    break
                if is_proxy_error(exc):
                    # A bad proxy: rotate and retry this video through the next
                    # one; if no proxy is left, treat it as an ordinary failure.
                    egress_manager.on_proxy_failed()
                    if egress_manager.current_proxy() and rotations < max_rotations:
                        rotations += 1
                        continue
                    report_error(
                        f"Failed downloading {entry['id']} ({entry['title']})", exc
                    )
                    break
                if is_restricted_error(exc):
                    # Auth-gated video: expected and permanent, so skip it with
                    # a clean warning instead of a full traceback.
                    log.warning(
                        "Skipping %s (%s): not publicly available (members-only or premium-only)",
                        entry["id"], entry["title"],
                    )
                    break
                report_error(f"Failed downloading {entry['id']} ({entry['title']})", exc)
                break
    enforce_retention(channel_url, latest_n)


# ---------------------------------------------------------------------------
# Keep yt-dlp current. The project tracks yt-dlp's nightly (pre-release)
# builds, which change daily, so the build baked into the image goes stale
# quickly. At the start of each sync cycle we ask pip for the newest --pre
# release and reload the yt_dlp modules in this process.
#
# Two things make this non-trivial:
#   * The container runs as an unprivileged user while site-packages is
#     root-owned, so the upgrade must go to the *user* site (--user), which
#     shadows the system install on sys.path.
#   * A running interpreter caches imported modules in sys.modules, so
#     `pip install --upgrade` alone would not take effect until a restart; we
#     therefore drop the cached yt_dlp modules and re-import, then rebind the
#     two names the app uses (YoutubeDL, ImpersonateTarget) — those are
#     resolved as module globals at call time, so every call site picks up the
#     new classes.
# ---------------------------------------------------------------------------
YTDLP_PACKAGE = "yt-dlp[default,curl-cffi]"
PIP_UPGRADE_TIMEOUT = 180  # seconds; a slow network must not stall a cycle


def _pip_upgrade_ytdlp():
    # Run `pip install --user --upgrade --pre yt-dlp[...]` in a subprocess.
    # Returns True on success. Never raises — a pip failure (e.g. no network)
    # must not abort the sync; we simply keep using the currently installed
    # build.
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--user", "--upgrade", "--pre",
        "--no-cache-dir", "--disable-pip-version-check",
        YTDLP_PACKAGE,
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=PIP_UPGRADE_TIMEOUT,
            text=True,
        )
    except Exception as exc:
        log.warning("yt-dlp upgrade did not run: %s", exc)
        return False
    if proc.returncode != 0:
        log.warning("yt-dlp upgrade failed (exit %d):\n%s",
                    proc.returncode, (proc.stdout or "")[-2000:])
        return False
    log.debug("yt-dlp upgrade output:\n%s", (proc.stdout or "")[-1000:])
    return True


def _reload_ytdlp():
    # Re-import yt_dlp after an upgrade so this running process uses the new
    # build. Drop every cached yt_dlp module and re-import, then rebind the two
    # names the app uses. (curl_cffi is left alone: it is a separate, stable
    # dependency and its C extension should not be hot-reloaded.)
    global YoutubeDL, ImpersonateTarget
    for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
        del sys.modules[name]
    yt_dlp = importlib.import_module("yt_dlp")
    impersonate = importlib.import_module("yt_dlp.networking.impersonate")
    version_mod = importlib.import_module("yt_dlp.version")
    YoutubeDL = yt_dlp.YoutubeDL
    ImpersonateTarget = impersonate.ImpersonateTarget
    return version_mod.__version__, yt_dlp.__file__


def ensure_latest_ytdlp():
    # Upgrade to the newest nightly and reload it. Best-effort: any failure is
    # logged and the cycle proceeds with the currently installed build.
    current = "unknown"
    try:
        current = importlib.import_module("yt_dlp.version").__version__
    except Exception:
        pass
    if not _pip_upgrade_ytdlp():
        return
    try:
        version, path = _reload_ytdlp()
    except Exception as exc:
        log.warning("Failed to reload yt-dlp after upgrade: %s", exc)
        return
    if version != current:
        log.info("yt-dlp updated: %s -> %s (loaded from %s)", current, version, path)
    else:
        log.debug("yt-dlp already current (%s, %s)", version, path)


def _sync_channels():
    # Shared body of a sync cycle: process every channel with N>0.
    # First make sure we are on the newest yt-dlp nightly (see above). This
    # runs under the sync lock, so no other cycle can use yt_dlp mid-reload.
    ensure_latest_ytdlp()
    for ch in load_channels():
        if ch["latest_n"] <= 0:
            # C8: channels with N=0 are skipped during the scheduled poll.
            continue
        try:
            sync_channel(ch["url"], ch["latest_n"])
        except RateLimitedError as exc:
            # A rate-limit abort stops the whole cycle: continuing to hammer
            # other channels from the same IP would only extend the block.
            log.warning("Stopping sync cycle due to rate limiting: %s", exc)
            break
        except Exception as exc:
            report_error(f"Channel sync failed: {ch['url']}", exc)


def sync_all():
    if is_paused():
        log.info("Archiver is paused; skipping sync")
        return
    if not lock.acquire(blocking=False):
        log.info("Sync already running; skipping overlapping cycle")
        return
    try:
        _sync_channels()
    finally:
        lock.release()


def sync_all_blocking():
    # Manual "refresh now": wait for the lock (instead of skipping) so the
    # requested cycle always runs to completion, even if the worker is busy.
    if is_paused():
        return
    with lock:
        _sync_channels()


def worker():
    while True:
        paused = is_paused()
        apply_log_level()
        started = time.monotonic()
        try:
            sync_all()
        except Exception as exc:
            report_error("Sync cycle failed", exc)
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

# In-flight state for the manual "refresh now" action (lost on restart, which is
# fine — it only reflects the in-flight manual sync, not persisted state).
refresh_state = {"running": False, "last": None, "error": None}
refresh_state_lock = threading.Lock()


def trigger_refresh():
    # Kick off an immediate sync cycle in the background. Returns False if a
    # manual refresh is already in flight. sync_all() itself skips (non-blocking
    # lock) if the scheduled worker is mid-sync, so cycles never overlap.
    with refresh_state_lock:
        if refresh_state["running"]:
            return False
        refresh_state["running"] = True
        refresh_state["error"] = None

    def run():
        try:
            sync_all_blocking()
        except Exception as exc:
            log.exception("Manual refresh failed")
            with refresh_state_lock:
                refresh_state["error"] = str(exc)
        finally:
            with refresh_state_lock:
                refresh_state["running"] = False
                refresh_state["last"] = now_iso()

    threading.Thread(target=run, daemon=True, name="manual-refresh").start()
    return True


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
        report_error(f"On-demand download failed: {url_or_id}", exc, force=True)
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


@app.get("/api/disk")
@require_auth
def api_disk():
    # Disk usage of the data volume, broken out so the UI can show how much
    # space active videos vs. the orphaned folder consume, plus free space.
    try:
        fs = shutil.disk_usage(DATA_DIR)
        fs_total, fs_used, fs_free = fs.total, fs.used, fs.free
    except OSError:
        fs_total = fs_used = fs_free = None
    videos_total = dir_size(VIDEO_DIR)
    orphaned = dir_size(VIDEO_DIR / "orphaned")
    return jsonify({
        "data_total": dir_size(DATA_DIR),
        "videos_total": videos_total,
        "videos_active": max(0, videos_total - orphaned),
        "orphaned": orphaned,
        "fs_total": fs_total,
        "fs_used": fs_used,
        "fs_free": fs_free,
    })


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
    # SMTP / error-email settings.
    if "smtp_host" in data:
        set_setting("smtp_host", str(data["smtp_host"]).strip())
    if "smtp_port" in data:
        try:
            port = int(data["smtp_port"])
        except (TypeError, ValueError):
            port = None
            errors["smtp_port"] = "must be an integer"
        if port is not None and not 1 <= port <= 65535:
            port = None
            errors["smtp_port"] = "must be between 1 and 65535"
        if port is not None:
            set_setting("smtp_port", port)
    if "smtp_encryption" in data:
        enc = str(data["smtp_encryption"]).lower()
        if enc not in SMTP_ENCRYPTION:
            errors["smtp_encryption"] = "must be one of: " + ", ".join(SMTP_ENCRYPTION)
        else:
            set_setting("smtp_encryption", enc)
    if "smtp_username" in data:
        set_setting("smtp_username", str(data["smtp_username"]).strip())
    if "smtp_password" in data:
        set_setting("smtp_password", str(data["smtp_password"]))
    if "smtp_from" in data:
        set_setting("smtp_from", str(data["smtp_from"]).strip())
    if "smtp_to" in data:
        set_setting("smtp_to", str(data["smtp_to"]).strip())
    # Anti-detection settings (see dev/PLANNING-antidetect.md).
    if "user_agent" in data:
        ua = str(data["user_agent"]).strip()
        if not ua:
            errors["user_agent"] = "cannot be empty"
        else:
            set_setting("user_agent", ua)
    if "download_delay_min" in data:
        try:
            dmin = int(data["download_delay_min"])
        except (TypeError, ValueError):
            dmin = None
            errors["download_delay_min"] = "must be an integer"
        if dmin is not None and dmin < 0:
            dmin = None
            errors["download_delay_min"] = "must be >= 0"
        if dmin is not None:
            set_setting("download_delay_min", dmin)
    if "download_delay_max" in data:
        try:
            dmax = int(data["download_delay_max"])
        except (TypeError, ValueError):
            dmax = None
            errors["download_delay_max"] = "must be an integer"
        if dmax is not None and dmax < 0:
            dmax = None
            errors["download_delay_max"] = "must be >= 0"
        if dmax is not None:
            set_setting("download_delay_max", dmax)
    if "rate_limit_action" in data:
        action = str(data["rate_limit_action"]).lower()
        if action not in RATE_LIMIT_ACTIONS:
            errors["rate_limit_action"] = "must be one of: " + ", ".join(RATE_LIMIT_ACTIONS)
        else:
            set_setting("rate_limit_action", action)
    if "rate_limit_backoff_seconds" in data:
        try:
            backoff = int(data["rate_limit_backoff_seconds"])
        except (TypeError, ValueError):
            backoff = None
            errors["rate_limit_backoff_seconds"] = "must be an integer"
        if backoff is not None and backoff < 0:
            backoff = None
            errors["rate_limit_backoff_seconds"] = "must be >= 0"
        if backoff is not None:
            set_setting("rate_limit_backoff_seconds", backoff)
    if "tls_impersonate" in data:
        target = str(data["tls_impersonate"]).strip().lower()
        if target not in TLS_IMPERSONATE_CHOICES:
            errors["tls_impersonate"] = "must be one of: " + ", ".join(TLS_IMPERSONATE_CHOICES)
        else:
            set_setting("tls_impersonate", target)
    # Phase 4: SOCKS5 proxy rotation (see dev/PLANNING-antidetect.md).
    if "proxy_enabled" in data:
        set_setting("proxy_enabled", "1" if data["proxy_enabled"] else "0")
    if "proxy_countries" in data:
        # Comma-separated ISO country codes; empty means "all countries".
        set_setting("proxy_countries", str(data["proxy_countries"]).strip())
    if "proxy_timeout_ms" in data:
        try:
            pto = int(data["proxy_timeout_ms"])
        except (TypeError, ValueError):
            pto = None
            errors["proxy_timeout_ms"] = "must be an integer"
        if pto is not None and pto < 1:
            pto = None
            errors["proxy_timeout_ms"] = "must be >= 1"
        if pto is not None:
            set_setting("proxy_timeout_ms", pto)
    if "proxy_vet_limit" in data:
        try:
            pvl = int(data["proxy_vet_limit"])
        except (TypeError, ValueError):
            pvl = None
            errors["proxy_vet_limit"] = "must be an integer"
        if pvl is not None and pvl < 1:
            pvl = None
            errors["proxy_vet_limit"] = "must be >= 1"
        if pvl is not None:
            set_setting("proxy_vet_limit", pvl)
    if "proxy_recovery_probe_every" in data:
        try:
            pre = int(data["proxy_recovery_probe_every"])
        except (TypeError, ValueError):
            pre = None
            errors["proxy_recovery_probe_every"] = "must be an integer"
        if pre is not None and pre < 1:
            pre = None
            errors["proxy_recovery_probe_every"] = "must be >= 1"
        if pre is not None:
            set_setting("proxy_recovery_probe_every", pre)
    if "proxy_max_rotations" in data:
        try:
            pmr = int(data["proxy_max_rotations"])
        except (TypeError, ValueError):
            pmr = None
            errors["proxy_max_rotations"] = "must be an integer"
        if pmr is not None and pmr < 0:
            pmr = None
            errors["proxy_max_rotations"] = "must be >= 0"
        if pmr is not None:
            set_setting("proxy_max_rotations", pmr)
    if errors:
        return jsonify({"error": errors}), 400
    return jsonify(get_settings())


@app.post("/api/settings/test-email")
@require_auth
@require_csrf
def api_test_email():
    # Send a one-off test message so the user can verify their SMTP settings
    # before relying on them for error reporting.
    ok, msg = send_email(
        "[video-archiver] Test email",
        "This is a test email from your Online Video Archiver.\n"
        "If you received this, your SMTP settings are working and error "
        "reports will be delivered here.",
    )
    if ok:
        return jsonify({"ok": True, "message": "Test email sent — check your inbox."})
    return jsonify({"ok": False, "message": msg}), 400


@app.get("/api/proxy")
@require_auth
def api_proxy_status():
    # Live egress state: direct vs proxy, pool size, active proxy, and when the
    # pool was last vetted. Read-only; the UI polls this to render the status.
    return jsonify(egress_manager.status())


@app.post("/api/proxy/reset")
@require_auth
@require_csrf
def api_proxy_reset():
    # Force direct egress and clear the vetted pool (user-initiated).
    egress_manager.reset()
    return jsonify(egress_manager.status())


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


@app.post("/api/refresh")
@require_auth
@require_csrf
def api_refresh():
    # Manually trigger an immediate sync cycle (the "Refresh now" button).
    # Respects the pause flag — if paused, nothing downloads.
    if is_paused():
        return jsonify({"error": "Archiver is paused — turn on the running switch first."}), 409
    if not trigger_refresh():
        return jsonify({"status": "already_running"}), 202
    return jsonify({"status": "started"}), 202


@app.get("/api/refresh")
@require_auth
def api_refresh_status():
    with refresh_state_lock:
        return jsonify(dict(refresh_state))


if __name__ == "__main__":
    if not WEB_PASSWORD:
        log.critical(
            "WEB_PASSWORD is not set. The web UI password is required — set the "
            "WEB_PASSWORD environment variable (e.g. in docker-compose.yml) and restart."
        )
        sys.exit(1)
    if WEB_PASSWORD in PLACEHOLDER_PASSWORDS:
        log.critical(
            "WEB_PASSWORD is set to a placeholder value (%r). Choose a strong, "
            "non-obvious password — the app refuses to start with the example "
            "password from the docs.", WEB_PASSWORD,
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
