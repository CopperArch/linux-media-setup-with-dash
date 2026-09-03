#!/usr/bin/env python3
"""linux-media-setup-with-dash — graphical installer entry point.

    python3 install.py            # GUI wizard (tkinter)
    python3 install.py --cli      # terminal installer
    python3 install.py --cli --profile my-profile.yaml --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        from installer.cli import main as cli_main
        return cli_main()
    try:
        from installer.gui import main as gui_main
    except SystemExit as e:      # tkinter missing message
        print(e)
        print("Falling back to the CLI installer.\n")
        from installer.cli import main as cli_main
        return cli_main()
    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
