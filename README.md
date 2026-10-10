# Online Video Archiver

A small Docker service that keeps the newest N videos from each configured channel on a mounted volume.

## Features

- Password-locked web UI for managing channels and settings (password via the
  `WEB_PASSWORD` env var; the app refuses to start if it is unset or left as an
  example/placeholder value).
- Starts **paused** on first run; nothing downloads until you unpause it.
- Downloads the newest N videos per channel, where N is set per channel.
- Polls each channel periodically for new uploads (channels with N=0 are skipped),
  and a **Refresh now** button runs an immediate sync on demand.
- Deletes older downloads so each channel retains only its newest N.
- Channels, settings, and download state are stored in a SQLite database.
- Saves videos as `ChannelName-PostDate-VideoTitle-VideoID.mp4` (the ID suffix makes
  filename collisions impossible); names are truncated by bytes so they never exceed
  filesystem filename/path limits.
- Limits source video to 1080p maximum and selects H.264/AVC `avc1` only (HEVC `hvc1`
  and VP9/AV1 are never used, for broad Apple TV compatibility).
- Uses AAC/M4A audio.
- Uses FFmpeg only for container mux/remux (with `+faststart`); it does **not**
  transcode/re-encode.
- Shows disk usage of the data volume (active videos, orphaned folder, free space).
- Exposes a public `/health` endpoint on port 8080; everything else is behind the login.

## Quick start

1. Set the web UI password in `docker-compose.yml`. Replace the `WEB_PASSWORD`
   placeholder — the app **refuses to start** if it is unset or left as an example
   value like `change-me`.

2. Start:

```bash
docker compose up -d --build
```

3. Open `http://localhost:8080`, sign in, add a channel, and flip the switch to
   start the archiver (see [Channels & settings](#channels--settings)).

Videos appear under `./data/videos/`; the SQLite database is `./data/archive.db`.

Health check (public, used by the Docker healthcheck):

```bash
curl http://localhost:8080/health
```

All other endpoints are behind the login; the web UI is the normal way to
interact with the service.

Follow logs:

```bash
docker compose logs -f
```

## Hosted image

The Docker image is built automatically on every push to `main` and published to
the GitHub Container Registry at
`ghcr.io/xelprep/online-video-archiver` (tag `latest`). It is built for both
`linux/amd64` and `linux/arm64`, so it runs on Intel/AMD and Apple Silicon
hosts. You can pull it instead of building locally:

```bash
docker pull ghcr.io/xelprep/online-video-archiver:latest
```

To use it with `docker compose`, replace the `build: .` line in
`docker-compose.yml` with:

```yaml
    image: ghcr.io/xelprep/online-video-archiver:latest
```

Or run it directly:

```bash
docker run -d --name online-video-archiver \
  -e WEB_PASSWORD="change-me" \
  -p 8080:8080 \
  -v ./data:/data \
  ghcr.io/xelprep/online-video-archiver:latest
```

## Format/Apple TV behavior

The downloader selects **H.264/AVC (`avc1`) only** and applies a hard 1080p maximum to the selected source video. HEVC (`hvc1`) and VP9/AV1 streams are deliberately rejected — older Apple TV models do not decode VP9, and the goal is broad compatibility rather than maximum quality. Audio is M4A/AAC.

If the selected video and audio are separate streams, FFmpeg muxes them into MP4 **without re-encoding** (a remux, not a transcode) and writes the file with `+faststart` so the moov atom sits at the front and Apple TV can begin playback immediately. H.264 in an MP4 container with AAC audio is supported by every Apple TV 4K model.

The service does not attempt to convert an incompatible video. If a video has no `avc1` MP4 stream at or below 1080p, that video is logged as failed and retried on a later polling cycle.

## Channels & settings

Everything is managed from the web UI at `http://localhost:8080` (sign in with
the `WEB_PASSWORD` you set in `docker-compose.yml`):

- **Status** — a switch that pauses/resumes all archiving (persisted; the app
  starts paused on first run), plus a **Refresh now** button that runs an immediate
  sync cycle (handy right after adding a channel, instead of waiting for the next
  poll).
- **Storage** — disk usage of the data volume: active videos, the orphaned folder,
  the data folder total, and free space on the volume.
- **Settings** — poll interval and log level (persisted in the database).
- **Channels** — add, edit, and remove channels. Each channel requires an
  explicit "keep latest N" (0 = keep only protected videos). Accepted URL
  formats: `@handle`, `https://www.youtube.com/@handle`,
  `https://www.youtube.com/channel/UC...`.

Removing a channel deletes it from the database and moves its downloaded files
into `./data/videos/orphaned/` (renamed with an `-orphaned-<timestamp>` suffix).
They are no longer referenced by the app or the UI — delete them from disk when
you no longer need them.

Channels and settings live in the SQLite database (`./data/archive.db`), so they
survive restarts. If you ever need to, you can also edit the database directly
with any SQLite tool (the `./data` directory is a host mount):

```sql
-- Add a channel: url + how many recent videos to keep (N)
INSERT OR IGNORE INTO channels (url, latest_n, added_at)
VALUES ('https://www.youtube.com/@yourchannel', 10, datetime('now'));

-- Unpause the archiver (takes effect within ~30 s, no restart needed)
UPDATE settings SET value = '0' WHERE key = 'paused';
```

### Per-channel retention

Each channel stores its own `latest_n` in the `channels` table — there is no
global default. A channel with `latest_n = 0` is skipped by the scheduled poll;
it only keeps videos that are explicitly protected.

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
