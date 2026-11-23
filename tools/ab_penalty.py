#!/usr/bin/env python3
"""A/B: the same engine, the same model, only presence_penalty differs.

Why: the 18.10 community run had presence_penalty 0.0 in the run config (the
engine's own "off"), the current config has 0.5.  The engine computes the
penalty pass per token only when it is not zero, and it also shifts the
sampling distribution away from the tokens the draft model predicted - which
lowers the MTP/spec acceptance rate.

For each variant: a fresh engine, the same model, the same flags; the community
suite measures the running server.

    python ab-penalty.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
LOCAL = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
CONTROL = LOCAL / "Strata" / "components" / "control"
PROJECTS = Path(__file__).resolve().parent
if not (PROJECTS / "tools" / "strata_bench.py").is_file():
    PROJECTS = Path(r"C:\Projects\Strata-fork")
LAUNCH = LOCAL / "Strata" / "launch"
SOURCE_CONFIG = LAUNCH / "iq2_xs-6gb_cache.json"
PORT = 8081
KEY = "JDa134gh17dhgn"
VARIANTS = [("pp0.0", 0.0), ("pp0.5", 0.5)]


def newest_control() -> Path:
    found = []
    for path in CONTROL.iterdir():
        if (path / "python" / "python.exe").is_file() and (path / "serve" / "server.py").is_file():
            found.append(path)
    return sorted(found, key=lambda p: p.name)[-1]


def health(timeout: float = 3.0) -> dict:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=timeout) as response:
            return json.load(response)
    except Exception:
        return {}


def wait_ready(minutes: float = 30.0) -> bool:
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        if health().get("loaded"):
            return True
        time.sleep(5)
    return False


def main() -> int:
    control = newest_control()
    python = control / "python" / "python.exe"
    config = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    work = PROJECTS / "bench" / "ab"
    work.mkdir(parents=True, exist_ok=True)

    for name, penalty in VARIANTS:
        cfg = dict(config)
        cfg["sampling"] = dict(config.get("sampling") or {})
        cfg["sampling"]["presence_penalty"] = penalty
        cfg["port"] = PORT
        path = work / f"ab-{name}.json"
        path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

        print(f"=== {name}: presence_penalty={penalty} -> {path}", flush=True)
        log = open(work / f"server-{name}.log", "w", encoding="utf-8")
        server = subprocess.Popen([str(python), "-m", "serve.server", "--engine", "strata",
                                   "--config", str(path), "--host", "127.0.0.1", "--port", str(PORT)],
                                  cwd=str(control), stdout=log, stderr=subprocess.STDOUT)
        try:
            if not wait_ready():
                print("  [!] the model did not load in time", flush=True)
                continue
            print("  [i] model loaded, running the community suite ...", flush=True)
            out = PROJECTS / "bench" / "results" / f"2026-10-22-ab-{name}"
            subprocess.run([str(PROJECTS / ".venv" / "Scripts" / "python.exe"),
                            str(PROJECTS / "tools" / "strata_bench.py"),
                            "--url", f"http://127.0.0.1:{PORT}", "--api-key", KEY,
                            "--runs", "3", "--out", str(out)], cwd=str(PROJECTS))
        finally:
            server.terminate()
            try:
                server.wait(timeout=120)
            except subprocess.TimeoutExpired:
                server.kill()
            log.close()
            time.sleep(5)

    print()
    print("=== сводка ===")
    for name, _ in VARIANTS:
        path = PROJECTS / "bench" / "results" / f"2026-10-22-ab-{name}" / "results.json"
        if not path.is_file():
            print(f"  {name}: нет результата")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        requests = data.get("results", {}).get("chat", {}).get("requests", [])
        rates = sorted(r.get("decode_tps") or 0 for r in requests if r.get("decode_tps"))
        offered = sum(r.get("drafts_offered") or 0 for r in requests)
        accepted = sum(r.get("drafts_accepted") or 0 for r in requests)
        if rates:
            median = rates[len(rates) // 2]
            print(f"  {name}: chat decode tok/s median {median:.1f} "
                  f"(min {rates[0]:.1f}, max {rates[-1]:.1f}, n={len(rates)}); "
                  f"drafts {accepted}/{offered} = {100 * accepted / offered if offered else 0:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
