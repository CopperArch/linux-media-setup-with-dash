#!/usr/bin/env python3
"""GUI installer (tkinter) for linux-media-setup-with-dash.

Wizard: welcome -> profile source -> questions -> accepts -> sudo -> live
install log -> summary. Every system-changing step needs an explicit accept;
the sudo password lives in memory only.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext
except ImportError:
    raise SystemExit("tkinter missing: install python3-tk (apt) / "
                     "python3-tkinter (dnf) / tk (pacman)")

from lib.platform import Platform
from lib.profile import Profile
from lib.util import Sudo, log

BG, FG, ACCENT, INPUT = "#292c33", "#e8eaed", "#4f8cff", "#3a3f4b"


class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Linux Media Setup with Dashboard")
        self.geometry("880x640")
        self.configure(bg=BG)
        self.profile: Profile | None = None
        self.accept_steps: dict[str, bool] = {}
        self.results: list[tuple[str, bool, str]] = []
        self.installing = False
        self.logq: queue.Queue[str] = queue.Queue()
        self.c = tk.Frame(self, bg=BG)          # page container
        self.c.pack(fill="both", expand=True, padx=20, pady=14)
        self.show("welcome")

    # ── helpers ──────────────────────────────────────────────────────────
    def header(self, text):
        tk.Label(self.c, text=text, bg=BG, fg=ACCENT,
                 font=("Sans", 16, "bold")).pack(anchor="w", pady=(0, 10))

    def para(self, text):
        tk.Label(self.c, text=text, bg=BG, fg=FG, justify="left",
                 font=("Sans", 11)).pack(anchor="w")

    def nav(self, on_back=None, on_next=None, next_label="Next"):
        bar = tk.Frame(self.c, bg=BG)
        bar.pack(side="bottom", fill="x", pady=(14, 0))
        if on_back:
            tk.Button(bar, text="Back", command=on_back, bg="#3a3f4b", fg=FG,
                      activebackground="#4a505e").pack(side="left")
        if on_next:
            tk.Button(bar, text=next_label, command=on_next, bg=ACCENT, fg="#fff",
                      font=("Sans", 10, "bold")
                      ).pack(side="right")

    def show(self, page):
        for w in self.c.winfo_children():
            w.destroy()
        getattr(self, f"page_{page}")()

    # ── 1 welcome ────────────────────────────────────────────────────────
    def page_welcome(self):
        self.header("Linux Media Setup with Dashboard")
        self.para(
            "Sets up a full self-healing home-server, on Ubuntu/Debian, Fedora or Arch:\n\n"
            "   • Docker stacks — Plex, Jellyfin, Immich, Nextcloud, seerr,\n"
            "     qBittorrent + *arr behind a VPN, Caddy reverse proxy\n"
            "   • Desktop status dashboard with one-click repairs\n"
            "   • Nightly self-healing maintenance routine\n\n"
            "Nothing is installed without an explicit accept from you.\n"
            "Load a profile to reproduce an existing machine, or answer fresh\n"
            "questions for your own setup.")
        self.nav(on_next=lambda: self.show("profile"), next_label="Start")

    # ── 2 profile source ─────────────────────────────────────────────────
    def page_profile(self):
        self.header("Profile")
        self.para("A profile stores the answers (domains, paths, choices).\n"
                  "Loading someone else's profile installs THEIR choices, then asks\n"
                  "for YOUR credentials. Starting fresh asks you everything.")
        tk.Button(self.c, text="Load a profile file…", width=30,
                  command=self._load).pack(anchor="w", pady=(12, 4))
        tk.Button(self.c, text="Start fresh (ask me questions)", width=30,
                  command=self._fresh).pack(anchor="w", pady=4)
        self.nav(on_back=lambda: self.show("welcome"),
                 on_next=lambda: self.show("questions"), next_label="Continue")

    def _fresh(self):
        self.profile = Profile.with_generated_secrets()
        self.show("questions")

    def _load(self):
        path = filedialog.askopenfilename(filetypes=[("Profile", "*.yaml *.yml")])
        if not path:
            return
        try:
            self.profile = Profile.from_yaml(Path(path))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Profile", f"Could not load:\n{e}")
            return
        errs = self.profile.validate()
        if errs:
            messagebox.showwarning("Profile", "\n".join(errs))
        self.show("questions")

    # ── 3 questions ──────────────────────────────────────────────────────
    def page_questions(self):
        self.header("Your setup")
        p = self.profile
        rows = tk.Frame(self.c, bg=BG)
        rows.pack(anchor="w", fill="x")
        text_fields = [
            ("Username", "user"),
            ("LAN IP of this machine", "lan_ip"),
            ("Base domain (blank = skip)", "domain"),
            ("Media pool mount", "media_pool"),
            ("Nextcloud data mount", "nextcloud_data"),
        ]
        self.entries = {}
        for i, (label, attr) in enumerate(text_fields):
            tk.Label(rows, text=label, bg=BG, fg=FG, width=30,
                     anchor="w").grid(row=i, column=0, sticky="w", pady=3)
            e = tk.Entry(rows, width=36, bg=INPUT, fg=FG, insertbackground=FG)
            e.insert(0, str(getattr(p, attr) or ""))
            e.grid(row=i, column=1, sticky="w", pady=3)
            e.bind("<KeyRelease>", lambda ev, a=attr, en=e:
                   self._set(p, a, en.get()))
            self.entries[attr] = e

        tk.Label(self.c, text="Components", bg=BG, fg=ACCENT
                 ).pack(anchor="w", pady=(16, 4))
        checks = [
            ("VPN download stack (gluetun/qBit/Sonarr/Radarr)", "use_vpn_stack"),
            ("Media stack (Plex/Jellyfin/Immich/Nextcloud)", "use_media_stack"),
            ("Desktop dashboard + self-healing", "use_dashboard"),
            ("Nightly maintenance routine", "use_daily_routine"),
            ("DuckDNS dynamic DNS", "use_duckdns"),
            ("Post-reboot verification", "use_post_reboot_check"),
        ]
        self.checks = {}
        for label, attr in checks:
            v = tk.BooleanVar(value=bool(getattr(p, attr)))
            cb = tk.Checkbutton(self.c, text=label, variable=v, bg=BG, fg=FG,
                                selectcolor=INPUT, activebackground=BG,
                                anchor="w",
                                command=lambda a=attr, vv=v: self._set(p, a, vv.get()))
            cb.pack(anchor="w", fill="x", pady=1)
            self.checks[attr] = v
        self.nav(on_back=lambda: self.show("profile"),
                 on_next=lambda: self.show("secrets"), next_label="Continue")

    @staticmethod
    def _set(p, attr, value):
        try:
            if isinstance(getattr(p, attr), bool):
                value = bool(value)
            elif isinstance(getattr(p, attr), (int, float)):
                value = type(getattr(p, attr))(value)
        except (ValueError, TypeError):
            pass
        setattr(p, attr, value)

    # ── 3b credentials ───────────────────────────────────────────────────
    def page_secrets(self):
        self.header("Credentials")
        p = self.profile
        self.para("Secrets are stored in chmod-600 files under ~/.config and in the\n"
                  "stack .env files — never in the scripts. Generated passwords are\n"
                  "already filled in; overwrite only what you want to change.")
        rows = tk.Frame(self.c, bg=BG)
        rows.pack(anchor="w", fill="x", pady=10)
        secret_fields = [
            ("VPN username", "vpn_user"),
            ("VPN password", "vpn_password"),
            ("VPN provider", "vpn_provider"),
            ("VPN countries", "vpn_server_countries"),
            ("Plex claim token", "plex_claim"),
            ("qBittorrent password", "qbit_password"),
            ("Nextcloud DB password", "nc_db_password"),
            ("Immich DB password", "immich_db_password"),
            ("DuckDNS domains (a,b)", "duckdns_domains"),
            ("DuckDNS token", "duckdns_token"),
            ("OpenRouter API key (AI panes)", "openrouter_api_key"),
        ]
        self.secrets = {}
        for i, (label, attr) in enumerate(secret_fields):
            tk.Label(rows, text=label, bg=BG, fg=FG, width=30,
                     anchor="w").grid(row=i, column=0, sticky="w", pady=2)
            e = tk.Entry(rows, width=36, bg=INPUT, fg=FG, insertbackground=FG)
            e.insert(0, str(getattr(p, attr) or ""))
            e.grid(row=i, column=1, sticky="w", pady=2)
            e.bind("<KeyRelease>", lambda ev, a=attr, en=e:
                   self._set(p, a, en.get()))
            self.secrets[attr] = e
        self.nav(on_back=lambda: self.show("questions"),
                 on_next=self._to_accepts, next_label="Continue")

    def _to_accepts(self):
        errs = self.profile.validate()
        if errs:
            if not messagebox.askyesno("Profile incomplete",
                                       "\n".join(errs) +
                                       "\n\nContinue anyway?"):
                return
        self.show("accepts")

    # ── 4 accepts ────────────────────────────────────────────────────────
    def page_accepts(self):
        self.header("What may the installer do?")
        self.para("Tick what you accept; anything unticked is skipped and can be\n"
                  "installed later by running the installer again.")
        groups = [
            ("Install system packages (python, git, curl, cron, htop…)",
             "System packages", True),
            ("Install Docker engine + compose plugin", "Docker engine", True),
            ("Write configs to ~/docker, ~/.local, ~/.config", "Docker stacks", True),
            ("Install cron jobs (maintenance schedule)", "Cron jobs", True),
            ("Pull images and start all containers", "Start containers", True),
            ("Run verification at the end", "Verify", True),
        ]
        self.accepts = {}
        for label, key, default in groups:
            v = tk.BooleanVar(value=default)
            tk.Checkbutton(self.c, text=label, variable=v, bg=BG, fg=FG,
                           selectcolor=INPUT, activebackground=BG, anchor="w"
                           ).pack(anchor="w", fill="x", pady=2)
            self.accepts[key] = v
        self.nav(on_back=lambda: self.show("secrets"),
                 on_next=self._to_password, next_label="Install")

    def _to_password(self):
        self.accept_steps = {k: v.get() for k, v in self.accepts.items()}
        self.show("password")

    # ── 5 sudo password ──────────────────────────────────────────────────
    def page_password(self):
        self.header("Sudo password")
        self.para("Package installation needs root. Your sudo password is kept in\n"
                  "memory for this run only — never saved or logged.")
        e = tk.Entry(self.c, show="*", width=30, bg=INPUT, fg=FG,
                     insertbackground=FG)
        e.pack(anchor="w", pady=(10, 4))
        self.sudo_entry = e
        self.nav(on_back=lambda: self.show("accepts"),
                 on_next=self._begin, next_label="Begin install")

    # ── 5 install ────────────────────────────────────────────────────────
    def page_install(self):
        self.header("Installing…")
        box = scrolledtext.ScrolledText(self.c, bg="#1e2128", fg="#c8d0dc",
                                        insertbackground=FG, font=("Mono", 9))
        box.pack(fill="both", expand=True)
        self.logbox = box
        self.after(150, self._pump)
        threading.Thread(target=self._worker, daemon=True).start()

    def _pump(self):
        try:
            while True:
                self.logbox.insert("end", self.logq.get_nowait())
        except queue.Empty:
            pass
        self.logbox.see("end")
        if self.installing or not self.logq.empty():
            self.after(150, self._pump)
        elif getattr(self, "results", None):
            self.after(400, lambda: self.show("done"))

    def _worker(self):
        from lib.steps import Ctx, run_all
        try:
            plat = Platform.detect()
            sudo = Sudo(gui_password=self.sudo_entry.get())
            ok, msg = sudo.validate()
            log(f"sudo: {msg}")
            if not ok:
                raise RuntimeError(msg)
            ctx = Ctx(self.profile, plat, sudo,
                      repo_root=Path(__file__).resolve().parent.parent)
            self.results = run_all(ctx, self._accept, self.accept_steps)
            self.installing = False
        except Exception as e:  # noqa: BLE001
            log(f"FATAL: {type(e).__name__}: {e}")
            self.installing = False

    def _accept(self, question: str) -> bool:
        ans = {"v": False}
        evt = threading.Event()

        def ask():
            ans["v"] = messagebox.askyesno("Accept", question)
            evt.set()

        self.after(0, ask)
        evt.wait()
        return ans["v"]

    # ── 6 done ───────────────────────────────────────────────────────────
    def page_done(self):
        self.header("Finished")
        failed = [r for r in self.results if not r[1]]
        if failed:
            self.para("Install finished with failures:")
            for name, _, msg in failed:
                tk.Label(self.c, text=f"  ✗ {name}: {msg}", bg=BG, fg="#e06c75",
                         anchor="w").pack(anchor="w")
        else:
            self.para("Install complete.\n\n"
                      "   • Dashboard:   http://127.0.0.1:8099\n"
                      "   • Self-heal:   nightly at 03:00 (daily-routine)\n"
                      "   • Re-running the installer is safe (idempotent).")
        tk.Button(self.c, text="Save profile…", width=20,
                  command=self._save_profile).pack(anchor="w", pady=(12, 0))
        self.nav(on_next=self.destroy, next_label="Close")

    def _save_profile(self):
        path = filedialog.asksaveasfilename(defaultextension=".yaml",
                                            initialfile="my-profile.yaml")
        if path:
            self.profile.save(Path(path))
            messagebox.showinfo("Saved", f"Profile saved to {path}")


def main():
    w = Wizard()
    w.mainloop()


if __name__ == "__main__":
    main()
