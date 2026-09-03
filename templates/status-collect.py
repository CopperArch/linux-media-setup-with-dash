#!/usr/bin/env python3
"""
status-collect.py — gather full host + container state into one JSON blob for
the desktop status dashboard (see ~/.local/share/status-dashboard/index.html).

Every collector is independently guarded: a source that is down or slow shows
up as an "unavailable" panel instead of taking the whole dashboard with it.
Written for the homelab box; run it from the systemd user timer:

    systemctl --user start status-collect.service
"""
from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from status_keys import load_env, load_netns

OUT_DIR = Path.home() / ".local/share/status-dashboard"
OUT_FILE = OUT_DIR / "status.json"
LOG_DIR = Path.home() / ".hermes/maintenance-logs"

# Deployment-specific knobs, all in one place.
EXPECTED_VPN_COUNTRY = "Netherlands"
DAILY_ROUTINE_NEXT_RUN = "03:00 daily"
INTERESTING_MOUNTS = {"/", "{{MEDIA_POOL}}", "{{NEXTCLOUD_DATA}}",
                      "{{MEDIA_DISK1}}", "{{MEDIA_DISK2}}",
                      "{{MEDIA_DISK3}}", "/boot", "/boot/efi",
                      "/var/lib/docker"}

# ── service config ───────────────────────────────────────────────────────────
# Credentials live in ~/.config/status-dashboard/media-keys.env (chmod 600) so
# they are not baked into this group-readable script. See status-keys.py.
KEYS = load_env()
QBIT = {"url": "http://localhost:8080",
        "user": KEYS.get("QBIT_USER", "admin"),
        "pass": KEYS.get("QBIT_PASS", "")}
RADARR = {"url": "http://localhost:7878", "key": KEYS.get("RADARR_KEY", "")}
SONARR = {"url": "http://localhost:8989", "key": KEYS.get("SONARR_KEY", "")}
PROWLARR = {"url": "http://localhost:9696", "key": KEYS.get("PROWLARR_KEY", "")}
JELLYFIN = {"url": "http://localhost:8096", "key": KEYS.get("JELLYFIN_KEY", "")}
PLEX = {"url": "http://localhost:32400", "token": None}  # token read from Preferences.xml

# Endpoints probed for the "services" panel: (label, url, expected-ok codes)
SERVICE_PROBES = [
    ("qBittorrent", "http://localhost:8080", (200, 401, 403)),
    ("Prowlarr", "http://localhost:9696", (200, 401)),
    ("Radarr", "http://localhost:7878", (200, 401)),
    ("Sonarr", "http://localhost:8989", (200, 401)),
    ("Overseerr", "http://localhost:5055", (200, 307, 302)),
    ("Jellyseerr", "http://localhost:5056", (200, 307, 302)),
    ("Plex", "http://localhost:32400/identity", (200,)),
    ("Jellyfin", "http://localhost:8096/health", (200,)),
    ("Nextcloud", "http://localhost:8090/status.php", (200,)),
    ("Immich", "http://localhost:2283/api/server/ping", (200,)),
    ("Portainer", "http://localhost:9000", (200, 307, 302)),
    ("Tugtainer", "http://localhost:9412", (200, 302)),
    # Caddy binds the LAN IP for :80/:443 and only exposes a loopback alias
    # on :8095, so probe that rather than localhost:80 (which never listens).
    ("Caddy", "http://127.0.0.1:8095", (200, 301, 302, 308, 404, 502)),
]

# Containers that share the VPN hub's network namespace. Losing this binding
# is the classic post-reboot / post-recreate failure here, so it gets its own
# check. Kept in ~/.config/status-dashboard/netns-group.json, which
# status-dashboard-server.py also reads for its recreate fix — edit there, not
# in two places.
NETNS_HUB, NETNS_SIBLINGS = load_netns()

TIMEOUT = 8


# ── helpers ──────────────────────────────────────────────────────────────────
def run(cmd, timeout=15):
    """Run a command, return stdout ('' on any failure)."""
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return p.stdout
    except Exception:
        return ""


def http(url, headers=None, timeout=TIMEOUT, data=None, method=None):
    """Return (status_code, body_text). status None on connection failure."""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception:
        return None, ""


def http_json(url, headers=None, timeout=TIMEOUT):
    code, body = http(url, headers, timeout)
    if code != 200:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def guard(name, fn, *a, **kw):
    """Run a collector, converting any explosion into an error marker."""
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 - deliberately broad
        return {"error": f"{type(e).__name__}: {e}"}


def human(n, unit=1024):
    n = float(n or 0)
    for s in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < unit:
            return f"{n:.0f}{s}" if s in ("B", "K") else f"{n:.1f}{s}"
        n /= unit
    return f"{n:.1f}E"


# ── host ─────────────────────────────────────────────────────────────────────
def cpu_sample():
    """Per-core + aggregate CPU busy% over a short sampling window."""
    def read():
        stats = {}
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("cpu"):
                parts = line.split()
                if len(parts) < 5:
                    continue
                vals = [int(x) for x in parts[1:]]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                stats[parts[0]] = (sum(vals), idle)
            else:
                break
        return stats

    a = read()
    time.sleep(0.4)
    b = read()

    out = {}
    for k in b:
        if k not in a:
            continue
        dt = b[k][0] - a[k][0]
        di = b[k][1] - a[k][1]
        out[k] = round(100.0 * (dt - di) / dt, 1) if dt > 0 else 0.0
    cores = sorted((k for k in out if k != "cpu"),
                   key=lambda x: int(x[3:] or 0))
    return {"total": out.get("cpu", 0.0), "cores": [out[c] for c in cores]}


def collect_sensors():
    """Temperatures / fans / power from lm-sensors JSON."""
    raw = run(["sensors", "-j"], timeout=10)
    temps, fans = [], []
    if not raw:
        return {"temps": temps, "fans": fans}
    try:
        data = json.loads(raw)
    except Exception:
        return {"temps": temps, "fans": fans}

    for chip, entries in data.items():
        if not isinstance(entries, dict):
            continue
        for label, vals in entries.items():
            if not isinstance(vals, dict):
                continue
            for key, v in vals.items():
                if not isinstance(v, (int, float)):
                    continue
                if "_input" not in key:
                    continue
                crit = None
                for ck in ("_crit", "_max"):
                    for k2, v2 in vals.items():
                        if k2.endswith(ck) and isinstance(v2, (int, float)) and v2 > 0:
                            crit = v2
                            break
                    if crit:
                        break
                if key.startswith("temp"):
                    temps.append({"chip": chip.split("-")[0], "label": label,
                                  "value": round(v, 1), "crit": crit})
                elif key.startswith("fan") and v > 0:
                    fans.append({"chip": chip.split("-")[0], "label": label,
                                 "rpm": int(v)})
    # Keep the interesting ones first: package/edge temps before per-core.
    temps.sort(key=lambda t: (0 if re.search(r"pkg|package|edge|composite", t["label"], re.I)
                              else 1, t["label"]))
    return {"temps": temps, "fans": fans}


def collect_host():
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        mem[k] = int(v.strip().split()[0]) * 1024

    uptime = float(Path("/proc/uptime").read_text().split()[0])
    load = os.getloadavg()
    ncpu = os.cpu_count() or 1

    model = ""
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break

    reboot_required = Path("/var/run/reboot-required").exists()
    reboot_pkgs = []
    p = Path("/var/run/reboot-required.pkgs")
    if p.exists():
        reboot_pkgs = sorted(set(p.read_text().split()))

    failed_units = [l.split()[0] for l in
                    run(["systemctl", "--failed", "--no-legend", "--plain"]).splitlines()
                    if l.strip()]

    # Pending updates (apt's own cache; no network hit).
    upg = run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"], timeout=25)
    pending = [l.split()[1] for l in upg.splitlines() if l.startswith("Inst ")]
    security = [l for l in upg.splitlines()
                if l.startswith("Inst ") and re.search(r"-security", l)]

    total, used = mem.get("MemTotal", 0), 0
    avail = mem.get("MemAvailable", 0)
    used = total - avail

    return {
        "hostname": socket.gethostname(),
        "kernel": run(["uname", "-r"]).strip(),
        "os": (re.search(r'PRETTY_NAME="([^"]+)"',
                         Path("/etc/os-release").read_text()) or [None, "Linux"])[1],
        "uptime_seconds": int(uptime),
        "booted_at": datetime.fromtimestamp(time.time() - uptime).isoformat(timespec="seconds"),
        "load": {"1": load[0], "5": load[1], "15": load[2], "ncpu": ncpu},
        "cpu": {"model": model, "cores": ncpu, **cpu_sample()},
        "memory": {
            "total": total, "used": used, "available": avail,
            "cached": mem.get("Cached", 0), "buffers": mem.get("Buffers", 0),
            "percent": round(100.0 * used / total, 1) if total else 0,
            "swap_total": mem.get("SwapTotal", 0),
            "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
        },
        "sensors": collect_sensors(),
        "reboot_required": reboot_required,
        "reboot_packages": reboot_pkgs,
        "failed_units": failed_units,
        "updates_pending": len(pending),
        "updates_security": len(security),
        "update_packages": pending[:40],
    }


# ── disks ────────────────────────────────────────────────────────────────────
def collect_disks():
    """Filesystem usage, with mergerfs pool members broken out separately."""
    out, seen = [], set()
    raw = run(["df", "-B1", "--output=source,fstype,size,used,avail,pcent,target"],
              timeout=15)
    for line in raw.splitlines()[1:]:
        f = line.split(None, 6)
        if len(f) < 7:
            continue
        src, fstype, size, used, avail, pcent, target = f
        if fstype in ("tmpfs", "devtmpfs", "squashfs", "overlay", "efivarfs"):
            continue
        if target not in INTERESTING_MOUNTS:
            continue
        if target in seen:
            continue
        seen.add(target)
        size_i, used_i, avail_i = int(size), int(used), int(avail)
        out.append({
            "mount": target, "source": src, "fstype": fstype,
            "size": size_i, "used": used_i, "avail": avail_i,
            "percent": float(pcent.rstrip("%") or 0),
            "pool_member": target.startswith(({{MEDIA_DISK_PREFIX}})),
        })
    out.sort(key=lambda d: (d["pool_member"], d["mount"]))
    return {"filesystems": out}


# ── docker ───────────────────────────────────────────────────────────────────
def recent_restart_loops(containers):
    """Containers whose RestartCount grew since the previous run, plus any
    currently bouncing. RestartCount is cumulative since container creation,
    so only the delta against the last collection (the timer fires every ~60s)
    says the restarts are *recent* — a bare threshold would warn forever about
    a container that crash-looped once weeks ago."""
    prev = {}
    try:
        prev = json.loads((OUT_DIR / "restart-baseline.json").read_text())
    except Exception:  # noqa: BLE001 — no baseline yet: store one, flag nothing
        pass
    loops = [c["name"] for c in containers
             if c["state"] == "restarting"
             or c["restarts"] - prev.get(c["name"], c["restarts"]) >= 2]
    try:
        (OUT_DIR / "restart-baseline.json").write_text(
            json.dumps({c["name"]: c["restarts"] for c in containers}))
    except OSError:
        pass
    return loops


def collect_docker():
    fmt = ("{{.Names}}\x1f{{.State}}\x1f{{.Status}}\x1f{{.Image}}"
           "\x1f{{.RunningFor}}\x1f{{.Ports}}")
    raw = run(["docker", "ps", "-a", "--format", fmt], timeout=25)
    if not raw.strip():
        return {"error": "docker ps returned nothing (daemon down?)"}

    containers = {}
    for line in raw.splitlines():
        p = line.split("\x1f")
        if len(p) < 5:
            continue
        name, state, status, image, since = p[0], p[1], p[2], p[3], p[4]
        health = ""
        m = re.search(r"\((healthy|unhealthy|health: starting)\)", status)
        if m:
            health = m.group(1).replace("health: ", "")
        containers[name] = {
            "name": name, "state": state, "status": status, "image": image,
            "since": since, "health": health, "restarts": 0,
            "cpu": None, "mem": None, "mem_pct": None,
        }

    # Restart counts + start time in one inspect call.
    names = list(containers)
    if names:
        insp = run(["docker", "inspect", "-f",
                    "{{.Name}}\x1f{{.RestartCount}}\x1f{{.State.StartedAt}}"] + names,
                   timeout=25)
        for line in insp.splitlines():
            p = line.split("\x1f")
            if len(p) < 3:
                continue
            n = p[0].lstrip("/")
            if n in containers:
                containers[n]["restarts"] = int(p[1] or 0)
                containers[n]["started_at"] = p[2]

    # Live CPU/memory. --no-stream still takes a couple of seconds; that is
    # fine at the timer's cadence and it is the only source for per-container
    # resource use without scraping cgroups by hand.
    stats = run(["docker", "stats", "--no-stream", "--format",
                 "{{.Name}}\x1f{{.CPUPerc}}\x1f{{.MemUsage}}\x1f{{.MemPerc}}"],
                timeout=40)
    for line in stats.splitlines():
        p = line.split("\x1f")
        if len(p) < 4 or p[0] not in containers:
            continue
        c = containers[p[0]]
        try:
            c["cpu"] = float(p[1].rstrip("%"))
            c["mem_pct"] = float(p[3].rstrip("%"))
        except ValueError:
            pass
        c["mem"] = p[2]

    lst = sorted(containers.values(), key=lambda c: c["name"])
    running = [c for c in lst if c["state"] == "running"]
    return {
        "containers": lst,
        "total": len(lst),
        "running": len(running),
        "unhealthy": [c["name"] for c in lst if c["health"] == "unhealthy"],
        "not_running": [c["name"] for c in lst if c["state"] != "running"],
        "restart_loops": recent_restart_loops(lst),
    }


# ── vpn ──────────────────────────────────────────────────────────────────────
def collect_vpn():
    """Exit IP from gluetun's own logs, plus the netns binding check."""
    info = {"ip": None, "country": None, "city": None, "healthy": None,
            "siblings": [], "siblings_ok": True}

    health = run(["docker", "inspect", "-f",
                  "{{.State.Health.Status}}", "gluetun"], timeout=10).strip()
    info["healthy"] = health or None

    logs = run(["docker", "logs", "--tail", "400", "gluetun"], timeout=15)
    for line in reversed(logs.splitlines()):
        m = re.search(r"Public IP address is ([0-9a-fA-F.:]+) \(([^)]+)\)", line)
        if m:
            info["ip"] = m.group(1)
            parts = [x.strip() for x in m.group(2).split(",")]
            info["country"] = parts[0] if parts else None
            info["city"] = parts[-1].split(" - ")[0] if len(parts) > 1 else None
            break

    gid = run(["docker", "inspect", "-f", "{{.Id}}", "gluetun"], timeout=10).strip()
    for name in NETNS_SIBLINGS:
        mode = run(["docker", "inspect", "-f",
                    "{{.HostConfig.NetworkMode}}", name], timeout=10).strip()
        bound = bool(gid) and mode == f"container:{gid}"
        info["siblings"].append({"name": name, "bound": bound, "mode": mode})
        if not bound:
            info["siblings_ok"] = False
    return info


# ── media ────────────────────────────────────────────────────────────────────
def plex_token():
    if PLEX["token"]:
        return PLEX["token"]
    raw = run(["docker", "exec", "plex", "sh", "-c",
               'grep -o \'PlexOnlineToken="[^"]*"\' '
               '"/config/Library/Application Support/Plex Media Server/Preferences.xml"'],
              timeout=15)
    m = re.search(r'PlexOnlineToken="([^"]+)"', raw)
    PLEX["token"] = m.group(1) if m else None
    return PLEX["token"]


# Cloudflare's own error codes, which arrive as a 403 body rather than a
# challenge page. 1006/1007/1008 are IP/range bans and 1015 is rate limiting —
# none of them are solvable by FlareSolverr, so they need a different exit IP.
CF_BAN_CODES = {
    "1006": "IP address banned by the site owner",
    "1007": "IP range banned by the site owner",
    "1008": "ASN banned by the site owner",
    "1015": "rate limited by Cloudflare",
}
CF_LINE = re.compile(
    r"\[GET\]\s+(?P<url>https?://(?P<host>[^/\s]+)\S*):\s*403", re.I)
CF_CODE = re.compile(r"error code:\s*(?P<code>10\d\d)")


def _container_tz_offset(container="prowlarr"):
    """The container's UTC offset in seconds, so its log timestamps can be
    turned into real epochs without assuming the container shares the host
    timezone. Returns 0 when the offset cannot be read (prowlarr defaults to
    UTC anyway, and if docker exec fails the log fetch below fails too)."""
    out = run(["docker", "exec", container, "sh", "-c", "date +%z"], timeout=10).strip()
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})", out)
    if not m:
        return 0
    sign = 1 if m.group(1) == "+" else -1
    return sign * (int(m.group(2)) * 3600 + int(m.group(3)) * 60)


def detect_cloudflare_blocks(hours=6, live_hosts=None):
    """Scan Prowlarr's log for Cloudflare bans so they surface immediately
    instead of waiting for the next stalled-download incident.

    live_hosts filters out trackers that are no longer configured — a removed
    indexer leaves its 403s in the log for hours and would otherwise show up
    forever as an unfixable failure."""
    raw = run(["docker", "exec", "prowlarr", "sh", "-c",
               "tail -n 4000 /config/logs/prowlarr.txt 2>/dev/null"], timeout=30)
    if not raw:
        return {"blocked": [], "checked": False}
    # Log timestamps are container-local; interpret them with the container's
    # own offset rather than the host's, or the cutoff silently mis-filters.
    tz_off = _container_tz_offset()

    cutoff = time.time() - hours * 3600
    hits = {}
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        m = CF_LINE.search(line)
        if not m:
            continue
        ts = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
        if ts:
            try:
                when = (calendar.timegm(
                    time.strptime(ts.group(1), "%Y-%m-%d %H:%M:%S")) - tz_off)
                if when < cutoff:
                    continue
            except ValueError:
                pass
        # The "error code: NNNN" lands on the same line or the next few.
        code = None
        for j in range(i, min(i + 4, len(lines))):
            c = CF_CODE.search(lines[j])
            if c:
                code = c.group("code")
                break
        host = m.group("host")
        if code not in CF_BAN_CODES:
            continue
        if live_hosts is not None:
            # Match on the registrable-ish tail so www./mirror subdomains of a
            # configured tracker still count.
            tail = ".".join(host.split(".")[-2:])
            if not any(tail == ".".join(h.split(".")[-2:]) for h in live_hosts):
                continue
        hits[host] = {"host": host, "code": code,
                      "reason": CF_BAN_CODES[code],
                      "url": m.group("url")[:120]}
    return {"blocked": sorted(hits.values(), key=lambda h: h["host"]),
            "checked": True}


def collect_media():
    """Query every media source. Each source is one task run in parallel —
    the sources are independent services, and the timer fires every minute,
    so the wall-clock cost is the slowest source instead of the sum."""
    out = {}

    def radarr():
        d = {}
        rh = {"X-Api-Key": RADARR["key"]}
        movies = http_json(f"{RADARR['url']}/api/v3/movie", rh, timeout=25)
        if isinstance(movies, list):
            have = [m for m in movies if m.get("hasFile")]
            d["radarr"] = {
                "movies": len(movies),
                "with_file": len(have),
                "missing": len([m for m in movies
                                if m.get("monitored") and not m.get("hasFile")]),
                "size": sum(m.get("sizeOnDisk", 0) or 0 for m in movies),
            }
        # Queue (what is actually being grabbed right now) — independent of
        # the library fetch, so a dead /movie endpoint still shows a queue.
        q = http_json(f"{RADARR['url']}/api/v3/queue?pageSize=200", rh, timeout=20)
        if isinstance(q, dict):
            recs = q.get("records", [])
            d.setdefault("radarr", {})["queue"] = len(recs)
            d["radarr"]["queue_warn"] = len(
                [r for r in recs if r.get("trackedDownloadStatus") == "warning"])
        return d

    def sonarr():
        d = {}
        sh = {"X-Api-Key": SONARR["key"]}
        series = http_json(f"{SONARR['url']}/api/v3/series", sh, timeout=25)
        if isinstance(series, list):
            stats = [s.get("statistics", {}) or {} for s in series]
            d["sonarr"] = {
                "series": len(series),
                "episodes": sum(s.get("episodeFileCount", 0) for s in stats),
                "episodes_total": sum(s.get("totalEpisodeCount", 0) for s in stats),
                "missing": sum(max(0, s.get("episodeCount", 0) - s.get("episodeFileCount", 0))
                               for s in stats),
                "size": sum(s.get("sizeOnDisk", 0) or 0 for s in stats),
            }
        q = http_json(f"{SONARR['url']}/api/v3/queue?pageSize=200", sh, timeout=20)
        if isinstance(q, dict):
            recs = q.get("records", [])
            d.setdefault("sonarr", {})["queue"] = len(recs)
            d["sonarr"]["queue_warn"] = len(
                [r for r in recs if r.get("trackedDownloadStatus") == "warning"])
        return d

    def prowlarr():
        d = {}
        ph = {"X-Api-Key": PROWLARR["key"]}
        idx = http_json(f"{PROWLARR['url']}/api/v1/indexer", ph, timeout=20)
        status = http_json(f"{PROWLARR['url']}/api/v1/indexerstatus", ph, timeout=20)

        # Hosts of the indexers that are actually configured right now.
        live_hosts = set()
        for i in (idx if isinstance(idx, list) else []):
            for f in i.get("fields", []):
                if f.get("name") == "baseUrl" and f.get("value"):
                    try:
                        h = urllib.parse.urlparse(str(f["value"])).hostname
                        if h:
                            live_hosts.add(h)
                    except Exception:  # noqa: BLE001
                        pass
        d["cloudflare"] = detect_cloudflare_blocks(live_hosts=live_hosts or None)

        if isinstance(idx, list):
            failing = {s.get("indexerId") for s in (status or []) if s.get("disabledTill")}
            d["prowlarr"] = {
                "indexers": len(idx),
                "enabled": len([i for i in idx if i.get("enable")]),
                "failing": sorted(i.get("name", "?") for i in idx
                                  if i.get("id") in failing),
            }
        return d

    def qbittorrent():
        try:
            cj = __import__("http.cookiejar", fromlist=["CookieJar"]).CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            data = urllib.parse.urlencode({"username": QBIT["user"],
                                           "password": QBIT["pass"]}).encode()
            req = urllib.request.Request(f"{QBIT['url']}/api/v2/auth/login", data=data,
                                         headers={"Referer": QBIT["url"]})
            op.open(req, timeout=TIMEOUT).read()

            def qget(path):
                r = urllib.request.Request(f"{QBIT['url']}{path}",
                                           headers={"Referer": QBIT["url"]})
                return json.loads(op.open(r, timeout=TIMEOUT).read().decode())

            tr = qget("/api/v2/transfer/info")
            tor = qget("/api/v2/torrents/info")
            states = {}
            for t in tor:
                states[t["state"]] = states.get(t["state"], 0) + 1
            active = [t for t in tor if t.get("progress", 1) < 1]
            return {"qbittorrent": {
                "torrents": len(tor),
                "incomplete": len(active),
                "states": states,
                "dl_speed": tr.get("dl_info_speed", 0),
                "up_speed": tr.get("up_info_speed", 0),
                "connection": tr.get("connection_status", "?"),
                "dht": tr.get("dht_nodes", 0),
                "active": sorted(
                    [{"name": t["name"][:60], "state": t["state"],
                      "progress": round(t.get("progress", 0) * 100, 1),
                      "dl": t.get("dlspeed", 0), "seeds": t.get("num_seeds", 0),
                      "eta": t.get("eta", 0)} for t in active],
                    key=lambda x: -x["dl"])[:12],
            }}
        except Exception as e:  # noqa: BLE001
            return {"qbittorrent": {"error": str(e)}}

    def jellyfin():
        d = {}
        jh = {"Authorization": f'MediaBrowser Token={JELLYFIN["key"]}'}
        jf = http_json(f"{JELLYFIN['url']}/Items/Counts", jh, timeout=20)
        if isinstance(jf, dict):
            d["jellyfin"] = {
                "movies": jf.get("MovieCount", 0),
                "series": jf.get("SeriesCount", 0),
                "episodes": jf.get("EpisodeCount", 0),
            }
        sessions = http_json(f"{JELLYFIN['url']}/Sessions", jh, timeout=15)
        if isinstance(sessions, list):
            d.setdefault("jellyfin", {})["streams"] = len(
                [s for s in sessions if s.get("NowPlayingItem")])
        return d

    def plex(tok):
        d = {}
        ph2 = {"X-Plex-Token": tok, "Accept": "application/json"}
        secs = http_json(f"{PLEX['url']}/library/sections", ph2, timeout=20)
        libs = []
        if isinstance(secs, dict):
            for sec in secs.get("MediaContainer", {}).get("Directory", []):
                key, title, typ = sec.get("key"), sec.get("title"), sec.get("type")
                cnt = http_json(
                    f"{PLEX['url']}/library/sections/{key}/all"
                    f"?X-Plex-Container-Start=0&X-Plex-Container-Size=0", ph2, timeout=20)
                total = (cnt or {}).get("MediaContainer", {}).get("totalSize")
                libs.append({"title": title, "type": typ, "count": total})
        ses = http_json(f"{PLEX['url']}/status/sessions", ph2, timeout=15)
        d["plex"] = {
            "libraries": libs,
            "streams": (ses or {}).get("MediaContainer", {}).get("size", 0),
        }
        return d

    jobs = {"radarr": radarr, "sonarr": sonarr, "prowlarr": prowlarr,
            "qbittorrent": qbittorrent, "jellyfin": jellyfin}
    tok = plex_token()
    if tok:
        jobs["plex"] = lambda: plex(tok)

    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {ex.submit(fn): name for name, fn in jobs.items()}
        for fut in as_completed(futs):
            try:
                out.update(fut.result())
            except Exception as e:  # noqa: BLE001 — one dead source, not all
                out[futs[fut]] = {"error": f"{type(e).__name__}: {e}"}

    return out


# ── services ─────────────────────────────────────────────────────────────────
def probe(item):
    label, url, ok_codes = item
    t0 = time.time()
    code, _ = http(url, timeout=6)
    ms = int((time.time() - t0) * 1000)
    return {"name": label, "url": url, "code": code,
            "ok": code in ok_codes, "ms": ms}


def collect_services():
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(probe, SERVICE_PROBES))


# ── daily-routine log ────────────────────────────────────────────────────────
STATUS_RE = re.compile(r"\[(OK|WARN|FAIL|SKIP)\]\s*(.*)$")
TS_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]\s*(.*)$")
BANNER_RE = re.compile(r"^=+\s*(.+?)\s*=+$")


def collect_daily_routine():
    logs = sorted(LOG_DIR.glob("daily-routine-*.log"))
    if not logs:
        return {"error": "no daily-routine logs found", "entries": []}
    latest = logs[-1]
    text = latest.read_text(errors="replace")

    section = "start"
    entries = []
    counts = {"OK": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    for raw_line in text.splitlines():
        line = raw_line
        m = TS_RE.match(line)
        stamp = None
        if m:
            stamp, line = m.group(1), m.group(2)
        line = re.sub(r"^WARNING:\s*", "", line.strip())

        b = BANNER_RE.match(line)
        if b:
            section = b.group(1)
            continue

        s = STATUS_RE.search(line)
        if s:
            kind, msg = s.group(1), s.group(2).strip()
            counts[kind] += 1
            if kind != "OK":
                entries.append({"kind": kind, "section": section,
                                "message": msg, "time": stamp})
            continue

        # Helper scripts log their own warnings without the [KIND] tag.
        if re.search(r"\bWARNING\b|\bERROR\b|had errors", raw_line):
            counts["WARN"] += 1
            entries.append({"kind": "WARN", "section": section,
                            "message": line[:200], "time": stamp})

    mtime = latest.stat().st_mtime
    age_h = (time.time() - mtime) / 3600.0
    return {
        "log": str(latest),
        "ran_at": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
        "age_hours": round(age_h, 1),
        "stale": age_h > 26,
        "next_run": DAILY_ROUTINE_NEXT_RUN,
        "counts": counts,
        "entries": entries,
        "history": [p.name for p in logs[-7:]],
    }


# ── attention: what is broken / what needs doing ─────────────────────────────
def build_attention(host, disks, docker, vpn, services, media, routine):
    """Fold every signal into three buckets: failed, warning, todo."""
    failed, warning, todo = [], [], []

    def add(bucket, source, message, action=None, fix=None):
        """fix: {"id": <registered handler>, "args": {...}, "label": str} or None.
        Only findings carrying a fix become clickable in the dashboard; the
        handler ids are whitelisted server-side (see status-dashboard-server.py)."""
        bucket.append({"source": source, "message": message,
                       "action": action, "fix": fix})

    # Which container backs each probed endpoint, so a dead service can be
    # restarted by name rather than guessed at.
    SERVICE_CONTAINER = {
        "qBittorrent": "qbittorrent", "Prowlarr": "prowlarr", "Radarr": "radarr",
        "Sonarr": "sonarr", "Overseerr": "overseerr", "Jellyseerr": "jellyseerr",
        "Plex": "plex", "Jellyfin": "jellyfin", "Nextcloud": "nextcloud",
        "Immich": "immich-server", "Portainer": "portainer",
        "Tugtainer": "tugtainer", "Caddy": "caddy",
    }

    # ---- live container / service state
    if isinstance(docker, dict) and not docker.get("error"):
        for n in docker.get("not_running", []):
            add(failed, "docker", f"Container {n} is not running",
                f"docker start {n}",
                {"id": "docker_start", "args": {"name": n},
                 "label": f"Start {n}"})
        for n in docker.get("unhealthy", []):
            add(failed, "docker", f"Container {n} reports unhealthy",
                f"docker logs --tail 50 {n}",
                {"id": "docker_restart", "args": {"name": n},
                 "label": f"Restart {n}"})
        for n in docker.get("restart_loops", []):
            add(warning, "docker", f"Container {n} has restarted repeatedly",
                f"docker logs --tail 100 {n}")
    elif isinstance(docker, dict):
        add(failed, "docker", docker["error"], "systemctl status docker")

    for s in services if isinstance(services, list) else []:
        if not s["ok"]:
            code = s["code"] if s["code"] is not None else "no connection"
            cname = SERVICE_CONTAINER.get(s["name"])
            add(failed, "service", f"{s['name']} not responding ({code})",
                f"curl -v {s['url']}",
                {"id": "docker_restart", "args": {"name": cname},
                 "label": f"Restart {cname}"} if cname else None)

    # ---- VPN
    if isinstance(vpn, dict) and not vpn.get("error"):
        if vpn.get("healthy") and vpn["healthy"] != "healthy":
            add(failed, "vpn", f"gluetun health is {vpn['healthy']}",
                "docker logs --tail 50 gluetun")
        if not vpn.get("siblings_ok"):
            broken = [s["name"] for s in vpn.get("siblings", []) if not s["bound"]]
            add(failed, "vpn",
                f"Not sharing gluetun netns: {', '.join(broken)} — traffic may bypass the VPN",
                "docker compose -f ~/docker/vpn-stack up -d --force-recreate",
                {"id": "vpn_recreate", "args": {},
                 "label": "Recreate the whole gluetun netns group"})
        if vpn.get("country") and vpn["country"] != EXPECTED_VPN_COUNTRY:
            add(warning, "vpn", f"VPN exit is {vpn['country']}, expected {EXPECTED_VPN_COUNTRY}",
                "~/.local/bin/gluetun-rotate.sh",
                {"id": "vpn_rotate", "args": {}, "label": "Rotate VPN exit IP"})

    # ---- disks
    for fs in (disks or {}).get("filesystems", []):
        if fs["percent"] >= 90:
            add(failed, "disk", f"{fs['mount']} is {fs['percent']:.0f}% full "
                                f"({human(fs['avail'])} free)")
        elif fs["percent"] >= 80:
            add(warning, "disk", f"{fs['mount']} is {fs['percent']:.0f}% full "
                                 f"({human(fs['avail'])} free)")

    # ---- host
    if host.get("reboot_required"):
        pkgs = ", ".join(host.get("reboot_packages", [])[:4])
        add(todo, "system", f"Reboot required ({pkgs or 'kernel/libc update'})",
            "sudo reboot")
    for u in host.get("failed_units", []):
        add(failed, "systemd", f"Failed unit: {u}", f"systemctl status {u}",
            {"id": "restart_unit", "args": {"unit": u}, "label": f"Restart {u}"})
    if host.get("updates_security"):
        add(todo, "updates", f"{host['updates_security']} security update(s) pending",
            "sudo apt-get -y upgrade",
            {"id": "apt_upgrade", "args": {}, "label": "Install pending updates"})
    elif host.get("updates_pending"):
        add(todo, "updates", f"{host['updates_pending']} package update(s) pending",
            "sudo apt-get -y upgrade",
            {"id": "apt_upgrade", "args": {}, "label": "Install pending updates"})

    # ---- temperatures
    for t in (host.get("sensors") or {}).get("temps", []):
        crit = t.get("crit")
        if crit and t["value"] >= crit * 0.92:
            add(warning, "thermal",
                f"{t['chip']} {t['label']} at {t['value']}°C (limit {crit:.0f}°C)")

    # ---- media pipeline
    # Cloudflare bans first — they are the upstream cause of indexer failures,
    # and FlareSolverr cannot solve them, so they need their own remedy.
    cf = (media or {}).get("cloudflare", {})
    for b in cf.get("blocked", []):
        add(failed, "cloudflare",
            f"{b['host']} is blocking this VPN exit — Cloudflare {b['code']} "
            f"({b['reason']}); FlareSolverr cannot bypass this",
            "~/.local/bin/gluetun-rotate.sh  # then re-test indexers",
            {"id": "cloudflare_unblock", "args": {"host": b["host"]},
             "label": f"Rotate VPN exit and re-test ({b['host']})"})

    pw = (media or {}).get("prowlarr", {})
    if pw.get("failing"):
        add(warning, "indexers", f"Indexers failing: {', '.join(pw['failing'])}",
            "Prowlarr → Indexers → Test All",
            {"id": "prowlarr_testall", "args": {},
             "label": "Re-test all indexers"})
    qb = (media or {}).get("qbittorrent", {})
    if qb.get("error"):
        add(warning, "qbittorrent", f"qBittorrent API error: {qb['error'][:90]}",
            None, {"id": "docker_restart", "args": {"name": "qbittorrent"},
                   "label": "Restart qBittorrent"})
    elif qb.get("incomplete") and qb.get("dl_speed", 0) < 50_000:
        add(warning, "qbittorrent",
            f"{qb['incomplete']} incomplete torrent(s) but download speed is "
            f"{human(qb.get('dl_speed', 0))}/s — possible stall",
            "~/.local/bin/media-stack-selfheal.py",
            {"id": "selfheal", "args": {}, "label": "Run media-stack self-heal"})

    # ---- daily routine findings
    if isinstance(routine, dict):
        if routine.get("stale"):
            add(warning, "daily-routine",
                f"Last run was {routine.get('age_hours')}h ago — cron may not be firing",
                "crontab -l; grep daily-routine /var/log/syslog")
        for e in routine.get("entries", []):
            # SKIP means an optional helper simply is not installed. That is
            # background noise, not something broken — it stays in the routine
            # panel and never reaches the failed/warning buckets.
            if e["kind"] == "SKIP":
                continue
            bucket = failed if e["kind"] == "FAIL" else warning
            # These come from the 03:00 log, so the finding may already be
            # stale. Re-running the quick pass is the honest "fix": it either
            # clears the entry or confirms it is still real.
            add(bucket, f"daily-routine › {e['section']}", e["message"], None,
                {"id": "daily_routine_quick", "args": {},
                 "label": "Re-run daily routine (quick)"})

    # De-duplicate identical messages while keeping order.
    def dedupe(items):
        seen, out = set(), []
        for i in items:
            k = i["message"]
            if k in seen:
                continue
            seen.add(k)
            out.append(i)
        return out

    return {"failed": dedupe(failed), "warning": dedupe(warning),
            "todo": dedupe(todo)}


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    host = guard("host", collect_host)
    disks = guard("disks", collect_disks)
    docker = guard("docker", collect_docker)
    vpn = guard("vpn", collect_vpn)
    services = guard("services", collect_services)
    media = guard("media", collect_media)
    routine = guard("daily_routine", collect_daily_routine)

    attention = guard("attention", build_attention,
                      host if isinstance(host, dict) else {},
                      disks if isinstance(disks, dict) else {},
                      docker if isinstance(docker, dict) else {},
                      vpn if isinstance(vpn, dict) else {},
                      services if isinstance(services, list) else [],
                      media if isinstance(media, dict) else {},
                      routine if isinstance(routine, dict) else {})

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generated_epoch": time.time(),
        "collect_seconds": round(time.time() - t0, 1),
        "host": host, "disks": disks, "docker": docker, "vpn": vpn,
        "services": services, "media": media, "daily_routine": routine,
        "attention": attention,
    }

    tmp = OUT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(OUT_FILE)  # atomic: the dashboard never reads a half-written file
    print(f"wrote {OUT_FILE} in {payload['collect_seconds']}s "
          f"(fail={len(attention.get('failed', []))} "
          f"warn={len(attention.get('warning', []))} "
          f"todo={len(attention.get('todo', []))})")


if __name__ == "__main__":
    sys.exit(main())
