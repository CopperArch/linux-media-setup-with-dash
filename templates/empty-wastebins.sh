#!/bin/bash
# empty-wastebins.sh — force-empty every trash/recycle bin on this box, immediately.
#   Runs hourly from cron. No retention window: anything sitting in any of
#   these bins is gone within the hour it lands there.
set -uo pipefail

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

LOG="{{HOME}}/.hermes/maintenance-logs/empty-wastebins-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$(dirname "$LOG")"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# ─── 1. System (freedesktop) trash ───────────────────────────────────────────
TRASH_DIR="{{HOME}}/.local/share/Trash"
if [[ -d "$TRASH_DIR" ]]; then
    rm -rf "${TRASH_DIR:?}"/files/* "${TRASH_DIR:?}"/info/* 2>/dev/null
    log "System trash emptied"
else
    log "System trash dir not present, skipping"
fi

# ─── 2. Nextcloud trash bin ──────────────────────────────────────────────────
# Force retention to 0/0 so trashbin:expire purges everything regardless of
# how old it is, for every user.
docker exec -u www-data nextcloud php occ config:app:set files_trashbin trashbin_retention_obligation --value="0, 0" >/dev/null 2>&1
if docker exec -u www-data nextcloud php occ trashbin:expire >>"$LOG" 2>&1; then
    log "Nextcloud trash emptied"
else
    log "WARNING: Nextcloud trashbin:expire failed"
fi

# ─── 3. Immich trash ──────────────────────────────────────────────────────────
IMMICH_KEY_FILE="{{HOME}}/.hermes/immich-api-key"
if [[ -f "$IMMICH_KEY_FILE" ]]; then
    IMMICH_KEY=$(cat "$IMMICH_KEY_FILE")
    CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
        -H "x-api-key: $IMMICH_KEY" http://localhost:2283/api/trash/empty --data-raw '')
    if [[ "$CODE" == "200" ]]; then
        log "Immich trash emptied"
    else
        log "WARNING: Immich trash empty call returned HTTP $CODE"
    fi
else
    log "WARNING: Immich API key file missing at $IMMICH_KEY_FILE, skipping"
fi

log "Done"
