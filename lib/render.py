"""{{PLACEHOLDER}} template renderer."""
from __future__ import annotations

import re
import shutil
from pathlib import Path


class MissingVar(Exception):
    pass


def render_text(text: str, vars: dict, src: str = "<text>") -> str:
    def sub(m):
        key = m.group(1)
        if key not in vars or vars[key] is None:
            raise MissingVar(f"{src}: {{{{{key}}}}} has no value in the profile")
        return str(vars[key])
    return re.sub(r"\{\{([A-Z0-9_]+)\}\}", sub, text)


def render_file(src: Path, dst: Path, vars: dict, mode: int = 0o755) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        out = render_text(src.read_text(), vars, str(src))
        dst.write_text(out)
    except UnicodeDecodeError:
        # binary asset (e.g. the ttyd binary) — copy verbatim
        import shutil
        shutil.copyfile(src, dst)
    dst.chmod(mode)
    return dst


def render_tree(src_dir: Path, dst_dir: Path, vars: dict,
                skip: tuple = ()) -> list[Path]:
    """Render every file under src_dir into dst_dir, preserving subpaths."""
    done = []
    for f in sorted(src_dir.rglob("*")):
        if not f.is_file() or f.name in skip:
            continue
        rel = f.relative_to(src_dir)
        dst = dst_dir / rel
        # shell/python scripts -> executable; data/config -> 644; secrets -> 600
        if f.suffix == ".env" or "keys" in f.name:
            mode = 0o600
        elif f.suffix in (".sh", ".py") or f.suffix == "":
            mode = 0o755
        else:
            mode = 0o644
        render_file(f, dst, vars, mode)
        done.append(dst)
    return done
