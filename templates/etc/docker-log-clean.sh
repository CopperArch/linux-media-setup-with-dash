#!/usr/bin/env bash
# /usr/local/sbin/docker-log-clean.sh
#
# Docker log-rotation guard. Runs nightly from daily-routine.sh §9 (Cleanup) via
# a narrow NOPASSWD rule: `sudo -n /usr/local/sbin/docker-log-clean.sh`.
#
# Background: this box filled its 100G root disk twice (2026-07-10, 2026-07-11).
# The first was unbounded Docker json-file logs — no /etc/docker/daemon.json at
# all, so all 23 containers logged without a cap. Fix was a 10m x 3 rotation
# policy. This script exists so that policy can never silently disappear again.
#
# It guards TWO layers, because the policy alone is not sufficient:
#
#   1. /etc/docker/daemon.json still carries the rotation policy.
#      Self-heals (merge + restart Docker) only if drifted.
#
#   2. Every RUNNING container actually has a cap in effect.
#      log-opts are baked into a container at CREATION time — restarting the
#      daemon does NOT apply them retroactively. A container created before the
#      policy keeps an empty LogConfig and logs forever. On 2026-08-09 three
#      containers (overseerr, nextcloud-db, nextcloud-redis) were found in
#      exactly that state, 8 months of uptime after the policy was added, which
#      layer 1 alone would have happily reported as healthy. Such containers
#      need RECREATING (restart is not enough) and that is the user's call, so
#      this only reports them — with one exception, below.
#
# It does NOT routinely truncate logs: the 30MB/container cap is what does the
# capping, and logs are kept for debugging (deliberate choice, 2026-07-10). The
# single exception is the emergency valve at EMERGENCY_MB, which only ever
# triggers for an UNCAPPED container that is actively threatening the root disk
# — i.e. the precise disaster this script exists to prevent.
#
# Always exits 0 on a healthy or self-healed run; non-zero only on real failure.

set -uo pipefail

DAEMON_JSON="/etc/docker/daemon.json"
WANT_MAX_SIZE="10m"
WANT_MAX_FILE="3"
EMERGENCY_MB=500          # truncate an UNCAPPED container's log above this
COMPOSE_VPN="{{HOME}}/docker/vpn-stack"

# Containers sharing gluetun's network namespace (network_mode: service:gluetun).
# Needed only for the post-restart sanity check below.
NETNS_MEMBERS=(gluetun qbittorrent radarr sonarr prowlarr flaresolverr jellyseerr chrome)

RC=0
log()  { echo "    $*"; }
warn() { echo "    [WARN] $*"; RC=1; }

if [[ $EUID -ne 0 ]]; then
    echo "    [FAIL] must run as root" >&2
    exit 2
fi

# ── Layer 1: the daemon-wide policy ────────────────────────────────────────
# Merged with python3 rather than overwritten, so any unrelated keys survive.
# This matters more than it looks: the volumes dir was relocated to disk3 on
# this box, and blindly rewriting a daemon.json that had grown a "data-root"
# key would point Docker at an empty directory and orphan every volume.
policy_ok() {
    python3 - "$DAEMON_JSON" "$WANT_MAX_SIZE" "$WANT_MAX_FILE" <<'PY'
import json, sys
path, want_size, want_file = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as fh:
        cfg = json.load(fh)
except Exception:
    sys.exit(1)
opts = cfg.get("log-opts") or {}
ok = (cfg.get("log-driver") == "json-file"
      and str(opts.get("max-size")) == want_size
      and str(opts.get("max-file")) == want_file)
sys.exit(0 if ok else 1)
PY
}

heal_policy() {
    python3 - "$DAEMON_JSON" "$WANT_MAX_SIZE" "$WANT_MAX_FILE" <<'PY'
import json, os, sys
path, want_size, want_file = sys.argv[1], sys.argv[2], sys.argv[3]
raw = None
try:
    with open(path) as fh:
        raw = fh.read()
    cfg = json.loads(raw)
    if not isinstance(cfg, dict):
        cfg = {}
except FileNotFoundError:
    cfg = {}
except Exception:
    # Corrupt/unparseable: start clean rather than leave Docker with a file it
    # will refuse to boot on. The original is preserved in the backup below.
    cfg = {}
if raw is not None:
    os.makedirs("/var/backups", exist_ok=True)
    with open("/var/backups/daemon.json.bak", "w") as bak:
        bak.write(raw)
cfg["log-driver"] = "json-file"
opts = cfg.get("log-opts")
if not isinstance(opts, dict):
    opts = {}
opts["max-size"] = want_size
opts["max-file"] = want_file
cfg["log-opts"] = opts
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
PY
}

log "Docker log rotation guard:"

if policy_ok; then
    log "  [OK]   daemon.json policy intact (${WANT_MAX_SIZE} x ${WANT_MAX_FILE})"
else
    warn "daemon.json rotation policy MISSING/DRIFTED — repairing"
    if ! heal_policy; then
        echo "    [FAIL] could not rewrite $DAEMON_JSON" >&2
        exit 2
    fi
    # A full restart is required. `systemctl reload docker` silently does NOT
    # pick up log-opts — verified on this box 2026-07-10 with a log-spam test.
    log "  restarting Docker to apply (reload is not enough for log-opts)"
    if systemctl restart docker; then
        for _ in $(seq 1 30); do
            docker info >/dev/null 2>&1 && break
            sleep 2
        done
        if docker info >/dev/null 2>&1; then
            log "  [FIX ] policy restored and Docker restarted"
        else
            echo "    [FAIL] Docker did not come back after restart" >&2
            exit 2
        fi
        # A daemon restart restarts containers in place (IDs are preserved, so
        # gluetun's netns is not recreated and dependents normally reattach
        # fine). Still worth confirming, because a vpn-stack member that fails
        # to come back leaves the download pipeline silently dead — that class
        # of failure has twice gone unnoticed here for days.
        DOWN=()
        for c in "${NETNS_MEMBERS[@]}"; do
            docker inspect "$c" >/dev/null 2>&1 || continue
            state=$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)
            [[ "$state" == "true" ]] || DOWN+=("$c")
        done
        if (( ${#DOWN[@]} )); then
            warn "vpn-stack members down after restart: ${DOWN[*]} — recreating the whole netns group"
            # Whole group together: recreating gluetun alone gives it a fresh
            # namespace and strips networking from every sibling.
            if docker compose -f "$COMPOSE_VPN/docker-compose.yml" \
                 --project-directory "$COMPOSE_VPN" \
                 up -d --force-recreate "${NETNS_MEMBERS[@]}"; then
                log "  [FIX ] vpn-stack netns group recreated"
            else
                warn "could not recreate vpn-stack netns group — check it by hand"
            fi
        fi
    else
        echo "    [FAIL] systemctl restart docker failed" >&2
        exit 2
    fi
fi

# ── Layer 2: containers that predate the policy ────────────────────────────
UNCAPPED=()
while read -r name; do
    [[ -n "$name" ]] || continue
    size=$(docker inspect -f '{{index .HostConfig.LogConfig.Config "max-size"}}' \
           "$name" 2>/dev/null)
    [[ -n "$size" && "$size" != "<no value>" ]] || UNCAPPED+=("$name")
done < <(docker ps --format '{{.Names}}')

if (( ${#UNCAPPED[@]} == 0 )); then
    log "  [OK]   all running containers have a log cap in effect"
else
    warn "${#UNCAPPED[@]} container(s) logging WITHOUT a cap (predate the policy; need RECREATING, not restarting): ${UNCAPPED[*]}"
    for name in "${UNCAPPED[@]}"; do
        path=$(docker inspect -f '{{.LogPath}}' "$name" 2>/dev/null)
        [[ -n "$path" && -f "$path" ]] || continue
        mb=$(( $(stat -c %s "$path") / 1024 / 1024 ))
        if (( mb >= EMERGENCY_MB )); then
            # Emergency valve only. Truncating loses history, but an uncapped
            # log at this size is on a direct path to a full root disk, and a
            # full root disk takes every service on the box down with it.
            : > "$path"
            warn "EMERGENCY: truncated ${name} log at ${mb}MB (uncapped, >= ${EMERGENCY_MB}MB)"
        else
            log "        ${name}: ${mb}MB and growing unbounded"
        fi
    done
fi

exit $RC
