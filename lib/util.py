"""Shared helpers: logging, command execution, sudo handling, confirmations."""
import getpass
import os
import shutil
import subprocess
import sys
import time

LOG_LINES: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)
    LOG_LINES.append(msg)


class Cancelled(Exception):
    pass


def die(msg: str) -> None:
    log(f"FATAL: {msg}")
    raise SystemExit(1)


def run(cmd, *, check=False, timeout=None, input_text=None, quiet=False):
    """Run an argv list. Returns CompletedProcess (never raises on rc!=0 unless check)."""
    if isinstance(cmd, str):
        cmd = [cmd]
    if not quiet:
        log(f"  $ {' '.join(str(c) for c in cmd)}")
    try:
        p = subprocess.run(
            [str(c) for c in cmd], capture_output=True, text=True,
            timeout=timeout, input=input_text)
        if p.stdout and not quiet:
            for line in p.stdout.strip().splitlines()[-20:]:
                log(f"    {line}")
        if p.returncode != 0 and p.stderr and not quiet:
            for line in p.stderr.strip().splitlines()[-20:]:
                log(f"    ! {line}")
        if check and p.returncode != 0:
            raise RuntimeError(f"command failed rc={p.returncode}: {cmd}")
        return p
    except FileNotFoundError:
        raise RuntimeError(f"command not found: {cmd[0]}")


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


class Sudo:
    """sudo access with a cached credential.

    CLI: inherits the terminal so the user can type their password.
    GUI: collects the password once in memory and feeds `sudo -S` on stdin.
    The password is never written to disk or logs.
    """

    def __init__(self, gui_password: str | None = None):
        self.gui_password = gui_password
        self._validated = False

    def validate(self) -> tuple[bool, str]:
        """Ensure a cached sudo credential exists; returns (ok, message)."""
        if self._validated:
            return True, "sudo already validated"
        p = run(["sudo", "-n", "true"], quiet=True)
        if p.returncode == 0:
            self._validated = True
            return True, "passwordless sudo available"
        if self.gui_password is not None:
            p = run(["sudo", "-S", "-p", "", "-v"],
                    input_text=self.gui_password + "\n", quiet=True, timeout=30)
            if p.returncode == 0:
                self._validated = True
                return True, "sudo validated (password)"
            return False, "sudo password rejected"
        return False, ("sudo needs a password but no terminal is available — "
                       "run the CLI installer, or enter the password in the GUI prompt")

    def run(self, cmd, *, check=False, timeout=None, quiet=False):
        """Run a command as root via the cached credential."""
        self.validate()
        return run(["sudo", "-n"] + [str(c) for c in cmd],
                   check=check, timeout=timeout, quiet=quiet)

    def keepalive(self) -> None:
        """Refresh the cached credential (call before long steps)."""
        self.validate()


def confirm(question: str, default: bool = False, yes_all: bool = False,
            prompt_fn=None) -> bool:
    """Ask for acceptance. prompt_fn(question) -> bool overrides the CLI prompt."""
    if yes_all:
        return True
    if prompt_fn:
        return prompt_fn(question)
    ans = input(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


prompt_fn = None   # set by the GUI to route accept-prompts into dialogs
yes_all = False    # set by --yes
