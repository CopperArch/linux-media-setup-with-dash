#!/usr/bin/env python3
"""
media-stack-selfheal.py — checks the *arr/qBittorrent download pipeline and
auto-fixes the failure modes we've actually hit on this box. Called from
daily-routine.sh at 3am (after the VPN has been rotated + containers restarted).

Safe to run any time; every action is idempotent and logged. It will:
  0. Verify gluetun's tunnel actually has a public IP (not just a "healthy"
     Docker healthcheck, which only pings gluetun's own control server and
     stays green through an AUTH_FAILED loop — see vpn_tunnel_health()).
     Attempts one gluetun-rotate.sh cycle to recover, alerts if still down.
  1. Ensure the expected containers are running (start stopped ones).
  2. Reapply qBittorrent's desired queue/safety settings if they've drifted:
     dont_count_slow_torrents=true, max_active_downloads=20,
     max_active_torrents=30, max_active_uploads=0 (download-only, no seeding),
     max_ratio_act=0/pause (NOT remove — see comment on DESIRED_PREFS), and
     excluded_file_names blocking .exe/.scr/.bat/etc so malware droppers
     disguised as video releases never get downloaded.
  3. Verify Radarr/Sonarr can reach qBittorrent (download-client test).
  4. Blocklist + remove genuinely-dead downloads (0 seeds, availability < 1,
     stalled — including forcedDL/forcedMetaDL/missingFiles states — older
     than DEAD_NO_SEED_AGE_H hours when the swarm has no seed at all, or
     DEAD_AGE_H hours otherwise) and trigger a replacement search, so the
     queue never sits stuck behind un-completable or dead torrents.
  5. Clean up the downloads folder: remove entries no longer tracked by
     qBittorrent, but only once every file inside already has a hardlink
     twin in the library (i.e. it was actually imported) — never deletes
     the only copy of anything.
  6. Audit every Sonarr episode file and Radarr movie file against actual
     disk state (added 2026-07-21, after Sonarr's own BulkMoveSeries command
     silently deleted a just-downloaded file mid-repoint). A small number of
     missing files gets self-healed: clear the stale DB record and trigger a
     re-search — the same recovery used the day this was found. A large
     number all at once is reported only, never auto-fixed, since that's far
     more likely a mount/environment problem (e.g. a disk not mounted yet)
     than real data loss, and mass-clearing those would trigger a
     re-download storm instead of catching the real issue.
  7. Repair missing Jellyfin posters: re-fetch artwork for anything showing a
     blank tile, and fall back to the series poster for seasons that have no
     artwork upstream at all. Images only — it never touches titles/metadata.

Torrents auto-vanish from qBittorrent's list once *arr confirms an import
(removeCompletedDownloads=true on both Radarr's and Sonarr's downloadclient
config) — not via qBittorrent's own ratio-limit action, which races against
*arr's 1-minute import poll and can cause a silently-missed import.

Exit code is always 0 (best-effort maintenance); problems print [FAIL]/[WARN]
so the parent routine's log captures them.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from http.cookiejar import CookieJar

# ── config ───────────────────────────────────────────────────────────────────
QBIT = {"url": "http://localhost:8080", "user": "{{QBIT_USER}}", "pass": "{{QBIT_PASSWORD}}"}
RADARR = {"url": "http://localhost:7878",
          "key": "3794f810421647a3b2ad0c8e0e597081", "cat": "radarr"}
SONARR = {"url": "http://localhost:8989",
          "key": "e260d0f230e14ef49c3c58ac7885bd77", "cat": "sonarr"}
PROWLARR = {"url": "http://localhost:9696",
            "key": "0db7bbe5a32440a8808a5621f103d59c"}
# Plex and Jellyfin both run with network_mode: host.
JELLYFIN = {"url": "http://localhost:8096",
            "key": "23691717b6604ac99ffedb4ded797e54"}

# Artwork repair. Items whose poster never downloaded show as blank tiles in
# every client. Two distinct causes, so two distinct remedies (see
# fix_missing_artwork): a provider fetch that simply failed once, and items
# that have no upstream artwork at all — misparsed "Season Unknown" entries,
# oddly-named season folders, and fan-made series that aren't on TMDB/TVDB.
# The second kind will never self-heal from a refresh, so seasons fall back to
# inheriting their series poster, which is what clients show anyway rather
# than a blank.
ARTWORK_TYPES = ("Movie", "Series", "Season", "Episode")

# Desired qBittorrent queue prefs (the settings tuned 2026-07-08).
DESIRED_PREFS = {
    "queueing_enabled": True,
    "dont_count_slow_torrents": True,
    "max_active_downloads": 20,
    "max_active_torrents": 30,
    "max_active_uploads": 0,
    "max_ratio_enabled": True,
    "max_ratio": 0,
    "max_ratio_act": 0,  # 0=pause at ratio 0 (NOT 1=remove). Tried "remove" on
                         # 2026-07-13 for auto-cleanup, but Radarr/Sonarr's own
                         # downloadclient/test endpoint errors on it: "unable
                         # to perform Completed Download Handling" — qBittorrent
                         # can yank a finished torrent before the 1-min *arr
                         # poll imports it, silently losing the download. Auto-
                         # removal instead comes from *arr's own
                         # removeCompletedDownloads (see downloadclient config),
                         # which only removes AFTER a confirmed import.
    "excluded_file_names_enabled": True,
    "excluded_file_names": "*.exe\n*.scr\n*.bat\n*.cmd\n*.com\n*.pif\n*.vbs\n"
                           "*.vbe\n*.js\n*.jse\n*.wsf\n*.msi\n*.ps1\n*.lnk\n"
                           "*.jar\n*.txt",
    # Added 2026-07-13 after fake "House of the Dragon" torrents from
    # LimeTorrents delivered .exe/.scr malware droppers instead of video.
    # Blocks these extensions from ever being fetched, for every torrent
    # added by Radarr, Sonarr, or manually.
}

# Indexer release-quality policy (added 2026-08-07 after the third "everything
# stalled" incident). minimumSeeders=5 lets the *arrs grab releases whose swarm
# is already dead, which is what produced 17 torrents doing 0 B/s for 112h.
# This tuning had been applied by hand twice before (2026-07-24, 2026-07-29) and
# was found reverted both times — hence reconciling it on every run.
INDEXER_MIN_SEEDERS = 15
# Trackers with a history of stale/inflated seed counts or malware-bearing
# releases. Higher number = LOWER priority in *arr's release ranking.
DEPRIORITIZED_INDEXERS = {"1337x": 45, "LimeTorrents": 45}

# Malware/executable release blocking (added 2026-08-25 after the second .exe
# dropper -- "A Knight of the Seven Kingdoms" from TorrentDownload -- disguised
# as an episode, this time inside Radarr/Sonarr's own grab decision rather than
# just qBittorrent's excluded_file_names above. qBittorrent zeroing the file out
# still left a dead, un-importable download sitting in the queue for hours; this
# Custom Format rejects the release before it is ever grabbed. Matches a
# disguised executable/script extension at the END of the release title (the
# indexer-reported title, e.g. "...H.264-NTb.exe" -- confirmed via the real
# grab this was written to fix).
MALWARE_FORMAT_NAME = "Executable/Script (Malware)"
MALWARE_FORMAT_PATTERN = (
    r"(?i)[\.\-_ ](exe|scr|bat|cmd|com|pif|vbs|vbe|js|jse|wsf|msi|ps1|lnk)$")
MALWARE_FORMAT_SCORE = -10000

# Trackers that sit behind Cloudflare and therefore MUST carry the
# `flaresolverr` tag, or Prowlarr queries them directly and every request dies
# on the challenge page. This is the known-bad list; verify_indexers() also
# learns new ones from Prowlarr's own log, so a tracker that switches
# Cloudflare on later is picked up without editing this.
FLARESOLVERR_TAG = "flaresolverr"
CLOUDFLARE_INDEXERS = {"EZTV", "LimeTorrents", "Torrent[CORE]", "Uindex",
                       "Torrent Downloads"}

# Backlog search. Neither *arr has a recurring missing-search task, so without
# this a request whose release was already posted to the indexers before it was
# requested never gets searched again. Bounded per night: each search fans out
# over every indexer, and hammering trackers is how this box earned an IP ban.
WANTED_SEARCH_BATCH = 12
WANTED_RETRY_DAYS = 3
WANTED_STATE = os.path.expanduser("~/.local/state/media-stack-wanted.json")

# The two Seerr front-ends. Each is checked against whichever media server it
# is actually configured against (Overseerr->Plex, jellyseerr->Jellyfin).
SEERR_INSTANCES = [
    {"name": "Overseerr", "container": "overseerr",
     "url": "http://localhost:5055"},
    {"name": "jellyseerr", "container": "jellyseerr",
     "url": "http://localhost:5056"},
]

# Containers that must be running.
CONTAINERS = ["gluetun", "qbittorrent", "radarr", "sonarr", "prowlarr",
              "flaresolverr", "jellyseerr", "overseerr", "plex", "jellyfin"]

# Which compose project each container belongs to, so a container that has been
# *deleted* (not merely stopped) can be recreated rather than silently skipped.
# Added 2026-08-06 after daily-routine.sh's §11 ran
# `docker compose up -d --remove-orphans gluetun chrome flaresolverr` in
# vpn-stack: --remove-orphans is evaluated against the named service subset, so
# Compose deleted the five vpn-stack services that weren't listed
# (sonarr/radarr/prowlarr/qbittorrent/jellyseerr). They vanished from `docker ps -a`
# entirely, ensure_containers() logged "not defined on this host", and the
# download pipeline stayed dead for two days with nothing alerting on it.
HOME = os.path.expanduser("~")
COMPOSE_DIRS = {
    "gluetun":      f"{HOME}/docker/vpn-stack",
    "qbittorrent":  f"{HOME}/docker/vpn-stack",
    "radarr":       f"{HOME}/docker/vpn-stack",
    "sonarr":       f"{HOME}/docker/vpn-stack",
    "prowlarr":     f"{HOME}/docker/vpn-stack",
    "flaresolverr": f"{HOME}/docker/vpn-stack",
    "jellyseerr":   f"{HOME}/docker/vpn-stack",
    "overseerr":    f"{HOME}/docker/media-stack",
    "plex":         f"{HOME}/docker/media-stack",
    "jellyfin":     f"{HOME}/docker/media-stack",
}

# Recreating gluetun tears down the shared network namespace, so every container
# using `network_mode: service:gluetun` must be recreated with it or they come
# back with no connectivity. Never recreate gluetun alone.
NETNS_DEPENDENTS = ["qbittorrent", "radarr", "sonarr", "prowlarr",
                    "flaresolverr", "jellyseerr", "chrome"]

DEAD_AGE_H = 48      # only treat a torrent as dead once it's this old
# 2026-08-08: 48h is far too patient for the common case. Sonarr/Radarr grab
# releases off indexer-reported seed counts that are routinely stale or simply
# fake (the long-running 1337x problem), so a dead grab is dead the moment it
# lands — it sits in metaDL with the tracker itself reporting zero complete
# copies. Twelve of those clogged the queue for 34h while the 03:00 run kept
# saying "0 torrent(s) dead >48h". When the tracker says nobody anywhere has a
# full copy, no amount of extra waiting changes the answer, so reap those on a
# short grace instead. The full DEAD_AGE_H wait still applies to the ambiguous
# case (seeds exist but we can't currently reach them), which can recover.
DEAD_NO_SEED_AGE_H = 3
# Below this aggregate download rate the pipeline counts as not moving. Set
# above zero deliberately: a single crawling torrent must not mask a queue
# that is otherwise entirely stalled (see pipeline_throughput).
STALL_FLOOR_BPS = 512 * 1024
NOW = time.time()
GLUETUN_ROTATE = os.path.expanduser("~/.local/bin/gluetun-rotate.sh")

# Host-side path to qBittorrent's save_path (container sees /data/media/downloads,
# same mergerfs pool as {{MEDIA_POOL}} — see media-stack-cascade-storage notes).
DOWNLOADS_DIR = "{{MEDIA_POOL}}/downloads"
DOWNLOADS_GRACE_H = 24   # never touch anything newer than this (might be mid-import)


def log(msg):
    print(f"    {msg}", flush=True)


# ── alerting ─────────────────────────────────────────────────────────────────
# Collected during the run and emailed once at the end (one digest, not one
# mail per event). Only genuinely notable things go in here — a normal quiet
# night must send nothing at all, or the alerts get ignored.
ALERTS = []


def alert(msg):
    ALERTS.append(msg)


def flush_alerts():
    if not ALERTS:
        return
    host = os.uname().nodename
    subject = f"[homelab] {len(ALERTS)} media-stack issue(s) on {host}"
    if any(a.startswith("RECREATED") for a in ALERTS):
        subject = f"[homelab] containers were DELETED and rebuilt on {host}"
    body = ("media-stack-selfheal.py acted on the following:\n\n"
            + "\n".join(f"  - {a}" for a in ALERTS)
            + "\n\nFull log: ~/.hermes/maintenance-logs/daily-routine-*.log\n")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "send_alert", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "send-alert.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rc, info = mod.send(subject, body)
    except Exception as e:
        log(f"  [WARN] alert email failed to load sender: {e}")
        return
    if rc == 0:
        log(f"  [OK]   alert email {info}")
    elif rc == 2:
        log(f"  [SKIP] alert email not sent — {info}")
    else:
        log(f"  [WARN] alert email failed — {info}")


# ── docker ───────────────────────────────────────────────────────────────────
def docker(*args, timeout=120):
    try:
        return subprocess.run(["docker", *args], capture_output=True,
                              text=True, timeout=timeout).stdout.strip()
    except Exception as e:
        return f"__ERR__{e}"


def compose_up(compose_dir, services, timeout=300):
    """`docker compose up -d --no-deps <services>` in compose_dir.

    --no-deps so recreating a dependent never drags gluetun down with it.
    NEVER add --remove-orphans here: with an explicit service subset it deletes
    every other container in the compose file (that is the exact bug this
    function exists to repair).
    """
    try:
        r = subprocess.run(["docker", "compose", "up", "-d", "--no-deps", *services],
                           cwd=compose_dir, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode == 0, (r.stderr or r.stdout).strip()[-400:]
    except Exception as e:
        return False, str(e)


def vpn_tunnel_health():
    """Verify gluetun's OpenVPN tunnel actually has a public IP.

    Docker's healthcheck on gluetun only pings its own control server, so it
    reports "healthy" even while OpenVPN is stuck retrying AUTH_FAILED
    against a bad exit node — that hid a 5+ hour outage on 2026-09-03 where
    every indexer and download silently died but the dashboard's only signal
    was a downstream qBittorrent stall. This checks the tunnel directly, and
    if it's down, tries the same soft-cycle gluetun-rotate.sh already does
    hourly before giving up and alerting.
    """
    log("Gluetun VPN tunnel:")

    def public_ip():
        try:
            r = subprocess.run(
                ["docker", "exec", "gluetun", "wget", "-qO-", "--timeout=10",
                 "https://api.ipify.org"],
                capture_output=True, text=True, timeout=15)
            ip = r.stdout.strip()
            return ip if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) else ""
        except Exception:
            return ""

    ip = public_ip()
    if ip:
        log(f"  [OK]   tunnel up, exit IP {ip}")
        return True

    log("  [FAIL] no public IP through the gluetun tunnel — VPN is down")
    healed = False
    if os.path.isfile(GLUETUN_ROTATE):
        log("  [FIX ] attempting recovery via gluetun-rotate.sh")
        try:
            subprocess.run(["bash", GLUETUN_ROTATE], capture_output=True,
                           text=True, timeout=200)
        except Exception as e:
            log(f"  [WARN] gluetun-rotate.sh failed to run: {e}")
        healed = bool(public_ip())

    if healed:
        log("  [OK]   tunnel recovered after rotation")
        return True

    tail = docker("logs", "--tail", "15", "gluetun")
    log("  [FAIL] tunnel still down after recovery attempt")
    alert("gluetun VPN tunnel is DOWN — no public IP through the tunnel "
          "(Prowlarr/qBittorrent/Sonarr/Radarr all cut off from the "
          "internet). Auto-recovery via gluetun-rotate.sh did not bring it "
          f"back; check 'docker logs gluetun'.\nLast log lines:\n{tail}")
    return False


def ensure_containers():
    log("Container health:")
    running = docker("ps", "--format", "{{.Names}}").splitlines()
    defined = docker("ps", "-a", "--format", "{{.Names}}").splitlines()

    # Group deleted containers by compose project so each project is brought up
    # in a single call, and so gluetun (if it too went missing) is recreated
    # together with its netns dependents rather than on its own.
    missing = {}
    for c in CONTAINERS:
        if c in running or c in defined:
            continue
        d = COMPOSE_DIRS.get(c)
        if not d or not os.path.isdir(d):
            log(f"  [SKIP] {c} gone but no compose dir known — cannot recreate")
            continue
        missing.setdefault(d, []).append(c)

    for compose_dir, names in sorted(missing.items()):
        svcs = list(names)
        if "gluetun" in svcs:
            # gluetun's netns is shared; recreate every dependent alongside it.
            svcs += [s for s in NETNS_DEPENDENTS if s not in svcs]
        log(f"  [FIX ] MISSING (deleted, not stopped): {', '.join(names)} "
            f"— recreating via compose in {compose_dir}")
        ok, err = compose_up(compose_dir, svcs)
        if ok:
            log(f"  [OK]   recreated: {', '.join(svcs)}")
            alert(f"RECREATED containers that had been DELETED: {', '.join(names)} "
                  f"(rebuilt {', '.join(svcs)} from {compose_dir}). "
                  f"Check what removed them — see the --remove-orphans note in "
                  f"daily-routine.sh §11.")
        else:
            log(f"  [FAIL] could not recreate {', '.join(svcs)}: {err}")
            alert(f"FAILED to recreate deleted container(s) {', '.join(names)} "
                  f"from {compose_dir}: {err}")

    # Anything that merely stopped just needs starting.
    if missing:
        running = docker("ps", "--format", "{{.Names}}").splitlines()
        defined = docker("ps", "-a", "--format", "{{.Names}}").splitlines()
    for c in CONTAINERS:
        if c in running:
            continue
        if c not in defined:
            log(f"  [FAIL] {c} still absent after recreate attempt")
            alert(f"{c} is still missing after a recreate attempt — manual fix needed")
            continue
        log(f"  [FIX ] {c} is down — starting it")
        docker("start", c)
        alert(f"{c} was stopped and has been restarted")
    log("  [OK]   container check complete")


# ── http ─────────────────────────────────────────────────────────────────────
def request(method, url, headers=None, data=None, opener=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    return opener.open(req, timeout=timeout) if opener \
        else urllib.request.urlopen(req, timeout=timeout)


# ── qBittorrent ──────────────────────────────────────────────────────────────
def qbit_login():
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()))
    body = urllib.parse.urlencode(
        {"username": QBIT["user"], "password": QBIT["pass"]}).encode()
    try:
        request("POST", f"{QBIT['url']}/api/v2/auth/login",
                {"Referer": QBIT["url"]}, body, opener, timeout=15)
        return opener
    except Exception as e:
        log(f"  [FAIL] qBittorrent login failed: {e}")
        return None


def qbit_get(opener, path):
    return json.load(request("GET", f"{QBIT['url']}{path}",
                             {"Referer": QBIT["url"]}, None, opener))


def qbit_post(opener, path, fields):
    body = urllib.parse.urlencode(fields).encode()
    return request("POST", f"{QBIT['url']}{path}",
                   {"Referer": QBIT["url"]}, body, opener)


def reconcile_prefs(opener):
    log("qBittorrent queue settings:")
    try:
        cur = qbit_get(opener, "/api/v2/app/preferences")
    except Exception as e:
        log(f"  [FAIL] can't read preferences: {e}")
        return
    drift = {k: v for k, v in DESIRED_PREFS.items() if cur.get(k) != v}
    if not drift:
        log("  [OK]   all queue settings already correct")
        return
    log(f"  [FIX ] correcting drifted settings: {drift}")
    qbit_post(opener, "/api/v2/app/setPreferences",
              {"json": json.dumps(DESIRED_PREFS)})
    log("  [OK]   queue settings reapplied")


def dead_torrents(opener):
    """Return [(hash, category, name)] for genuinely-dead downloads."""
    try:
        ts = qbit_get(opener, "/api/v2/torrents/info")
    except Exception as e:
        log(f"  [FAIL] can't list torrents: {e}")
        return []
    dead = []
    for t in ts:
        incomplete = t["progress"] < 1.0
        if not incomplete:
            continue
        state = t["state"]
        # missingFiles = qBittorrent can't find the piece data on disk at
        # all — unrecoverable regardless of age/seeds, blocklist immediately.
        if state == "missingFiles":
            dead.append((t["hash"].lower(), t.get("category", ""), t["name"]))
            continue
        # availability < 1.0 means no combination of currently-reachable
        # peers has a complete copy, so this can never finish even if the
        # tracker's stale "seed count" (num_complete) says otherwise.
        no_source = t.get("availability", 0) < 1.0
        # "forced" variants (torrents force-started/force-resumed, e.g. after
        # past debugging) were previously invisible to this check entirely.
        # 2026-07-21: also catch stopped/paused/errored states — a torrent
        # whose only file(s) matched excluded_file_names (e.g. the "House of
        # the Dragon" .scr malware dropper) downloads 0 bytes and qBittorrent
        # auto-stops it (state stoppedUP/stoppedDL/pausedUP/pausedDL/error),
        # which the old state list never treated as stalled — it just sat in
        # Sonarr's queue as a permanent "import failed" spam source instead.
        stalled = state in ("stalledDL", "queuedDL", "metaDL", "forcedMetaDL",
                             "stoppedDL", "stoppedUP", "pausedDL", "pausedUP",
                             "error", "unknown") or \
            (state in ("downloading", "forcedDL") and t["dlspeed"] == 0)
        age_h = (NOW - t.get("added_on", NOW)) / 3600
        # num_complete is the tracker's own count of full copies in the swarm.
        # Zero means there is no seed to find, as opposed to no_source, which
        # only says we haven't assembled a complete copy from reachable peers
        # yet. Nothing has been downloaded either, so there is nothing to lose
        # by re-searching for a healthier release.
        no_seed_anywhere = t.get("num_complete", 0) <= 0 and t["progress"] == 0
        if no_source and stalled:
            if age_h > DEAD_AGE_H or (no_seed_anywhere
                                      and age_h > DEAD_NO_SEED_AGE_H):
                dead.append((t["hash"].lower(), t.get("category", ""),
                             t["name"]))
    return dead


def pipeline_throughput(opener):
    """Report whether the download pipeline is actually moving any bytes.

    dead_torrents() only catches torrents past DEAD_AGE_H, so a pipeline that
    broke wholesale (VPN wedged, every grab seed-starved) looks healthy for two
    full days before anything fires. This is the cheap catch-all: incomplete
    torrents present, nothing downloading, and most of them stalled.
    """
    log("Download pipeline throughput:")
    try:
        info = qbit_get(opener, "/api/v2/transfer/info")
        ts = qbit_get(opener, "/api/v2/torrents/info")
    except Exception as e:
        log(f"  [FAIL] can't read transfer info: {e}")
        return
    speed = info.get("dl_info_speed", 0)
    conn = info.get("connection_status", "unknown")
    incomplete = [t for t in ts if t["progress"] < 1.0]
    STALLED = ("stalledDL", "metaDL", "forcedMetaDL", "queuedDL")
    stalled = [t for t in incomplete if t["state"] in STALLED]
    log(f"  [INFO] {len(incomplete)} incomplete, {len(stalled)} stalled, "
        f"{speed / 1048576:.2f} MB/s, connection={conn}")
    if conn not in ("connected", "firewalled"):
        log(f"  [WARN] qBittorrent connection status is '{conn}' — check gluetun")
        alert(f"qBittorrent connection status is '{conn}' (expected connected)")
    if not incomplete:
        return
    # Two thirds stalled with no meaningful throughput is the signature of the
    # 2026-08-07 incident. A handful of slow torrents alongside healthy ones is
    # normal and must stay quiet, or the alert gets ignored.
    #
    # 2026-08-08: this used to require `speed == 0` exactly, and that let the
    # whole thing through — the 03:00 run saw 13 of 15 stalled but measured
    # 0.10 MB/s dribbling out of two half-finished torrents, so it logged
    # "pipeline moving" and stayed silent for another day. Any single crawling
    # torrent was enough to suppress the alarm. Compare against a floor
    # instead: below this, nothing is meaningfully being fetched.
    if speed < STALL_FLOOR_BPS and len(stalled) >= max(3, (2 * len(incomplete)) // 3):
        log(f"  [WARN] pipeline appears stalled: {len(stalled)}/"
            f"{len(incomplete)} incomplete torrents stalled at 0 B/s")
        alert(f"download pipeline stalled: {len(stalled)}/{len(incomplete)} "
              f"incomplete torrents at 0 B/s (VPN up, connection={conn}). "
              f"Dead grabs clear automatically after {DEAD_NO_SEED_AGE_H}h "
              f"(zero-seed) or {DEAD_AGE_H}h; if this repeats, check indexer "
              f"seed quality.")
    else:
        log("  [OK]   pipeline moving or stalls within normal range")


def jf_request(method, path, headers=None, body=None):
    h = {"Authorization": f"MediaBrowser Token={JELLYFIN['key']}"}
    h.update(headers or {})
    return request(method, f"{JELLYFIN['url']}{path}", h, body)


def jf_items(itemtype, fields=""):
    f = f"&Fields={fields}" if fields else ""
    return json.load(jf_request(
        "GET", f"/Items?Recursive=true&IncludeItemTypes={itemtype}"
               f"&Limit=30000{f}")).get("Items", [])


def fix_missing_artwork():
    """Find Jellyfin items with no poster and give them one.

    Added 2026-08-08 after 17 items (1 movie, 1 series, 15 seasons) were found
    showing blank tiles. Order matters:

      1. Refresh images only. metadataRefreshMode stays None deliberately —
         a FullRefresh of *metadata* re-runs the filename parser and can
         overwrite a correctly-matched title with the raw release name (it
         renamed "Mortal Kombat II" to "Mortal.Kombat.II.2026.DCPRip.1080p-
         SOFCJ" during the manual repair). Images are what's missing; leave
         the metadata alone.
      2. Whatever is still bare afterwards has no artwork upstream at all, so
         seasons inherit their series poster rather than staying blank.

    Nothing here deletes or replaces existing artwork: every write targets an
    item that currently has no Primary image.
    """
    log("Artwork check (items with no poster):")
    try:
        missing = {t: [i for i in jf_items(t)
                       if not (i.get("ImageTags") or {}).get("Primary")]
                   for t in ARTWORK_TYPES}
    except Exception as e:
        log(f"  [FAIL] can't read Jellyfin library ({e})")
        return
    total = sum(len(v) for v in missing.values())
    if not total:
        log("  [OK]   every movie/series/season/episode has a poster")
        return
    log(f"  [INFO] {total} item(s) missing a poster: "
        + ", ".join(f"{t}={len(v)}" for t, v in missing.items() if v))

    for t, items in missing.items():
        for i in items:
            try:
                jf_request("POST",
                           f"/Items/{i['Id']}/Refresh"
                           "?metadataRefreshMode=None"
                           "&imageRefreshMode=FullRefresh"
                           "&replaceAllImages=false",
                           {"Content-Length": "0"})
            except urllib.error.HTTPError as e:
                log(f"  [WARN] refresh failed for {i.get('Name')} ({e.code})")
    time.sleep(45)  # provider fetches are queued, not synchronous

    # Seasons with nothing upstream: inherit the series poster.
    try:
        seasons = {i["Id"]: i for i in jf_items("Season", "ParentId")}
        series = {i["Id"]: i for i in jf_items("Series")}
    except Exception as e:
        log(f"  [FAIL] can't re-read library ({e})")
        return
    inherited = 0
    for sid, s in seasons.items():
        if (s.get("ImageTags") or {}).get("Primary"):
            continue
        parent = series.get(s.get("SeriesId") or s.get("ParentId"))
        if not parent or not (parent.get("ImageTags") or {}).get("Primary"):
            continue
        try:
            img = jf_request("GET",
                             f"/Items/{parent['Id']}/Images/Primary?maxWidth=600")
            ctype = img.headers.get("Content-Type", "image/jpeg")
            jf_request("POST", f"/Items/{sid}/Images/Primary",
                       {"Content-Type": ctype},
                       base64.b64encode(img.read()))
            inherited += 1
        except urllib.error.HTTPError as e:
            log(f"  [WARN] could not set poster on "
                f"{s.get('SeriesName')} / {s.get('Name')} ({e.code})")
    if inherited:
        log(f"  [FIX ] {inherited} season(s) inherited their series poster")

    # Uploaded images don't appear in ImageTags until the library re-indexes,
    # and clients key off those tags — without this the posters exist on disk
    # but still render as blank tiles.
    if inherited:
        try:
            jf_request("POST", "/Library/Refresh", {"Content-Length": "0"})
            log("  [FIX ] triggered library re-index so new posters register")
        except urllib.error.HTTPError as e:
            log(f"  [WARN] library re-index failed ({e.code})")

    try:
        left = sum(1 for t in ARTWORK_TYPES for i in jf_items(t)
                   if not (i.get("ImageTags") or {}).get("Primary"))
    except Exception:
        return
    if left:
        log(f"  [WARN] {left} item(s) still have no poster — no artwork "
            f"available from any provider, may need one set by hand")
        alert(f"{left} Jellyfin item(s) still missing a poster after repair")
    else:
        log("  [OK]   all posters present")


def clean_orphaned_downloads(opener):
    """Remove downloads-folder leftovers *arr no longer tracks in qBittorrent
    (imported + already removed via removeCompletedDownloads, or manually
    cleared) — but ONLY if every file inside already has a hardlink twin in
    the library (st_nlink > 1). That's what Radarr/Sonarr's hardlink-import
    leaves behind on a successful import, so it's the actual, checkable
    signal that a copy safely exists elsewhere. A file with nlink==1 is the
    only copy anywhere and is never touched, even past the grace period."""
    log("Downloads folder cleanup:")
    if not os.path.isdir(DOWNLOADS_DIR):
        log(f"  [SKIP] {DOWNLOADS_DIR} not present on this host")
        return
    try:
        ts = qbit_get(opener, "/api/v2/torrents/info")
    except Exception as e:
        log(f"  [FAIL] can't list torrents for cleanup: {e}")
        return
    active_names = {t.get("name", "") for t in ts}

    try:
        entries = os.listdir(DOWNLOADS_DIR)
    except Exception as e:
        log(f"  [FAIL] can't list {DOWNLOADS_DIR}: {e}")
        return

    removed, kept_unimported = 0, 0
    for entry in entries:
        if entry == "incomplete" or entry in active_names:
            continue
        full = os.path.join(DOWNLOADS_DIR, entry)
        try:
            mtime = os.path.getmtime(full)
        except FileNotFoundError:
            continue
        if (NOW - mtime) / 3600 < DOWNLOADS_GRACE_H:
            continue  # too fresh — could still be mid-import

        if os.path.isdir(full) and not os.path.islink(full):
            files = [os.path.join(r, n) for r, _, ns in os.walk(full) for n in ns]
        elif os.path.isfile(full):
            files = [full]
        else:
            continue
        if not files:
            continue

        try:
            all_imported = all(os.stat(f).st_nlink > 1 for f in files)
        except FileNotFoundError:
            continue
        if not all_imported:
            kept_unimported += 1
            continue

        try:
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
            removed += 1
        except Exception as e:
            log(f"  [WARN] failed to remove orphan '{entry}': {e}")

    if removed:
        log(f"  [FIX ] removed {removed} orphaned download(s) — each had a "
            f"hardlink twin already in the library, so no data was lost")
    else:
        log("  [OK]   no orphaned downloads to clean")
    if kept_unimported:
        log(f"  [WARN] left {kept_unimported} untracked item(s) alone — no "
            f"library hardlink found (never imported), needs a manual look")


# ── *arr ─────────────────────────────────────────────────────────────────────
def arr_get(arr, path):
    return json.load(request("GET", f"{arr['url']}/api/v3{path}",
                             {"X-Api-Key": arr["key"]}))


def arr_test_downloadclient(arr, name):
    try:
        clients = arr_get(arr, "/downloadclient")
    except Exception as e:
        log(f"  [FAIL] {name}: API unreachable ({e})")
        return
    if not clients:
        log(f"  [WARN] {name}: no download client configured")
        return
    body = json.dumps(clients[0]).encode()
    try:
        request("POST", f"{arr['url']}/api/v3/downloadclient/test",
                {"X-Api-Key": arr["key"], "Content-Type": "application/json"},
                body)
        log(f"  [OK]   {name}: download client reachable")
    except urllib.error.HTTPError as e:
        log(f"  [FAIL] {name}: download client test failed ({e.code})")


def arr_put(arr, path, payload):
    """PUT to an *arr resource, bypassing its live re-validation.

    Without forceSave the *arr re-tests the indexer against its tracker on every
    save and rejects the whole write with HTTP 400 if that tracker happens to be
    rate-limiting (429) or briefly down — so a settings change appears to apply
    but silently isn't persisted. That is why the minimumSeeders tuning below
    was found reverted on 2026-07-29 and again on 2026-08-07.
    """
    body = json.dumps(payload).encode()
    return request("PUT", f"{arr['url']}/api/v3{path}?forceSave=true",
                   {"X-Api-Key": arr["key"],
                    "Content-Type": "application/json"}, body)


def reconcile_prowlarr():
    """Enforce the seed/priority policy at Prowlarr, which owns the indexers.

    2026-08-08, the actual reason this policy kept coming back reverted. Every
    indexer in Radarr/Sonarr is Prowlarr-managed ("1337x (Prowlarr)"), and both
    apps are registered with syncLevel=fullSync, so on each sync Prowlarr
    rewrites their indexer definitions from its own state. Prowlarr's only App
    Profile still carried the default minimumSeeders=5, so it stamped 5 back
    over reconcile_indexers()'s 15 — which is why the 03:00 run reported
    "[FIX] corrected 8 indexer(s)" every single night and the value was still 5
    the next morning. Fixing the *arrs alone can never hold; the App Profile is
    the source of truth, so set it there and let fullSync propagate 15.

    reconcile_indexers() still runs afterwards: Prowlarr's own sync does not use
    forceSave, so an indexer whose tracker is rate-limiting at sync time gets
    skipped and needs the direct write.
    """
    log("Prowlarr indexer policy (source of truth for both *arrs):")
    try:
        profiles = json.load(request("GET", f"{PROWLARR['url']}/api/v1/appprofile",
                                     {"X-Api-Key": PROWLARR["key"]}))
    except Exception as e:
        log(f"  [FAIL] can't read Prowlarr app profiles ({e})")
        alert(f"Prowlarr app profiles unreadable ({e}) — indexer seed policy "
              f"cannot be enforced at source and will revert on next sync")
        return
    changed = False
    for prof in profiles:
        if prof.get("minimumSeeders") == INDEXER_MIN_SEEDERS:
            continue
        was = prof.get("minimumSeeders")
        prof["minimumSeeders"] = INDEXER_MIN_SEEDERS
        try:
            request("PUT",
                    f"{PROWLARR['url']}/api/v1/appprofile/{prof['id']}",
                    {"X-Api-Key": PROWLARR["key"],
                     "Content-Type": "application/json"},
                    json.dumps(prof).encode())
            log(f"  [FIX ] app profile '{prof.get('name')}': "
                f"minimumSeeders {was} -> {INDEXER_MIN_SEEDERS}")
            changed = True
        except urllib.error.HTTPError as e:
            log(f"  [FAIL] app profile '{prof.get('name')}' write failed "
                f"({e.code})")
            alert(f"Prowlarr app profile '{prof.get('name')}' could not be set "
                  f"to minimumSeeders={INDEXER_MIN_SEEDERS} (HTTP {e.code})")

    try:
        indexers = json.load(request("GET", f"{PROWLARR['url']}/api/v1/indexer",
                                     {"X-Api-Key": PROWLARR["key"]}))
    except Exception as e:
        log(f"  [FAIL] can't read Prowlarr indexers ({e})")
        indexers = []
    for ix in indexers:
        fixes = []

        target = DEPRIORITIZED_INDEXERS.get(ix.get("name", "").strip())
        if target is not None and ix.get("priority") != target:
            ix["priority"] = target
            fixes.append(f"priority -> {target}")

        # Third and lowest layer, found 2026-08-09. Each Prowlarr indexer can
        # carry its OWN seeder override in torrentBaseSettings.appMinimumSeeders,
        # and that value beats the App Profile when Prowlarr builds the *arr
        # indexer during fullSync. LimeTorrents, The Pirate Bay and YTS each had
        # a stale 5 here, which is why exactly those three kept reading
        # minSeeders=5 in both *arrs the morning after a run that had reported
        # "[FIX] corrected 3 indexer(s)" — the App Profile fix from 2026-08-08
        # was correct but sat one layer too high to stop them. Leaving the field
        # null is fine (the profile applies), so only a non-null wrong value is
        # rewritten.
        for field in ix.get("fields", []):
            if (field.get("name") == "torrentBaseSettings.appMinimumSeeders"
                    and field.get("value") is not None
                    and field.get("value") != INDEXER_MIN_SEEDERS):
                was = field["value"]
                field["value"] = INDEXER_MIN_SEEDERS
                fixes.append(f"appMinimumSeeders {was} -> {INDEXER_MIN_SEEDERS}")

        if not fixes:
            continue
        try:
            request("PUT",
                    f"{PROWLARR['url']}/api/v1/indexer/{ix['id']}"
                    f"?forceSave=true",
                    {"X-Api-Key": PROWLARR["key"],
                     "Content-Type": "application/json"},
                    json.dumps(ix).encode())
            log(f"  [FIX ] {ix['name']}: " + "; ".join(fixes))
            changed = True
        except urllib.error.HTTPError as e:
            log(f"  [FAIL] {ix['name']} write failed ({e.code}): "
                + "; ".join(fixes))
            alert(f"Prowlarr indexer '{ix['name']}' could not be corrected "
                  f"(HTTP {e.code}): {'; '.join(fixes)}")

    if not changed:
        log("  [OK]   Prowlarr policy already correct")
        return
    # Push the corrected state out now rather than waiting for Prowlarr's own
    # sync interval, so the *arrs are consistent before tonight's grabs.
    try:
        request("POST", f"{PROWLARR['url']}/api/v1/command",
                {"X-Api-Key": PROWLARR["key"],
                 "Content-Type": "application/json"},
                json.dumps({"name": "ApplicationIndexerSync"}).encode())
        log("  [FIX ] triggered ApplicationIndexerSync to both *arrs")
    except urllib.error.HTTPError as e:
        log(f"  [FAIL] could not trigger app sync ({e.code})")


def _flaresolverr_tag_id():
    """id of the 'flaresolverr' tag, or None if it doesn't exist."""
    try:
        tags = json.load(request("GET", f"{PROWLARR['url']}/api/v1/tag",
                                 {"X-Api-Key": PROWLARR["key"]}))
    except Exception as e:
        log(f"  [FAIL] can't read Prowlarr tags ({e})")
        return None
    for t in tags:
        if t.get("label", "").lower() == FLARESOLVERR_TAG:
            return t["id"]
    return None


def _cloudflare_indexers_from_log():
    """Indexer names Prowlarr has logged a Cloudflare block for.

    The static CLOUDFLARE_INDEXERS set below can only ever list trackers we
    already know about; trackers turn Cloudflare on whenever they feel like it,
    so the log is the authoritative source for *new* cases. Prowlarr writes
    "Cloudflare protection detected for [Name], Flaresolverr may be required."
    at Error level each time it hits an unsolved challenge.
    """
    out = docker("exec", "prowlarr", "sh", "-c",
                 "grep -h 'Cloudflare protection detected' "
                 "/config/logs/prowlarr.txt /config/logs/prowlarr.0.txt "
                 "2>/dev/null | tail -300")
    if out.startswith("__ERR__"):
        log(f"  [WARN] could not read Prowlarr logs for Cloudflare hits "
            f"({out[7:]}) — falling back to the static list only")
        return set()
    return set(re.findall(r"Cloudflare protection detected for \[([^\]]+)\]",
                          out))


def _tag_for_flaresolverr(ix, tag_id):
    """Add the flaresolverr tag to one indexer. Returns True on success."""
    ix = dict(ix)
    ix["tags"] = sorted(set(ix.get("tags") or []) | {tag_id})
    try:
        # forceSave: the same rate-limit case reconcile_prowlarr() documents —
        # a tracker throttling us at write time otherwise 400s the whole PUT.
        request("PUT",
                f"{PROWLARR['url']}/api/v1/indexer/{ix['id']}?forceSave=true",
                {"X-Api-Key": PROWLARR["key"],
                 "Content-Type": "application/json"},
                json.dumps(ix).encode())
        return True
    except urllib.error.HTTPError as e:
        log(f"  [FAIL] {ix.get('name')}: could not add flaresolverr tag "
            f"(HTTP {e.code})")
        alert(f"Prowlarr indexer '{ix.get('name')}' needs the flaresolverr "
              f"tag to get past Cloudflare but the write failed (HTTP {e.code})")
        return False


def verify_indexers():
    """Test every Prowlarr indexer, and repair the ones Cloudflare is blocking.

    2026-08-21. "Torrent Downloads" had been dead for three days — Prowlarr's
    health page said "Indexers unavailable due to failures for more than 6
    hours" and every search silently returned nothing from it. Root cause was
    not the tracker being down: it had switched on Cloudflare, and the indexer
    carried no `flaresolverr` tag, so Prowlarr never routed it through Byparr
    and every query died on the challenge page. Nothing in this script noticed,
    because a blocked indexer is not a dead torrent — the pipeline just quietly
    gets fewer releases to choose from, which looks like "nothing was
    available" rather than a fault.

    Two detectors, because either alone has a blind spot:
      - the log scan catches an indexer that fails only intermittently (the
        real one failed at ~14:10 daily but tested fine at 03:00), and
      - testall catches an indexer that is broken right now for any other
        reason (dead domain, expired cookie, tracker gone).
    A test that passes also clears Prowlarr's failure backoff, so an indexer
    that recovered on its own stops being reported as unavailable.
    """
    log("Indexer availability (Cloudflare routing + live test):")
    try:
        indexers = json.load(request("GET", f"{PROWLARR['url']}/api/v1/indexer",
                                     {"X-Api-Key": PROWLARR["key"]}))
    except Exception as e:
        log(f"  [FAIL] can't read Prowlarr indexers ({e})")
        alert(f"Prowlarr indexers unreadable ({e}) — indexer availability "
              f"could not be checked")
        return

    tag_id = _flaresolverr_tag_id()
    if tag_id is None:
        log("  [WARN] no 'flaresolverr' tag in Prowlarr — cannot route "
            "Cloudflare-protected indexers through Byparr")
        alert("Prowlarr has no 'flaresolverr' tag, so Cloudflare-protected "
              "indexers cannot be repaired automatically")

    needs_cf = CLOUDFLARE_INDEXERS | _cloudflare_indexers_from_log()
    tagged = []
    if tag_id is not None:
        for ix in indexers:
            name = (ix.get("name") or "").strip()
            if name in needs_cf and tag_id not in (ix.get("tags") or []):
                if _tag_for_flaresolverr(ix, tag_id):
                    tagged.append(name)
    if tagged:
        log(f"  [FIX ] routed {len(tagged)} Cloudflare-blocked indexer(s) "
            f"through Byparr: {', '.join(sorted(tagged))}")
        alert(f"indexer(s) were being blocked by Cloudflare with no "
              f"flaresolverr tag, now routed through Byparr: "
              f"{', '.join(sorted(tagged))}")

    # Live test of everything. Slow (each Cloudflare solve costs ~10-20s), but
    # this runs once a night and it is the only check that proves a tracker
    # will actually answer a query tonight.
    try:
        results = json.load(request(
            "POST", f"{PROWLARR['url']}/api/v1/indexer/testall",
            {"X-Api-Key": PROWLARR["key"], "Content-Type": "application/json"},
            b"", timeout=600))
    except Exception as e:
        log(f"  [FAIL] indexer testall failed ({e})")
        alert(f"Prowlarr indexer test sweep failed ({e})")
        return

    by_id = {ix["id"]: (ix.get("name") or "").strip() for ix in indexers}
    broken = []
    for r in results:
        if r.get("isValid"):
            continue
        name = by_id.get(r.get("id"), f"id={r.get('id')}")
        why = "; ".join(f.get("errorMessage", "")
                        for f in r.get("validationFailures", [])) or "unknown"
        # Second chance: a failure on an indexer with no flaresolverr tag is
        # most likely a challenge page, whatever the message says. Tag it and
        # retest that one before reporting it as broken.
        ix = next((i for i in indexers if i["id"] == r.get("id")), None)
        if (tag_id is not None and ix is not None
                and tag_id not in (ix.get("tags") or [])):
            if _tag_for_flaresolverr(ix, tag_id):
                ix["tags"] = sorted(set(ix.get("tags") or []) | {tag_id})
                try:
                    retest = request(
                        "POST", f"{PROWLARR['url']}/api/v1/indexer/test",
                        {"X-Api-Key": PROWLARR["key"],
                         "Content-Type": "application/json"},
                        json.dumps(ix).encode(), timeout=180)
                    if retest.status in (200, 202):
                        log(f"  [FIX ] {name}: failed, then passed once routed "
                            f"through Byparr")
                        alert(f"indexer '{name}' was failing and recovered "
                              f"after being routed through Byparr")
                        continue
                except Exception:
                    pass
        broken.append(f"{name} ({why[:120]})")

    if broken:
        log(f"  [WARN] {len(broken)} indexer(s) still failing: "
            f"{'; '.join(broken)}")
        alert(f"{len(broken)} indexer(s) failing and not auto-repairable: "
              f"{'; '.join(broken)} — fewer releases will be found for "
              f"requests until fixed")
    else:
        log(f"  [OK]   all {len(results)} indexer(s) answered a live query")

    if tagged:
        try:
            request("POST", f"{PROWLARR['url']}/api/v1/command",
                    {"X-Api-Key": PROWLARR["key"],
                     "Content-Type": "application/json"},
                    json.dumps({"name": "ApplicationIndexerSync"}).encode())
            log("  [FIX ] triggered ApplicationIndexerSync to both *arrs")
        except urllib.error.HTTPError as e:
            log(f"  [FAIL] could not trigger app sync ({e.code})")


def reconcile_indexers(arr, name):
    """Enforce the minimum-seeders / priority policy on every indexer."""
    try:
        indexers = arr_get(arr, "/indexer")
    except Exception as e:
        log(f"  [FAIL] {name}: can't read indexers ({e})")
        return
    fixed, failed = [], []
    for ix in indexers:
        want = []
        for field in ix.get("fields", []):
            if (field.get("name") == "minimumSeeders"
                    and field.get("value") != INDEXER_MIN_SEEDERS):
                field["value"] = INDEXER_MIN_SEEDERS
                want.append(f"minSeeders={INDEXER_MIN_SEEDERS}")
        # Indexer names arrive as "1337x (Prowlarr)" once Prowlarr has synced.
        base = ix.get("name", "").replace("(Prowlarr)", "").strip()
        target = DEPRIORITIZED_INDEXERS.get(base)
        if target is not None and ix.get("priority") != target:
            ix["priority"] = target
            want.append(f"priority={target}")
        if not want:
            continue
        try:
            arr_put(arr, f"/indexer/{ix['id']}", ix)
            fixed.append(f"{base} ({', '.join(want)})")
        except Exception as e:
            failed.append(f"{base}: {e}")
    if fixed:
        log(f"  [FIX ] {name}: corrected {len(fixed)} indexer(s): "
            f"{'; '.join(fixed)}")
        alert(f"{name}: indexer settings had drifted, corrected "
              f"{len(fixed)}: {'; '.join(fixed)}")
    if failed:
        log(f"  [FAIL] {name}: could not update {len(failed)} indexer(s): "
            f"{'; '.join(failed)}")
        alert(f"{name}: indexer settings could not be written: "
              f"{'; '.join(failed)}")
    if not fixed and not failed:
        log(f"  [OK]   {name}: all {len(indexers)} indexer(s) match policy")


def reconcile_malware_format(arr, name):
    """Ensure the malware/executable Custom Format exists, matches the
    canonical pattern, and is scored -10000 in every quality profile.

    Returns the format's id (for verify_malware_format), or None if it
    couldn't be created/read.
    """
    try:
        cfs = arr_get(arr, "/customformat")
    except Exception as e:
        log(f"  [FAIL] {name}: can't read custom formats ({e})")
        return None

    spec = {
        "name": "Executable extension in release title",
        "implementation": "ReleaseTitleSpecification",
        "negate": False,
        "required": True,
        "fields": [{"name": "value", "value": MALWARE_FORMAT_PATTERN}],
    }
    body = {"name": MALWARE_FORMAT_NAME, "includeCustomFormatWhenRenaming": False,
            "specifications": [spec]}

    cf = next((c for c in cfs if c["name"] == MALWARE_FORMAT_NAME), None)
    if cf is None:
        try:
            cf = json.load(request(
                "POST", f"{arr['url']}/api/v3/customformat",
                {"X-Api-Key": arr["key"], "Content-Type": "application/json"},
                json.dumps(body).encode()))
            log(f"  [FIX ] {name}: recreated missing "
                f"'{MALWARE_FORMAT_NAME}' custom format")
            alert(f"{name}: the malware-blocking custom format was missing "
                  f"(deleted or never synced) and has been recreated")
        except Exception as e:
            log(f"  [FAIL] {name}: could not create malware custom format ({e})")
            return None
    else:
        cur_pattern = next(
            (f.get("value") for s in cf.get("specifications", [])
             for f in s.get("fields", []) if f.get("name") == "value"), None)
        if cur_pattern != MALWARE_FORMAT_PATTERN:
            body["id"] = cf["id"]
            try:
                request("PUT", f"{arr['url']}/api/v3/customformat/{cf['id']}",
                        {"X-Api-Key": arr["key"], "Content-Type": "application/json"},
                        json.dumps(body).encode())
                log(f"  [FIX ] {name}: corrected drifted malware-format pattern")
                alert(f"{name}: the malware-blocking custom format's pattern "
                      f"had drifted and was corrected")
            except Exception as e:
                log(f"  [FAIL] {name}: could not update malware custom "
                    f"format ({e})")

    cf_id = cf["id"]
    try:
        profiles = arr_get(arr, "/qualityprofile")
    except Exception as e:
        log(f"  [FAIL] {name}: can't read quality profiles ({e})")
        return cf_id

    fixed = []
    for qp in profiles:
        items = qp.get("formatItems", [])
        item = next((i for i in items if i["format"] == cf_id), None)
        if item is not None and item.get("score") == MALWARE_FORMAT_SCORE:
            continue
        if item is None:
            items.append({"format": cf_id, "name": MALWARE_FORMAT_NAME,
                          "score": MALWARE_FORMAT_SCORE})
            qp["formatItems"] = items
        else:
            item["score"] = MALWARE_FORMAT_SCORE
        try:
            request("PUT", f"{arr['url']}/api/v3/qualityprofile/{qp['id']}",
                    {"X-Api-Key": arr["key"], "Content-Type": "application/json"},
                    json.dumps(qp).encode())
            fixed.append(qp["name"])
        except Exception as e:
            log(f"  [FAIL] {name}: could not fix profile '{qp['name']}' ({e})")

    if fixed:
        log(f"  [FIX ] {name}: malware-format score corrected in "
            f"profile(s): {', '.join(fixed)}")
        alert(f"{name}: malware-blocking custom format score had drifted in "
              f"{len(fixed)} quality profile(s), corrected to "
              f"{MALWARE_FORMAT_SCORE}")
    return cf_id


def verify_malware_format(arr, name, cf_id):
    """Prove end-to-end that a disguised-executable release is actually
    rejected by the real grab-decision pipeline, not just present in config.

    release/push runs a release through the exact same DownloadDecisionMaker
    used for automatic RSS/search grabs; a rejected release is never queued
    or written to history, so this is safe to run every night.
    """
    if cf_id is None:
        return
    try:
        if arr is RADARR:
            library = arr_get(arr, "/movie")
            if not library:
                log(f"  [SKIP] {name}: no movies in library for live-fire test")
                return
            # Radarr's parser refuses to recognise a release as a movie at
            # all (400 "Unable to parse") without a year in the title.
            base_title = (f"{library[0]['title'].replace(':', '').strip()} "
                          f"{library[0].get('year', '')}").strip()
        else:
            library = arr_get(arr, "/series")
            if not library:
                log(f"  [SKIP] {name}: no series in library for live-fire test")
                return
            base_title = f"{library[0]['title'].replace(':', '').strip()} S01E01"

        stamp = str(int(time.time()))
        bad_title = f"{base_title} 720p WEB h264-SELFHEAL{stamp}.exe"
        push_body = {
            "title": bad_title,
            "downloadUrl": f"magnet:?xt=urn:btih:{'0' * 39}1&dn=selfheal-{stamp}",
            "magnetUrl": f"magnet:?xt=urn:btih:{'0' * 39}1&dn=selfheal-{stamp}",
            "protocol": "torrent",
            "publishDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "size": 900000000,
            "indexerId": 0,
            "indexer": "selfheal-test",
            "guid": f"selfheal-malware-test-{stamp}",
            "seeders": 10,
            "leechers": 1,
        }
        resp = request("POST", f"{arr['url']}/api/v3/release/push",
                       {"X-Api-Key": arr["key"], "Content-Type": "application/json"},
                       json.dumps(push_body).encode(), timeout=30)
        results = json.load(resp)
        rel = results[0] if results else {}
        matched = MALWARE_FORMAT_NAME in [c.get("name") for c in
                                           rel.get("customFormats", [])]
        rejected_for_it = any(MALWARE_FORMAT_NAME in r
                              for r in rel.get("rejections", []))
        if matched and rejected_for_it and rel.get("rejected"):
            log(f"  [OK]   {name}: live-fire test confirmed a disguised "
                f".exe release is rejected before grab")
        else:
            log(f"  [FAIL] {name}: live-fire test did NOT reject a "
                f"disguised .exe release -- protection is not working")
            alert(f"{name}: the malware/executable release blocker failed "
                  f"its live self-test -- disguised executables may be "
                  f"grabbable again, check the '{MALWARE_FORMAT_NAME}' "
                  f"custom format")
    except Exception as e:
        log(f"  [FAIL] {name}: malware-format live-fire test errored ({e})")
        alert(f"{name}: could not run the malware-blocker self-test ({e})")


def _wanted_state():
    try:
        with open(WANTED_STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_wanted_state(st):
    # Prune anything past its cooldown so the file can't grow without bound.
    cutoff = time.time() - (WANTED_RETRY_DAYS * 86400)
    st = {k: v for k, v in st.items() if v > cutoff}
    try:
        os.makedirs(os.path.dirname(WANTED_STATE), exist_ok=True)
        with open(WANTED_STATE, "w") as fh:
            json.dump(st, fh)
    except Exception as e:
        log(f"  [WARN] could not write {WANTED_STATE} ({e}) — the same items "
            f"will be re-searched tomorrow")


def grab_wanted():
    """Search a bounded batch of monitored-but-missing movies/episodes.

    2026-08-21. Overseerr reported 375 requests, every one of them "approved /
    processing" and none available, while Radarr held 130 missing movies and
    Sonarr 137 missing episodes with an empty download queue. Nothing was
    broken: neither *arr has ANY recurring "search for missing" task — the task
    list is RssSync plus housekeeping, and RssSync only ever sees what an
    indexer is publishing on its feed *right now*. So a title requested after
    its release was already posted gets searched exactly once (on add) and, if
    that moment happened to find nothing, never again. That is the whole reason
    requests sit in "processing" forever.

    Deliberately bounded rather than "search everything":
      - only items whose release/air date has actually passed, because a 2026
        `announced` film has nothing to find and burns indexer queries anyway
        (over half of Radarr's missing list is unreleased);
      - WANTED_SEARCH_BATCH items per *arr per night, since each search fans out
        across 9 indexers and hammering them is how this box got an indexer
        IP-banned before;
      - newest-first, so a fresh request is picked up the same night;
      - a per-item cooldown, so an unobtainable title cannot occupy a slot in
        the batch every night and starve the rest of the backlog.
    """
    log(f"Wanted-backlog search (newest first, "
        f"{WANTED_SEARCH_BATCH}/service/night, "
        f"{WANTED_RETRY_DAYS}-day per-item cooldown):")
    st = _wanted_state()
    now = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def pick(key_prefix, records, ready):
        out = []
        for rec in records:
            if len(out) >= WANTED_SEARCH_BATCH:
                break
            if not ready(rec):
                continue
            key = f"{key_prefix}:{rec['id']}"
            if now - st.get(key, 0) < WANTED_RETRY_DAYS * 86400:
                continue
            st[key] = now
            out.append(rec)
        return out

    # ── Radarr ───────────────────────────────────────────────────────────────
    try:
        w = arr_get(RADARR, "/wanted/missing?pageSize=200&sortKey=added"
                            "&sortDirection=descending&monitored=true")
        recs = w.get("records", [])
        # Radarr status: tba / announced / inCinemas / released / deleted.
        # Only "released" means a digital release exists to look for.
        want = pick("radarr", recs,
                    lambda r: r.get("status") == "released"
                    and not r.get("hasFile"))
        skipped = len(recs) - sum(1 for r in recs
                                  if r.get("status") == "released")
        if want:
            request("POST", f"{RADARR['url']}/api/v3/command",
                    {"X-Api-Key": RADARR["key"],
                     "Content-Type": "application/json"},
                    json.dumps({"name": "MoviesSearch",
                                "movieIds": [r["id"] for r in want]}).encode())
            log(f"  [FIX ] Radarr: searching {len(want)} missing movie(s) "
                f"({w.get('totalRecords')} missing total, {skipped} of this "
                f"page not released yet): "
                + ", ".join(r["title"][:28] for r in want[:5])
                + (" ..." if len(want) > 5 else ""))
        else:
            log(f"  [OK]   Radarr: nothing to search "
                f"({w.get('totalRecords')} missing, none both released and "
                f"off cooldown)")
    except Exception as e:
        log(f"  [FAIL] Radarr wanted search failed ({e})")
        alert(f"Radarr backlog search failed ({e}) — requested movies will "
              f"stay stuck in 'processing'")

    # ── Sonarr ───────────────────────────────────────────────────────────────
    try:
        w = arr_get(SONARR, "/wanted/missing?pageSize=200&sortKey=airDateUtc"
                            "&sortDirection=descending&monitored=true"
                            "&includeSeries=true")
        recs = w.get("records", [])
        # ISO-8601 UTC strings compare correctly as plain strings.
        want = pick("sonarr", recs,
                    lambda r: (r.get("airDateUtc") or "9999") <= now_iso)
        if want:
            request("POST", f"{SONARR['url']}/api/v3/command",
                    {"X-Api-Key": SONARR["key"],
                     "Content-Type": "application/json"},
                    json.dumps({"name": "EpisodeSearch",
                                "episodeIds": [r["id"] for r in want]}).encode())
            def _ep(r):
                s = r.get("series", {}).get("title", "?")[:20]
                return f"{s} S{r.get('seasonNumber'):02d}E{r.get('episodeNumber'):02d}"
            log(f"  [FIX ] Sonarr: searching {len(want)} missing episode(s) "
                f"({w.get('totalRecords')} missing total): "
                + ", ".join(_ep(r) for r in want[:5])
                + (" ..." if len(want) > 5 else ""))
        else:
            log(f"  [OK]   Sonarr: nothing to search "
                f"({w.get('totalRecords')} missing, none both aired and off "
                f"cooldown)")
    except Exception as e:
        log(f"  [FAIL] Sonarr wanted search failed ({e})")
        alert(f"Sonarr backlog search failed ({e}) — requested episodes will "
              f"stay stuck in 'processing'")

    _save_wanted_state(st)


def _seerr_key(container):
    """Seerr's API key out of its settings.json.

    GOTCHA the file is pretty-printed as `"apiKey": "..."` WITH a space after
    the colon. A sed/grep that assumes `"apiKey":"` silently yields an empty
    string, and every call then fails with "cookie 'connect.sid' required"
    rather than an auth error, which reads like the endpoint is broken.
    """
    out = docker("exec", container, "sh", "-c",
                 "grep -o '\"apiKey\": \"[^\"]*\"' /app/config/settings.json "
                 "| head -1")
    if out.startswith("__ERR__") or '"' not in out:
        return None
    try:
        return out.split(': "', 1)[1].rstrip('"').strip() or None
    except Exception:
        return None


def _seerr_get(inst, path, key, timeout=90):
    return json.load(request("GET", f"{inst['url']}/api/v1{path}",
                             {"X-Api-Key": key}, timeout=timeout))


def check_seerr_libraries():
    """Make sure each Seerr can actually see its media server's libraries.

    2026-08-21. Overseerr had marked NOTHING available since it was migrated:
    375 requests all sitting at "processing", and every media row carrying
    ratingKey=None — it had never matched a single request to a Plex item, even
    for films already in the library. Plex connectivity was fine and all three
    scan jobs were scheduled and running on time. The cause was that
    `GET /settings/plex` reported **`libraries: 0`**: the library list had never
    been synced and enabled, so every scan dutifully scanned nothing.

    That is worth guarding because it is invisible from every direction that
    normally gets checked — the containers are healthy, the jobs run, the API
    answers, and the only symptom is requests quietly never completing, which
    looks exactly like "the download hasn't happened yet".

    Repairing it is safe and idempotent (sync the library list, enable the
    libraries that exist, kick a full scan), so this fixes rather than only
    warning.
    """
    log("Seerr library configuration (media-server availability matching):")
    for inst in SEERR_INSTANCES:
        name = inst["name"]
        key = _seerr_key(inst["container"])
        if not key:
            log(f"  [WARN] {name}: could not read its API key — skipped")
            alert(f"{name}: API key unreadable, its library configuration "
                  f"could not be verified")
            continue

        # Which media server backs this instance: the one with a host set.
        server = None
        for kind in ("plex", "jellyfin"):
            try:
                cfg = _seerr_get(inst, f"/settings/{kind}", key)
            except Exception:
                continue
            if (cfg.get("ip") or cfg.get("hostname") or "").strip():
                server, scfg = kind, cfg
                break
        if server is None:
            log(f"  [WARN] {name}: no media server configured at all")
            alert(f"{name}: has no Plex/Jellyfin server configured, so it can "
                  f"never mark a request available")
            continue

        libs = scfg.get("libraries", []) or []
        enabled = [l for l in libs if l.get("enabled")]
        if enabled:
            log(f"  [OK]   {name}: {len(enabled)}/{len(libs)} {server} "
                f"librar(ies) enabled "
                f"({', '.join(l.get('name', '?') for l in enabled)})")
            continue

        # Zero enabled: the actual failure. Sync the list, then enable
        # everything that came back.
        log(f"  [WARN] {name}: {len(libs)} {server} librar(ies) configured, "
            f"NONE enabled — nothing can ever be marked available")
        try:
            found = _seerr_get(inst, f"/settings/{server}/library?sync=true",
                               key, timeout=180)
            ids = [str(l["id"]) for l in found]
            if not ids:
                log(f"  [FAIL] {name}: {server} returned no libraries to enable")
                alert(f"{name}: {server} reported no libraries at all — check "
                      f"the server connection")
                continue
            # NOTE: enabling is a GET with a comma-separated `enable=` query
            # param, not a POST body.
            after = _seerr_get(
                inst, f"/settings/{server}/library?enable={','.join(ids)}",
                key, timeout=180)
            now_on = [l.get("name", "?") for l in after if l.get("enabled")]
            # Kick a full scan so the backlog is matched immediately rather
            # than waiting for the overnight job.
            job = None
            try:
                jobs = _seerr_get(inst, "/settings/jobs", key)
                job = next((j["id"] for j in jobs
                            if j["id"].endswith("-full-scan")
                            and j["id"].startswith(server)), None)
            except Exception:
                pass
            if job:
                request("POST", f"{inst['url']}/api/v1/settings/jobs/{job}/run",
                        {"X-Api-Key": key, "Content-Type": "application/json"},
                        b"")
            log(f"  [FIX ] {name}: enabled {len(now_on)} {server} librar(ies) "
                f"({', '.join(now_on)})"
                + (f" and started {job}" if job else ""))
            alert(f"{name}: its {server} libraries were all disabled, so no "
                  f"request could ever be marked available — enabled "
                  f"{', '.join(now_on)} and started a full scan")
        except Exception as e:
            log(f"  [FAIL] {name}: could not enable {server} libraries ({e})")
            alert(f"{name}: {server} libraries are all disabled and could not "
                  f"be enabled ({e}) — requests will never show as available")


def blocklist_dead(arr, name, dead_hashes):
    """Blocklist+remove *arr queue records whose download is dead, re-search."""
    if not dead_hashes:
        return
    try:
        q = arr_get(arr, "/queue?pageSize=1000")
    except Exception as e:
        log(f"  [FAIL] {name}: can't read queue ({e})")
        return
    ids = [r["id"] for r in q.get("records", [])
           if (r.get("downloadId", "") or "").lower() in dead_hashes]
    if not ids:
        # Previously a silent `return`. On 2026-08-07 the 03:00 run detected 16
        # dead torrents and then printed nothing at all, so the stall stayed
        # invisible while the queue sat broken — the containers had only just
        # come back from a two-day outage and the *arr queues had not been
        # rebuilt from the download client yet. A dead torrent that no queue
        # record claims is exactly the case worth shouting about: nothing will
        # ever clear it, because clearing is driven off the queue.
        log(f"  [WARN] {name}: {len(dead_hashes)} dead torrent(s) in "
            f"qBittorrent have no matching queue record — cannot blocklist "
            f"(queue has {len(q.get('records', []))} record(s)); "
            f"if this repeats, the torrents are orphaned and need removing "
            f"from qBittorrent directly")
        alert(f"{name}: {len(dead_hashes)} dead torrent(s) could not be "
              f"cleared — no matching queue record")
        return
    body = json.dumps({"ids": ids}).encode()
    url = (f"{arr['url']}/api/v3/queue/bulk"
           "?removeFromClient=true&blocklist=true&skipRedownload=false")
    try:
        request("DELETE", url,
                {"X-Api-Key": arr["key"], "Content-Type": "application/json"},
                body)
        log(f"  [FIX ] {name}: blocklisted+re-searched {len(ids)} dead item(s)")
        alert(f"{name}: blocklisted+re-searched {len(ids)} dead download(s)")
    except urllib.error.HTTPError as e:
        log(f"  [FAIL] {name}: bulk blocklist failed ({e.code})")
        alert(f"{name}: bulk blocklist of {len(ids)} dead item(s) failed "
              f"(HTTP {e.code})")


# 2026-07-21: added after a fake "House of the Dragon" .scr malware dropper
# sat in Sonarr's queue for a full day spamming "import failed" — qBittorrent
# had already correctly excluded the file (0 bytes downloaded, torrent
# auto-stopped), but *arr can't know a torrent is permanently unimportable
# from its own queue view alone. The message below is *arr's own explicit
# "this will never import" signal, so unlike blocklist_dead() this doesn't
# wait on age or on qBittorrent-side torrent-state heuristics at all.
STUCK_IMPORT_PHRASES = (
    "no files found are eligible for import",
    "unable to import automatically",
)


def stuck_imports(arr, name):
    """Blocklist+remove *arr queue records that *arr itself has flagged as
    permanently unimportable (not a transient/slow-download warning)."""
    try:
        q = arr_get(arr, "/queue?pageSize=1000")
    except Exception as e:
        log(f"  [FAIL] {name}: can't read queue for stuck-import scan ({e})")
        return
    stuck_ids, stuck_titles = [], []
    for r in q.get("records", []):
        if r.get("trackedDownloadState") != "importPending":
            continue
        messages = " ".join(
            m for sm in r.get("statusMessages", []) for m in sm.get("messages", [])
        ).lower()
        if any(phrase in messages for phrase in STUCK_IMPORT_PHRASES):
            stuck_ids.append(r["id"])
            stuck_titles.append(r.get("title", "?"))
    if not stuck_ids:
        return
    body = json.dumps({"ids": stuck_ids}).encode()
    url = (f"{arr['url']}/api/v3/queue/bulk"
           "?removeFromClient=true&blocklist=true&skipRedownload=false")
    try:
        request("DELETE", url,
                {"X-Api-Key": arr["key"], "Content-Type": "application/json"},
                body)
        log(f"  [FIX ] {name}: cleared {len(stuck_ids)} permanently-unimportable "
            f"queue item(s): {', '.join(stuck_titles)}")
    except urllib.error.HTTPError as e:
        log(f"  [FAIL] {name}: bulk clear of stuck imports failed ({e.code})")


# ── library file audit (DB records vs. actual disk state) ──────────────────
# 2026-07-21: added after Sonarr's own BulkMoveSeries command silently
# deleted a just-downloaded episode file during a root-folder repoint —
# *arr's database kept reporting hasFile=true against a path that no longer
# existed on disk, and nothing else would ever have noticed. This audit reads
# every episode/movie file record's path (container-side) and checks it
# against the host filesystem.
AUDIT_PATH_MAP = [
    ("/data/media", "{{MEDIA_POOL}}"),
    ("/data/disk1", "{{MEDIA_DISK1}}"),
    ("/data/disk2", "{{MEDIA_DISK2}}"),
    ("/data/disk3", "{{MEDIA_DISK3}}"),
]
# Above this many missing files at once, don't auto-fix — far more likely a
# mount/environment problem (e.g. a disk not mounted yet) than real data
# loss, and clearing that many records would trigger a re-download storm.
AUDIT_AUTO_FIX_MAX = 10


def _audit_to_host(path):
    for c, h in AUDIT_PATH_MAP:
        if path.startswith(c):
            return h + path[len(c):]
    return path


def audit_sonarr_files():
    """Return [(episodeFileId, episodeId, title, path)] for episode file
    records whose path doesn't exist on disk."""
    missing = []
    try:
        series_list = arr_get(SONARR, "/series")
    except Exception as e:
        log(f"  [FAIL] Sonarr: can't list series for file audit ({e})")
        return missing
    for s in series_list:
        sid = s["id"]
        try:
            eps = arr_get(SONARR, f"/episode?seriesId={sid}")
            files = arr_get(SONARR, f"/episodefile?seriesId={sid}")
        except Exception as e:
            log(f"  [WARN] Sonarr: can't audit '{s['title']}' ({e})")
            continue
        ep_by_file = {e["episodeFileId"]: e["id"] for e in eps if e.get("hasFile")}
        for f in files:
            if not os.path.isfile(_audit_to_host(f["path"])):
                eid = ep_by_file.get(f["id"])
                missing.append((f["id"], eid, f"{s['title']}: {f['path']}"))
    return missing


def audit_radarr_files():
    """Return [(movieFileId, movieId, title, path)] for movie file records
    whose path doesn't exist on disk."""
    missing = []
    try:
        movies = arr_get(RADARR, "/movie")
    except Exception as e:
        log(f"  [FAIL] Radarr: can't list movies for file audit ({e})")
        return missing
    for m in movies:
        mf = m.get("movieFile")
        if not mf:
            continue
        if not os.path.isfile(_audit_to_host(mf["path"])):
            missing.append((mf["id"], m["id"], f"{m['title']}: {mf['path']}"))
    return missing


def heal_missing_sonarr(missing):
    if not missing:
        log("  [OK]   Sonarr: no missing episode files")
        return
    if len(missing) > AUDIT_AUTO_FIX_MAX:
        log(f"  [FAIL] Sonarr: {len(missing)} episode file(s) missing from disk "
            f"— too many to auto-fix safely (likely a disk/mount problem, not "
            f"real data loss). NOT auto-clearing; investigate manually:")
        for _, _, desc in missing[:5]:
            log(f"           {desc}")
        return
    log(f"  [FIX ] Sonarr: {len(missing)} episode file(s) missing from disk — "
        f"clearing stale record(s) and re-searching:")
    episode_ids = []
    for file_id, ep_id, desc in missing:
        log(f"           {desc}")
        try:
            request("DELETE", f"{SONARR['url']}/api/v3/episodefile/{file_id}",
                    {"X-Api-Key": SONARR["key"]})
        except Exception as e:
            log(f"           [WARN] couldn't clear episodefile {file_id}: {e}")
            continue
        if ep_id:
            episode_ids.append(ep_id)
    if episode_ids:
        body = json.dumps({"name": "EpisodeSearch", "episodeIds": episode_ids}).encode()
        try:
            request("POST", f"{SONARR['url']}/api/v3/command",
                    {"X-Api-Key": SONARR["key"], "Content-Type": "application/json"}, body)
            log(f"  [OK]   Sonarr: re-search triggered for {len(episode_ids)} episode(s)")
        except Exception as e:
            log(f"  [WARN] Sonarr: re-search trigger failed: {e}")


def heal_missing_radarr(missing):
    if not missing:
        log("  [OK]   Radarr: no missing movie files")
        return
    if len(missing) > AUDIT_AUTO_FIX_MAX:
        log(f"  [FAIL] Radarr: {len(missing)} movie file(s) missing from disk "
            f"— too many to auto-fix safely (likely a disk/mount problem, not "
            f"real data loss). NOT auto-clearing; investigate manually:")
        for _, _, desc in missing[:5]:
            log(f"           {desc}")
        return
    log(f"  [FIX ] Radarr: {len(missing)} movie file(s) missing from disk — "
        f"clearing stale record(s) and re-searching:")
    movie_ids = []
    for file_id, movie_id, desc in missing:
        log(f"           {desc}")
        try:
            request("DELETE", f"{RADARR['url']}/api/v3/moviefile/{file_id}",
                    {"X-Api-Key": RADARR["key"]})
        except Exception as e:
            log(f"           [WARN] couldn't clear moviefile {file_id}: {e}")
            continue
        movie_ids.append(movie_id)
    if movie_ids:
        body = json.dumps({"name": "MoviesSearch", "movieIds": movie_ids}).encode()
        try:
            request("POST", f"{RADARR['url']}/api/v3/command",
                    {"X-Api-Key": RADARR["key"], "Content-Type": "application/json"}, body)
            log(f"  [OK]   Radarr: re-search triggered for {len(movie_ids)} movie(s)")
        except Exception as e:
            log(f"  [WARN] Radarr: re-search trigger failed: {e}")


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    print("  --- media-stack self-heal ---", flush=True)
    vpn_tunnel_health()
    ensure_containers()
    time.sleep(5)   # let anything just-started settle

    opener = qbit_login()
    dead = []
    if opener:
        reconcile_prefs(opener)
        dead = dead_torrents(opener)
        log(f"Dead-download scan: {len(dead)} torrent(s) dead "
            f"(no full copy reachable; >{DEAD_NO_SEED_AGE_H}h with zero "
            f"seeds in the swarm, or >{DEAD_AGE_H}h otherwise)")

    if opener:
        pipeline_throughput(opener)

    log("Download-client connectivity:")
    arr_test_downloadclient(RADARR, "Radarr")
    arr_test_downloadclient(SONARR, "Sonarr")

    # Prowlarr first: it owns the indexer definitions and fullSyncs them over
    # whatever the *arrs hold, so fixing the *arrs before it would be undone.
    reconcile_prowlarr()

    # After the policy pass: a correctly-configured indexer is still useless if
    # Cloudflare is eating its queries, and that failure mode is silent.
    verify_indexers()

    log("Indexer release-quality policy:")
    reconcile_indexers(RADARR, "Radarr")
    reconcile_indexers(SONARR, "Sonarr")

    log("Malware/executable release blocking:")
    radarr_malware_cf = reconcile_malware_format(RADARR, "Radarr")
    sonarr_malware_cf = reconcile_malware_format(SONARR, "Sonarr")
    verify_malware_format(RADARR, "Radarr", radarr_malware_cf)
    verify_malware_format(SONARR, "Sonarr", sonarr_malware_cf)

    if dead:
        log("Clearing dead downloads (blocklist + re-search):")
        blocklist_dead(RADARR, "Radarr",
                       {h for h, c, _ in dead if c == RADARR["cat"]})
        blocklist_dead(SONARR, "Sonarr",
                       {h for h, c, _ in dead if c == SONARR["cat"]})

    log("Stuck-import scan (queue items *arr itself flags as unimportable):")
    stuck_imports(RADARR, "Radarr")
    stuck_imports(SONARR, "Sonarr")

    # After the queue has been cleaned, not before: dead/stuck items are
    # cleared above, so anything grabbed here starts against a free queue.
    grab_wanted()

    # The other half of "is a request ever going to complete": grab_wanted()
    # gets the file downloaded, this makes sure Seerr can then SEE it.
    check_seerr_libraries()

    if opener:
        clean_orphaned_downloads(opener)

    log("Library file audit (DB records vs. actual disk state):")
    heal_missing_sonarr(audit_sonarr_files())
    heal_missing_radarr(audit_radarr_files())

    # Runs last: it triggers a library re-index, and the audits above are
    # cheaper to reason about against a library that hasn't just been rescanned.
    fix_missing_artwork()

    flush_alerts()
    print("  --- self-heal complete ---", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"    [FAIL] self-heal crashed: {e}", flush=True)
        alert(f"self-heal script crashed: {type(e).__name__}: {e}")
        try:
            flush_alerts()
        except Exception:
            pass
    sys.exit(0)
