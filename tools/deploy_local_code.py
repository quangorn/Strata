#!/usr/bin/env python3
"""Copy this checkout's own Python changes into the Control component that runs on this PC.

This PC takes everything from upstream as binaries, and this checkout owns two things: the engine and the picture reader,
built by `packaging/build_engine_win_local.py`, and the Python of the launch and update scripts, patched here. A model serves
as `components\\control\\<version>\\python\\python.exe -m serve.server`, so the patched files have to be in the Control folder
that is active. The terminal is NOT touched: the terminal `strata` on this PC (terminal/current, an upstream release) has a
code base far older than this checkout, and putting files from a newer code base into an older install folder mixes two
versions of the same modules. The launch runs from this checkout instead: `bin\\strata.ps1` (or `bin\\strata.cmd`) uses its
`.venv`, and the same launcher upstream ships.

The files copied are the ones this checkout changed against upstream (`git diff --name-only upstream/main HEAD`, plus the
files that are edited and not committed), limited to `strata.py`, `setup.py`, `strata_cli/`, `strata_control/` and `serve/`.
Nothing else of the install is touched and nothing is downloaded. A Control folder's files are not hashed on a start
(`components/control/active.json` records the archive's SHA-256), so the copy is not refused; an engine package's files are
checked by SHA-256 against their manifest (`strata_control/engine_component.py`), which is why the engine reaches this PC
only as a package built here.

    python tools/deploy_local_code.py plan        show which files differ; nothing is written
    python tools/deploy_local_code.py            copy them (a file the target does not have is added)

Run it after `strata update`: a new Control version arrives as a new folder, and the patched files must go in there too. Pull
upstream first, so the files copied are the newest upstream code plus this checkout's own changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: the parts of a Strata install this checkout owns: the CLI, the control library, the server and the two entry scripts
PARTS = ("strata_cli", "strata_control", "serve")
LOOSE = ("strata.py", "setup.py")
SKIP = ("__pycache__", ".pytest_cache", ".venv")


def home() -> Path:
    base = os.environ.get("STRATA_HOME")
    if base:
        return Path(base)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Strata"


def targets(h: Path) -> list[tuple[str, Path]]:
    """The install folder that runs on this PC: the active Control component (the server's code)."""
    out: list[tuple[str, Path]] = []
    active = h / "components" / "control" / "active.json"
    if active.is_file():
        try:
            folder = json.loads(active.read_text(encoding="utf-8-sig")).get("folder")
        except (OSError, ValueError):
            folder = None
        if isinstance(folder, str) and folder:
            p = h / "components" / "control" / folder
            if (p / "strata.py").is_file():
                out.append(("control", p))
    return out


def git(*args) -> list[str]:
    """The lines a git command prints in this checkout; an empty list when git or the remote is not there."""
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except OSError:
        return []


def owned() -> list[Path]:
    """The files this checkout owns as paths relative to its root: the ones it changed against upstream, the ones edited
    and not committed, and the new ones it added. Only the parts of an install it owns are kept."""
    names = set(git("diff", "--name-only", "upstream/main", "HEAD"))            # committed fork changes
    names |= set(git("diff", "--name-only"))                                     # edited, not committed
    names |= set(git("ls-files", "--others", "--exclude-standard"))              # added, not tracked
    found = []
    for name in sorted(names):
        rel = Path(name.replace("\\", "/"))
        if not rel.parts:
            continue
        top = rel.parts[0]
        if top not in PARTS and rel.as_posix() not in LOOSE:
            continue
        if any(part in rel.parts for part in SKIP):
            continue
        if not (ROOT / rel).is_file():
            continue
        found.append(rel)
    return found


def digest(path: Path) -> str:
    """SHA-256 of the file with Windows line endings folded to Unix ones, so a copy that differs only in endings is not
    counted as changed (the release archives carry Unix endings, an editor here writes Windows ones)."""
    hash_ = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            hash_.update(block.replace(b"\r\n", b"\n"))
    return hash_.hexdigest()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    step = (argv[0] if argv and argv[0] in ("plan", "deploy") else "deploy")
    h = home()
    found = targets(h)
    if not found:
        print(f"[strata] no Control component to update under {h}: run Strata's installer first")
        return 1
    src = owned()
    if not src:
        print("[strata] nothing of this checkout's Python is changed against upstream: nothing to copy")
        return 0
    copied = 0
    for label, folder in found:
        print(f"{label}: {folder}")
        for rel in src:
            s = ROOT / rel
            t = folder / rel
            existed = t.is_file()
            if existed and digest(s) == digest(t):
                continue
            if step == "plan":
                print(f"  {'new' if not existed else 'patched'}  {rel}")
                copied += 1
                continue
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, t)
            print(f"  {'added' if not existed else 'updated'}  {rel}")
            copied += 1
        if step == "deploy":
            for part in PARTS:                                     # stale byte-code under the copied modules is dropped
                for cache in (folder / part).rglob("__pycache__"):
                    shutil.rmtree(cache, ignore_errors=True)
    print(f"[strata] {'files that differ' if step == 'plan' else 'files copied'}: {copied} in {len(found)} install folder(s)")
    if step == "deploy" and copied:
        # the running server keeps its own copy of the code it loaded; a model that is serving now starts with the new files
        print("[strata] restart the model to run the new code: strata launch --stop, then start it again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
