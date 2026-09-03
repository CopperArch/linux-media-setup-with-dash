#!/bin/bash
# post-reboot-check.sh — verify the whole box came back after a reboot.
#
# Runs automatically once after the next boot (see post-reboot-check.service,
# which this script disables again when it finishes, so it never fires twice).
# Writes a timestamped report to ~/.hermes/maintenance-logs/post-reboot-*.log
# so the evidence is waiting even if nobody is watching the screen.
#
#   post-reboot-check.sh          # run the checks now
set -uo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

ROOT="{{HOME}}"
LOG="$ROOT/.hermes/maintenance-logs/post-reboot-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1

PASS=0; FAIL=0
ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
info() { echo "         $*"; }
sec()  { echo; echo "===== $* ====="; }

echo "post-reboot check — $(date '+%Y-%m-%d %H:%M:%S')"
echo "kernel $(uname -r), up $(cut -d. -f1 /proc/uptime)s"

# Give slow starters (Immich, Plex, gluetun's tunnel) a chance before judging.
sec "Settling"
for i in $(seq 1 60); do
    running=$(docker ps -q 2>/dev/null | wc -l)
    [[ "$running" -ge 20 ]] && break
    sleep 5
done
info "waited $((i*5))s for containers to come up"
sleep 45   # health checks need a couple of cycles

sec "Containers"
TOTAL=$(docker ps -a -q 2>/dev/null | wc -l)
UP=$(docker ps -q 2>/dev/null | wc -l)
[[ "$UP" -eq "$TOTAL" && "$TOTAL" -gt 0 ]] && ok "$UP/$TOTAL containers running" \
    || bad "$UP/$TOTAL containers running"
DOWN=$(docker ps -a --filter 'status=exited' --filter 'status=created' --format '{{.Names}}' 2>/dev/null | tr '\n' ' ')
[[ -n "${DOWN// }" ]] && bad "not running: $DOWN"
UNHEALTHY=$(docker ps --filter health=unhealthy --format '{{.Names}}' 2>/dev/null | tr '\n' ' ')
[[ -n "${UNHEALTHY// }" ]] && bad "unhealthy: $UNHEALTHY" || ok "no unhealthy containers"

sec "Storage"
for m in / {{MEDIA_POOL}} {{NEXTCLOUD_DATA}} {{MEDIA_DISK1}} {{MEDIA_DISK2}} {{MEDIA_DISK3}}; do
    if mountpoint -q "$m" 2>/dev/null || [[ "$m" == "/" ]]; then
        ok "$m mounted ($(df -h "$m" 2>/dev/null | awk 'NR==2{print $5" used, "$4" free"}'))"
    else
        bad "$m NOT mounted"
    fi
done
MOVIES=$(ls {{MEDIA_POOL}}/Movies 2>/dev/null | wc -l)
TV=$(ls "{{MEDIA_POOL}}/Tv Shows" 2>/dev/null | wc -l)
[[ "$MOVIES" -gt 100 && "$TV" -gt 100 ]] && ok "pool content visible (Movies=$MOVIES, TV=$TV)" \
    || bad "pool content looks wrong (Movies=$MOVIES, TV=$TV)"

sec "VPN"
GID=$(docker inspect -f '{{.Id}}' gluetun 2>/dev/null)
HEALTH=$(docker inspect -f '{{.State.Health.Status}}' gluetun 2>/dev/null)
[[ "$HEALTH" == "healthy" ]] && ok "gluetun healthy" || bad "gluetun health=$HEALTH"
IP=$(docker logs --tail 300 gluetun 2>&1 | grep -oP 'Public IP address is \K[0-9.]+.*' | tail -1)
[[ -n "$IP" ]] && ok "exit IP: $IP" || bad "no exit IP found in gluetun logs"
echo "$IP" | grep -qi netherlands && ok "exit country is Netherlands" \
    || bad "exit country is NOT Netherlands"
BROKEN=""
for c in qbittorrent prowlarr radarr sonarr flaresolverr chrome jellyseerr; do
    MODE=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$c" 2>/dev/null)
    [[ "$MODE" == "container:$GID" ]] || BROKEN="$BROKEN $c"
done
[[ -z "$BROKEN" ]] && ok "all 7 netns siblings bound to gluetun" \
    || bad "NOT sharing gluetun netns:$BROKEN"

sec "Service endpoints"
check() {  # check <name> <url> <acceptable codes regex>
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "$2" 2>/dev/null)
    [[ "$code" =~ $3 ]] && ok "$1 ($code)" || bad "$1 -> $code"
}
check "qBittorrent" http://localhost:8080                  '^(200|401|403)$'
check "Prowlarr"    http://localhost:9696                  '^(200|401)$'
check "Radarr"      http://localhost:7878                  '^(200|401)$'
check "Sonarr"      http://localhost:8989                  '^(200|401)$'
check "Overseerr"   http://localhost:5055                  '^(200|30[27])$'
check "Jellyseerr"  http://localhost:5056                  '^(200|30[27])$'
check "Plex"        http://localhost:32400/identity        '^200$'
check "Jellyfin"    http://localhost:8096/health           '^200$'
check "Nextcloud"   http://localhost:8090/status.php       '^200$'
check "Immich"      http://localhost:2283/api/server/ping  '^200$'
check "Portainer"   http://localhost:9000                  '^(200|30[27])$'
check "Caddy"       http://127.0.0.1:8095                  '^(200|30[128]|404|502)$'

sec "Status dashboard"
for u in status-dashboard-server.service status-collect.timer status-dashboard.service; do
    STATE=$(systemctl --user is-active "$u" 2>/dev/null)
    [[ "$STATE" == "active" ]] && ok "$u active" || bad "$u is $STATE"
done
CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 10 http://127.0.0.1:8099/index.html)
[[ "$CODE" == "200" ]] && ok "dashboard page serves (200)" || bad "dashboard page -> $CODE"
if pgrep -f 'chrome.*--app=http://127.0.0.1:8099' >/dev/null 2>&1; then
    ok "dashboard window is up"
else
    bad "dashboard window NOT running"
fi
AGE=$(python3 - <<'PY' 2>/dev/null
import json,time
try:
    d=json.load(open('{{HOME}}/.local/share/status-dashboard/status.json'))
    print(int(time.time()-d['generated_epoch']))
except Exception: print(-1)
PY
)
if [[ "$AGE" -ge 0 && "$AGE" -lt 300 ]]; then ok "collector data is fresh (${AGE}s old)"
else bad "collector data stale/missing (age=${AGE}s)"; fi

sec "Indexers"
PK=$(docker exec prowlarr sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' /config/config.xml 2>/dev/null)
N=$(curl -s -m 20 -H "X-Api-Key: $PK" http://localhost:9696/api/v1/indexer 2>/dev/null \
     | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))' 2>/dev/null)
[[ "${N:-0}" -ge 9 ]] && ok "$N indexers configured" || bad "only ${N:-0} indexers configured"

sec "Host"
FAILED_UNITS=$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
[[ -z "${FAILED_UNITS// }" ]] && ok "no failed systemd units" || bad "failed units: $FAILED_UNITS"
info "load:$(cut -d' ' -f1-3 /proc/loadavg)  mem: $(free -h | awk 'NR==2{print $3"/"$2}')"

sec "RESULT"
echo "  $PASS passed, $FAIL failed"
echo "  report: $LOG"

# One-shot: don't run again on subsequent boots.
systemctl --user disable post-reboot-check.service >/dev/null 2>&1 && \
    echo "  (auto-check disarmed — re-arm with: systemctl --user enable post-reboot-check.service)"

exit $(( FAIL > 0 ))
