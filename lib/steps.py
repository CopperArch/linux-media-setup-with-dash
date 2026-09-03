"""Install steps, in order. Each step:

    takes (ctx, accept: AcceptFn)  ->  returns (ok: bool, summary: str)

`accept` is supplied by the UI (GUI dialog or CLI y/n): nothing that changes
the system runs without an explicit "accept" from the user, per component.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .platform import Platform
from .profile import Profile
from .render import render_file, render_tree, MissingVar
from .util import have, log, run

class Ctx:
    def __init__(self, profile: Profile, plat: Platform, sudo, dry_run=False,
                 repo_root: Path | None = None):
        self.profile = profile
        self.plat = plat
        self.sudo = sudo
        self.dry_run = dry_run
        self.repo = repo_root or Path(__file__).resolve().parent.parent
        self.tpl = self.repo / "templates"

    @property
    def v(self) -> dict:
        return self.profile.render_vars()


# ── 1. system packages ───────────────────────────────────────────────────────
def base_packages(ctx: Ctx) -> list[str]:
    need = ["python3", "git", "curl", "jq", "cron", "htop", "rsync", "unzip"]
    if ctx.profile.use_dashboard:
        need += ["tkinter", "xrandr"]
    if ctx.profile.use_daily_routine:
        need += ["smartmontools", "ncdu"]
    if ctx.profile.media_pool and "mergerfs" not in ctx.profile.media_pool:
        need += ["mergerfs"]
    return [l for l in need if ctx.plat.pkg_name(l)]


def install_packages(ctx: Ctx, accept) -> tuple[bool, str]:
    pkgs = base_packages(ctx)
    names = [ctx.plat.pkg_name(l) for l in pkgs]
    if not accept("Install system packages?\n  " + ", ".join(names)):
        return False, "packages declined"
    def do(ctx):
        if have("docker") and have("git") and have("curl"):
            return True, "already present (checked docker/git/curl)"
        return ctx.plat.install(ctx.sudo, pkgs)
    return _exec_step(ctx, f"install packages: {names}", do)


def install_docker(ctx: Ctx, accept) -> tuple[bool, str]:
    def do(ctx):
        if have("docker") and have("docker-compose"):
            ok, msg = True, "docker + compose already present"
        else:
            ok, msg = ctx.plat.install(ctx.sudo, ["docker", "docker-compose"])
        if ok:
            ctx.plat.service(ctx.sudo, "docker", "enable")
            ctx.plat.service(ctx.sudo, "docker", "start")
            user = ctx.profile.user or os.environ.get("USER") or Path.home().name
            p = ctx.sudo.run(["usermod", "-aG", "docker", user], timeout=30)
            if p.returncode == 0:
                msg += " (user added to docker group — log out/in once)"
        return ok, msg
    return _exec_step(ctx, "install + enable Docker", do)


def _exec_step(ctx: Ctx, desc, fn):
    if ctx.dry_run:
        log(f"  [DRY] {desc}")
        return True, "dry-run"
    try:
        return fn(ctx)
    except MissingVar as e:
        return False, f"missing profile value: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# ── 2. storage paths ─────────────────────────────────────────────────────────
def check_storage(ctx: Ctx) -> tuple[bool, str]:
    paths = [p for p in [ctx.profile.media_pool, ctx.profile.nextcloud_data,
             *ctx.profile.media_disks] if p]
    missing = [p for p in paths if not Path(p).exists()]
    for p in missing:
        try:
            Path(p).mkdir(parents=True, exist_ok=True)
            log(f"  created empty mount point {p} (mount your disk here)")
        except OSError as e:
            log(f"  could not create {p}: {e}")
    if missing:
        log("  NOTE: create fstab entries yourself, or ask the AI pane for help;")
        log("  the installer deliberately never edits /etc/fstab.")
    return True, f"{len(paths) - len(missing)}/{len(paths)} mounts present"


# ── 3. stacks (compose files + .env) ────────────────────────────────────────
def install_stacks(ctx: Ctx) -> tuple[bool, str]:
    def do(ctx):
        base = Path.home() / "docker"
        done = []
        if ctx.profile.use_media_stack:
            # caddy gets special treatment: domain blocks for domains the
            # user does not have are commented out before rendering.
            tpl = ctx.tpl / "stacks/media-stack/caddy/Caddyfile"
            text = prune_caddy_blocks(tpl.read_text(), ctx.profile)
            dst = base / "media-stack/caddy/Caddyfile"
            dst.parent.mkdir(parents=True, exist_ok=True)
            from .render import render_text
            dst.write_text(render_text(text, ctx.v, str(tpl)))
            dst.chmod(0o644)
            done += [dst]
            done += render_tree(ctx.tpl / "stacks/media-stack",
                                base / "media-stack", ctx.v,
                                skip=("Caddyfile",))
        if ctx.profile.use_vpn_stack:
            done += render_tree(ctx.tpl / "stacks/vpn-stack",
                                base / "vpn-stack", ctx.v)
        # compose .env files from profile secrets
        if ctx.profile.use_media_stack:
            env = build_media_env(ctx.profile)
            write_secret_env(base / "media-stack/.env", env)
        if ctx.profile.use_vpn_stack:
            env = build_vpn_env(ctx.profile)
            write_secret_env(base / "vpn-stack/.env", env)
        return True, f"rendered {len(done) + 2} files under {base}"
    return _exec_step(ctx, "render docker stacks", do)


def prune_caddy_blocks(text: str, p: Profile) -> str:
    """Comment out domain blocks the user has no domain for, so caddy starts.

    Runs on the TEMPLATE text (before rendering) so unrendered placeholders
    identify the blocks unambiguously; braces are counted so nested tls{}
    blocks stay inside the commented region."""
    out, drop, depth = [], False, 0
    for line in text.splitlines():
        s = line.strip()
        if not drop:
            trigger = ((not p.nextcloud_domain
                        and "http://{{NEXTCLOUD_DOMAIN}}" in line)
                       or (not p.duckdns_domains
                           and s.startswith("{{DUCKDNS_DOMAIN_")))
            if trigger:
                drop = True
                depth = 0
        if drop:
            depth += line.count("{") - line.count("}")
            out.append("# " + line if line else "#")
            if depth <= 0:
                drop = False
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def build_media_env(p: Profile) -> dict:
    return {
        "NEXTCLOUD_DB_ROOT_PASSWORD": p.nc_db_password,
        "NEXTCLOUD_DB_PASSWORD": p.nc_db_password,
        "REDIS_PASSWORD": p.nc_db_password,
        "NEXTCLOUD_ADMIN_USER": "admin",
        "NEXTCLOUD_ADMIN_PASSWORD": p.nc_admin_password,
        "NEXTCLOUD_TRUSTED_DOMAINS": f"localhost {p.lan_ip.rsplit('.',1)[0]}.*",
        "IMMICH_DB_PASSWORD": p.immich_db_password,
        "IMMICH_DOMAIN": p.immich_domain,
        "UPLOAD_LOCATION": f"{p.nextcloud_data}/immich",
        "PLEX_CLAIM": p.plex_claim,
        "HOST_IP": p.host_ip or p.lan_ip,
        "TUGTAINER_AGENT_SECRET": p.tugtainer_secret,
        "DUCKDNS_TOKEN": p.duckdns_token,
    }


def build_vpn_env(p: Profile) -> dict:
    return {
        "VPN_SERVICE_PROVIDER": p.vpn_provider,
        "VPN_TYPE": "openvpn",
        "OPENVPN_USER": p.vpn_user,
        "OPENVPN_PASSWORD": p.vpn_password,
        "SERVER_COUNTRIES": p.vpn_server_countries,
        "QBT_USER": p.qbit_user,
        "QBT_PASS": p.qbit_password,
        "VNC_PW": p.vnc_pw,
}


def write_secret_env(path: Path, env: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in env.items() if v]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


# ── 4. local scripts + units + dashboard page ───────────────────────────────
def install_scripts(ctx: Ctx) -> tuple[bool, str]:
    def do(ctx):
        bin_dir = Path.home() / ".local/bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in sorted((ctx.tpl).glob("*.py")) + sorted(ctx.tpl.glob("*.sh")):
            render_file(f, bin_dir / f.name, ctx.v, 0o755)
            n += 1
        # dashboard assets
        share = Path.home() / ".local/share/status-dashboard"
        share.mkdir(parents=True, exist_ok=True)
        render_file(ctx.tpl / "index.html", share / "index.html", ctx.v, 0o644)
        # conf dir: media-keys.env + netns-group.json
        conf = Path.home() / ".config/status-dashboard"
        conf.mkdir(parents=True, exist_ok=True)
        keys = {
            "PROWLARR_KEY": "", "QBIT_PASS": ctx.profile.qbit_password,
            "DEEPSEEK_API_KEY": ctx.profile.openrouter_api_key,
            "DEEPSEEK_BASE_URL": "https://openrouter.ai/api/v1",
        }
        write_secret_env(conf / "media-keys.env", keys)
        netns = conf / "netns-group.json"
        if not netns.exists():
            netns.write_text('{\n  "hub": "gluetun",\n  "siblings": ["qbittorrent",'
                             '"prowlarr", "radarr", "sonarr", "flaresolverr", '
                             '"chrome", "jellyseerr"]\n}\n')
            netns.chmod(0o600)
        # duckdns
        if ctx.profile.use_duckdns:
            ddir = Path.home() / ".config/duckdns"
            ddir.mkdir(parents=True, exist_ok=True)
            write_secret_env(ddir / "duckdns.env", {
                "DUCKDNS_DOMAINS": ctx.profile.duckdns_domains,
                "DUCKDNS_TOKEN": ctx.profile.duckdns_token})
        # alerts
        if ctx.profile.use_alerts and ctx.profile.smtp_host:
            hdir = Path.home() / ".hermes"
            hdir.mkdir(parents=True, exist_ok=True)
            write_secret_env(hdir / "alert-email.conf", {
                "SMTP_HOST": ctx.profile.smtp_host,
                "SMTP_PORT": ctx.profile.smtp_port,
                "SMTP_USER": ctx.profile.smtp_user,
                "SMTP_PASS": ctx.profile.smtp_pass,
                "MAIL_FROM": ctx.profile.mail_from or ctx.profile.smtp_user,
                "MAIL_TO": ctx.profile.mail_to})
        # gluetun rotate key
        if ctx.profile.use_vpn_stack and ctx.profile.vpn_rotate_hourly:
            gdir = Path.home() / ".config/gluetun"
            gdir.mkdir(parents=True, exist_ok=True)
            key = gdir / "rotate.key"
            if not key.exists():
                import secrets as _s
                key.write_text(_s.token_hex(16))
                key.chmod(0o600)
        # docker-log-clean (root-owned helper) + routine sudo rules
        etc = ctx.tpl / "etc"
        if (etc / "docker-log-clean.sh").exists():
            ctx.sudo.run(["install", "-o", "root", "-g", "root", "-m", "0755",
                          etc / "docker-log-clean.sh",
                          "/usr/local/sbin/docker-log-clean.sh"], timeout=30)
            ctx.sudo.run(["install", "-o", "root", "-g", "root", "-m", "0440",
                          etc / "docker-log-clean.sudoers",
                          "/etc/sudoers.d/docker-log-clean"], timeout=30)
        if ctx.plat.family == "apt" and (etc / "daily-routine.sudoers").exists():
            tmp = Path("/tmp/daily-routine.sudoers.rendered")
            render_file(etc / "daily-routine.sudoers", tmp, ctx.v, 0o644)
            ctx.sudo.run(["install", "-o", "root", "-g", "root", "-m", "0440",
                          tmp, "/etc/sudoers.d/daily-routine"], timeout=30)
            tmp.unlink()
        # ttyd (dashboard terminal pane) — static binary from templates/bin
        ttyd = ctx.tpl / "bin/ttyd"
        if ctx.profile.use_dashboard and ttyd.exists() and ctx.plat.arch == "x86_64":
            dst = Path.home() / ".local/bin/ttyd"
            shutil.copyfile(ttyd, dst)
            dst.chmod(0o755)
        elif ctx.profile.use_dashboard and ctx.plat.arch != "x86_64":
            log("  [SKIP] ttyd binary is x86_64-only — terminal pane disabled "
                "on this architecture (dashboard still works)")
        return True, f"{n} scripts + dashboard page installed"
    return _exec_step(ctx, "install local scripts", do)


def install_units(ctx: Ctx) -> tuple[bool, str]:
    def do(ctx):
        udir = Path.home() / ".config/systemd/user"
        udir.mkdir(parents=True, exist_ok=True)
        # the dashboard's own idempotent installer owns the 5 units
        if ctx.profile.use_dashboard:
            p = run(["bash", str(Path.home() / ".local/bin/status-dashboard-install.sh")],
                    timeout=120)
            if p.returncode != 0:
                return False, f"status-dashboard-install.sh rc={p.returncode}"
        # post-reboot one-shot (armed on demand, not at install time)
        if ctx.profile.use_post_reboot_check:
            src = ctx.tpl / "units/post-reboot-check.service"
            render_file(src, udir / src.name, ctx.v, 0o644)
        return True, "user units installed"
    return _exec_step(ctx, "install systemd user units", do)


def install_cron(ctx: Ctx) -> tuple[bool, str]:
    def do(ctx):
        entries = []
        if ctx.profile.use_daily_routine:
            entries.append("0 3 * * * " + str(Path.home() / ".local/bin/daily-routine.sh"))
        if ctx.profile.use_duckdns:
            entries.append("*/5 * * * * " + str(Path.home() / ".local/bin/duckdns-update.sh"))
        if ctx.profile.use_wastebins:
            entries.append("0 * * * * " + str(Path.home() / ".local/bin/empty-wastebins.sh"))
        if ctx.profile.use_vpn_stack and ctx.profile.vpn_rotate_hourly:
            entries.append("17 * * * * " + str(Path.home() / ".local/bin/gluetun-rotate.sh"))
        if entries and not ctx.plat.crontab_user(entries):
            return False, "crontab update failed"
        return True, f"{len(entries)} cron entries"
    return _exec_step(ctx, "install cron entries", do)


# ── 5. bring the stacks up ───────────────────────────────────────────────────
def start_stacks(ctx: Ctx, accept) -> tuple[bool, str]:
    def ask(ctx):
        return accept("Pull images and start the docker stacks now?\n"
                      "(first pull can take a long time)")
    if ctx.dry_run:
        log("  [DRY] docker compose up")
        return True, "dry-run"
    if not ask(ctx):
        return True, "stacks left stopped (start later with: docker compose up -d)"
    results = []
    base = Path.home() / "docker"
    if ctx.profile.use_media_stack:
        p = run(["docker", "compose", "up", "-d", "--build"],
                cwd=base / "media-stack", timeout=7200)
        results.append(f"media-stack rc={p.returncode}")
    if ctx.profile.use_vpn_stack:
        vpn = base / "vpn-stack"
        run(["docker", "compose", "build", "chrome"], cwd=vpn, timeout=3600)
        p = run(["docker", "compose", "up", "-d"], cwd=vpn, timeout=3600)
        results.append(f"vpn-stack rc={p.returncode}")
    ok = all(rc == "0" for rc in results) if results else True
    return ok, ", ".join(results)


# ── 6. verify ────────────────────────────────────────────────────────────────
def verify(ctx: Ctx) -> tuple[bool, str]:
    checks = []
    if have("docker") or shutil.which("docker"):
        p = run(["docker", "ps", "--format", "{{.Names}}"], quiet=True, timeout=30)
        if p.returncode == 0:
            checks.append(f"docker: {len(p.stdout.split())} containers running")
        else:
            checks.append("docker daemon unreachable")
    if ctx.profile.use_dashboard:
        p = run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "http://127.0.0.1:8099/index.html"], quiet=True, timeout=15)
        checks.append(f"dashboard: HTTP {p.stdout.strip() or '??'}")
    p = run(["systemctl", "--user", "is-active",
             "status-dashboard-server.service"], quiet=True)
    if ctx.profile.use_dashboard:
        checks.append(f"dashboard service: {p.stdout.strip() or '?'}")
    for line in checks:
        log(f"  {line}")
    return True, "; ".join(checks)


# ── 6. verify ────────────────────────────────────────────────────────────────


def run_all(ctx: Ctx, accept, accept_steps: dict[str, bool]) -> list[tuple[str, bool, str]]:
    """Execute each step. accept_steps maps step name -> user opted in."""
    from .util import log
    results = []
    order = [
        ("System packages", lambda: install_packages(ctx, accept)),
        ("Docker engine",   lambda: install_docker(ctx, accept)),
        ("Storage paths",   lambda: check_storage(ctx)),
        ("Docker stacks",   lambda: install_stacks(ctx)),
        ("Local scripts",   lambda: install_scripts(ctx)),
        ("Systemd units",   lambda: install_units(ctx)),
        ("Cron jobs",       lambda: install_cron(ctx)),
        ("Start containers", lambda: start_stacks(ctx, accept)),
        ("Verify",          lambda: verify(ctx)),
    ]
    for name, fn in order:
        log(f"\n== {name} ==")
        if not accept_steps.get(name, True):
            log("  [SKIP] not selected")
            results.append((name, True, "skipped by user"))
            continue
        if name not in ("Verify",) and name not in ("Storage paths",):
            ctx.sudo.keepalive()
        try:
            ok, msg = fn()
        except Exception as e:  # noqa: BLE001
            ok, msg = False, f"{type(e).__name__}: {e}"
        results.append((name, ok, msg))
        log(f"  -> {'OK' if ok else 'FAILED'}: {msg}")
    return results
