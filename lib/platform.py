"""Distro detection and package management for Ubuntu/Debian, Fedora,
Arch (and derivatives), plus OpenSUSE.

Everything the installer installs goes through PKGS[family], so adding a new
distro means adding a map entry — the install steps never call a package
manager directly.
"""
from __future__ import annotations

import os
import platform as _platform
from dataclasses import dataclass, field

from .util import have, run


@dataclass
class Platform:
    family: str            # apt | dnf | pacman | zypper
    distro_id: str         # ubuntu, fedora, arch, ...
    pretty: str
    arch: str = field(default_factory=lambda: _platform.machine())
    pkg_mgr: str = ""

    # ── package name map per family ──────────────────────────────────────
    # Every entry: logical-name -> (apt, dnf, pacman, zypper)  (None = N/A)
    PKGS = {
        "docker":        ("docker.io",              "docker-ce",           "docker",         "docker"),
        "docker-compose":("docker-compose-v2",      "docker-compose-plugin","docker-compose","docker-compose"),
        "python3":       ("python3",                "python3",             "python",         "python311"),
        "tkinter":       ("python3-tk",             "python3-tkinter",     "tk",             "python3-tk"),
        "git":           ("git",                    "git",                 "git",            "git"),
        "curl":          ("curl",                   "curl",                "curl",           "curl"),
        "jq":            ("jq",                     "jq",                  "jq",             "jq"),
        "cron":          ("cron",                   "cronie",              "cronie",         "cronie"),
        "smartmontools": ("smartmontools",          "smartmontools",       "smartmontools",  "smartmontools"),
        "htop":          ("htop",                   "htop",                "htop",           "htop"),
        "ncdu":          ("ncdu",                   "ncdu",                "ncdu",           "ncdu"),
        "rsync":         ("rsync",                  "rsync",               "rsync",          "rsync"),
        "unzip":         ("unzip",                  "unzip",               "unzip",          "unzip"),
        "mergerfs":      ("mergerfs",               "mergerfs",            None,             "mergerfs"),
        "ufw":           ("ufw",                    "ufw",                 None,             None),
        "xrandr":        ("x11-xserver-utils",      "xrandr",              "xorg-xrandr",    "xrandr"),
    }

    def pkg_name(self, logical: str) -> str | None:
        idx = {"apt": 0, "dnf": 1, "pacman": 2, "zypper": 3}[self.family]
        return self.PKGS[logical][idx]

    # ── detection ────────────────────────────────────────────────────────
    @staticmethod
    def detect() -> "Platform":
        os_rel = {}
        for path in ("/etc/os-release", "/usr/lib/os-release"):
            try:
                with open(path) as fh:
                    for line in fh:
                        if "=" in line:
                            k, _, v = line.strip().partition("=")
                            os_rel[k] = v.strip().strip('"')
                if os_rel:
                    break
            except OSError:
                continue
        did = (os_rel.get("ID") or "unknown").lower()
        likes = [x.strip() for x in (os_rel.get("ID_LIKE") or "").split()]
        pretty = os_rel.get("PRETTY_NAME") or did

        if did in ("ubuntu", "debian", "linuxmint", "pop") or "debian" in likes:
            fam = "apt"
        elif did in ("fedora", "rhel", "centos", "rocky", "alma", "nobara") or "fedora" in likes:
            fam = "dnf"
        elif did in ("arch", "endeavouros", "manjaro", "cachyos", "garuda") or "arch" in likes:
            fam = "pacman"
        elif "suse" in did or "suse" in likes:
            fam = "zypper"
        else:
            fam = "apt"  # best guess; dpkg check below corrects it
        if not have({"apt": "apt-get", "dnf": "dnf", "pacman": "pacman",
                     "zypper": "zypper"}[fam]):
            for f, cmd in (("apt", "apt-get"), ("dnf", "dnf"),
                           ("pacman", "pacman"), ("zypper", "zypper")):
                if have(cmd):
                    fam = f
                    break
        return Platform(family=fam, distro_id=did, pretty=pretty,
                        pkg_mgr={"apt": "apt-get", "dnf": "dnf",
                                 "pacman": "pacman", "zypper": "zypper"}[fam])

    # ── package installation ─────────────────────────────────────────────
    def install(self, sudo, logicals: list[str]) -> tuple[bool, str]:
        """Install logical packages. Fedora gets docker-ce via Docker's repo."""
        names = [self.pkg_name(l) for l in logicals]
        missing = [n for n, l in zip(names, logicals) if n is None]
        if missing:
            return False, ("no " + self.family + " package mapping for: "
                           + ", ".join(l for n, l in zip(names, logicals) if n is None))
        names = [n for n in names if n]

        if self.family == "apt":
            sudo.run(["apt-get", "update"], timeout=600)
            p = self._apt_install(sudo, names)
        elif self.family == "dnf":
            if "docker-ce" in names:
                self._setup_docker_repo_fedora(sudo)
            p = sudo.run(["dnf", "install", "-y", *names], timeout=3600)
        elif self.family == "pacman":
            p = sudo.run(["pacman", "-Sy", "--noconfirm", "--needed", *names],
                         timeout=3600)
        else:  # zypper
            p = sudo.run(["zypper", "--non-interactive", "install", *names],
                         timeout=3600)
        return p.returncode == 0, f"rc={p.returncode}"

    def _apt_install(self, sudo, names):
        """apt with DEBIAN_FRONTEND=noninteractive injected via env."""
        self.validate_sudo(sudo)
        import subprocess
        cmd = ["sudo", "-n", "apt-get", "-y", "install", *names]
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=3600, env=env)

    @staticmethod
    def validate_sudo(sudo) -> None:
        ok, msg = sudo.validate()
        if not ok:
            raise RuntimeError(msg)

    def _setup_docker_repo_fedora(self, sudo) -> None:
        """docker-ce lives in Docker's own repo on Fedora."""
        p = run(["dnf", "-q", "repolist", "--enabled"], quiet=True)
        if p.returncode == 0 and "docker-ce" in p.stdout:
            return
        sudo.run(["dnf", "-y", "install", "dnf-plugins-core"], timeout=600)
        sudo.run(["dnf", "-y", "config-manager", "--add-repo",
                  "https://download.docker.com/linux/fedora/docker-ce.repo"],
                 timeout=600)

    def service(self, sudo, unit: str, action: str) -> bool:
        return sudo.run(["systemctl", action, unit], timeout=120).returncode == 0

    def crontab_user(self, entries: list[str]) -> bool:
        """Merge extra crontab entries into the current user's crontab."""
        p = run(["crontab", "-l"], quiet=True)
        existing = p.stdout.splitlines() if p.returncode == 0 else []
        marker = "# linux-media-setup"
        have_block = any(marker in l for l in existing)
        if have_block:
            keep = [l for l in existing if not l.startswith(marker)]
        else:
            keep = existing
        block = [marker] + entries
        new = "\n".join(keep + block) + "\n"
        q = run(["crontab", "-"], input_text=new, quiet=True)
        return q.returncode == 0

    def firewall_open(self, sudo, ports: list[tuple[int, str]]) -> None:
        """Best-effort firewall: ufw (apt) / firewalld (dnf) / none (arch note)."""
        if self.family == "apt" and have("ufw"):
            for port, proto in ports:
                sudo.run(["ufw", "allow", f"{port}/{proto}"], timeout=60)
            return
        if self.family == "dnf" and have("firewall-cmd"):
            sudo.run(["systemctl", "enable", "--now", "firewalld"], timeout=60)
            for port, proto in ports:
                sudo.run(["firewall-cmd", "--permanent",
                          "--add-port", f"{port}/{proto}"], timeout=60)
            sudo.run(["firewall-cmd", "--reload"], timeout=60)
            return
        # arch: ufw is AUR-only; leave the firewall untouched and say so.
        print("  [SKIP] no ufw/firewalld — open these ports manually: "
              + ", ".join(f"{p}/{pr}" for p, pr in ports))
