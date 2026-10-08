# Online Video Archiver

A small Docker service that keeps the newest N videos from each configured channel on a mounted volume.

## Features

- Downloads the newest N videos on startup.
- Polls each channel periodically for new uploads.
- Deletes older downloads so each channel retains only its newest N.
- SQLite state database prevents duplicate downloads.
- Saves videos as `ChannelName-PostDate-VideoTitle-VideoID.mp4` (the ID suffix makes
  filename collisions impossible).
- Limits source video to 1080p maximum and prefers H.264/AVC `avc1`, then H.265/HEVC `hvc1`.
- Uses AAC/M4A audio.
- Uses FFmpeg only for container mux/remux; it does **not** transcode/re-encode.
- Exposes a small health/status HTTP API on port 8080.

## Quick start

1. Edit `config/channels.yml`.
2. Set `LATEST_N` and `POLL_MINUTES` in `docker-compose.yml` if desired.
3. Start:

```bash
docker compose up -d --build
```

Videos appear under `./data/videos/`; the SQLite database is `./data/archive.db`.

Health check:

```bash
curl http://localhost:8080/health
```

List tracked videos:

```bash
curl http://localhost:8080/api/videos
```

Follow logs:

```bash
docker compose logs -f
```

## Format/Apple TV behavior

The downloader deliberately rejects VP9 and AV1 video streams and applies a hard 1080p maximum to the selected source video. Within that limit it prefers H.264/AVC (`avc1`) first, then HEVC (`hvc1`), paired with M4A/AAC audio. If the selected video/audio are separate streams, FFmpeg muxes them into MP4 without re-encoding.

The service does not attempt to convert an incompatible video. If a video has no compatible `hvc1` or `avc1` MP4 stream, that video is logged as failed and retried on a later polling cycle.

## Per-channel retention

You can configure a global default:

```yaml
# docker-compose.yml
LATEST_N: "25"
```

Or override it per channel:

```yaml
channels:
  - url: "https://www.youtube.com/@channel-one"
    latest_n: 25
  - url: "https://www.youtube.com/@channel-two"
    latest_n: 5
```

## Notes

- The HTTP server binds to `0.0.0.0` inside the container, so it is reachable from your
  LAN through the published port. To serve on a different host port, change the left side
  of the mapping in `docker-compose.yml` (e.g. `"8090:8080"`); the container port is fixed
  at 8080.
- The container runs as an unprivileged user (UID 1000). On Linux hosts, make sure
  `./data` is writable by that UID (e.g. `chown -R 1000:1000 data`).
- YouTube may change or restrict available formats. yt-dlp is pinned to a specific
  release in `requirements.txt`; bump the pin periodically.
- Some videos can be age-restricted, members-only, region-restricted, or otherwise unavailable without browser cookies. This base version intentionally does not embed account cookies.
- The filename is sanitized for common filesystem-invalid characters, and the YouTube
  video ID is appended, so sanitized-name collisions cannot cause overwrites. The SQLite
  row remains the authoritative identity.
