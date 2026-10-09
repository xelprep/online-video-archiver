FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ffmpeg muxes/remuxes downloads. deno is the JavaScript runtime yt-dlp needs
# to decrypt YouTube signatures; the distro's node is older than yt-dlp's
# minimum (v22), so we install deno instead. Deno is yt-dlp's default runtime,
# so no extra option is required in the code. (unzip is needed by the installer.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the *contents* of app/ into /app so main.py lands at /app/main.py.
COPY app/ ./

# Run as an unprivileged user; /data is the only writable path the app needs.
RUN useradd --create-home appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"

CMD ["python", "/app/main.py"]
