FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ffmpeg muxes/remuxes downloads. deno is the JavaScript runtime yt-dlp needs
# to decrypt YouTube signatures; the distro's node is older than yt-dlp's
# minimum (v22), so we install deno instead. Deno is yt-dlp's default runtime,
# so no extra option is required in the code. (unzip is needed by the installer.)
# tzdata backs Python's zoneinfo so the app can schedule refreshes in IANA
# timezones (without it, zoneinfo silently falls back to UTC).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip dnsmasq tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app
COPY requirements.txt .

# Explicitly install nightly yt-dlp so any addiitonal requirements can remain regular releases
RUN pip install --no-cache-dir -U --pre yt-dlp[default,curl-cffi]
RUN pip install --no-cache-dir -r requirements.txt

# Copy the *contents* of app/ into /app so main.py lands at /app/main.py.
# (Also brings in entrypoint.sh and dnsmasq.conf.)
COPY app/ ./
RUN chmod +x /app/entrypoint.sh

# Run as an unprivileged user; /data is the only writable path the app needs.
# The container entrypoint runs as root (so it can start the dnsmasq DNS cache
# on port 53) and then drops to appuser for the app itself — see entrypoint.sh.
# We therefore no longer pin USER here.
#
# The app self-upgrades yt-dlp to the newest nightly at the start of each sync
# cycle via `pip install --user`. The default user-site base is HOME/.local,
# which is not writable by appuser (HOME is /root's or unset), so pip fails with
# "Permission denied: '/root/.local/lib/python3.13/site-packages'". We pin the
# base to a dedicated appuser-owned directory and export PYTHONUSERBASE so both
# the pip subprocess and the interpreter's sys.path agree on where it lives.
RUN useradd --create-home appuser \
    && mkdir -p /data /app/.ytdlp-userbase \
    && chown appuser:appuser /data /app/.ytdlp-userbase

ENV PYTHONUSERBASE=/app/.ytdlp-userbase

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"

CMD ["/app/entrypoint.sh"]
