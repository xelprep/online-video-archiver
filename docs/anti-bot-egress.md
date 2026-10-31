# Anti-bot egress: PO tokens, DNS hygiene, and why the pieces are ordered this way

This document explains the anti-bot changes introduced in app version 1.10.0: the **PO
token provider**, **force-IPv4 DNS resolution**, and the **DNS-chain (Pi-hole) fix**. It
also states the egress policy this project follows, so future changes keep their footing.

If you only skim: **PO tokens make the "sign in to confirm you're not a bot" wall
unlikely to start; they do not lift a block that already exists.** The block-avoidance
layers (UA, pacing, TLS impersonation, PO tokens) come first; public proxy rotation is
a last-resort fallback, not the answer.

## The layering, in order of trust

1. **Your own egress, looking like one clean human browser.** The archiver's
   always-on defaults (User-Agent, human-like pacing, curl_cffi Chrome TLS
   impersonation) plus the new PO token provider. All of these shape *the first
   impression Google gets of your residential IP*, which is the only egress whose
   "trust" you can influence without spending anything.
2. **Private egress you control** (a friend's network, a Raspberry Pi ssh -D tunnel,
   your phone's hotspot) — free-ish or cheap, fully under your control, residential
   trust tier. Not built into the app; use it if tier 1 ever proves insufficient.
3. **Public proxy pools (the existing reactive SOCKS5 rotation) — last resort.**
   Google flags well-known public-infrastructure IPs roughly the way it flags any
   public list; keep this as the fallback it already is (it only activates when your
   direct IP is already throttled, and drops back as soon as you recover).
4. **Tor exit nodes (the opt-in `tor_enabled` lane) — below the pool, same public
   category.** Tor is not a private exit: one exit node serves thousands of users and
   Google keeps blocklists of known exit IPs. It sits below tier 3 because the pool is
   re-vetted per cycle, rotates on failure and is already warm at escalation time,
   whereas Tor is a single fixed exit with a cold bootstrap and nothing to rotate
   through. Flipping `tor_priority` to `tor_first` is the deploy-network-specific
   exception (public SOCKS pools dead on your network, Tor reaching YouTube fine).

Never promote tier 3 or 4 above tier 1. Intermediary services (Invidious/Piped instances)
are the same tier-3 category: their own backend IPs get Google-blocked and current
Piped stacks cannot pass YouTube's defenses at all.

## 1. PO token provider

### What it is

YouTube's player and media (GVS) requests now carry a **Proof-of-Origin token** ("POT"):
a fingerprint produced by executing YouTube's own attestation code (BotGuard). yt-dlp
cannot produce those tokens itself, so they must come from an external **provider**.
The yt-dlp wiki's [PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
lists provider plugins; the featured, maintainer-maintained one is
**bgutil-ytdlp-pot-provider** ([repo](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)).

bgutil ships as a **pair of halves**, and this image has both:

- **The plugin half** (pip-installed in the archiver image) is the small side that
  *retrieves* a token for yt-dlp at request time.
- **The server half** runs as the `bgutil-pot` peer container in docker-compose.yml —
  the official prebuilt image
  [`brainicism/bgutil-ytdlp-pot-provider:latest`](https://hub.docker.com/r/brainicism/bgutil-ytdlp-pot-provider).
  It runs a JS runtime (Node.js by default; a Deno flavor exists) plus LuanRT's
  BgUtils BotGuard harness, and it answers on port `4416`.

When both halves are present, yt-dlp: extracts a video → needs a token for the
player/GVS step → asks the plugin → the plugin POSTs to
`http://bgutil-pot:4416/get_pot` on the peer container → the token comes back
attached to the request. The token is minted **from the same egress IP** that will use
it (both containers share your host's IPv4), which is exactly the binding YouTube
expects.

### Why a peer container instead of baking the server into the app image

- The upstream project maintains a finished, tested server image; version drift
  between the pip plugin and the vendored server is a known failure mode, and
  `:latest` + un-pinned pip keeps both halves fresh together.
- It isolates a dependency (JS runtime, canvas native module) from the app image.

Security note kept from the upstream README: the POT server is **unauthenticated**.
We deliberately publish **no port to the host** — it is reachable only over the
compose-internal network, so stray local/external clients cannot mint tokens through
it. Keep it that way.

### What the settings do

- `pot_provider` (default **on**): wires yt-dlp to the provider via
  `--extractor-args youtubepot-bgutilhttp:base_url=<POT_SERVER_URL>`. If the peer
  container is down, bgutil's plugin probes `{base}/ping`, logs
  "server is not available", and yt-dlp proceeds token-less (today's behaviour).
- `POT_SERVER_URL` (compose env, default `http://bgutil-pot:4416`): the compose
  **service name is the DNS name inside the network** — that's why no port
  publishing is needed. Change only if you host the server yourself elsewhere.
- The Settings tab shows `PO tokens: provider server reachable / NOT reachable /
  disabled` (it GETs `{base}/ping` — the same endpoint the plugin uses, so the UI
  cannot lie about what the plugin will do).

### Verifying it works

Run `docker compose logs -f video-archiver` while a download runs and look for lines
like:

```
[youtube] [pot:bgutil:http] Generating a gvs PO Token for web client via bgutil HTTP server
```

If you see the provider listed in verbose output but no "Generating … PO Token" line,
the extraction path is failing earlier (`LOGIN_REQUIRED`) — that is not a provider
fault; see yt-dlp issue #15392-class behavior and consider the cookies recipe below.

### Honest limits (straight from upstream)

- bgutil's README opens with: *"Providing a PO token does not guarantee bypassing
  403 errors or bot checks, but it may help your traffic seem more legitimate."*
- yt-dlp's pinned tracker (#10128) splits the two states:
  - **IP not yet blocked:** a provider makes the wall far less likely — this is the
    prevention layer, and it is what we are built for.
  - **IP already blocked while logged out:** tokens "will not help". Your options
    per the wiki are logged-in cookies (with an account-ban warning — see the
    cookies recipe and keep the rate gentle) or a different *residential* IP (tier
    2 above).
- Tokens are content-bound (video ID / session) and short-lived; the provider
  re-mints them automatically. Tokens minted through one egress should be used
  from that same egress — that is what the same-IP container pairing gives you
  today; if you later add a second proxy egress, mint POTs through that same
  proxy (or accept token-less fallback).

## 2. force-ipv4 (default on)

Your network leaks what it was built to avoid: the DNS chain
 can still hand back **AAAA (IPv6) records** for Google/YouTube hosts. 
Anything that resolves to those records tries a transport with
no working IPv6 path behind it — yt-dlp sits squarely in that class (that is why the
pinned tracker #11868 prescribes `--force-ipv4` for IPv6-capable users).

In `ydl_opts` we set the Python-API equivalent of that CLI flag:

```python
if get_setting("force_ipv4", "1") == "1":
    opts["force_ipv4"] = True
```

This only touches **name resolution** (pick the IPv4 record, ignore the IPv6 one), so
the download path is the one your infrastructure already serves correctly.

## 3. The DNS-chain problem and its two fixes (and why the docs are the fix)

### What the code here does *not* do, and why that is right

Pi-hole's stock blocklist literally **contains `s.youtube.com`** (it was a Google
ad-serving hostname in the 2010s). pi-hole then answers a known-good host with
NXDOMAIN — yt-dlp's own pinned tracker entry #8149 is "disable pi-hole or whitelist
`s.youtube.com`". 

1. **Fix the host chain.** In your Pi-hole UI: Blocklist → Add
   `s.youtube.com` as an **Exception** rule (allow).
2. **Bypass it for this container.** Set `DNS_UPSTREAM` in docker-compose
   env: the entrypoint then pins the dnsmasq cache's upstream resolver (e.g. `1.1.1.1`
   = Cloudflare's resolver). The container's own dnsmasq cache stops 
   inheriting the pi-hole chain's false negatives.
   Unset default = discover upstream as today, so nothing changes for other deploys.

## 4. What to watch after this change

- First boot of the new compose file pulls the provider image:
  `docker pull brainicism/bgutil-ytdlp-pot-provider:latest` happens implicitly on
  `docker compose up`; expect it once.
- Settings tab: two new toggles + a live provider status line.
- A sync cycle's logs should contain the `Generating a ... PO Token` line once
  extraction is healthy. Absence of that line with the toggles on → provider not
  wired or already-blocked IP (see the two states in §1).
- If you flip `pot_provider` off or delete the peer container, nothing else changes:
  plugin inert, token-less path, today's behavior.

## 5. The Tor lane (app 1.14.0): what it is, and why it sits below the pool

`tor_enabled` adds a second escape hatch beside the SOCKS5 pool: when the direct
IP is bot-blocked, route through a Tor exit node instead. Same reactive contract
as the pool — nothing touches Tor until a rate-limit/bot signal actually happens,
and the direct recovery probe still drops back as soon as you are throttled no
more.

**Order matters, and the default is the pool first.** Both tiers are public
infrastructure (Google maintains blocklists of known Tor exit IPs, and one exit
serves thousands of users), but the pool is a *searchable* public tier: it is
re-vetted every cycle with a real extract+download, rotated on failure, and
already warm at escalation time. Tor is one fixed exit with a cold bootstrap
(~10 s of onionroot/relay fetches before a clearnet circuit exists) and nothing
to rotate through when that exit is a bad one. So the ladder is:

```
direct  ->  vetted pool (rotate, re-vet)  ->  Tor exit  ->  give up (abort/backoff)
```

`tor_priority = tor_first` inverts the middle two for deploy networks where
public SOCKS pools are dead but a Tor circuit reaches YouTube; the pool is still
tried once Tor has been used and comes back exhausted.

### How the daemon is started (and why it is that way)

- `ensure_tor()` first probes the configured endpoint (`tor_socks_url`,
  default `socks5h://127.0.0.1:9050`). If something already listens there — a
  tor peer container on the compose network, a tor daemon on the NAS host — it
  is used as-is, which is how you keep Tor outside the app image if you prefer.
- Otherwise the image's own `tor` (installed by the Dockerfile apt line) is
  started. tor 0.4.9.15 as shipped in the image rejects a custom torrc's option
  spellings (`Socks5Port`, `LogLevel`, `HiddenRoot`, `module-config` all answer
  "Unknown option"), so the daemon is started with its built-in defaults and
  only the *environment* is shaped: tor's private data dir is `$HOME/.tor` and
  the container's HOME is not writable by the unprivileged `appuser`, so HOME
  is pointed at `DATA_DIR/tor`. Verified in the deploy image: tor started as
  `appuser` with that HOME opens `127.0.0.1:9050`; started without it, tor dies
  with "Couldn't create private data directory". That address stops being
  accurate once the torrc line in the last section below lands — tor then
  opens `0.0.0.0:9051` and `127.0.0.1:9050` does not exist, because a
  `SocksPort` line supersedes the built-in default. Read the last section
  as the current state and this bullet as the pre-change state.
- The daemon is left running until you press **Reset to direct** or turn Tor
  off, so the next escalation gets a warm circuit.

### Two facts this change depends on (verified against current source)

- **SOCKS needs PySocks.** Without it, both `requests` and yt-dlp's requests-
  based sessions raise `Missing dependencies for SOCKS support` — for the pool
  lane *and* the Tor lane. `requirements.txt` gained `pysocks` for that reason;
  the symptom is a `Missing dependencies for SOCKS support` line in the logs
  while the pool status card still claims a proxy is active.
- **PO tokens mint through the lane in use.** yt-dlp's YouTube extractor sets
  `request_proxy = select_proxy('https://www.youtube.com', proxies) or
  select_proxy(inntertube_host, proxies)`, and the bgutil HTTP provider sends
  that as the server's `proxy` field. So a Tor-routed download asks the provider
  server to fetch the BotGuard challenge through the same Tor endpoint — the
  same-egress doctrine from §1 keeps holding under Tor, rather than minting on
  the container's blocked IP and spending the token from an exit node.

### Honest limits

- A Tor exit is a public IP: it may already be flagged, and it is shared. Treat
  a working Tor cycle as *archiving continued*, exactly like the pool caveat.
- Exit stickiness is per instance, not guaranteed per video: measured in the
  deploy image, six same-host requests through one tor instance reused a single
  exit node, which is what keeps GVS media URLs usable for one download. A
  mid-video exit change is still guarded by `continuedl: False` (restart the
  video, never resume a `.part` from a different exit).
- Discovery stays direct (RSS feed GETs and channel-ID GETs are not the bot-
  defended surface); the Tor lane is for the InnerTube/download side.
- `force_ipv4` shapes yt-dlp's own name resolution, not Tor's routing.

### What to watch

- Settings card gains a Tor block and the egress line now reads
  `Routing via Tor exit socks5h://…` while that lane is live.
- First escalation logs `Tor lane ready at … (circuit still bootstrapping…)`;
  the first video of that cycle may burn an yt-dlp retry or two while tor
  finishes fetching relay descriptors.
- If you see `Tor lane failed repeatedly; retiring it and using direct egress`,
  the exit node is dead or flagged — that is the tier working as designed, not
  a fault to chase.

### One shared socket, both containers

The image binds tor's SOCKS socket to `0.0.0.0:9051` (the `SocksPort` line the
Dockerfile appends to the packaged `/etc/tor/torrc` — the file the app's `Popen(["tor"])`
reads). That is what lets the `bgutil-pot` peer container mint through the SAME
exit node the app is downloading through, instead of the peer minting on the
container IP and the token being spent from an exit node. One listener, no third
container. Two consequences of that line, both measured against the real image:

- A `SocksPort` line SUPERSEDES tor's built-in default, so `127.0.0.1:9050` no
  longer exists once the image carries one (`connect ECONNREFUSED`). The default
  `tor_socks_url` must therefore be raised to the container-name form
  `socks5h://<archiver-container-name>:9051` — in the settings UI, or with
  `-e TOR_SOCKS_URL=…` when the archiver container is created. One value feeds
  both sides: yt-dlp's `request_proxy` and the `/get_pot` body's `proxy` field,
  so the endpoint has to be reachable from *both* containers.
- Loopback is per container, never per network: a peer reaches the socket through
  the app container's *network name*, not `127.0.0.1` — inside the app container
  `/etc/hosts` lists only the container id, and name/alias resolution comes from
  the network's own hosts file, which only that network's containers consult.
  Pin a stable handle with `--network-alias` on the archiver container and use
  that in the setting, so a Container Manager rename cannot silently break the
  lane (the app's own `_tor_listening` probe resolves the same name, which is
  why the same value works on both sides).

Healthy escalation logs `Tor lane ready at socks5h://<name>:9051`. A dead
endpoint logs `tor never opened its SOCKS listener` / `Tor lane disabled` (name
or alias no longer resolves: rename, or container recreated without the alias).
On the POT side a dead endpoint is `Could not get BotGuard challenge … connect
ECONNREFUSED <peer network ip>:9051`, which yt-dlp surfaces as
`PoTokenProviderError` and the download then runs token-less.

Wildcard binding is exposure, not formality: with no host port published the
socket still answers from the NAS host at the container network IP
(`curl --socks5-hostname <net-ip>:9051` leaves through a Tor node). If the NAS
must not gain an open Tor proxy, restrict entry at the socket (the packaged
torrc carries `#SocksPolicy` lines — spelling on tor 0.4.9.x not re-verified
here) or keep tor in its own peer container instead.
