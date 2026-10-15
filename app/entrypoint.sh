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
    # Capture the upstream resolver Docker set up before we override it.
    UPSTREAM=$(awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf)
    if [ -z "$UPSTREAM" ]; then
        UPSTREAM=127.0.0.11
    fi

    # Point the container's resolver at our local cache.
    echo "nameserver 127.0.0.1" > /etc/resolv.conf

    if dnsmasq --conf-file=/app/dnsmasq.conf --server="$UPSTREAM"; then
        echo "DNS cache enabled (upstream $UPSTREAM)"
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
        echo "nameserver $UPSTREAM" > /etc/resolv.conf
    fi
fi

exec setpriv --reuid=appuser --regid=appuser --init-groups python /app/main.py
