#!/usr/bin/env bash
# Run a full dry-run of the installer with the example profile.
set -e
cd "$(dirname "$0")/.."
python3 - <<'EOF'
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

from lib.platform import Platform
from lib.profile import Profile
from lib.steps import Ctx, run_all
from lib.util import Sudo

plat = Platform.detect()
p = Profile.from_yaml(Path("profiles/example.yaml"))
p.lan_ip = "192.168.1.50"
p.use_vpn_stack = False          # no VPN creds in the example
ctx = Ctx(p, plat, Sudo(), dry_run=True, repo_root=Path.cwd())

accept = lambda q: True
accept_steps = {k: True for k in ("System packages", "Docker engine",
                                  "Docker stacks", "Local scripts",
                                  "Systemd units", "Cron jobs",
                                  "Start containers", "Verify")}
results = run_all(ctx, accept, accept_steps)
print("\n=== DRY RUN SUMMARY ===")
bad = 0
for name, ok, msg in results:
    print(f"  [{'OK' if ok else 'FAIL'}] {name}: {msg}")
    bad += 0 if ok else 1
raise SystemExit(1 if bad else 0)
EOF