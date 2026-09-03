#!/bin/sh
# Hand a magnet URI to qBittorrent's WebUI API.
# Chrome invokes this via xdg-open -> magnet-handler.desktop.
# qBittorrent shares this container's network namespace (gluetun), so it is on localhost.
MAGNET="$1"
QBT_URL="http://localhost:8080"
COOKIE_JAR="/tmp/.qbt-cookie-$(id -u)"

[ -z "$MAGNET" ] && exit 0

add_magnet() {
  curl -s -o /dev/null -w "%{http_code}" -b "$COOKIE_JAR" -X POST "$QBT_URL/api/v2/torrents/add" \
    -H "Referer: $QBT_URL" \
    -F "urls=$MAGNET"
}

# The session cookie is usually still valid; only log in when the add is rejected.
RESP=$(add_magnet)
if [ "$RESP" != "200" ]; then
  curl -s -c "$COOKIE_JAR" -X POST "$QBT_URL/api/v2/auth/login" \
    -H "Referer: $QBT_URL" \
    --data "username=${QBT_USER}&password=${QBT_PASS}" >/dev/null
  RESP=$(add_magnet)
fi

if [ "$RESP" = "200" ]; then
  notify-send -t 4000 "qBittorrent" "Magnet added to download queue" 2>/dev/null
else
  notify-send -t 6000 -u critical "qBittorrent" "Magnet FAILED (HTTP $RESP)" 2>/dev/null
fi
exit 0
