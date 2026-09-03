#!/usr/bin/env python3
"""Safe nightly auto-update for the *arr/Seerr/Plex/Jellyfin apps.

For each managed service: pull the image, and if a new one actually landed,
recreate the container and verify it's actually healthy (not just "up").
If verification fails, roll back to the previous image (and, for the small
config apps, the pre-update config backup too) and remember the bad image ID
so we don't hammer the same broken update every night.

Added 2026-07-23 after manually updating Radarr to 6.3.0 and checking it by
hand — this automates that same pull/verify/rollback pattern.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path.home()
VPN_DIR = ROOT / "docker/vpn-stack"
MEDIA_DIR = ROOT / "docker/media-stack"
BACKUP_DIR = ROOT / "backups/auto-update"
STATE_DIR = ROOT / ".hermes/state/auto-update"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

# backup=True services get a full config tar before update + tar restore on
# rollback (all under ~1GB combined). Plex/Jellyfin configs are 17-18GB each
# — too big to tar every run on a box that's filled its root disk twice
# before — so those two rely on their built-in Docker healthcheck + an
# image-only rollback (no data touched, so nothing to restore).
SERVICES = [
    dict(name="sonarr", compose_dir=VPN_DIR, image="lscr.io/linuxserver/sonarr:latest",
         mount="vpn-stack_sonarr_config", backup=True, kind="arr", port=8989, api_base="/api/v3"),
    dict(name="radarr", compose_dir=VPN_DIR, image="lscr.io/linuxserver/radarr:latest",
         mount="vpn-stack_radarr_config", backup=True, kind="arr", port=7878, api_base="/api/v3"),
    dict(name="prowlarr", compose_dir=VPN_DIR, image="lscr.io/linuxserver/prowlarr:latest",
         mount="vpn-stack_prowlarr_config", backup=True, kind="arr", port=9696, api_base="/api/v1"),
    dict(name="qbittorrent", compose_dir=VPN_DIR, image="lscr.io/linuxserver/qbittorrent:latest",
         mount="vpn-stack_qbittorrent_config", backup=True, kind="http", port=8080, path="/"),
    dict(name="jellyseerr", compose_dir=VPN_DIR, image="ghcr.io/seerr-team/seerr:v3.3.0",
         mount="vpn-stack_jellyseerr_config", backup=True, kind="seerr", port=5056),
    dict(name="overseerr", compose_dir=MEDIA_DIR, image="ghcr.io/seerr-team/seerr:v3.3.0",
         mount=str(MEDIA_DIR / "overseerr"), backup=True, kind="seerr", port=5055),
    dict(name="plex", compose_dir=MEDIA_DIR, image="plexinc/pms-docker",
         mount=None, backup=False, kind="dockerhealth", port=32400),
    dict(name="jellyfin", compose_dir=MEDIA_DIR, image="jellyfin/jellyfin",
         mount=None, backup=False, kind="dockerhealth", port=8096),
]


def log(msg):
    print(f"  {msg}", flush=True)


# Every warn() is also collected and emailed as one digest at the end of the
# run (added 2026-08-06). Before this, a failed update + rollback was visible
# only as a WARNING buried in that night's daily-routine log, which nobody reads.
WARNINGS = []


def warn(msg):
    print(f"  WARNING: {msg}", flush=True)
    WARNINGS.append(msg)


def flush_alerts():
    if not WARNINGS:
        return
    import importlib.util
    host = os.uname().nodename
    critical = any("CRITICAL" in w for w in WARNINGS)
    subject = (f"[homelab] CRITICAL: media-stack auto-update rollback failed on {host}"
               if critical else
               f"[homelab] {len(WARNINGS)} auto-update warning(s) on {host}")
    body = ("media-stack-auto-update.py reported:\n\n"
            + "\n".join(f"  - {w}" for w in WARNINGS)
            + "\n\nFull log: ~/.hermes/maintenance-logs/daily-routine-*.log\n")
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send-alert.py")
        spec = importlib.util.spec_from_file_location("send_alert", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rc, info = mod.send(subject, body)
    except Exception as e:
        print(f"  WARNING: alert email failed to load sender: {e}", flush=True)
        return
    if rc == 0:
        log(f"alert email {info}")
    elif rc == 2:
        log(f"alert email not sent — {info}")
    else:
        print(f"  WARNING: alert email failed — {info}", flush=True)


def sh(cmd, cwd=None, timeout=120):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "TIMEOUT"


def image_id_of_container(name):
    rc, out = sh(["docker", "inspect", name, "--format", "{{.Image}}"])
    return out.strip() if rc == 0 else None


def local_image_id(ref):
    rc, out = sh(["docker", "image", "inspect", ref, "--format", "{{.Id}}"])
    return out.strip() if rc == 0 else None


def http_get(url, timeout=5, headers=None):
    try:
        h = {"User-Agent": "auto-update-check"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return None, ""


def get_arr_api_key(container):
    rc, out = sh(["docker", "exec", container, "cat", "/config/config.xml"])
    if rc != 0:
        return None
    m = re.search(r"<ApiKey>([^<]+)</ApiKey>", out)
    return m.group(1) if m else None


def load_state(name):
    f = STATE_DIR / f"{name}.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return {}
    return {}


def save_state(name, state):
    (STATE_DIR / f"{name}.json").write_text(json.dumps(state))


def backup_config(svc):
    if not svc["backup"]:
        return True
    dest = BACKUP_DIR / f"{svc['name']}_preupdate.tar.gz"
    rc, out = sh([
        "docker", "run", "--rm",
        "-v", f"{svc['mount']}:/data:ro",
        "-v", f"{BACKUP_DIR}:/backup",
        "alpine", "tar", "czf", f"/backup/{dest.name}", "-C", "/data", "."
    ], timeout=180)
    if rc != 0:
        warn(f"{svc['name']}: config backup failed, proceeding without it ({out.strip()[-200:]})")
        return False
    return True


def restore_config(svc):
    if not svc["backup"]:
        return True
    src = BACKUP_DIR / f"{svc['name']}_preupdate.tar.gz"
    if not src.exists():
        warn(f"{svc['name']}: no backup file to restore from ({src})")
        return False
    rc, out = sh([
        "docker", "run", "--rm",
        "-v", f"{svc['mount']}:/data",
        "-v", f"{BACKUP_DIR}:/backup",
        "alpine", "sh", "-c",
        "rm -rf /data/* /data/.[!.]* /data/..?* 2>/dev/null; "
        f"tar xzf /backup/{src.name} -C /data"
    ], timeout=180)
    if rc != 0:
        warn(f"{svc['name']}: config restore failed ({out.strip()[-200:]})")
        return False
    return True


def compose(svc, *args, timeout=120):
    return sh(["docker", "compose", *args], cwd=svc["compose_dir"], timeout=timeout)


def verify_arr(svc, timeout=60):
    deadline = time.time() + timeout
    key = None
    while time.time() < deadline:
        key = key or get_arr_api_key(svc["name"])
        if key:
            status, body = http_get(f"http://localhost:{svc['port']}{svc['api_base']}/health",
                                     timeout=5, headers={"X-Api-Key": key})
            if status == 200:
                try:
                    issues = json.loads(body)
                except Exception:
                    issues = None
                if issues is not None:
                    errors = [i for i in issues if i.get("type") == "error"]
                    if errors:
                        return False, f"health API reports errors: {[e['message'] for e in errors]}"
                    return True, "health API clean"
        time.sleep(3)
    return False, "app never came up / API key unreadable within timeout"


def verify_http(svc, timeout=60):
    deadline = time.time() + timeout
    path = svc.get("path", "/")
    while time.time() < deadline:
        status, _ = http_get(f"http://localhost:{svc['port']}{path}", timeout=5)
        if status and status < 500:
            return True, f"HTTP {status}"
        time.sleep(3)
    return False, "no successful HTTP response within timeout"


def verify_seerr(svc, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = http_get(f"http://localhost:{svc['port']}/", timeout=5)
        rc, out = sh(["docker", "exec", svc["name"], "sh", "-c", "cat /app/config/settings.json"])
        if status and status < 500 and rc == 0:
            try:
                d = json.loads(out)
                init = d.get("public", {}).get("initialized", False)
                if init:
                    return True, f"HTTP {status}, initialized"
            except Exception:
                pass
        time.sleep(3)
    return False, "not serving + initialized within timeout"


def verify_dockerhealth(svc, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, out = sh(["docker", "inspect", svc["name"], "--format", "{{.State.Health.Status}}"])
        status = out.strip()
        if status == "healthy":
            return True, "docker healthcheck: healthy"
        if status == "unhealthy":
            return False, "docker healthcheck: unhealthy"
        time.sleep(5)
    return False, f"docker healthcheck never went healthy (last: {out.strip() if rc == 0 else 'unknown'})"


VERIFIERS = {
    "arr": verify_arr,
    "http": verify_http,
    "seerr": verify_seerr,
    "dockerhealth": verify_dockerhealth,
}


def container_restarting(name):
    rc, out = sh(["docker", "inspect", name, "--format", "{{.State.Status}} {{.RestartCount}}"])
    if rc != 0:
        return True, "?"
    parts = out.strip().split()
    status = parts[0] if parts else "?"
    return status not in ("running",), status


def process(svc):
    name = svc["name"]
    old_id = image_id_of_container(name)
    if old_id is None:
        warn(f"{name}: container not found, skipping")
        return

    rc, out = compose(svc, "pull", name, timeout=180)
    if rc != 0:
        warn(f"{name}: docker compose pull failed: {out.strip()[-300:]}")
        return

    new_id = local_image_id(svc["image"])
    if new_id == old_id:
        log(f"{name}: up to date (no new image)")
        return

    state = load_state(name)
    if state.get("known_bad_image_id") == new_id:
        log(f"{name}: skipping — image {new_id[:19]} already failed verification on a previous run, "
            f"waiting on upstream for a newer one")
        return

    log(f"{name}: new image found ({old_id[:19]} -> {new_id[:19]}), updating")
    backup_config(svc)

    rc, out = compose(svc, "up", "-d", "--no-deps", name)
    if rc != 0:
        warn(f"{name}: docker compose up failed: {out.strip()[-300:]}")
        return

    verifier = VERIFIERS[svc["kind"]]
    ok, detail = verifier(svc)
    restarting, dstatus = container_restarting(name)
    if ok and not restarting:
        log(f"{name}: [OK] update verified ({detail})")
        state["last_good_image_id"] = new_id
        state.pop("known_bad_image_id", None)
        save_state(name, state)
        return

    if not ok:
        detail = f"{detail}"
    else:
        detail = f"container not in 'running' state ({dstatus})"
    warn(f"{name}: update FAILED verification ({detail}) — rolling back to previous image")

    restore_config(svc)
    rc, out = sh(["docker", "tag", old_id, svc["image"]])
    if rc != 0:
        warn(f"{name}: CRITICAL — could not retag old image ({out.strip()[-200:]}), manual fix needed")
        state["known_bad_image_id"] = new_id
        save_state(name, state)
        return

    rc, out = compose(svc, "up", "-d", "--no-deps", name)
    if rc != 0:
        warn(f"{name}: CRITICAL — rollback recreate failed: {out.strip()[-300:]}")
        state["known_bad_image_id"] = new_id
        save_state(name, state)
        return

    ok2, detail2 = verifier(svc)
    if ok2:
        warn(f"{name}: rolled back successfully, previous version restored and verified ({detail2})")
    else:
        warn(f"{name}: CRITICAL — rollback verification ALSO failed ({detail2}). "
             f"Container may be broken; check manually. Backup at {BACKUP_DIR}/{name}_preupdate.tar.gz")

    state["known_bad_image_id"] = new_id
    save_state(name, state)


def main():
    for svc in SERVICES:
        try:
            process(svc)
        except Exception as e:
            warn(f"{svc['name']}: unexpected error in auto-update: {e}")
    flush_alerts()


if __name__ == "__main__":
    main()
