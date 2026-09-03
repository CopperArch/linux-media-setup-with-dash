#!/bin/bash
# daily-routine.sh — combined maintenance for this Linux server
#   Merges DB-update.sh + bobs-routine.sh, distro-portable rewrite.
#
#   ./daily-routine.sh          # run everything (updates included)
#   ./daily-routine.sh --quick  # health/connectivity checks only, skip updates
#
# Steps that delegate to ~/.hermes/scripts/*.sh are OPTIONAL: they run if the
# helper exists (copy them over from the old CachyOS box) and skip cleanly if not.
set -uo pipefail

# cron runs with a minimal environment — make PATH and apt behavior explicit.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export DEBIAN_FRONTEND=noninteractive

ROOT="{{HOME}}"
LOG="$ROOT/.hermes/maintenance-logs/daily-routine-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$(dirname "$LOG")"

log()    { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }
warn()   { echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: $*" | tee -a "$LOG"; }
banner() { log ""; log "========== $* =========="; }

# Run a helper script if present; skip gracefully if it isn't.
run_script() {
    local script="$1"
    if [[ -f "$script" ]]; then
        bash "$script" 2>&1 | tee -a "$LOG"
    else
        log "  [SKIP] $(basename "$script") not found (port it from the old box)"
    fi
}

QUICK=false
[[ "${1:-}" == "--quick" ]] && QUICK=true

banner "Daily Routine started (quick=$QUICK) on $(hostname)"

# ─── 1. System health check ──────────────────────────────────────────────────
banner "System Health Check"
run_script "$ROOT/.hermes/scripts/daily-check.sh"

# ─── 1b. Host health: failed units, SMART, docker restart-loops ─────────────
# Added 2026-07-11 after an audit found smartmontools.service dead (fixed
# below), plus this is the right place to catch the NEXT chrome-style crash
# loop early, before it fills the disk like the last one did.
banner "Host System Health Check"

FAILED_UNITS=$(systemctl --failed --no-legend 2>/dev/null)
if [[ -z "$FAILED_UNITS" ]]; then
    log "  [OK]   No failed systemd units"
else
    warn "  [FAIL] Failed systemd unit(s):"
    echo "$FAILED_UNITS" | tee -a "$LOG"
fi

if systemctl is-active --quiet smartmontools; then
    log "  [OK]   smartmontools (smartd) daemon is active"
else
    warn "  [FAIL] smartmontools (smartd) daemon is NOT active — check 'systemctl status smartmontools'"
fi

# SMART health on the disks that actually answer SAT/NVMe passthrough cleanly.
# By-id paths so USB re-enumeration (device letters shift on reconnect) can't
# make this silently check the wrong disk. Deliberately excludes:
#  - the "Expansion HDD" USB enclosure (fails SAT passthrough entirely)
#  - the SD/MMC card reader (not a real disk)
#  - the WD30EZRX "nextcloud-data" disk — smartd/smartctl probing hung its
#    USB bridge for 3+ min and reset the device on 2026-07-11, taking down
#    the live Nextcloud mount. Do not add it back here; see /etc/smartd.conf.
SMART_DEVICES=(
    "/dev/disk/by-id/ata-HGST_HUH721212ALE604_5PJVMRMB:12TB HGST (server-disk pool)"
    "/dev/disk/by-id/ata-WDC_WD120EMAZ-11BLFA0_8CK75M2F:12TB WD (server-disk pool)"
    "/dev/disk/by-id/nvme-CT500P3SSD8_240146957338:nvme0n1 (boot SSD)"
)
for entry in "${SMART_DEVICES[@]}"; do
    dev="${entry%%:*}"
    label="${entry#*:}"
    SMART_OUT=$(sudo -n smartctl -H -A "$dev" 2>&1)
    if [[ -z "$SMART_OUT" ]]; then
        warn "  [FAIL] SMART $label: no output from smartctl (NOPASSWD sudoers rule missing/stale?)"
        continue
    fi
    if echo "$SMART_OUT" | grep -qi "PASSED"; then
        log "  [OK]   SMART $label: PASSED"
    else
        warn "  [FAIL] SMART $label: $(echo "$SMART_OUT" | grep -i 'overall-health')"
    fi
    # Flag nonzero raw values on the classic pre-fail/failing attributes
    # (Reallocated_Sector_Ct, Reported_Uncorrect, Command_Timeout,
    # Reallocated_Event_Count, Current_Pending_Sector, Offline_Uncorrectable).
    BAD_ATTRS=$(echo "$SMART_OUT" | awk '$1 ~ /^(5|187|188|196|197|198)$/ && $10+0 > 0 {print $2"="$10}')
    [[ -n "$BAD_ATTRS" ]] && warn "  [WARN] SMART $label has nonzero pre-fail attributes: $BAD_ATTRS"
done

# Crash-loop detection: alert on any container stuck Restarting or with a
# high restart count, so a future runaway container (like the chrome/Selkies
# incident that filled 138G) gets caught same-day instead of days later.
RESTART_ALERT=false
for c in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
    RC=$(docker inspect -f '{{.RestartCount}}' "$c" 2>/dev/null || echo 0)
    ST=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null)
    if [[ "$ST" == "restarting" || "${RC:-0}" -ge 5 ]]; then
        warn "  [FAIL] Container '$c' looks crash-looping (status=$ST, restarts=${RC:-0})"
        RESTART_ALERT=true
    fi
done
$RESTART_ALERT || log "  [OK]   No containers in a restart loop"

# ─── 1c. Pending updates & reboot status (report-only; applied in step 10) ──
banner "Pending Updates & Reboot Status"
UPGRADABLE_COUNT=$(apt list --upgradable 2>/dev/null | grep -c upgradable)
SECURITY_COUNT=$(apt list --upgradable 2>/dev/null | grep -ic -- '-security' || true)
log "  Pending apt upgrades: $UPGRADABLE_COUNT"
if [[ "${SECURITY_COUNT:-0}" -gt 0 ]]; then
    warn "  [WARN] $SECURITY_COUNT of those are from the -security pocket"
else
    log "  [OK]   No pending -security pocket upgrades"
fi
if [[ -f /var/run/reboot-required ]]; then
    warn "  [WARN] Reboot required$( [[ -f /var/run/reboot-required.pkgs ]] && echo " (packages: $(tr '\n' ' ' < /var/run/reboot-required.pkgs))" )"
else
    log "  [OK]   No reboot required"
fi

# ─── 1d. Config-drift check (Ollama-from-Docker + its ufw hole) ─────────────
# These were one-off fixes made 2026-07-11; an OS update, package reinstall,
# or accidental `ufw reset` could silently revert either one.
banner "Config Drift Check (Ollama / UFW)"
# 2026-08-09: Ollama was decommissioned (service stopped + disabled), so these
# three checks had started FAILing every night for a service that is off on
# purpose. Skip them while it is disabled — if it is ever re-enabled the drift
# checks come back automatically.
if ! systemctl is-enabled ollama >/dev/null 2>&1; then
    log "  [SKIP] Ollama is disabled/decommissioned — drift checks not applicable"
else
    if sudo -n ufw status 2>/dev/null | grep -q "11434/tcp.*172.16.0.0/12"; then
        log "  [OK]   ufw rule for Ollama (172.16.0.0/12 -> 11434/tcp) present"
    else
        warn "  [FAIL] ufw rule for Ollama (172.16.0.0/12 -> 11434/tcp) MISSING — Docker containers can't reach Ollama"
    fi
    OLLAMA_OVERRIDE="/etc/systemd/system/ollama.service.d/override.conf"
    if [[ -f "$OLLAMA_OVERRIDE" ]] && grep -q 'OLLAMA_HOST=0.0.0.0:11434' "$OLLAMA_OVERRIDE"; then
        log "  [OK]   Ollama systemd override present (binds 0.0.0.0:11434)"
    else
        warn "  [FAIL] Ollama systemd override MISSING/changed — $OLLAMA_OVERRIDE"
    fi
    if ss -tln 2>/dev/null | grep -q '\*:11434'; then
        log "  [OK]   Ollama listening on *:11434 (reachable from Docker containers)"
    else
        warn "  [FAIL] Ollama is NOT listening on *:11434 — check 'systemctl status ollama'"
    fi
fi
# Added 2026-07-21: gluetun's outbound firewall must allow the LAN, or every
# container sharing its netns (jellyseerr, sonarr, radarr, prowlarr, chrome,
# flaresolverr) is silently cut off from Jellyfin/Plex on {{LAN_IP}} — this
# is what left jellyseerr un-onboardable until fixed. Env var only takes
# effect on container recreate, so check what gluetun actually booted with.
if docker inspect gluetun --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -q '^FIREWALL_OUTBOUND_SUBNETS={{LAN_CIDR}}'; then
    log "  [OK]   gluetun FIREWALL_OUTBOUND_SUBNETS allows the LAN ({{LAN_CIDR}})"
else
    warn "  [FAIL] gluetun FIREWALL_OUTBOUND_SUBNETS missing/changed — VPN-netns containers can't reach LAN services. Check docker/vpn-stack/docker-compose.yml and 'docker compose up -d --force-recreate gluetun <dependents>'"
fi

# ─── 2. Jellyfin remote connectivity (Cloudflare tunnel) ─────────────────────
banner "Jellyfin Remote Connectivity"
JELLY_CODE=$(curl -sL -o /dev/null -w "%{http_code}" --max-time 15 https://{{JELLYFIN_PUBLIC_URL}} 2>/dev/null)
if [[ "$JELLY_CODE" == "200" ]]; then
    log "  [OK]   {{JELLYFIN_PUBLIC_URL}} reachable (HTTP $JELLY_CODE)"
else
    warn "  [FAIL] {{JELLYFIN_PUBLIC_URL}} returned HTTP $JELLY_CODE — check cloudflared tunnel"
fi

# ─── 2b. VPN daily IP rotation ───────────────────────────────────────────────
# Roll the Surfshark exit IP once a day (avoids lingering IP bans + rotates our
# public footprint). Restarting the gluetun *container* recreates its network
# namespace, so every container sharing it (network_mode: service:gluetun) must
# be restarted too, or they lose all connectivity.
banner "VPN IP Rotation"
OLD_VPN_IP=$(docker exec gluetun wget -qO- https://api.ipify.org 2>/dev/null | tr -d '[:space:]')
log "  Rotating VPN exit IP (was ${OLD_VPN_IP:-unknown})"
docker restart gluetun >/dev/null 2>&1
for _ in $(seq 1 30); do
    sleep 5
    [[ "$(docker inspect -f '{{.State.Health.Status}}' gluetun 2>/dev/null)" == "healthy" ]] && break
done
docker restart prowlarr radarr sonarr qbittorrent flaresolverr jellyseerr chrome >/dev/null 2>&1
sleep 10
NEW_VPN_IP=$(docker exec gluetun wget -qO- https://api.ipify.org 2>/dev/null | tr -d '[:space:]')
if [[ -n "$NEW_VPN_IP" ]]; then
    log "  [OK]   VPN rotated: ${OLD_VPN_IP:-?} -> $NEW_VPN_IP"
else
    warn "  [FAIL] VPN did not return healthy after rotation — check 'docker logs gluetun'"
fi

# ─── 3. Gluetun VPN + qBittorrent status ─────────────────────────────────────
banner "VPN / qBittorrent Status"
VPN_IP=$(docker exec gluetun wget -qO- https://ipinfo.io/ip 2>/dev/null | tr -d '[:space:]')
HOME_IP=$(curl -s --max-time 10 https://ipinfo.io/ip 2>/dev/null | tr -d '[:space:]')
if [[ -n "$VPN_IP" && "$VPN_IP" != "$HOME_IP" ]]; then
    log "  [OK]   Gluetun VPN connected — public IP: $VPN_IP"
else
    warn "  [FAIL] Gluetun VPN not connected or leaking (VPN: ${VPN_IP:-none}, Host: $HOME_IP)"
fi

# qBittorrent WebUI credentials — read from env so they're not hardcoded here.
: "${QB_USER:=admin}"
: "${QB_PASS:={{QBIT_PASSWORD}}}"
QB_COOKIE=$(mktemp)
curl -s -c "$QB_COOKIE" -X POST http://localhost:8080/api/v2/auth/login \
    --data-urlencode "username=$QB_USER" --data-urlencode "password=$QB_PASS" >/dev/null 2>&1
QB_INFO=$(curl -s -b "$QB_COOKIE" http://localhost:8080/api/v2/transfer/info 2>/dev/null)
rm -f "$QB_COOKIE"
if [[ -n "$QB_INFO" ]]; then
    QB_CONN=$(echo "$QB_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('connection_status','unknown'))" 2>/dev/null)
    QB_DHT=$(echo "$QB_INFO"  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('dht_nodes',0))" 2>/dev/null)
    if [[ "$QB_CONN" == "connected" ]]; then
        log "  [OK]   qBittorrent connected (DHT nodes: $QB_DHT)"
    elif [[ "$QB_CONN" == "firewalled" ]]; then
        log "  [WARN] qBittorrent firewalled — outbound OK, inbound port unreachable (VPN limitation)"
        log "         DHT nodes: $QB_DHT"
    else
        warn "  [FAIL] qBittorrent status: ${QB_CONN:-unknown}"
    fi
else
    warn "  [FAIL] qBittorrent API unreachable on port 8080"
fi

# ─── 4. Gluetun iptables rules (re-apply after container restart) ────────────
banner "Gluetun iptables rules"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'gluetun'; then
    for PORT in 9696 8080 8989 7878; do
        docker exec gluetun iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null || \
            docker exec gluetun iptables -A INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null
    done
    log "  [OK]   iptables rules applied (9696/8080/8989/7878)"
else
    warn "  [FAIL] gluetun container not running"
fi

# ─── 4b. Media pipeline self-heal (qBittorrent queue + dead downloads) ───────
# Reapplies qBittorrent's queue settings if drifted, verifies Radarr/Sonarr can
# reach the download client, and blocklists+re-searches genuinely-dead torrents
# (0 seeds / no full copy, stalled >48h) so the queue never freezes behind them.
banner "Media Pipeline Self-Heal"
if [[ -f "$ROOT/.local/bin/media-stack-selfheal.py" ]]; then
    python3 "$ROOT/.local/bin/media-stack-selfheal.py" 2>&1 | tee -a "$LOG"
else
    log "  [SKIP] media-stack-selfheal.py not found"
fi

# ─── 4c. Seerr (Overseerr/jellyseerr) connectivity check ────────────────────
# Added 2026-07-21: jellyseerr sat un-onboarded (no Jellyfin/Radarr/Sonarr,
# public.initialized=false) for weeks with nobody noticing, because it fails
# silent — requests just never get created. Report-only (re-onboarding needs
# the Jellyfin admin password, not something to automate unattended).
banner "Seerr Connectivity Check"
check_seerr() {
    local name="$1" container="$2"
    local result
    result=$(docker exec "$container" sh -c 'cat /app/config/settings.json' 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print('FAIL', f'could not parse settings.json ({e})')
    sys.exit()
init = d.get('public', {}).get('initialized', False)
radarr = len(d.get('radarr', []))
sonarr = len(d.get('sonarr', []))
mst = d.get('main', {}).get('mediaServerType', 0)
ok = init and radarr > 0 and sonarr > 0 and mst not in (0, 4)
print('OK' if ok else 'FAIL', f'initialized={init} radarr={radarr} sonarr={sonarr} mediaServerType={mst}')
")
    if [[ -z "$result" ]]; then
        warn "  [FAIL] $name: can't read settings.json (container down or path changed?)"
        return
    fi
    local status="${result%% *}" detail="${result#* }"
    if [[ "$status" == "OK" ]]; then
        log "  [OK]   $name: connected ($detail)"
    else
        warn "  [FAIL] $name: not fully wired up ($detail) — check Settings > Services in the UI"
    fi
}
check_seerr "Overseerr" "overseerr"
check_seerr "jellyseerr" "jellyseerr"

# ─── 5. Plex health ──────────────────────────────────────────────────────────
banner "Plex Health Check"
run_script "$ROOT/.hermes/scripts/plex-health-check.sh"

# ─── 6. Immich duplicate scan (DELETES losing assets, to Immich's trash) ─────
banner "Immich Duplicate Scan"
IMMICH_HELPER="$ROOT/.hermes/scripts/immich-duplicate-cleanup.sh"
if [[ -f "$IMMICH_HELPER" ]]; then
    # --apply trashes lower-quality duplicate assets (recoverable via Immich's
    # own trash retention window). Reads the API key from ~/.hermes/immich-api-key.
    bash "$IMMICH_HELPER" --apply 2>&1 | tee -a "$LOG"
else
    log "  [SKIP] $(basename "$IMMICH_HELPER") not found"
fi

# ─── 7. Nextcloud duplicate scan (DELETES exact-hash-duplicate files) ────────
banner "Nextcloud Duplicate Scan"
NC_HELPER="$ROOT/.hermes/scripts/nextcloud-duplicate-cleanup.sh"
if [[ -f "$NC_HELPER" ]]; then
    # --apply deletes redundant byte-identical copies (SHA-256 match only, so
    # there's no "quality" ambiguity) and rescans affected users via occ.
    bash "$NC_HELPER" --apply 2>&1 | tee -a "$LOG"
else
    log "  [SKIP] $(basename "$NC_HELPER") not found"
fi

# ─── 8. Jellyfin duplicate removal + collection de-dup (DELETES losing files) ─
banner "Jellyfin Duplicate Removal"
JF_HELPER="$ROOT/.hermes/maintenance/jellyfin-duplicates.sh"
if [[ -f "$JF_HELPER" ]]; then
    # --apply deletes lower-quality duplicate files from disk and removes
    # repeated collections. Reads the API key from ~/.hermes/jellyfin-api-key.
    bash "$JF_HELPER" --apply 2>&1 | tee -a "$LOG"
else
    log "  [SKIP] $(basename "$JF_HELPER") not found"
fi

# ─── 9. Cleanup (inlined: Docker prune, journal vacuum, log rotation) ────────
# ─── 8b. Desktop status dashboard ───────────────────────────────────────────
# Idempotent: re-writes the systemd units only if they drifted, then makes sure
# the collector, the local server and the dashboard window are all running.
# This is what makes the dashboard portable — restore System-Recovery/ (or the
# "DB service" Nextcloud folder) onto a fresh Linux box, run this routine, and
# the dashboard installs and displays itself.
banner "Desktop Status Dashboard"
DASH_INSTALL="$ROOT/.local/bin/status-dashboard-install.sh"
if [[ -x "$DASH_INSTALL" ]]; then
    bash "$DASH_INSTALL" 2>&1 | tee -a "$LOG"
else
    log "  [SKIP] status-dashboard-install.sh not found"
fi

# ─── 8c. Local LLM models ───────────────────────────────────────────────────
# `ollama pull` only transfers layers whose manifest changed, so running this
# nightly keeps deepseek-r1 current without re-downloading 9GB every time.
# Models live on the mergerfs pool (OLLAMA_MODELS={{MEDIA_POOL}}/ollama-models) —
# never the root disk, which is what killed the previous Ollama install.
banner "Local LLM Model Updates"
OLLAMA_UPDATE="$ROOT/.local/bin/ollama-model-update.sh"
if [[ -x "$OLLAMA_UPDATE" ]]; then
    bash "$OLLAMA_UPDATE" 2>&1 | tee -a "$LOG"
else
    log "  [SKIP] ollama-model-update.sh not found"
fi

# ─── 8d. AI dashboard pane model check ──────────────────────────────────────
# Added 2026-09-03. The dashboard's Ox Alpha/DeepSeek/Minimax M3/ChatGPT panes
# are all backed by OpenRouter (dashboard-pane.sh), whose free/stealth model
# slugs churn — Ox Alpha and DeepSeek's free tiers already expired within
# hours of being wired up. This re-verifies each of the four still resolves
# (falling back to the cheapest paid variant rather than leaving a pane dead)
# and re-picks the current flagship OpenAI model for the ChatGPT pane. Writes
# into deepseek.env's managed block; dashboard-pane.sh picks changes up on the
# next pane open, no service restart needed. Network-only, so runs even
# in --quick mode; never fails the routine (see the script's own docstring).
banner "AI Dashboard Pane Model Check"
AI_PANES_CHECK="$ROOT/.local/bin/ai-panes-check.py"
if [[ -f "$AI_PANES_CHECK" ]]; then
    python3 "$AI_PANES_CHECK" 2>&1 | tee -a "$LOG"
else
    log "  [SKIP] ai-panes-check.py not found"
fi

banner "Cleanup"
if docker system prune -f >/dev/null 2>&1; then
    log "  [OK]   docker system prune complete"
else
    warn "  docker prune had errors"
fi
# Explicit dangling/untagged-image prune (images only). Never `docker volume
# prune` or `system prune -a` here — this box has lost data to over-eager
# pruning before; -a would also remove images for stopped-but-wanted compose
# services, not just truly-dangling ones.
RECLAIM_BEFORE=$(docker system df --format '{{.Type}}\t{{.Reclaimable}}' 2>/dev/null | awk -F'\t' '$1=="Images"{print $2}')
if docker image prune -f >/dev/null 2>&1; then
    RECLAIM_AFTER=$(docker system df --format '{{.Type}}\t{{.Reclaimable}}' 2>/dev/null | awk -F'\t' '$1=="Images"{print $2}')
    log "  [OK]   docker image prune (dangling only) complete — reclaimable images: ${RECLAIM_BEFORE:-?} -> ${RECLAIM_AFTER:-?}"
else
    warn "  docker image prune had errors"
fi
# Docker log rotation guard: verify /etc/docker/daemon.json still enforces the
# 30MB/container cap (self-heals + restarts Docker if it ever drifts). Rotation
# does the ongoing capping; this just keeps the policy in place. Runs via a
# narrow NOPASSWD rule for this one root-owned helper.
DLC="/usr/local/sbin/docker-log-clean.sh"
if [[ -x "$DLC" ]]; then
    # Exit-code contract (see the script's header): 0 = clean, 1 = healthy but
    # reporting something that needs a human (e.g. a container logging without
    # a cap, which needs RECREATING to fix), 2 = the guard itself failed.
    # PIPESTATUS[0] is needed because `set -o pipefail` otherwise collapses all
    # three into one indistinguishable "non-zero", which used to render every
    # self-heal run as "check the NOPASSWD rule" — the one thing it wasn't.
    sudo -n "$DLC" 2>&1 | tee -a "$LOG"
    DLC_RC=${PIPESTATUS[0]}
    case "$DLC_RC" in
        0) log "  [OK]   docker log rotation guard complete" ;;
        1) warn "  docker log rotation guard needs attention — see its lines above" ;;
        *) warn "  docker log rotation guard FAILED (rc=$DLC_RC) — check the NOPASSWD rule for $DLC and /etc/docker/daemon.json" ;;
    esac
else
    log "  [SKIP] $DLC not installed (see scratchpad install steps)"
fi
if sudo journalctl --vacuum-time=14d >/dev/null 2>&1; then
    log "  [OK]   journal vacuumed to 14 days"
else
    warn "  journal vacuum failed (sudo/systemd?)"
fi
# Rotate our own maintenance logs: keep the last 30 days.
DELETED=$(find "$ROOT/.hermes/maintenance-logs" -name '*.log' -mtime +30 -print -delete 2>/dev/null | wc -l)
log "  [OK]   removed $DELETED maintenance log(s) older than 30 days"

# ─── 9b. System-Recovery backup to Nextcloud (WebDAV) ────────────────────────
# Added 2026-07-13 as a scripts-only backup ("Scripts-Backup"); widened
# 2026-07-23 into a full disaster-recovery mirror ("System-Recovery" folder):
# scripts + compose/env files + system config (crontab/fstab/fuse.conf/docker
# daemon.json/sudoers recipe) + a mirror of Claude's memory about this box, so
# a full reinstall can be pointed at one Nextcloud folder to reintegrate
# everything. See ~/.local/bin/nextcloud-system-backup.sh for exact scope
# (deliberately excludes app data — Sonarr/Radarr/Plex/Jellyfin/etc — that's
# a separate, much bigger backup problem). Nextcloud keeps its own version
# history on overwrite, so this doubles as an off-box undo log too.
banner "System-Recovery Backup to Nextcloud"
if [[ -f "$ROOT/.local/bin/nextcloud-system-backup.sh" ]]; then
    bash "$ROOT/.local/bin/nextcloud-system-backup.sh" 2>&1 | tee -a "$LOG"
else
    log "  [SKIP] nextcloud-system-backup.sh not found"
fi

# ─── 10. System updates (apt + snap + flatpak + firmware) ────────────────────
if ! $QUICK; then
    banner "APT System Update"
    sudo apt-get update 2>&1 | tee -a "$LOG"
    if sudo apt-get -y upgrade 2>&1 | tee -a "$LOG"; then
        log "  [OK]   apt upgrade completed"
    else
        warn "  apt upgrade finished with errors — continuing"
    fi
    sudo apt-get -y autoremove 2>&1 | tee -a "$LOG"

    banner "Snap Updates"
    if command -v snap &>/dev/null; then
        sudo snap refresh 2>&1 | tee -a "$LOG"
        log "  [OK]   snap refresh completed"
    else
        log "  [SKIP] snap not installed"
    fi

    banner "Flatpak Updates"
    if command -v flatpak &>/dev/null; then
        flatpak update -y 2>&1 | tee -a "$LOG"
        log "  [OK]   flatpak update completed"
    else
        log "  [SKIP] flatpak not installed"
    fi

    banner "Firmware Updates (fwupdmgr)"
    if command -v fwupdmgr &>/dev/null; then
        fwupdmgr refresh 2>&1 | tee -a "$LOG" || true
        if timeout 600 fwupdmgr update -y 2>&1 | tee -a "$LOG"; then
            log "  [OK]   fwupdmgr update completed"
        else
            log "  fwupdmgr finished (no updates, or timed out) — continuing"
        fi
    else
        log "  [SKIP] fwupdmgr not installed"
    fi

    # ─── 10b. Topgrade (Claude Code / Claude Code Plugins / OpenCode) ────────
    # Added 2026-09-03. apt/snap/firmware/docker are already handled above and
    # in the Managed App Auto-Update / Docker Image Updates steps below with
    # their own careful logic (NOPASSWD sudoers, per-container verify+rollback),
    # so those 4 steps are disabled in ~/.config/topgrade.toml to avoid
    # duplicate/conflicting work (topgrade's blind `docker pull` would also
    # 401 on the 2 locally-built images, chrome and caddy). Topgrade here only
    # covers what nothing else does: Claude Code + its plugins, and OpenCode.
    banner "Topgrade (Claude Code / OpenCode)"
    if command -v topgrade &>/dev/null; then
        topgrade -y --notify-end never 2>&1 | tee -a "$LOG"
        log "  [OK]   topgrade completed"
    else
        log "  [SKIP] topgrade not installed"
    fi
else
    log "Skipping system updates (--quick)"
fi

# ─── 11a. Managed app auto-update (pull, verify, auto-rollback) ──────────────
# Added 2026-07-23 after manually updating Radarr to 6.3.0 and hand-verifying
# it. Covers Sonarr/Radarr/Prowlarr/qBittorrent/Overseerr/Jellyseerr/Plex/
# Jellyfin: pulls each image, and ONLY if a new one actually landed, recreates
# the container and verifies it's genuinely healthy (arr health API / HTTP /
# Seerr settings.json / Docker healthcheck depending on the app). A failed
# verification auto-rolls-back to the previous image (+ config backup for the
# small config apps — Plex/Jellyfin configs are 17-18GB, too big to tar every
# run, so those two roll back on image only, backed by their own Docker
# healthcheck). Failures land as WARNINGs in this log — there's no push
# notification wired up, so check the log after a rollback fires.
# These 8 containers are handled here, NOT by the blind pull in step 11 below
# (a later blind `compose pull` would re-fetch the same broken image from the
# registry and silently undo a rollback within the same run).
banner "Managed App Auto-Update"
if ! $QUICK; then
    if [[ -f "$ROOT/.local/bin/media-stack-auto-update.py" ]]; then
        python3 "$ROOT/.local/bin/media-stack-auto-update.py" 2>&1 | tee -a "$LOG"
    else
        log "  [SKIP] media-stack-auto-update.py not found"
    fi
else
    log "  Skipping (--quick)"
fi

# ─── 11. Docker image pulls (everything else) ────────────────────────────────
# sonarr/radarr/prowlarr/qbittorrent/jellyseerr (vpn-stack) and
# plex/overseerr/jellyfin (media-stack) are excluded — step 11a above owns them.
if ! $QUICK; then
    banner "Docker Image Updates"
    declare -A STACK_SERVICES=(
        ["$ROOT/docker/media-stack"]="immich-redis immich-postgres immich-server immich-machine-learning portainer tugtainer watchstate caddy"
        ["$ROOT/docker/vpn-stack"]="gluetun chrome flaresolverr"
    )
    for COMPOSE_DIR in "${!STACK_SERVICES[@]}"; do
        SERVICES="${STACK_SERVICES[$COMPOSE_DIR]}"
        if [[ ! -d "$COMPOSE_DIR" ]]; then
            log "  [SKIP] $COMPOSE_DIR not found"
            continue
        fi
        log "  Pulling images: $COMPOSE_DIR ($SERVICES)"
        if ! cd "$COMPOSE_DIR"; then
            warn "  cannot cd into $COMPOSE_DIR — skipping"
            continue
        fi
        if docker compose pull $SERVICES 2>&1 | tee -a "$LOG"; then
            log "  [OK]   Pull succeeded: $COMPOSE_DIR"
        else
            warn "  Pull had errors in $COMPOSE_DIR — attempting up anyway"
        fi
        # NO --remove-orphans here: it is evaluated against the *named* subset,
        # so it deletes every container in the compose file that is not listed
        # in $SERVICES. On 2026-08-04 that destroyed sonarr/radarr/prowlarr/
        # qbittorrent/jellyseerr (defined in vpn-stack but owned by step 11a),
        # taking the download pipeline down for 2 days.
        # gluetun owns the network namespace that sonarr/radarr/prowlarr/
        # qbittorrent/jellyseerr/chrome/flaresolverr all join via
        # network_mode: service:gluetun. Recreating gluetun alone destroys that
        # namespace and leaves the siblings running with no network at all —
        # they must be recreated in the SAME command. Until 2026-08-09 this was
        # latent: `compose pull` always died on the un-pullable local chrome
        # image, so gluetun's image never actually changed and `up -d` never
        # recreated it. With the pull fixed, gluetun does update, so the
        # siblings have to come along.
        UP_SERVICES="$SERVICES"
        if [[ "$COMPOSE_DIR" == "$ROOT/docker/vpn-stack" ]]; then
            RUNNING_IMG=$(docker inspect gluetun --format '{{.Image}}' 2>/dev/null)
            LATEST_IMG=$(docker image inspect qmcgaw/gluetun --format '{{.Id}}' 2>/dev/null)
            if [[ -n "$RUNNING_IMG" && -n "$LATEST_IMG" && "$RUNNING_IMG" != "$LATEST_IMG" ]]; then
                UP_SERVICES="$SERVICES sonarr radarr prowlarr qbittorrent jellyseerr"
                log "  gluetun image changed — recreating all netns members together"
                if docker compose up -d --force-recreate $UP_SERVICES 2>&1 | tee -a "$LOG"; then
                    log "  [OK]   Compose up (gluetun netns): $COMPOSE_DIR"
                else
                    warn "  Compose up had errors in $COMPOSE_DIR"
                fi
                continue
            fi
        fi
        if docker compose up -d $UP_SERVICES 2>&1 | tee -a "$LOG"; then
            log "  [OK]   Compose up: $COMPOSE_DIR"
        else
            warn "  Compose up had errors in $COMPOSE_DIR"
        fi
    done
else
    log "Skipping Docker updates (--quick)"
fi

banner "Daily Routine complete"
log "Log saved to: $LOG"
