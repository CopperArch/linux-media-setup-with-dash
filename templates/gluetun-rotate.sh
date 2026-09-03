#!/usr/bin/env bash
# Rotate the gluetun VPN exit IP by cycling the tunnel via gluetun's control
# server. This deliberately does NOT restart the gluetun container: seven
# containers share its network namespace (qbittorrent, sonarr, radarr,
# prowlarr, jellyseerr, flaresolverr, chrome) and a container restart orphans
# their netns, forcing a --force-recreate of all of them.
#
# Installed 2026-07-29. Runs hourly from cron.
set -uo pipefail

KEY_FILE="${HOME}/.config/gluetun/rotate.key"
LOG="${HOME}/.local/state/gluetun-rotate.log"
CTRL="http://127.0.0.1:8000"
MAX_WAIT=150

mkdir -p "$(dirname "$LOG")"
log() { printf '%s %s\n' "$(date -Is)" "$*" >> "$LOG"; }

[ -r "$KEY_FILE" ] || { log "ERROR: no key at $KEY_FILE"; exit 1; }
KEY="$(cat "$KEY_FILE")"

ctrl() { # ctrl <method> <path> [body]
  docker exec gluetun wget -q -O- --method="$1" \
    --header="X-API-Key: ${KEY}" \
    ${3:+--header="Content-Type: application/json" --body-data="$3"} \
    "${CTRL}$2" 2>/dev/null
}

# gluetun's control server reports the public IP only from its own ip-getter
# (PUBLICIP_API=ipinfo,ifconfigco,ip2location,cloudflare). Those providers
# rate-limit the shared Surfshark exit IPs, and when the lookup fails gluetun
# leaves publicip EMPTY until the next successful fetch -- which made every
# rotation abort with "no public IP after 90s" for ~18h a day even though the
# tunnel itself was fine. Fall back to reading the address straight through
# the tunnel, which is what daily-routine.sh does and never fails.
ip_now() {
  local ip
  ip="$(ctrl GET /v1/publicip/ip | sed -n 's/.*"public_ip":"\([^"]*\)".*/\1/p')"
  if [ -z "$ip" ]; then
    ip="$(docker exec gluetun wget -qO- --timeout=10 https://api.ipify.org 2>/dev/null \
          | tr -d '[:space:]')"
    case "$ip" in *[!0-9.]*|"") ip="" ;; esac
  fi
  printf '%s' "$ip"
}

docker ps --format '{{.Names}}' | grep -qx gluetun || { log "ERROR: gluetun not running"; exit 1; }

OLD="$(ip_now)"
log "rotating; current exit ${OLD:-unknown}"

ctrl PUT /v1/openvpn/status '{"status":"stopped"}' >/dev/null
sleep 3
ctrl PUT /v1/openvpn/status '{"status":"running"}' >/dev/null

# Wait for the tunnel to come back and hand us an address.
for _ in $(seq 1 $((MAX_WAIT / 3))); do
  sleep 3
  NEW="$(ip_now)"
  [ -n "$NEW" ] && break
done

if [ -z "${NEW:-}" ]; then
  log "ERROR: no public IP after ${MAX_WAIT}s - tunnel may be down"
  exit 1
fi

if [ "$NEW" = "$OLD" ]; then
  log "reconnected but got the same exit ${NEW} (provider reassigned it)"
else
  log "rotated ${OLD:-unknown} -> ${NEW}"
fi
