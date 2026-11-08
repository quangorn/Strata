#!/usr/bin/env python3
"""Copy this checkout's own Python changes into the Control component that runs on this PC.

This PC takes everything from upstream as binaries, and this checkout owns two things: the engine and the picture reader,
built by `packaging/build_engine_win_local.py`, and the Python of the launch and update scripts, patched here. A model serves
as `components\\control\\<version>\\python\\python.exe -m serve.server`, so the patched files have to be in the Control folder
that is active. The terminal on this PC (terminal/current, an upstream release 0.1.48.1) is not touched by the copy: its launcher
hands the command over to the installed Control (`strata_cli/handover.py`), so a model started from the terminal runs the Control
folder's code, and that code has to be complete. The daily launch runs from this checkout: `bin\\strata` (the alias in
`~/.bashrc`), through its `.venv`, with the same launcher upstream ships. Measured 2026-11-08: before the alias was pointed at
the checkout, `strata launch --preset "IQ2_XS low_ram" --no-tui` died with
`ImportError: cannot import name 'arg_layers' from 'strata_control'`; after it the same command plans with rc=0.

The files copied are **this checkout's whole tree of the parts a Control runs** — `strata.py`, `setup.py`, `strata_cli/`,
`strata_control/`, `serve/` — not a diff against any commit. Nothing else of the install is touched, nothing is downloaded and
nothing in the install is deleted (measured 2026-11-08: no file in the parts of the active Control is missing from this
checkout, so the copy cannot leave a stale module behind). A Control folder's files are not hashed on a start
(`components/control/active.json` records the archive's SHA-256), so the copy is not refused; an engine package's files are
checked by SHA-256 against their manifest (`strata_control/engine_component.py`), which is why the engine reaches this PC only
as a package built here.

The diff rules were tried and both are wrong on this PC, measured 2026-11-08. Against `upstream/main` the copy was 6 files:
the active Control (`components\\control\\0.1.56`) names `c5ab5d40`, **312 commits behind** `upstream/main`, so every file
upstream added after that release is identical to `upstream/main`, never copied, and the launch code that IS copied imports it
— `strata launch --preset "IQ2_XS low_ram" --no-tui` died with
`ImportError: cannot import name 'arg_layers' from 'strata_control'` (`strata_cli/launch/runner.py:546`). Against the install's
`COMMIT` the copy was 63 files, and the launch got further and died at
`AttributeError: module 'strata_control.gpu_fit' has no attribute 'strix_q4_stall_note'`: 18 of the parts' files in the install
are the release's own, and `COMMIT` is not a tree the folder matches (the content of `strata_control/gpu_fit.py` there is in no
commit of this checkout, and it is missing a function `c5ab5d40` has — the folder is older than the commit it names). A Control
folder of this PC is a release shell (`python\\`, `BUNDLE.json`, `COMMIT`) plus this checkout's Python, and the only shape that
is consistent inside the folder is the whole tree. The server that runs is the code it loaded at its start, so deploy first,
then stop and start the model.

    python tools/deploy_local_code.py plan        show which files differ from the install; nothing is written
    python tools/deploy_local_code.py            copy them (a file the target does not have is added)

Run it after `strata update`: a new Control version arrives as a new folder, and this checkout's tree must go in there too.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
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


def parts() -> list[Path]:
    """Every file of this checkout under the parts a Control runs, as paths relative to its root, plus the two entry scripts.
    Byte-code folders are not copied. `tools/` is not part of a Control's run, so it stays out (`tools/gguf_reader.py`, the one
    module the launch imports from there, is byte for byte the release's — md5 `314a728b…`)."""
    found = []
    for part in PARTS:
        for s in (ROOT / part).rglob("*"):
            if not s.is_file():
                continue
            rel = s.relative_to(ROOT)
            if any(skip in rel.parts for skip in SKIP):
                continue
            found.append(rel)
    for name in LOOSE:
        if (ROOT / name).is_file():
            found.append(Path(name))
    return sorted(found)


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
    src = parts()
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
