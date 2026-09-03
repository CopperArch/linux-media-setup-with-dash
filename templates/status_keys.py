"""Shared config loading for the status dashboard scripts.

Both status-collect.py and status-dashboard-server.py live in ~/.local/bin and
are run as plain scripts, so this module is importable from either (Python puts
the script's own directory first on sys.path). Credentials live in
~/.config/status-dashboard/media-keys.env rather than in the scripts, which are
world/group readable; the netns group lives in netns-group.json so the collector
and the repair server cannot drift apart.
"""
import json
from pathlib import Path

CONF_DIR = Path.home() / ".config/status-dashboard"
KEYS_FILE = CONF_DIR / "media-keys.env"
NETNS_FILE = CONF_DIR / "netns-group.json"

DEFAULT_NETNS = {"hub": "gluetun",
                 "siblings": ["qbittorrent", "prowlarr", "radarr", "sonarr",
                              "flaresolverr", "chrome", "jellyseerr"]}


def load_env(path=KEYS_FILE):
    """Parse a simple KEY=value file into a dict ('#' comments allowed)."""
    env = {}
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def load_netns():
    """(hub, siblings) containers sharing the VPN hub's network namespace.

    Falls back to the built-in list when the shared config file is missing or
    malformed, so a broken file degrades to the previous hardcoded behaviour
    instead of disabling the binding check."""
    try:
        data = json.loads(NETNS_FILE.read_text())
        hub, siblings = data.get("hub"), data.get("siblings")
        if (isinstance(hub, str) and hub
                and isinstance(siblings, list) and siblings
                and all(isinstance(s, str) and s for s in siblings)):
            return hub, [str(s) for s in siblings]
    except Exception:  # noqa: BLE001 — missing/corrupt file just means defaults
        pass
    return DEFAULT_NETNS["hub"], list(DEFAULT_NETNS["siblings"])
