#!/usr/bin/env python3
"""
send-alert.py — minimal SMTP alert sender for this box's maintenance jobs.

Stdlib only (no msmtp/ssmtp/sendmail installed here, and none needed).

Credentials are NOT stored in this file: it reads ~/.hermes/alert-email.conf
(chmod 600), so this script stays safe to sync to Nextcloud/System-Recovery
alongside the rest of ~/.local/bin. Config format is plain KEY=VALUE lines:

    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=you@gmail.com
    SMTP_PASS=<16-char Google App Password, NOT the account password>
    MAIL_FROM=you@gmail.com
    MAIL_TO=someone@gmail.com

Gmail note: SMTP_PASS must be an App Password
(https://myaccount.google.com/apppasswords). Google blocks plain account
passwords over SMTP, so a normal password will fail with 535.

Usage:
    send-alert.py "subject line"                 # body on stdin
    send-alert.py "subject line" "body text"
    send-alert.py --test                         # send a self-test mail

Exit codes: 0 sent, 1 config/send error, 2 not configured (missing conf or
placeholder password) — 2 is deliberately distinct so callers can stay quiet
until the user has actually filled the password in.
"""
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formatdate

CONF = os.path.expanduser("~/.hermes/alert-email.conf")
PLACEHOLDER = "PUT_GOOGLE_APP_PASSWORD_HERE"


def load_conf(path=CONF):
    if not os.path.isfile(path):
        return None, f"no config at {path}"
    cfg = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    for req in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_TO"):
        if not cfg.get(req):
            return None, f"{path}: missing {req}"
    if cfg["SMTP_PASS"] == PLACEHOLDER:
        return None, "SMTP_PASS is still the placeholder — not configured yet"
    cfg.setdefault("MAIL_FROM", cfg["SMTP_USER"])
    return cfg, None


def send(subject, body, cfg=None):
    if cfg is None:
        cfg, err = load_conf()
        if cfg is None:
            return 2, err
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["MAIL_FROM"]
    msg["To"] = cfg["MAIL_TO"]
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body or "(no body)")

    port = int(cfg["SMTP_PORT"])
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, context=ctx, timeout=30) as s:
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["SMTP_HOST"], port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        return 1, (f"auth rejected ({e.smtp_code}). For Gmail this almost always "
                   f"means SMTP_PASS is not a valid App Password.")
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"
    return 0, f"sent to {cfg['MAIL_TO']}"


def main(argv):
    if len(argv) >= 2 and argv[1] == "--test":
        host = os.uname().nodename
        rc, info = send(f"[homelab] test alert from {host}",
                        "If you are reading this, alerting works.\n\n"
                        "Sent by ~/.local/bin/send-alert.py --test\n")
        print(info)
        return rc
    if len(argv) < 2:
        print(__doc__.strip())
        return 1
    subject = argv[1]
    body = argv[2] if len(argv) > 2 else (sys.stdin.read() if not sys.stdin.isatty() else "")
    rc, info = send(subject, body)
    print(info)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
