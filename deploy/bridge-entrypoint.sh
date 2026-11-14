#!/bin/sh
# Bridge entrypoint for the Bambuddy cloud deployment (Interface, 2026-11-14).
#
# Railway containers have no /dev/net/tun and no NET_ADMIN (proven by the
# ts-diag diagnostic service, 2026-11-14), so the tailnet leg runs as a
# userspace tailscaled inside this container:
#   1. tailscaled --tun=userspace-networking with a SOCKS5 server on
#      localhost:1055; state persists on the data volume so the node
#      keeps its tailnet identity across deploys (auth key needed once).
#   2. tailscale up --accept-routes (the two on-site subnet advertisers
#      carry 192.168.68.0/24 and 192.168.12.0/24).
#   3. loopback_forwarder.py gives each printer a 127.0.1.x address whose
#      Bambu ports relay through the SOCKS proxy to the real LAN address.
# Then the stock entrypoint runs byte-identically ("$@" = the app CMD).
# The app starts regardless of tunnel state: a dark tunnel must read as
# printers-disconnected on a live face, never as a dead face.

set -u
DATA_DIR_RESOLVED="${DATA_DIR:-/app/data}"
TS_STATE_DIR="${DATA_DIR_RESOLVED}/tailscale"
TS_SOCKET="/var/run/tailscale/tailscaled.sock"

mkdir -p "$TS_STATE_DIR" /var/run/tailscale

echo "[bridge] starting tailscaled (userspace) state=${TS_STATE_DIR}"
tailscaled --tun=userspace-networking \
    --socks5-server=localhost:1055 \
    --socket="$TS_SOCKET" \
    --state="${TS_STATE_DIR}/tailscaled.state" \
    >/var/log/tailscaled.log 2>&1 &

i=0
while [ ! -S "$TS_SOCKET" ] && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done

if [ -S "$TS_SOCKET" ]; then
    if [ -n "${TS_AUTHKEY:-}" ]; then
        echo "[bridge] tailscale up (auth key present)"
        timeout 25 tailscale --socket="$TS_SOCKET" up \
            --authkey="$TS_AUTHKEY" \
            --hostname=bambuddy-cloud \
            --accept-routes 2>&1 | sed 's/^/[bridge] /' || true
    else
        echo "[bridge] tailscale up (stored state, no key in env)"
        timeout 25 tailscale --socket="$TS_SOCKET" up \
            --hostname=bambuddy-cloud \
            --accept-routes 2>&1 | sed 's/^/[bridge] /' || true
    fi
    tailscale --socket="$TS_SOCKET" status 2>&1 | head -3 | sed 's/^/[bridge] /' || true
else
    echo "[bridge] WARNING: tailscaled socket never appeared; app starts without the tunnel"
fi

echo "[bridge] starting loopback forwarders"
python3 /usr/local/bin/loopback_forwarder.py >/var/log/forwarder.log 2>&1 &

exec /usr/local/bin/docker-entrypoint.sh "$@"
