#!/bin/bash
# status-dashboard-install.sh — idempotent installer for the desktop status
# dashboard. Safe to run on every boot / from daily-routine.sh: it only writes
# what is missing or out of date, then makes sure the services are enabled.
#
# The point of this script is portability. Restore System-Recovery/ onto a
# fresh Linux box, run daily-routine.sh, and the dashboard comes back by
# itself — units, KWin rule, services and all.
#
#   status-dashboard-install.sh           # install/repair, then ensure running
#   status-dashboard-install.sh --check   # report only, change nothing
set -uo pipefail

BIN="$HOME/.local/bin"
SHARE="$HOME/.local/share/status-dashboard"
UNITS="$HOME/.config/systemd/user"
PAGE="$SHARE/index.html"
CHECK=false
[[ "${1:-}" == "--check" ]] && CHECK=true

say() { echo "  $*"; }
changed=0

# ── prerequisites ───────────────────────────────────────────────────────────
BROWSER=""
for b in google-chrome google-chrome-stable chromium chromium-browser \
         brave-browser microsoft-edge; do
    if command -v "$b" >/dev/null 2>&1; then BROWSER="$(command -v $b)"; break; fi
done
if [[ -z "$BROWSER" ]]; then
    say "[FAIL] no Chromium-family browser found — dashboard cannot be displayed"
    say "       install one of: google-chrome, chromium, brave-browser"
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    say "[FAIL] python3 missing — the collector and server both need it"
    exit 1
fi

for f in "$BIN/status-collect.py" "$BIN/status-dashboard-server.py" \
         "$BIN/status-dashboard-run.sh"; do
    [[ -f "$f" ]] || { say "[FAIL] missing $f (restore it from System-Recovery/scripts/)"; exit 1; }
done

if [[ ! -f "$PAGE" ]]; then
    # Try the places a restore might have left it before giving up.
    for cand in "$HOME/System-Recovery/desktop-dashboard/index.html" \
                "$HOME/Nextcloud/System-Recovery/desktop-dashboard/index.html" \
                "/usr/local/share/status-dashboard/index.html"; do
        [[ -f "$cand" ]] && { mkdir -p "$SHARE"; cp "$cand" "$PAGE"; changed=1
                              say "[FIX]  restored index.html from $cand"; break; }
    done
fi
[[ -f "$PAGE" ]] || { say "[FAIL] $PAGE missing (restore System-Recovery/desktop-dashboard/index.html)"; exit 1; }

# Screen size, so the window matches the display rather than assuming 1080p.
GEO="1920,1080"
if command -v kscreen-doctor >/dev/null 2>&1; then
    G=$(kscreen-doctor -o 2>/dev/null | grep -oP 'Geometry:\s*\d+,\d+ \K\d+x\d+' | head -1)
    [[ -n "${G:-}" ]] && GEO="${G/x/,}"
elif command -v xrandr >/dev/null 2>&1; then
    G=$(xrandr 2>/dev/null | grep -oP '\d+x\d+(?=\+)' | head -1)
    [[ -n "${G:-}" ]] && GEO="${G/x/,}"
fi

$CHECK && { say "[OK]   browser=$BROWSER geometry=$GEO page=$PAGE"; }

# ── unit files ──────────────────────────────────────────────────────────────
write_unit() {         # write_unit <name> <content>
    local name="$1" content="$2" path="$UNITS/$1"
    # Compare through command substitution on BOTH sides: it strips trailing
    # newlines, so the heredoc's trailing \n does not make every run look
    # like a change (which would rewrite units and log [FIX] forever).
    if [[ -f "$path" ]] && [[ "$(cat "$path")" == "$(printf '%s' "$content")" ]]; then
        return 0
    fi
    $CHECK && { say "[WARN] $name would be updated"; return 0; }
    mkdir -p "$UNITS"
    printf '%s' "$content" > "$path"
    changed=1
    say "[FIX]  wrote $name"
}

write_unit "status-collect.service" "[Unit]
Description=Collect host/container status for the desktop dashboard

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 %h/.local/bin/status-collect.py
Nice=10
IOSchedulingClass=idle
"

write_unit "status-collect.timer" "[Unit]
Description=Refresh the desktop status dashboard data

[Timer]
OnBootSec=45s
OnUnitActiveSec=60s
AccuracySec=5s
Unit=status-collect.service

[Install]
WantedBy=timers.target
"

write_unit "status-dashboard-server.service" "[Unit]
Description=Local HTTP server for the desktop status dashboard
After=network.target

[Service]
# Loopback only — this page exposes host detail and must not reach the LAN.
ExecStart=/usr/bin/env python3 %h/.local/bin/status-dashboard-server.py
Restart=always
RestartSec=3
Nice=10

[Install]
WantedBy=default.target
"

write_unit "status-dashboard.service" "[Unit]
Description=System status dashboard on the desktop background layer
After=graphical-session.target status-dashboard-server.service
Wants=status-dashboard-server.service
PartOf=graphical-session.target

[Service]
Type=simple
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 60); do curl -sf -o /dev/null http://127.0.0.1:8099/index.html && exit 0; sleep 1; done; exit 1'
ExecStartPre=-%h/.local/bin/status-dashboard-show.sh --rule-only
# Not the browser directly: Chrome moves its browser process into a transient
# scope of its own, which drops it out of this cgroup and makes systemd think
# the unit died — Restart=always then loops forever while an orphaned window
# nobody owns sits on the desktop. The wrapper stays put and supervises it.
Environment=DASHBOARD_URL=http://127.0.0.1:8099/index.html
Environment=DASHBOARD_BROWSER=$BROWSER
Environment=DASHBOARD_GEOMETRY=$GEO
ExecStart=%h/.local/bin/status-dashboard-run.sh
Restart=always
RestartSec=5
KillMode=mixed
TimeoutStopSec=15

[Install]
WantedBy=graphical-session.target
"

# Terminal pane. ttyd is a single static binary kept in ~/.local/bin;
# if it is missing the dashboard still works, the pane just stays empty.
if [[ -x "$BIN/ttyd" ]]; then
    write_unit "dashboard-terminal.service" "[Unit]
Description=Terminal bridge for the dashboard's Claude Code pane
After=default.target

[Service]
# Loopback only: --writable grants a real shell, so this must never be
# reachable from the LAN. Same trust boundary as the dashboard's repair API.
# --check-origin refuses WebSocket upgrades whose Origin host differs from
# the Host header — without it, any web page the user has open could drive
# this shell from the browser (WebSockets are not CORS-governed).
ExecStart=%h/.local/bin/ttyd \\
  --port 7682 \\
  --interface 127.0.0.1 \\
  --writable \\
  --check-origin \\
  --url-arg \\
  --max-clients 2 \\
  --client-option fontSize=13 \\
  --client-option 'theme={\\\"background\\\":\\\"#292c33\\\",\\\"foreground\\\":\\\"#ffffff\\\",\\\"cursor\\\":\\\"#ffffff\\\",\\\"cursorAccent\\\":\\\"#363a43\\\",\\\"selectionBackground\\\":\\\"#40444c\\\"}' \\
  --client-option fontFamily='JetBrains Mono,DejaVu Sans Mono,monospace' \\
  --client-option cursorBlink=true \\
  --client-option titleFixed='Dashboard' \\
  %h/.local/bin/dashboard-pane.sh
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"
else
    say "[SKIP] ttyd not in ~/.local/bin — terminal pane disabled"
    say "       get it: curl -sL -o ~/.local/bin/ttyd \\"
    say "         https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 && chmod +x ~/.local/bin/ttyd"
fi

$CHECK && exit 0

# An old autostart entry would double-launch alongside the service.
if [[ -f "$HOME/.config/autostart/status-dashboard.desktop" ]]; then
    rm -f "$HOME/.config/autostart/status-dashboard.desktop"
    say "[FIX]  removed superseded autostart .desktop"
    changed=1
fi

# ── enable ──────────────────────────────────────────────────────────────────
systemctl --user daemon-reload 2>/dev/null

UNITS_TO_RUN=(status-dashboard-server.service status-collect.timer)
[[ -x "$BIN/ttyd" ]] && UNITS_TO_RUN+=(dashboard-terminal.service)
for u in "${UNITS_TO_RUN[@]}"; do
    if ! systemctl --user is-enabled "$u" >/dev/null 2>&1; then
        systemctl --user enable "$u" >/dev/null 2>&1 && say "[FIX]  enabled $u"
        changed=1
    fi
    systemctl --user is-active "$u" >/dev/null 2>&1 || {
        systemctl --user start "$u" >/dev/null 2>&1 && say "[FIX]  started $u"; changed=1; }
done

# The window itself only makes sense inside a graphical session; on a headless
# boot the collector and server still run, and this unit waits for the session.
if ! systemctl --user is-enabled status-dashboard.service >/dev/null 2>&1; then
    systemctl --user enable status-dashboard.service >/dev/null 2>&1 \
        && say "[FIX]  enabled status-dashboard.service"
    changed=1
fi
if [[ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]] \
   && ! systemctl --user is-active status-dashboard.service >/dev/null 2>&1; then
    systemctl --user start status-dashboard.service >/dev/null 2>&1 \
        && say "[FIX]  started status-dashboard.service"
    changed=1
fi

# KWin rule (KDE only; harmless no-op elsewhere).
if command -v kwriteconfig6 >/dev/null 2>&1 || command -v kwriteconfig5 >/dev/null 2>&1; then
    "$BIN/status-dashboard-show.sh" --rule-only >/dev/null 2>&1 \
        && say "[OK]   KWin below-layer rule present"
else
    say "[SKIP] not KDE — set the dashboard window 'always on bottom' in your WM"
fi

if [[ $changed -eq 0 ]]; then
    say "[OK]   dashboard already installed and running"
else
    say "[OK]   dashboard install/repair complete"
fi
