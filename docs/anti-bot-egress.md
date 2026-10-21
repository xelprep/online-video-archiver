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

Never promote tier 3 above tier 1. Intermediary services (Invidious/Piped instances)
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

