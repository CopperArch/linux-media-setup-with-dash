#!/bin/bash
# status-dashboard-show.sh — put the system status dashboard on the desktop.
#
# It is a Chrome app window, not a wallpaper image: wallpapers cannot scroll,
# and the dashboard needs each panel to scroll independently. A KWin rule
# (installed below) forces the window borderless, full screen, on every desktop
# and *below* everything else, so it behaves like a live background — normal
# windows cover it, and "Show Desktop" (Meta+D) reveals it.
#
#   status-dashboard-show.sh          # start it (no-op if already running)
#   status-dashboard-show.sh --restart
#   status-dashboard-show.sh --stop
set -uo pipefail

URL="http://127.0.0.1:8099/index.html"
PROFILE="$HOME/.local/share/status-dashboard/chrome-profile"
TITLE_MATCH="system status"
# Wayland app_id | X11 --class value. Unescaped dots are deliberate: KConfig
# mangles backslashes in values, and "." matching any char is harmless here.
CLASS_MATCH="chrome-127.0.0.1__index.html-Default|status-dashboard"
RULES="$HOME/.config/kwinrulesrc"
RULE_ID="statusdashboard"

stop_dashboard() {
    # Stop the unit first where there is one: killing only the browser leaves
    # Restart=always to bring it straight back a few seconds later.
    systemctl --user cat status-dashboard.service >/dev/null 2>&1 \
        && systemctl --user stop status-dashboard.service 2>/dev/null
    pkill -f "user-data-dir=$PROFILE" 2>/dev/null
    echo "dashboard stopped"
}

install_kwin_rule() {
    # Match on the page title *and* the window class. Title alone is not enough:
    # any window whose title happens to contain "system status" — a status page
    # opened in a normal browser, say — would get shoved behind everything and
    # hidden from the taskbar, which looks exactly like a broken browser.
    # The class is regex-matched because Chrome reports it differently per
    # backend: the app_id on Wayland, the --class value on X11.
    if ! grep -q "^\[$RULE_ID\]" "$RULES" 2>/dev/null; then
        # 2 = Force (SetRule); 2 = SubstringMatch, 3 = RegExpMatch (StringMatch)
        cat >> "$RULES" <<EOF

[$RULE_ID]
Description=System status dashboard (desktop background layer)
title=$TITLE_MATCH
titlematch=2
types=1
wmclass=$CLASS_MATCH
wmclasscomplete=false
wmclassmatch=3
below=true
belowrule=2
noborder=true
noborderrule=2
skiptaskbar=true
skiptaskbarrule=2
skippager=true
skippagerrule=2
skipswitcher=true
skipswitcherrule=2
placement=4
placementrule=2
maximizehoriz=true
maximizehorizrule=2
maximizevert=true
maximizevertrule=2
EOF
        # Register the rule in [General] without clobbering rules already there.
        python3 - "$RULES" "$RULE_ID" <<'PY'
import re, sys
path, rid = sys.argv[1], sys.argv[2]
text = open(path).read()
m = re.search(r"^\[General\]\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
if m:
    block = m.group(1)
    ids = re.search(r"^rules=(.*)$", block, re.M)
    have = [x for x in (ids.group(1).split(",") if ids else []) if x]
    if rid not in have:
        have.append(rid)
    new = re.sub(r"^rules=.*$", "rules=" + ",".join(have), block, flags=re.M) \
        if ids else block.rstrip("\n") + "\nrules=" + ",".join(have) + "\n"
    new = re.sub(r"^count=.*$", f"count={len(have)}", new, flags=re.M) \
        if re.search(r"^count=", new, re.M) else f"count={len(have)}\n" + new
    text = text[:m.start(1)] + new + text[m.end(1):]
else:
    text = f"[General]\ncount=1\nrules={rid}\n" + text
open(path, "w").write(text)
PY
        echo "installed KWin rule [$RULE_ID]"
    fi
    qdbus6 org.kde.KWin /KWin reconfigure 2>/dev/null \
        || qdbus org.kde.KWin /KWin reconfigure 2>/dev/null
}

case "${1:-}" in
    --stop)      stop_dashboard; exit 0 ;;
    --restart)   stop_dashboard; sleep 1 ;;
    # Install/refresh the KWin rule and exit — used by the systemd unit, which
    # launches Chrome itself so that Restart=always actually supervises it.
    --rule-only) install_kwin_rule; exit 0 ;;
esac

# Prefer the systemd unit where there is one, so the browser always has exactly
# one owner. Launching it from here as well leaves a copy systemd cannot
# supervise or stop, and Chrome's process singleton then quietly redirects the
# unit's own launch into that orphan.
if systemctl --user cat status-dashboard.service >/dev/null 2>&1; then
    install_kwin_rule
    systemctl --user restart status-dashboard.service
    echo "dashboard (re)started via systemd — $URL"
    exit 0
fi

if pgrep -f "user-data-dir=$PROFILE" >/dev/null; then
    echo "dashboard already running (use --restart to reload)"
    exit 0
fi

# Wait for the local server; on a cold boot the autostart can beat it.
for _ in $(seq 1 30); do
    curl -sf -o /dev/null "$URL" && break
    sleep 1
done

install_kwin_rule

mkdir -p "$PROFILE"
setsid google-chrome \
    --app="$URL" \
    --user-data-dir="$PROFILE" \
    --class=status-dashboard \
    --window-position=0,0 \
    --window-size=1920,1080 \
    --start-maximized \
    --no-first-run \
    --no-default-browser-check \
    --password-store=basic \
    --disable-features=TranslateUI,InfiniteSessionRestore \
    --disable-session-crashed-bubble \
    --hide-crash-restore-bubble \
    --disable-infobars \
    --noerrdialogs \
    >/dev/null 2>&1 < /dev/null &

echo "dashboard starting — $URL"
