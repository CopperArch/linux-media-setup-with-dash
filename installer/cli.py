#!/usr/bin/env python3
"""CLI installer for linux-media-setup-with-dash.

    python3 installer/cli.py                          # interactive questions
    python3 installer/cli.py --profile my.yaml        # install from a saved profile
    python3 installer/cli.py --dry-run --profile my.yaml
    python3 installer/cli.py --yes --profile my.yaml  # no accept prompts
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.platform import Platform
from lib.profile import Profile
from lib.steps import run_all
from lib.util import Sudo, confirm, log


def ask_questions(p: Profile) -> Profile:
    """Fresh install: ask every question a new user must answer."""
    print("\nAnswer the questions to build your profile "
          "(empty = accept the [default]).\n")
    p.user = _in("Your username", p.user or Path.home().name)
    p.lan_ip = _in("This machine's LAN IP (e.g. 192.168.1.50)", p.lan_ip)
    p.domain = _in("Base domain you own (blank to skip)", p.domain)
    if p.domain:
        p.jellyfin_public_url = _in("Jellyfin public URL", f"https://jellyfin.{p.domain}")
        p.nextcloud_domain = _in("Nextcloud domain", f"nextcloud.{p.domain}")
        p.immich_domain = _in("Immich domain", f"immich.{p.domain}")
    p.media_pool = _in("Media pool mount", p.media_pool)
    p.nextcloud_data = _in("Nextcloud data mount", p.nextcloud_data)

    if confirm("Install the VPN download stack (gluetun/qBittorrent/Sonarr/Radarr)?"):
        p.use_vpn_stack = True
        p.vpn_provider = _in("VPN provider (surfshark/mullvad/...)", p.vpn_provider)
        p.vpn_user = _in("VPN username", secret=False)
        p.vpn_password = _in("VPN password", secret=True)
        p.vpn_server_countries = _in("VPN server countries", p.vpn_server_countries)
    if confirm("Install the media stack (Plex/Jellyfin/Immich/Nextcloud/seerr/Caddy)?"):
        p.use_media_stack = True
        if confirm("Do you have a Plex claim token (plex.tv/claim)?"):
            p.plex_claim = _in("Plex claim", secret=True)
    if confirm("Install the desktop status dashboard + self-healing?"):
        p.use_dashboard = True
        p.use_daily_routine = True
    if confirm("Set up DuckDNS dynamic DNS?"):
        p.use_duckdns = True
        p.duckdns_domains = _in("DuckDNS domains (comma separated)")
        p.duckdns_token = _in("DuckDNS token", secret=True)
    if confirm("Set up email alerts (SMTP)?"):
        p.use_alerts = True
        p.smtp_host = _in("SMTP host", "smtp.gmail.com")
        p.smtp_user = _in("SMTP user (from address)")
        p.smtp_pass = _in("SMTP app password", secret=True)
        p.mail_to = _in("Alert recipient")
    return p


def _in(q, default="", secret=False):
    from getpass import getpass
    if secret:
        v = getpass.getpass(f"{q}: ")
        return v or default
    v = input(f"{q} [{default}]: ").strip()
    return v or default


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="linux-media-setup-with-dash installer")
    ap.add_argument("--profile", type=Path, help="load a saved profile YAML")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen")
    ap.add_argument("--yes", action="store_true", help="accept every prompt")
    ap.add_argument("--save-profile", type=Path,
                    help="save the final profile here (e.g. my-profile.yaml)")
    args = ap.parse_args()

    plat = Platform.detect()
    log(f"Detected: {plat.pretty} ({plat.family})")

    if args.profile and args.profile.exists():
        p = Profile.from_yaml(args.profile)
        log(f"Loaded profile: {args.profile}")
    else:
        p = Profile.with_generated_secrets()
        p = ask_questions(p)

    errs = p.validate()
    if errs:
        for e in errs:
            print(f"  !! {e}")
        if not confirm("Continue anyway?", default=False):
            return 1

    if args.save_profile:
        p.save(args.save_profile)
        log(f"Profile saved (chmod 600): {args.save_profile}")

    sudo = Sudo()
    ok, msg = sudo.validate()
    if not ok:
        print(f"NOTE: {msg}")

    from lib.steps import Ctx
    ctx = Ctx(p, plat, sudo, dry_run=args.dry_run,
              repo_root=Path(__file__).resolve().parent.parent)

    accept = lambda q: confirm(q, default=True, yes_all=args.yes)
    accept_steps = {name: True for name in ("System packages", "Docker engine",
                                            "Start containers")}
    print("\nComponents to install:")
    for name, enabled in component_flags(p).items():
        print(f"  {'x' if enabled else ' '} {name}")
        accept_steps[name] = enabled
    if not confirm("\nProceed with the install?", default=True):
        return 1

    results = run_all(ctx, accept, accept_steps)
    print("\n===== SUMMARY =====")
    failed = 0
    for name, ok, msg in results:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}: {msg}")
        failed += 0 if ok else 1
    print("\nDone." if not failed else f"\n{failed} step(s) failed — see the log above.")
    return 0 if not failed else 1


def component_flags(p: Profile) -> dict:
    return {
        "System packages": True,
        "Docker engine": True,
        "Docker stacks": p.use_media_stack or p.use_vpn_stack,
        "Local scripts": True,
        "Systemd units": p.use_dashboard or p.use_post_reboot_check,
        "Cron jobs": p.use_daily_routine or p.use_duckdns or p.use_wastebins,
        "Start containers": p.use_media_stack or p.use_vpn_stack,
        "Verify": True,
    }


if __name__ == "__main__":
    raise SystemExit(main())
