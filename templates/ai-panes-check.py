#!/usr/bin/env python3
"""
ai-panes-check.py — nightly sanity/refresh pass for the desktop dashboard's
online AI chat panes: Ox Alpha, DeepSeek, Minimax M3 (nominally free) and
ChatGPT, Gemini, Hy4 (deliberately paid — one flagship per remaining major
provider not already covered by DeepSeek/Ox Alpha/Minimax or native Claude
Code).

What it actually checks, against OpenRouter's public /api/v1/models list:
  - Each of the three free-tier model slugs still exists and is still
    priced at $0 (OpenRouter free tiers do get retired/renamed). If one
    breaks, this WARNS rather than guessing a replacement — "best free
    model" isn't something OpenRouter's models API exposes (no usage-rank
    field), and picking a wrong slug unattended would just swap one broken
    pane for a different broken pane.
  - Each of the three paid slots IS re-derived deterministically every run:
    among that provider's chat models (excluding mini/nano/instruct/batch/
    audio/image/etc. variants), providers ship each generation as a same-day
    family and price the flagship highest — so "newest generation, highest
    completion price in that generation" reliably tracks the flagship
    without hardcoding a tier name like "sol-pro" that stops matching next
    generation.

Writes results into the model-slug lines in
~/.config/status-dashboard/deepseek.env, inside a clearly marked block —
dashboard-pane.sh sources this file fresh on every pane open, so a change
here takes effect on the next click with no service restart needed. Also
writes ~/.config/status-dashboard/model-pricing.json (current $/M-token
pricing for all six slots) so the dashboard can show live price next to each
paid pane instead of a number that goes stale the moment a model gets repriced.

Never fails the caller (daily-routine.sh): network errors and unresolvable
models are reported as warnings, exit code is always 0.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ENV_FILE = Path.home() / ".config/status-dashboard/deepseek.env"
PRICING_FILE = Path.home() / ".config/status-dashboard/model-pricing.json"
MODELS_URL = "https://openrouter.ai/api/v1/models"

# Known-good values as of 2026-09-03 — used to seed the managed block the
# first time this runs, and as the fallback if a lookup can't be resolved.
DEFAULTS = {
    "OXALPHA_MODEL":  "stealth/ox-alpha",
    "DEEPSEEK_MODEL": "deepseek/deepseek-v4-flash:free",
    "MINIMAX_MODEL":  "minimax/minimax-m3:free",
    "CHATGPT_MODEL":  "openai/gpt-5.6-sol-pro",
    "GEMINI_MODEL":   "google/gemini-3.7-flash",
    "HY4_MODEL":      "tencent/hy4-preview",
}
FREE_KEYS = ("OXALPHA_MODEL", "DEEPSEEK_MODEL", "MINIMAX_MODEL")
# (env key, OpenRouter provider prefix) — one flagship auto-picked per provider.
PAID_SLOTS = (
    ("CHATGPT_MODEL", "openai/"),
    ("GEMINI_MODEL",  "google/"),
    ("HY4_MODEL",     "tencent/"),
)
# Fallback substrings to search by if a free slug's exact id 404s (providers
# occasionally rev a free slug's suffix, e.g. a date stamp).
FAMILY_HINT = {
    "OXALPHA_MODEL":  "ox-alpha",
    "DEEPSEEK_MODEL": "deepseek-v4-flash",
    "MINIMAX_MODEL":  "minimax-m3",
}
# Stealth-model de-anonymizations confirmed by reporting, consulted only when
# both the exact id and the family-substring search find nothing at all —
# stealth slugs vanish outright rather than reving a suffix, so there's no
# substring left to search by. Add an entry here if another stealth pane's
# identity gets revealed and the old slug disappears.
KNOWN_RENAMES = {
    "stealth/ox-alpha": "z-ai/glm-5.3-flash",  # confirmed by Bloomberg/TechCrunch, 2026-08-23
}

START = "# --- ai-panes-check.py managed block — edit by re-running the script, not by hand ---"
END = "# --- end ai-panes-check.py managed block ---"

# Hyphen-anchored: a bare "mini" would also match inside "ge-mini", silently
# excluding every Gemini model and falling through to Google's unrelated
# Lyria (music-generation) line — caught 2026-09-03 when that's exactly what
# happened. Keep every size/variant token here anchored the same way.
EXCLUDE_TOKENS = ("-mini", "-nano", "instruct", "realtime", "-audio", "-image",
                   "transcribe", "-search", "embed", "whisper", "tts",
                   "moderation", "chat-latest", "codex", "-lite", "gemma",
                   "lyria", "-clip")
# Only these output modalities count as a chat pane candidate — excludes
# audio/image-output models (like Lyria) that EXCLUDE_TOKENS might miss by
# name alone. Checked directly against the model's own architecture field
# rather than guessed from its id.
ALLOWED_OUTPUT_MODALITIES = {"text"}


def fetch_models():
    req = urllib.request.Request(MODELS_URL, headers={"User-Agent": "ai-panes-check/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["data"]


def is_free(model):
    p = model.get("pricing", {})
    try:
        return float(p.get("prompt", 1)) == 0.0 and float(p.get("completion", 1)) == 0.0
    except (TypeError, ValueError):
        return False


def check_free_slot(key, current, models_by_id, warnings):
    m = models_by_id.get(current)
    if m and is_free(m):
        return current, None

    hint = FAMILY_HINT.get(key, "")
    same_family = {mid: mm for mid, mm in models_by_id.items()
                   if hint in mid and not mid.endswith(":batch")}
    free_candidates = [mid for mid, mm in same_family.items() if is_free(mm)]
    if free_candidates:
        new = sorted(free_candidates)[-1]
        why = "is no longer free" if m else "no longer exists"
        warnings.append(f"{key}: {current} {why} — switched to {new} "
                         f"(same family, still free). Verify the pane still works.")
        return new, "replaced (free)"

    # No $0 option left anywhere in the family — this is what actually
    # happened to Ox Alpha and DeepSeek's stealth/promo pricing within
    # hours of being wired up. Rather than leave the pane permanently
    # broken, fall back to the cheapest paid variant available (these run a
    # few hundredths of a cent per query) and say so loudly — this silently
    # turns a "free" pane into a billed one otherwise.
    #
    # Candidates: any same-family match, PLUS the exact id itself if it still
    # resolves (`m`) or its known rename does. Without including the exact/
    # renamed match here, a *second* run against an already-downgraded value
    # (e.g. current == "z-ai/glm-5.3-flash", which contains no "ox-alpha"
    # substring and isn't a KNOWN_RENAMES key itself) would find an empty
    # same_family and wrongly report "broken" even though the model is
    # perfectly resolvable — caught 2026-09-03 on exactly this id.
    fallback_candidates = dict(same_family)
    if m:
        fallback_candidates[current] = m
    elif current in KNOWN_RENAMES:
        renamed = KNOWN_RENAMES[current]
        rm = models_by_id.get(renamed)
        if rm:
            fallback_candidates[renamed] = rm

    if fallback_candidates:
        cheapest = min(fallback_candidates.items(),
                        key=lambda kv: float(kv[1].get("pricing", {}).get("completion", 0) or 0))
        new, mm = cheapest
        p = mm.get("pricing", {})
        if new != current:
            warnings.append(f"{key}: no free tier left for this model "
                             f"(was {current}) — falling back to the cheapest paid "
                             f"variant {new} (${p.get('prompt')}/${p.get('completion')} "
                             f"per token). This pane now bills the OpenRouter account, "
                             f"even though it's still listed as free-tier in the dashboard.")
        return new, "downgraded to paid"

    warnings.append(f"{key}: {current} no longer exists on OpenRouter and no "
                     f"replacement was found at all — pane will fail until "
                     f"fixed by hand.")
    return current, "broken"


def pick_best_paid(key, prefix, models_by_id, warnings):
    candidates = []
    for mid, m in models_by_id.items():
        if not mid.startswith(prefix) or mid.endswith(":batch"):
            continue
        if any(tok in mid for tok in EXCLUDE_TOKENS):
            continue
        out_mod = set(m.get("architecture", {}).get("output_modalities") or [])
        if out_mod and not out_mod <= ALLOWED_OUTPUT_MODALITIES:
            continue
        created = m.get("created")
        if not created:
            continue
        candidates.append((mid, created, m))
    if not candidates:
        warnings.append(f"{key}: could not list any {prefix}* models — "
                         f"keeping the current value.")
        return None
    newest = max(c[1] for c in candidates)
    generation = [c for c in candidates if newest - c[1] <= 7 * 86400]

    def price(c):
        try:
            return float(c[2].get("pricing", {}).get("completion", 0))
        except (TypeError, ValueError):
            return 0.0

    best = max(generation, key=price)
    return best[0]


def price_entry(mid, models_by_id):
    m = models_by_id.get(mid)
    if not m:
        return {"id": mid, "prompt": None, "completion": None, "free": None}
    p = m.get("pricing", {})
    try:
        prompt, completion = float(p.get("prompt", 0)), float(p.get("completion", 0))
    except (TypeError, ValueError):
        prompt = completion = None
    free = (prompt == 0.0 and completion == 0.0) if prompt is not None else None
    return {"id": mid, "prompt": prompt, "completion": completion, "free": free}


def read_env():
    if not ENV_FILE.exists():
        return "", {}
    text = ENV_FILE.read_text()
    values = dict(DEFAULTS)
    m = re.search(re.escape(START) + r"\n(.*?)\n" + re.escape(END), text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
    return text, values


def write_env(text, values):
    block = START + "\n" + "\n".join(f"{k}={values[k]}" for k in DEFAULTS) + "\n" + END
    if START in text:
        text = re.sub(re.escape(START) + r"\n.*?\n" + re.escape(END), block, text, flags=re.S)
    else:
        sep = "\n" if text and not text.endswith("\n") else ""
        text = text + sep + "\n" + block + "\n"
    ENV_FILE.write_text(text)


def main():
    warnings = []
    text, values = read_env()

    try:
        models = fetch_models()
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] ai-panes-check: couldn't reach OpenRouter ({type(e).__name__}: {e}) "
              f"— skipping tonight, keeping existing config unchanged.")
        return 0

    models_by_id = {m["id"]: m for m in models}
    new_values = dict(values)

    for key in FREE_KEYS:
        new_values[key], status = check_free_slot(key, values[key], models_by_id, warnings)

    for key, prefix in PAID_SLOTS:
        best = pick_best_paid(key, prefix, models_by_id, warnings)
        if best and best != values[key]:
            warnings.append(f"{key}: {values[key]} -> {best} "
                             f"(newer/pricier flagship found — still PAID).")
            new_values[key] = best

    changed = new_values != values
    if changed:
        write_env(text, new_values)

    pricing = {key: price_entry(new_values[key], models_by_id) for key in DEFAULTS}
    PRICING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRICING_FILE.write_text(json.dumps(pricing, indent=2) + "\n")

    print("--- ai-panes-check ---")
    for key in DEFAULTS:
        mark = " (changed)" if new_values[key] != values.get(key) else ""
        pe = pricing[key]
        price_str = ("free" if pe["free"] else
                     f"${pe['prompt']}/${pe['completion']} per token" if pe["prompt"] is not None
                     else "price unknown")
        print(f"  {key} = {new_values[key]}{mark}  ({price_str})")
    if warnings:
        for w in warnings:
            print(f"  [WARN] {w}")
    else:
        print("  [OK] all six models still resolve as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
