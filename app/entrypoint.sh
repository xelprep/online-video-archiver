#!/bin/sh
# Container entrypoint.
#
# yt-dlp opens a fresh connection — and therefore a fresh DNS lookup — for
# essentially every request it makes to www.youtube.com. With no caching,
# archiving a backlog of videos sends thousands of lookups per minute to the
# upstream resolver, which can get the host's DNS rate-limited. To fix that we
# run a small local DNS cache (dnsmasq) on 127.0.0.1 and point the container's
# resolver at it; dnsmasq forwards misses to the upstream server Docker
# configured. Repeated lookups for the same name are then answered from cache
# for the record's TTL.
#
# dnsmasq must bind port 53, which requires root, so this entrypoint runs as
# root, starts dnsmasq, then drops privileges to the unprivileged `appuser`
# for the application itself (preserving the original security model).
#
# Set DNS_CACHE=0 to disable the cache and run the app directly.

set -e

if [ "${DNS_CACHE:-1}" != "0" ] && [ "${DNS_CACHE:-1}" != "false" ]; then
    # Capture the upstream resolver Docker set up before we override anything.
    # DNS_UPSTREAM (env) is DOMAIN-SCOPED, not global: it becomes a dnsmasq
    # domain-upstream (dnsmasq --server=/domain/ip) for the YouTube family
    # (youtube.com/googlevideo.com/ytimg.com + subdomains) so a deploy can
    # bypass a blocking host chain for YouTube traffic only. Everything else
    # — crucially peer container names like `bgutil-pot` on the network — keeps
    # resolving through the Docker-discovered default. A GLOBAL override would
    # route peer lookups to the external resolver and NXDOMAIN them, breaking
    # the POT provider wiring; scoping is why this flag is safe.
    UPSTREAM_DEFAULT=$(awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf)
    if [ -z "$UPSTREAM_DEFAULT" ]; then
        UPSTREAM_DEFAULT=127.0.0.11
    fi
    set -- dnsmasq --conf-file=/app/dnsmasq.conf --server="$UPSTREAM_DEFAULT"
    if [ -n "${DNS_UPSTREAM:-}" ]; then
        for _d in youtube.com googlevideo.com ytimg.com; do
            set -- "$@" --server="/$_d/$DNS_UPSTREAM"
        done
    fi

    # Point the container's resolver at our local cache.
    echo "nameserver 127.0.0.1" > /etc/resolv.conf

    if "$@"; then
        echo "DNS cache enabled (default upstream $UPSTREAM_DEFAULT; ${DNS_UPSTREAM:+YouTube-family scoped to $DNS_UPSTREAM})"
        # Bounded wait until the cache is actually answering; this also warms
        # it with the first lookup. Never fails the entrypoint.
        python - <<'PY'
import socket, time
for _ in range(50):
    try:
        socket.getaddrinfo("www.youtube.com", 443)
        break
    except Exception:
        time.sleep(0.1)
PY
    else
        echo "WARNING: dnsmasq failed to start; continuing without DNS cache" >&2
        # Restore the resolver so the app can still reach the upstream.
        echo "nameserver $UPSTREAM_DEFAULT" > /etc/resolv.conf
    fi
fi

exec setpriv --reuid=appuser --regid=appuser --init-groups python /app/main.py
