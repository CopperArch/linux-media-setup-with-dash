# linux-media-setup-with-dash

A graphical installer that reproduces a complete self-healing Linux home
server — Docker media/VPN stacks, a desktop status dashboard with one-click
repairs, and a nightly self-healing maintenance routine — on
**Ubuntu/Debian, Fedora and Arch**.

## What it installs

| Component | Contents |
|---|---|
| **Media stack** | Plex, Jellyfin, Immich (+DB/ML), Nextcloud stack, Seerr, Portainer, Tugtainer, WatchState, Caddy reverse proxy |
| **VPN stack** | Gluetun (VPN hub), qBittorrent, Sonarr, Radarr, Prowlarr, Seerr-v2, FlareSolverr/Byparr, KasmVNC Chrome (magnet handler wired to qBittorrent) |
| **Dashboard** | Local status page (loopback-only HTTP server), 60s live collector, one-click repair API (origin-guarded, whitelisted fixes only), terminal panes via ttyd (`--check-origin`), desktop window with below-layer KWin rule |
| **Self-healing** | Nightly `daily-routine.sh` at 03:00: SMART health, container crash-loop detection, VPN IP rotation, qBittorrent queue self-heal, media-stack auto-update with verify+rollback, dashboard re-install on drift, docker log-rotation guard, backups, system updates |
| **Cron jobs** | Daily routine, DuckDNS updater, wastebin emptier, hourly VPN exit rotation |

Everything it writes is idempotent — run the installer again on a drifted
machine and it repairs itself. The nightly routine re-checks the same things,
so the system keeps healing itself long after the install.

## The profile is the key

The installer asks you questions once and saves the answers as a **profile
YAML**. Hand that file to the installer on another machine (Ubuntu today,
Fedora tomorrow) and you get the same setup.

- **Your profile, your data**: a profile carries *choices*, not installer
  logic. Secrets (VPN credentials, passwords) live in it too, `chmod 600` —
  treat it like a password file.
- **Someone else's profile**: loading a friend's profile installs *their*
  choices but then asks *you* for your own credentials — it never copies their
  secrets unless they deliberately left them in.
- **No profile?** Start fresh: the wizard asks the questions and generates
  strong passwords for every service.

## Usage

```bash
# graphical wizard (tkinter)
python3 install.py

# terminal installer
python3 install.py --cli

# reproduce an existing machine from its saved profile
python3 install.py --cli --profile my-profile.yaml

# see what would happen without changing anything
python3 install.py --cli --profile my-profile.yaml --dry-run

# no prompts at all (scripted installs)
python3 install.py --cli --profile my-profile.yaml --yes
```

Every step that changes the system shows an explicit **accept** prompt:
system packages, Docker engine, config writes, cron jobs, starting
containers. Untick what you don't want; the installer is safe to re-run.

After the install, the profile can be saved for reuse
(`--save-profile my-profile.yaml`, or the GUI's "Save profile…" button).

## Distro support

Distro detection and every package install go through a single platform
layer (`lib/platform.py`) with per-family package maps:

| Family | Distros | Package manager |
|---|---|---|
| apt | Ubuntu, Debian, Mint, Pop!_OS | `apt-get` |
| dnf | Fedora, CentOS, Rocky, Nobara | `dnf` (+ Docker CE repo setup) |
| pacman | Arch, EndeavourOS, CachyOS, Manjaro | `pacman` |
| zypper | openSUSE | `zypper` |

Notes:
- **Arch**: `mergerfs` and `ufw` live in the AUR — the installer skips them
  and prints what to do; everything else installs from official repos.
- **Fedora**: Docker CE is installed from Docker's official repository.
- **Firewall** is best-effort: `ufw` on Debian-family, `firewalld` on Fedora,
  a printed note on Arch.
- The **daily-routine sudoers rules** are only installed on apt systems
  (they cover apt-get); on other distros the routine's package-update step
  is skipped by the routine itself.

## Self-healing, in three layers

1. **systemd** — dashboard server/terminal units run with `Restart=always`,
   the collector runs every 60s from a timer.
2. **Dashboard repairs** — the web dashboard exposes a closed whitelist of
   fixes (container start/restart, VPN recreate/rotate, Prowlarr test-all,
   Cloudflare-ban unblock, apt upgrade, quick routine). Guards: loopback
   binding, `Sec-Fetch-Site`/Origin CSRF checks, 64 KB body cap, confirmed
   reboot endpoint, argv-list execution only.
3. **Nightly routine** — `daily-routine.sh` re-installs drifted dashboard
   units, re-applies gluetun iptables rules, verifies the docker log-cap
   policy, auto-updates the arr stack with verify+auto-rollback, and runs
   `media-stack-selfheal.py` (qBittorrent queue + dead-torrent recovery).
   VPN IP rotation retries up to 3 restarts before giving up (a single bad
   exit node used to leave the tunnel dead for hours), and both it and the
   hourly `gluetun-rotate.sh` cron alert by email (`send-alert.py`, debounced
   so a prolonged outage sends one mail, not one an hour) if the tunnel
   won't come back — `media-stack-selfheal.py` also checks the tunnel
   directly (`vpn_tunnel_health()`) rather than trusting gluetun's Docker
   healthcheck, which stays "healthy" even when OpenVPN is stuck failing
   auth in a loop.

## Repository layout

```
install.py              # entry point (GUI by default, --cli fallback)
installer/cli.py        # terminal installer
installer/gui.py        # tkinter wizard
lib/platform.py         # distro detect + package maps
lib/profile.py          # profile load/save/validate
lib/render.py           # {{PLACEHOLDER}} template renderer
lib/steps.py            # ordered install steps
lib/util.py             # run/log/sudo helpers
templates/              # your exact system, templatized
  bin/ttyd              # static terminal binary
  daily-routine.sh      # nightly self-healing routine
  media-stack-*.py      # self-heal + verified auto-update
  status-*              # dashboard collector/server/installer
  index.html            # the dashboard page
  stacks/media-stack/   # compose + caddy (DuckDNS build)
  stacks/vpn-stack/     # gluetun/qbit/arr/seerr/chrome compose + Kasm build
  units/                # post-reboot check unit
  etc/                  # root-owned helpers + sudoers
profiles/example.yaml   # fill-in template profile
scripts/dry-run.sh      # offline full dry-run test
```

## Privacy

Templates in this repo contain no personal data — names, domains, IPs,
credentials and API keys are all `{{PLACEHOLDER}}` values rendered from the
profile at install time. The example profile ships with blanks.

## Requirements

- Any systemd distro from the table above (x86_64 for the bundled ttyd)
- Docker-capable kernel (the installer installs Docker if missing)
- A desktop session for the dashboard window (the server/collector run
  headless too)

## License

MIT — see [LICENSE](LICENSE).
