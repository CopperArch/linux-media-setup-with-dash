#!/bin/bash
# status-dashboard-run.sh — run the dashboard browser in the foreground so
# systemd can actually supervise it.
#
# Chrome moves its own browser process into a transient scope of its own
# (app-com.google.Chrome-<pid>.scope) when it starts under a user manager.
# That takes the browser out of the service's cgroup, systemd sees the unit's
# main process go away, and Restart=always relaunches it a few seconds later —
# at which point Chrome's process singleton finds the escaped browser, prints
# "Opening in existing browser session", hands the URL over and exits 0. The
# unit then loops forever at RestartSec while one orphaned window, which no
# unit owns any more, sits on the desktop.
#
# So the unit runs this instead of the browser. It launches Chrome, finds the
# real browser PID wherever Chrome has moved it, and blocks until that PID is
# gone, keeping a live process in the unit's cgroup for the whole session.
# Restart=always goes back to meaning "restart if the dashboard dies", and
# stopping the unit stops the browser with it (see the trap below — the
# browser is outside the cgroup, so KillMode cannot reach it).
set -uo pipefail

URL="${DASHBOARD_URL:-http://127.0.0.1:8099/index.html}"
GEOMETRY="${DASHBOARD_GEOMETRY:-1920,1080}"
PROFILE="$HOME/.local/share/status-dashboard/chrome-profile"

BROWSER="${DASHBOARD_BROWSER:-}"
if [[ -z "$BROWSER" ]]; then
    for b in google-chrome google-chrome-stable chromium chromium-browser \
             brave-browser microsoft-edge; do
        if command -v "$b" >/dev/null 2>&1; then BROWSER="$(command -v $b)"; break; fi
    done
fi
[[ -n "$BROWSER" ]] || { echo "no Chromium-family browser found" >&2; exit 1; }

# The browser process is the only one for this profile started without
# --type=; every other match is a zygote, renderer, GPU or utility child.
# argv[0] has to be a browser binary as well, because pgrep happily matches any
# process that merely mentions the profile path on its command line — a shell
# running pkill/pgrep against it, this script's own callers, an admin's grep.
browser_pid() {
    local pid argv0
    for pid in $(pgrep -f -- "--user-data-dir=$PROFILE" 2>/dev/null); do
        grep -qa -- '--type=' "/proc/$pid/cmdline" 2>/dev/null && continue
        # argv is NUL-separated and bash cannot hold a NUL, hence tr. The
        # browser rewrites its process title into a SINGLE argv entry holding
        # the whole command line, so cut at the first space too — otherwise
        # "basename" returns the tail of the last path in the arguments.
        argv0=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | head -1)
        argv0=${argv0%% *}
        [[ "${argv0##*/}" == *chrom* || "${argv0##*/}" == *brave* \
           || "${argv0##*/}" == *edge* ]] || continue
        echo "$pid"
        return 0
    done
    return 1
}

# Wait up to $2 tenths of a second for browser_pid to (dis)appear.
wait_for_browser() {   # wait_for_browser gone|alive <tries>
    local want="$1" tries="$2"
    while (( tries-- > 0 )); do
        if [[ "$want" == alive ]]; then browser_pid >/dev/null && return 0
        else browser_pid >/dev/null || return 0; fi
        sleep 0.5
    done
    return 1
}

# A browser left over from an earlier launch would swallow this one through the
# process singleton and leave the unit supervising nothing, so clear it first.
if stale=$(browser_pid); then
    kill "$stale" 2>/dev/null
    wait_for_browser gone 20 || { stale=$(browser_pid) && kill -9 "$stale" 2>/dev/null; }
    wait_for_browser gone 10
fi

cleanup() {
    trap - TERM INT EXIT
    local pid
    pid=$(browser_pid) && kill "$pid" 2>/dev/null
    exit 0
}
trap cleanup TERM INT EXIT

mkdir -p "$PROFILE"

"$BROWSER" \
    --app="$URL" \
    --user-data-dir="$PROFILE" \
    --class=status-dashboard \
    --window-position=0,0 \
    --window-size="$GEOMETRY" \
    --start-maximized \
    --no-first-run \
    --no-default-browser-check \
    --password-store=basic \
    --disable-features=TranslateUI,InfiniteSessionRestore \
    --disable-session-crashed-bubble \
    --hide-crash-restore-bubble \
    --disable-infobars \
    --noerrdialogs \
    >/dev/null 2>&1 &

if ! wait_for_browser alive 40; then
    echo "browser never came up for $URL" >&2
    exit 1
fi

pid=$(browser_pid)
echo "dashboard running — $URL (browser pid $pid)"

# Block for the browser's lifetime. It is usually not our child (Chrome
# re-parents it into its own scope), so poll instead of wait.
while kill -0 "$pid" 2>/dev/null; do
    sleep 5
done

echo "dashboard browser exited — letting systemd restart it"
