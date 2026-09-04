#!/usr/bin/env python3
"""
status-dashboard-server.py — serves the desktop status dashboard and executes
its one-click remediations.

Binds loopback only. It runs real repair commands, so the handler set is a
closed whitelist keyed by id: the page can ask for "docker_restart" with a
container name, and nothing else. No shell strings ever come from the client —
arguments are validated against live system state (a container name must match
an actual container) and every command is run as an argv list, never through a
shell.

Deliberately NOT offered as auto-fixes: reboot, disk cleanup, anything that
deletes media. Those stay manual.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from status_keys import load_env, load_netns

ROOT = Path.home() / ".local/share/status-dashboard"
BIN = Path.home() / ".local/bin"
COMPOSE_DIR = Path.home() / "docker/vpn-stack"
PORT = 8099
BIND = "127.0.0.1"
MAX_BODY = 64 * 1024          # the largest request the page ever sends is ~4 KB

# API keys live in ~/.config/status-dashboard/media-keys.env (chmod 600) rather
# than in this group-readable script. See status-keys.py.
PROWLARR = {"url": "http://localhost:9696",
            "key": load_env().get("PROWLARR_KEY", "")}

# Containers sharing the VPN hub's netns. Recreating the hub alone silently
# strips networking from the others, so they are always brought up together.
# Shared with status-collect.py via netns-group.json — edit there, not here.
_NETNS_HUB, NETNS_GROUP = load_netns()

_lock = threading.Lock()          # one repair at a time
_running = {"id": None, "since": 0}

# State of the current/last repair batch, polled by the page via /api/job.
_job_lock = threading.Lock()
_job = {"running": False, "done": False, "results": [], "total": 0,
        "started": 0, "current": None, "error": None}


# ── helpers ──────────────────────────────────────────────────────────────────
def sh(argv, timeout=300):
    """Run an argv list (never a shell string); return (rc, combined output)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {shlex.join(argv)}"
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def known_containers():
    rc, out = sh(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=30)
    return set(out.split()) if rc == 0 else set()


def valid_container(name):
    """A container name is only accepted if docker actually reports it."""
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        return False
    return name in known_containers()


def recollect():
    """Refresh status.json so the page reflects the repair immediately."""
    return sh(["python3", str(BIN / "status-collect.py")], timeout=180)


# ── fix handlers ─────────────────────────────────────────────────────────────
def fix_docker_start(args):
    n = args.get("name")
    if not valid_container(n):
        return False, f"unknown container: {n!r}"
    rc, out = sh(["docker", "start", n], timeout=120)
    return rc == 0, out or f"started {n}"


def fix_docker_restart(args):
    n = args.get("name")
    if not valid_container(n):
        return False, f"unknown container: {n!r}"
    # Restarting the hub alone breaks its netns siblings — route to the group.
    if n == _NETNS_HUB:
        return fix_vpn_recreate({})
    rc, out = sh(["docker", "restart", n], timeout=180)
    return rc == 0, out or f"restarted {n}"


def fix_vpn_recreate(args):
    if not COMPOSE_DIR.is_dir():
        return False, f"compose dir not found: {COMPOSE_DIR}"
    present = known_containers()
    group = [c for c in [_NETNS_HUB] + NETNS_GROUP if c in present]
    rc, out = sh(["docker", "compose", "--project-directory", str(COMPOSE_DIR),
                  "up", "-d", "--force-recreate"] + group, timeout=420)
    return rc == 0, out or f"recreated: {', '.join(group)}"


def fix_vpn_rotate(args):
    script = BIN / "gluetun-rotate.sh"
    if not script.exists():
        return False, "gluetun-rotate.sh not found"
    rc, out = sh(["bash", str(script)], timeout=180)
    return rc == 0, out or "VPN exit rotated"


def fix_selfheal(args):
    script = BIN / "media-stack-selfheal.py"
    if not script.exists():
        return False, "media-stack-selfheal.py not found"
    rc, out = sh(["python3", str(script)], timeout=900)
    return rc == 0, out[-4000:] or "self-heal complete"


def fix_prowlarr_testall(args):
    # Prowlarr answers testall with HTTP 400 whenever *any* indexer fails
    # validation — but the body still carries the full per-indexer result, so
    # the error body is the payload we want, not a failure to report.
    req = urllib.request.Request(
        f"{PROWLARR['url']}/api/v1/indexer/testall", data=b"", method="POST",
        headers={"X-Api-Key": PROWLARR["key"]})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"

    try:
        data = json.loads(body or "[]")
    except Exception:
        return False, f"unparseable response: {body[:300]}"

    names = {}
    try:
        nreq = urllib.request.Request(f"{PROWLARR['url']}/api/v1/indexer",
                                      headers={"X-Api-Key": PROWLARR["key"]})
        with urllib.request.urlopen(nreq, timeout=60) as r:
            names = {i["id"]: i.get("name", str(i["id"]))
                     for i in json.loads(r.read().decode())}
    except Exception:
        pass

    bad = []
    for x in data:
        if x.get("isValid"):
            continue
        why = "; ".join(f.get("errorMessage", "")
                        for f in x.get("validationFailures", []))
        bad.append(f"{names.get(x.get('id'), x.get('id'))}: {why[:120]}")
    if bad:
        return False, (f"{len(bad)} of {len(data)} indexers still failing:\n  "
                       + "\n  ".join(bad))
    return True, f"all {len(data)} indexers passed"


def _exit_ip():
    rc, out = sh(["docker", "logs", "--tail", "200", "gluetun"], timeout=30)
    ips = re.findall(r"Public IP address is ([0-9a-fA-F.:]+)", out)
    return ips[-1] if ips else None


def fix_cloudflare_unblock(args):
    """A Cloudflare 1006/1007/1008/1015 is an IP/range ban, so the only remedy
    is a different exit IP. Rotate, confirm the host actually lets us back in,
    and retry up to a few times — consecutive IPs often sit in the same banned
    range, which is exactly why a single rotation is not enough."""
    host = args.get("host") or ""
    if host and not re.fullmatch(r"[A-Za-z0-9.\-]{3,120}", host):
        return False, f"refusing suspicious host: {host!r}"

    script = BIN / "gluetun-rotate.sh"
    if not script.exists():
        return False, "gluetun-rotate.sh not found"

    log = []
    before = _exit_ip()
    log.append(f"exit IP before: {before or 'unknown'}")

    for attempt in range(1, 4):
        rc, out = sh(["bash", str(script)], timeout=240)
        if rc != 0:
            log.append(f"attempt {attempt}: rotation failed — {out[:200]}")
            break
        time.sleep(12)
        now = _exit_ip()
        log.append(f"attempt {attempt}: exit IP now {now or 'unknown'}")

        if not host:
            break
        # Probe from inside the VPN namespace; a 403 means still banned.
        rc2, body = sh(["docker", "exec", "qbittorrent", "sh", "-c",
                        f"wget -S -qO- -T15 https://{host}/ 2>&1 | head -3"],
                       timeout=60)
        if "403" not in body:
            log.append(f"{host} no longer returns 403 — unblocked")
            break
        log.append(f"{host} still 403 on this IP")
    else:
        log.append("exhausted 3 rotations; the whole provider range looks banned")

    ok_test, test_out = fix_prowlarr_testall({})
    log.append(test_out)
    return ok_test, "\n".join(log)


def fix_apt_upgrade(args):
    # Both commands are in the NOPASSWD sudoers entry verbatim.
    rc, out = sh(["sudo", "-n", "/usr/bin/apt-get", "update"], timeout=300)
    rc2, out2 = sh(["sudo", "-n", "/usr/bin/apt-get", "-y", "upgrade"], timeout=1800)
    return rc2 == 0, (out[-800:] + "\n" + out2[-3000:]).strip()


def fix_daily_routine_quick(args):
    script = BIN / "daily-routine.sh"
    if not script.exists():
        return False, "daily-routine.sh not found"
    rc, out = sh(["bash", str(script), "--quick"], timeout=1800)
    return rc == 0, out[-4000:] or "daily routine (quick) complete"


def fix_topgrade(args):
    """Claude Code / Claude Code Plugins / OpenCode only — apt/snap/firmware/
    containers are disabled in ~/.config/topgrade.toml since daily-routine.sh
    already covers those with its own verify+rollback logic."""
    if not shutil.which("topgrade"):
        return False, "topgrade not installed"
    rc, out = sh(["topgrade", "-y", "--notify-end", "never"], timeout=600)
    return rc == 0, out[-4000:] or "topgrade complete"


def fix_restart_unit(args):
    """Only user units — system units would need a password prompt nobody sees."""
    unit = args.get("unit", "")
    if not re.fullmatch(r"[A-Za-z0-9@_.\\-]{1,80}\.(service|timer|socket)", unit):
        return False, f"refusing suspicious unit name: {unit!r}"
    rc, out = sh(["systemctl", "--user", "restart", unit], timeout=120)
    if rc == 0:
        return True, f"restarted user unit {unit}"
    return False, (out or "failed")[:600] + \
        "\n(system-level units must be restarted manually with sudo)"


FIXES = {
    "docker_start": fix_docker_start,
    "docker_restart": fix_docker_restart,
    "vpn_recreate": fix_vpn_recreate,
    "vpn_rotate": fix_vpn_rotate,
    "selfheal": fix_selfheal,
    "prowlarr_testall": fix_prowlarr_testall,
    "cloudflare_unblock": fix_cloudflare_unblock,
    "apt_upgrade": fix_apt_upgrade,
    "daily_routine_quick": fix_daily_routine_quick,
    "restart_unit": fix_restart_unit,
    "topgrade": fix_topgrade,
}


def run_fix(fix_id, args):
    handler = FIXES.get(fix_id)
    if handler is None:
        return {"id": fix_id, "ok": False, "output": f"unknown fix id: {fix_id}"}
    t0 = time.time()
    try:
        ok, output = handler(args or {})
    except Exception as e:  # noqa: BLE001
        ok, output = False, f"{type(e).__name__}: {e}"
    return {"id": fix_id, "ok": bool(ok), "output": output,
            "seconds": round(time.time() - t0, 1)}


# ── terminal panes ───────────────────────────────────────────────────────────
# Offered in the dashboard's pane picker. "needs" is the command that has to
# exist for the entry to be listed, so a machine without htop simply does not
# show it rather than opening a pane that immediately dies. Ids must match the
# whitelist in dashboard-pane.sh. "group" drives the picker's section headers
# (agent / paid / free / local / system) and, for "paid"/"free", which env-var
# key in model-pricing.json (written nightly by ai-panes-check.py) prices it.
PANES = [
    ("claude",  "Claude Code",     "claude",     "agent", None),
    ("opencode","opencode",        "opencode",   "agent", None),
    # Best available paid model per remaining major provider (OpenAI/Google/
    # Tencent) — re-picked nightly by ai-panes-check.py. Deliberately paid;
    # every query bills the OpenRouter account.
    ("gpt",     "ChatGPT",         "curl",       "paid",  "CHATGPT_MODEL"),
    ("gm",      "Gemini",          "curl",       "paid",  "GEMINI_MODEL"),
    ("hy",      "Hy4",             "curl",       "paid",  "HY4_MODEL"),
    # Nominally free tier — ai-panes-check.py falls back to the cheapest paid
    # variant rather than leaving the pane dead if a free tier expires, so
    # these can briefly show a price too; see model-pricing.json.
    ("oa",      "Ox Alpha",        "curl",       "free",  "OXALPHA_MODEL"),
    ("ds",      "DeepSeek",        "curl",       "free",  "DEEPSEEK_MODEL"),
    ("mm",      "Minimax M3",      "curl",       "free",  "MINIMAX_MODEL"),
    ("llm",     "Local LLM",       "ollama",     "local", None),
    ("ask",     "Ask Claude",      "claude",     "hidden", None),
    ("askllm",  "Ask Local LLM",   "ollama",     "hidden", None),
    ("askgpt",  "Ask ChatGPT",     "curl",       "hidden", "CHATGPT_MODEL"),
    ("askgm",   "Ask Gemini",      "curl",       "hidden", "GEMINI_MODEL"),
    ("askhy",   "Ask Hy4",         "curl",       "hidden", "HY4_MODEL"),
    ("askoa",   "Ask Ox Alpha",    "curl",       "hidden", "OXALPHA_MODEL"),
    ("askds",   "Ask DeepSeek",    "curl",       "hidden", "DEEPSEEK_MODEL"),
    ("askmm",   "Ask Minimax M3",  "curl",       "hidden", "MINIMAX_MODEL"),
    ("shell",   "Shell",           "bash",       "system", None),
    ("htop",    "Processes",       "htop",       "system", None),
    ("docker",  "Container stats", "docker",     "system", None),
    ("logs",    "System log",      "journalctl", "system", None),
    ("dashlog", "Dashboard log",   "journalctl", "system", None),
    ("disk",    "Disk usage",      "ncdu",       "system", None),
    ("routine", "Daily routine",   "less",       "system", None),
]

PRICING_FILE = Path.home() / ".config/status-dashboard/model-pricing.json"


def read_pricing():
    try:
        return json.loads(PRICING_FILE.read_text())
    except Exception:  # noqa: BLE001 — missing/stale file just means no price shown
        return {}


def format_price(entry):
    if not entry:
        return None
    if entry.get("free"):
        return "free"
    p, c = entry.get("prompt"), entry.get("completion")
    if p is None or c is None:
        return None
    return f"${p * 1e6:.2f}/${c * 1e6:.2f} per M tokens"


def available_panes():
    pricing = read_pricing()
    out = []
    for pid, label, needs, group, price_key in PANES:
        if group == "hidden":
            continue
        if not shutil.which(needs):
            continue
        entry = pricing.get(price_key) if price_key else None
        price = format_price(entry)
        # 2026-09-04: PANES' group is a static label, but ai-panes-check.py's
        # FREE_KEYS loop already re-derives the real per-model "free" bool
        # every night into model-pricing.json -- the static label here just
        # never picked that up when a nominally-free model's tier expired.
        # Let the live data override the label whenever it's actually known,
        # so this self-corrects for any of the free-tier panes without
        # another hardcoded fix later.
        eff_group = group
        if group in ("free", "paid") and entry is not None and "free" in entry:
            eff_group = "free" if entry["free"] else "paid"
        out.append({"id": pid, "label": label, "group": eff_group, "price": price})
    return out


def ask_targets():
    pricing = read_pricing()
    out = []
    for pid, label, needs, group, price_key in PANES:
        if group != "hidden" or not pid.startswith("ask") or not shutil.which(needs):
            continue
        price = format_price(pricing.get(price_key)) if price_key else None
        out.append({"id": pid, "label": label.removeprefix("Ask "), "price": price})
    return out


# ── http ─────────────────────────────────────────────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *a):  # keep the journal readable
        if "/api/" in (self.path or ""):
            super().log_message(fmt, *a)

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The page navigated away or gave up waiting. The repair itself
            # already ran; losing the reply is not an error worth a traceback.
            pass

    def _same_origin(self):
        """True when the request provably comes from the dashboard page itself.

        Loopback binding does not stop other web pages the user has open: a
        random site can cross-origin POST to 127.0.0.1 with a JSON-looking
        body and no preflight. Browsers tag such requests with
        Sec-Fetch-Site / Origin, so anything marked cross-site is refused
        before a single repair runs. Headerless clients (curl, scripts) pass.
        """
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None:
            return site != "cross-site"
        origin = self.headers.get("Origin")
        if origin:
            return origin in (f"http://{BIND}:{PORT}",
                              f"http://localhost:{PORT}")
        return True

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/job":
            with _job_lock:
                return self._json(200, dict(_job))
        if route == "/api/panes":
            return self._json(200, {"panes": available_panes()})
        if route == "/api/ask-targets":
            return self._json(200, {"targets": ask_targets()})
        if route.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        return super().do_GET()

    def do_POST(self):
        route = urlparse(self.path).path
        if not route.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._same_origin():
            return self._json(403, {"error": "cross-origin requests are refused"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad content-length"})
        if n > MAX_BODY:
            return self._json(413, {"error": "request body too large"})
        try:
            body = json.loads(self.rfile.read(n) or "{}")
        except Exception:
            return self._json(400, {"error": "bad json"})

        if route == "/api/reboot":
            # Never reachable from the fix whitelist — only this endpoint, and
            # only with the explicit confirmation token the button sends.
            if body.get("confirm") != "REBOOT":
                return self._json(400, {"ok": False,
                                        "error": "missing confirmation"})
            # Answer before going down, or the page never sees the reply.
            def go():
                time.sleep(2)
                rc, _ = sh(["systemctl", "reboot"], timeout=30)
                if rc != 0:
                    sh(["busctl", "call", "org.freedesktop.login1",
                        "/org/freedesktop/login1",
                        "org.freedesktop.login1.Manager", "Reboot", "b", "true"],
                       timeout=30)
            rc, out = sh(["busctl", "--system", "call", "org.freedesktop.login1",
                          "/org/freedesktop/login1",
                          "org.freedesktop.login1.Manager", "CanReboot"],
                         timeout=15)
            if "yes" not in out:
                return self._json(403, {
                    "ok": False,
                    "error": f"logind refuses to reboot for this session ({out.strip()}). "
                             f"Run 'sudo reboot' manually."})
            threading.Thread(target=go, daemon=True).start()
            return self._json(200, {"ok": True,
                                    "output": "reboot scheduled in 2s"})

        if route == "/api/refresh":
            rc, out = recollect()
            return self._json(200, {"ok": rc == 0, "output": out[-500:]})

        if route != "/api/fix":
            return self._json(404, {"error": "not found"})

        # A repair batch: [{id, args}, ...]
        items = body.get("fixes")
        if items is None:
            items = [{"id": body.get("id"), "args": body.get("args", {})}]
        if not isinstance(items, list) or len(items) > 25:
            return self._json(400, {"error": "bad fix list"})

        # De-duplicate here so the client's row order matches the results.
        seen, todo = set(), []
        for it in items:
            fid = (it or {}).get("id")
            args = (it or {}).get("args") or {}
            key = (fid, json.dumps(args, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            todo.append((fid, args))

        if not _lock.acquire(blocking=False):
            return self._json(409, {"error": f"a repair is already running "
                                             f"({_running['id']})"})

        # Repairs run in a worker and the page polls /api/job. A full
        # daily-routine pass takes minutes — far longer than the browser will
        # hold a fetch open — so answering synchronously made a successful
        # repair look like a failure when the socket timed out.
        with _job_lock:
            _job.update({"running": True, "done": False, "results": [],
                         "total": len(todo), "started": time.time(),
                         "current": None, "error": None})

        def worker():
            try:
                for fid, args in todo:
                    with _job_lock:
                        _job["current"] = fid
                    _running.update({"id": fid, "since": time.time()})
                    r = run_fix(fid, args)
                    with _job_lock:
                        _job["results"].append(r)
                _running.update({"id": "recollect", "since": time.time()})
                recollect()
            except Exception as e:  # noqa: BLE001
                with _job_lock:
                    _job["error"] = f"{type(e).__name__}: {e}"
            finally:
                _running.update({"id": None, "since": 0})
                with _job_lock:
                    _job.update({"running": False, "done": True,
                                 "current": None})
                _lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return self._json(202, {"started": True, "total": len(todo)})


def main():
    os.chdir(ROOT)
    srv = ThreadingHTTPServer((BIND, PORT), partial(Handler, directory=str(ROOT)))
    print(f"status dashboard on http://{BIND}:{PORT} (root {ROOT})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
