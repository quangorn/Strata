#!/usr/bin/env python3
"""Is the slowdown the CUDA runtime?  Same engine version (0.1.47), two builds.

The 18.10 community run used a local source build of 0.1.45; the current install
is the official release for CUDA 13.0.  The feed also carries the same engine
for CUDA 12.9, so: measure the CUDA 13 build, install and pin the CUDA 12 one,
measure again under identical conditions, then restore.

    python ab-cuda.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
LOCAL = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
COMPONENTS = LOCAL / "Strata" / "components"
CONTROL = COMPONENTS / "control"
ENGINE_ROOT = COMPONENTS / "engine"
STATE = COMPONENTS / ".updates" / "state.json"
PROJECTS = Path(__file__).resolve().parent
if not (PROJECTS / "tools" / "strata_bench.py").is_file():
    PROJECTS = Path(r"C:\Projects\Strata-fork")
LAUNCH = LOCAL / "Strata" / "launch"
SOURCE_CONFIG = LAUNCH / "iq2_xs-6gb_cache.json"
PORT = 8081
KEY = "JDa134gh17dhgn"
URL = ("https://github.com/Niko1221/Strata/releases/latest/download/"
       "strata-engine-0.1.47-windows-x86_64-cuda-cuda12.tar.gz")
SHA256 = "5647820167f62a168e9ec1bfc29a8dfd4d6daf4a521a801f76f834250164b4d1"
FOLDER = "0.1.47-windows-x86_64-cuda-cuda12"
ARCHIVE = Path(os.environ.get("TEMP", HOME)) / "strata-engine-cuda12.tar.gz"


def newest_control() -> Path:
    found = [p for p in CONTROL.iterdir()
             if (p / "python" / "python.exe").is_file() and (p / "serve" / "server.py").is_file()]
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def write_state(data: dict) -> None:
    STATE.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def set_pin(folder: str | None) -> None:
    data = read_state()
    data.setdefault("engine", {})["pinned"] = folder
    write_state(data)


def resolved_engine() -> str:
    code = ("import sys; from strata_control import engine_component as ec; "
            "c = ec.resolve(); print(c.root.name, c.build_id)")
    out = subprocess.run([str(newest_control() / "python" / "python.exe"), "-c", code],
                         capture_output=True, text=True)
    return (out.stdout or out.stderr).strip()


def install_cuda12() -> bool:
    if not ARCHIVE.is_file():
        print(f"[i] downloading {URL} ...", flush=True)
        with urllib.request.urlopen(URL, timeout=120) as response, open(ARCHIVE, "wb") as out:
            shutil.copyfileobj(response, out)
    print("[i] verifying the download ...", flush=True)
    got = sha256_file(ARCHIVE)
    if got != SHA256:
        print(f"[x] sha256 mismatch: {got}")
        return False
    target = ENGINE_ROOT / FOLDER
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    print(f"[i] unpacking into {target} ...", flush=True)
    with tarfile.open(ARCHIVE, "r:gz") as tar:
        tar.extractall(target)
    entries = [p for p in target.iterdir() if p.is_dir()]
    if len(entries) == 1 and not (target / "engine-component.json").is_file():
        inner = entries[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(target / item.name))
        inner.rmdir()
    if not (target / "engine-component.json").is_file():
        print("[x] the package has no engine-component.json")
        return False
    set_pin(FOLDER)
    print("[i] pinned:", resolved_engine(), flush=True)
    return True


def run_suite(tag: str) -> None:
    config = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    config["port"] = PORT
    work = PROJECTS / "bench" / "ab"
    work.mkdir(parents=True, exist_ok=True)
    path = work / f"cuda-{tag}.json"
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    control = newest_control()
    log = open(work / f"cuda-{tag}.server.log", "w", encoding="utf-8")
    server = subprocess.Popen([str(control / "python" / "python.exe"), "-m", "serve.server",
                               "--engine", "strata", "--config", str(path),
                               "--host", "127.0.0.1", "--port", str(PORT)],
                              cwd=str(control), stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_ready():
            print(f"[!] {tag}: the model did not load", flush=True)
            return
        print(f"[i] {tag}: model loaded, running the community suite ...", flush=True)
        out = PROJECTS / "bench" / "results" / f"2026-10-22-cuda-{tag}"
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


def summarize() -> None:
    print()
    print("=== сводка (одинаковые условия: свежий движок, одна карта) ===")
    for tag in ("cuda13", "cuda12"):
        files = list((PROJECTS / "bench" / "results" / f"2026-10-22-cuda-{tag}").glob("*/results.json"))
        if not files:
            print(f"  {tag}: нет результата")
            continue
        data = json.loads(files[0].read_text(encoding="utf-8"))
        reqs = data["results"]["chat"]["requests"]
        dec = sorted(r["decode_tps"] for r in reqs if r.get("decode_tps"))
        machine = data["machine"]
        slots = (machine.get("engine_info") or {}).get("expert_slots")
        print(f"  {tag}: chat decode median {dec[len(dec)//2]:.1f} "
              f"(min {dec[0]:.1f}, max {dec[-1]:.1f}); engine {machine.get('engine_version')}; "
              f"expert slots {slots}")
    print("  18.10 (source 0.1.45): chat decode median 129.4")


def main() -> int:
    print("=== 1/2: базовый замер на текущем движке (cuda13) ===")
    run_suite("cuda13")
    print()
    print("=== установка варианта cuda12.9 ===")
    ok = False
    try:
        ok = install_cuda12()
        if ok:
            print("=== 2/2: замер на cuda12.9 ===")
            run_suite("cuda12")
    finally:
        set_pin(None)
        shutil.rmtree(ENGINE_ROOT / FOLDER, ignore_errors=True)
        print("[i] пин снят, папка cuda12 удалена; активный движок:", resolved_engine())
    summarize()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
