# Discovery via RSS + single-extraction downloads

This release changes *how the archiver finds new videos* and *how many times it
probes each video before downloading it*. Both changes target the same thing:
the volume of InnerTube (bot-defended) traffic the app generates.

## What a refresh cycle used to do

Every cycle, every channel:

1. yt-dlp extracts the channel's `/videos` tab → one **InnerTube** request
   (the same bot-defended surface that answers "sign in to confirm you're not
   a bot"). On a blocked egress this *listing* itself fails, so the cycle dies
   before it can even tell you what is new.
2. For every video it decides to download, yt-dlp got the full metadata once
   (`get_full_info`) and then `ydl.download([url])` re-fetched the player
   response a **second** time. Two player calls per downloaded video.

## What it does now

### 1. Feed-first discovery (`list_latest`)

YouTube exposes an Atom feed per channel:
`https://www.youtube.com/feeds/videos.xml?channel_id=UC…`

That feed is a **plain HTTP GET**. It is not served through InnerTube, has no
bot defense, and keeps working even when your download egress is currently
bot-walled. It carries the channel's **latest 15 uploads** with video ID,
title and publish timestamp — exactly what a steady-state refresh needs:

```
one plain GET per channel per cycle
→ compare entry IDs against the SQLite videos table
→ yt-dlp player calls happen only for the videos actually downloaded
```

An idle cycle (nothing new) costs N plain GETs for N channels and zero InnerTube
calls. Previously it cost N InnerTube listing calls at minimum.

The feed order is not guaranteed newest-first, so the app sorts entries by the
`<published>` stamp itself.

### 2. Persisted `channel_id`

The feed path needs the 24-char channel ID (`UC…`), so it is resolved **once at
add time**: a plain GET of the channel page and a scan for
`"channelId":"UC…"` (for `/channel/UC…` URLs the ID is in the URL itself;
`/c/Name`, `/user/Name` and `@handle` all resolve via the page HTML). It is
stored in a new `channels.channel_id` column (older DBs get it via ALTER, same
migration path as the `protected` flag). If resolution fails (rare, e.g. a
page-fetch hiccup), it is retried lazily on a later refresh — nothing is
permanently lost.

### 3. Single-extraction downloads (`download_with_info_file`)

The app already needed full video metadata (title, upload date, channel name)
*before* downloading, to pick the filename. Instead of making yt-dlp download
from a fresh re-extraction, the app dumps that one extraction result as JSON
and replays it via yt-dlp's `--load-info-json` mechanism
(`download_with_info_file`). The download consumes the already-fetched info;
yt-dlp re-extracts from `webpage_url` on its own only if the download itself
fails. Net effect: **2 player calls per video → 1**. The most bot-sensitive
traffic in the system is exactly halved.

## When the yt-dlp listing is still used (and why that's correct)

The InnerTube tab listing path (`list_channel`) stays as a fallback for:

- **Initial seeds with `latest_n > 15`** — feeds only carry 15 entries, so
  seeding a channel that wants, say, its latest 50 needs one tab listing. It
  happens once per channel: once the DB has any history for that channel,
  steady-state refreshes use the feed, and any *new* upload necessarily lands
  inside the feed's 15-entry window regardless of `latest_n`.
- Channels whose `channel_id` could not be resolved (yet).
- Failed or empty feed fetches when the channel clearly has history — almost
  certainly a transient feed hiccup, so fall back and retry rather than
  conclude "nothing exists".

## Egress/flakiness tuning (ydl_opts)

The download options encode the "flaky exit" doctrine — survive deaths with
in-video retries on the SAME egress, and only ever change egress at whole-video
boundaries:

- `continuedl: False` (no resume). Google Video Server media URLs are bound to
  the IP/session that fetched them; resuming a half-written `.part` from a
  different proxy is what produces the infamous 403 storm. Egress rotation only
  happens in the outer loop after a whole-video failure, and the failure path
  deletes hidden partials — so a restarted video always starts fresh on the new
  egress.
- `retries: 10`, `fragment_retries: 10`, `retry_sleep: 2` — patient in-egress
  retries with 2s breathing room.
- `socket_timeout: 30` — a dead exit fails fast enough to trigger rotation
  without hanging the cycle.
- `concurrent_fragment_downloads: 4` — parallel fragments for HLS/DASH sources.
- `http_chunk_size: 10_000_000` — chunked HTTP at exactly 10MB: YouTube
  throttles chunks above ~10MB, and yt-dlp's own default is a single streaming
  request (no chunking at all).
- `sleep_requests: 0.5` — gentle fixed jitter between extraction-stage requests;
  the human-like jitter between *videos* is `_human_delay` elsewhere.

## Caveats you should know

- **Shorts are excluded from the archive (policy).** The Atom feed covers all
  uploads and does not tag type, so filtering happens after extraction: the
  primary signal is free (portrait frame + ≤3 minutes duration on the one
  extraction we already do); a plain watch-page canonical probe (`/shorts/<id>`
  canonical/og:url) confirms and catches landscape Shorts. Skipped Shorts are
  never downloaded, never recorded in the DB, and can occupy a slot in the
  feed's 15-entry window (so a window can yield fewer than N downloads); they
  are re-probed cheaply each cycle. A deliberately portrait, sub-3-minute
  regular upload would also be skipped by the heuristic — rare, and the
  thresholds are the one place to tweak if that ever bites you.
- **Scheduled live events / premieres never occupy a window slot (1.13.9).**
  The watch-page probe that classifies Shorts now also classifies not-yet-started
  live events (their watch page carries `LIVE_STREAM_OFFLINE`): like Shorts, they
  are filtered BEFORE the N-truncation in both listing paths, are skipped with a
  clean warning if a listing surfaces them anyway (yt-dlp's extraction fails with
  "This live event will begin in 14 hours." — that is no longer emailed as a
  failure), and never count in the last-run summary. Currently-airing streams are
  NOT marked offline and still download; after an event airs, its entry becomes
  an ordinary ended VOD and a later cycle archives it in full.

## Self-healing channel identity (1.13.9)

A persisted `channel_id` is now bound-checked once per process: one cheap page
GET (og:title) + one feed GET (author name). If the feed behind the stored id
answers as a DIFFERENT channel (renamed handle, or an id persisted by an older
pre-validation resolution), the app logs it, clears the stored id, and
re-resolves through the validated-candidate procedure — otherwise a foreign
channel's feed would be downloaded and counted under the added channel every
cycle (wrong folders, wrong last-run email counts, retention churn). Same-UC
rebrands keep matching because both names move together.

## Last-run summary email honesty (1.13.9)

The email is sent only after a cycle that actually ran: paused cycles and
scheduled ticks that found a manual refresh already holding the lock used to
re-send the PREVIOUS cycle's summary, listing channels with "new videos" that
have none.
- **Members-only entries aren't flagged in the feed.** The tab listing used to
  drop subscriber/premium-only entries before counting N. The feed can't tell
  they're restricted, so one can occupy a slot in the window; at download time
  it's skipped cleanly with a warning (never recorded in the DB), so it will be
  retried on the next cycle — cheap, bounded by the 15-entry window.
- Age-restricted videos still require cookies and are skipped with a warning
  (unchanged behavior).

## DB/API surface changes

- `channels` gains a nullable `channel_id` column (auto-migrated; old DBs keep
  working).
- `POST /api/channels` resolves it eagerly at add time; `GET /api/channels`
  unchanged.
- No UI changes, no new settings. `APP_VERSION` bumps to 1.11.0.
