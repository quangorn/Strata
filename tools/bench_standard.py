#!/usr/bin/env python3
"""Strata standard benchmark (standard-v1, docs/BENCHMARK_PROTOCOL.md).

Nothing here is tied to a model or a project: the control bundle that carries
the standard suite is found automatically, the model facts are read from the
running server, and the reports go to D:\\tmp\\bench\\results by default
(--root changes the root, --out a single explicit folder).

    python bench_standard.py                        measure the running instance
    python bench_standard.py --profile NAME         let the runner start and stop
                                                   the engine of that profile
    python bench_standard.py --check                check the environment only

Options:
    --profile NAME   saved serving profile (AppData\\Roaming\\Strata\\profiles)
    --url URL        server root (default http://127.0.0.1:8080)
    --quant NAME     quantization for the report (auto when the server answers)
    --track NAME     default | custom (default custom)
    --notes TEXT     free text for the report
    --out DIR        a new output folder (default ./bench/results)
    --key VALUE      the server api key (auto, else $STRATA_API_KEY)
    --hash-model     hash the model files (off by default: 1-5 minutes, and the
                     run is marked nonconforming without the hashes)
    --no-start       never start an instance, only measure a running one
    --timeout MIN    how long to wait for the model to load (default 30)

Anything else is passed on to "strata bench run", e.g.
    python bench_standard.py --profile launch-gyro-s-256k --optional-concurrency
    python bench_standard.py -- --skip-model-hash
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
ROAMING = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
TERM = LOCAL / "Strata" / "terminal" / "current"
CONTROLS = LOCAL / "Strata" / "components" / "control"
PROFILES = ROAMING / "Strata" / "profiles"
DEFAULT_KEY = "JDa134gh17dhgn"
# where the reports go: <root>\bench\results\<date>-standard-<gpu>-<run id>
DEFAULT_ROOT = Path(os.environ.get("STRATA_BENCH_ROOT", r"D:\tmp"))

QUANTS = ("IQ1_M", "IQ1_S", "IQ2_XS", "IQ2_XXS", "IQ2_M", "IQ3_XXS", "IQ3_XS", "IQ3_S",
          "IQ4_XS", "TQ1_0", "TQ2_0", "Q2_0", "Q3_K_M", "Q4_K_M", "Q4_K_XL", "Q5_K_M",
          "Q6_K", "Q8_0", "BF16", "F16")


# --------------------------------------------------------------- helpers ----
def version_key(name: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", name))


def quant_of(text: str) -> str:
    """The quantization in a name or a path: TQ2_0 is not Q2_0, IQ2_XS is not Q2_XS."""
    upper = str(text or "").upper()
    for token in QUANTS:
        if re.search(r"(?<![A-Z0-9])" + re.escape(token), upper):
            return token
    return ""


def http_get(url: str, path: str, key: str = "", timeout: float = 10) -> dict:
    request = urllib.request.Request(url.rstrip("/") + path)
    if key:
        request.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except Exception:
        return {}


def find_bundle() -> Path | None:
    """The newest control bundle that carries the standard benchmark."""
    found = []
    try:
        names = os.listdir(CONTROLS)
    except OSError:
        return None
    for name in names:
        path = CONTROLS / name
        if ((path / "strata_cli" / "bench_standard.py").is_file()
                and (path / "bin" / "strata.cmd").is_file()):
            found.append((version_key(name), path))
    return sorted(found)[-1][1] if found else None


def read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def profile_info(name: str) -> dict:
    """What a saved serving profile points at: its run config, model and key."""
    data = read_json(PROFILES / (name + ".json"))
    source = str(data.get("source_config") or "")
    cfg = read_json(Path(source)) if source else {}
    model = str(cfg.get("model_name") or "")
    args_list = cfg.get("args") or []
    model_path = ""
    for index, item in enumerate(args_list):
        if item in ("--native", "--ple-gguf") and index + 1 < len(args_list):
            model_path = args_list[index + 1]
            break
    return {"model": model, "quant": quant_of(model_path) or quant_of(model),
            "model_path": model_path, "port": cfg.get("port"),
            "host": cfg.get("host"), "source_config": source,
            "api_key": str(cfg.get("api_key") or "")}


def server_facts(url: str, key: str) -> dict:
    health = http_get(url, "/health", key)
    status = http_get(url, "/v1/status", key)
    props = http_get(url, "/props", key)
    metrics = http_get(url, "/metrics", key)
    tele = http_get(url, "/v1/telemetry", key)
    model = str(props.get("model_alias") or status.get("model") or "").strip()
    path = str(props.get("model_path") or "")
    static = metrics.get("hardware_static") or {}
    gpu = str(static.get("gpu_name") or "").strip()
    if not gpu:
        devices = tele.get("devices") or []
        if devices and isinstance(devices[0], dict):
            gpu = str(devices[0].get("name") or "").strip()
    engine = str((metrics.get("engine") or {}).get("version") or status.get("engine") or "").strip()
    return {
        "loaded": bool(health.get("loaded")),
        "service": str(health.get("service") or ""),
        "model": model,
        "quant": quant_of(path) or quant_of(model),
        "gpu": gpu,
        "engine": engine,
        "context": str(health.get("max_context") or "").strip(),
        "model_path": path,
        "prior_requests": (status.get("activity") or {}).get("requests"),
    }


def find_key(profile: str) -> str:
    if os.environ.get("STRATA_API_KEY"):
        return os.environ["STRATA_API_KEY"]
    if profile:
        key = profile_info(profile).get("api_key")
        if key:
            return key
    for path in sorted(PROFILES.glob("launch-*.json")):
        key = profile_info(path.stem).get("api_key")
        if key:
            return key
    return DEFAULT_KEY


def strata_cmd(bundle: Path) -> list[str]:
    return ["cmd", "/c", str(bundle / "bin" / "strata.cmd")]


def list_profiles() -> None:
    cmd = strata_cmd(TERM) if (TERM / "bin" / "strata.cmd").is_file() else None
    if not cmd:
        print("saved profiles:", ", ".join(sorted(p.stem for p in PROFILES.glob("*.json"))))
        return
    subprocess.run(cmd + ["profile", "list"])


def wait_loaded(url: str, key: str, minutes: float) -> bool:
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        if server_facts(url, key).get("loaded"):
            return True
        print("    ... waiting for the model", flush=True)
        time.sleep(10)
    return False


def start_instance(profile: str) -> None:
    cmd = strata_cmd(TERM)
    print(f"[i] starting {profile} ...", flush=True)
    subprocess.run(cmd + ["runtime", "start", "--profile", profile, "--wait", "300"])


# ------------------------------------------------------------------ main ----
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=True, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--quant")
    parser.add_argument("--track", default="custom", choices=("default", "custom"))
    parser.add_argument("--notes")
    parser.add_argument("--out")
    parser.add_argument("--root", default=str(DEFAULT_ROOT),
                        help="report root (default %s): the report goes to <root>\\bench\\results" % DEFAULT_ROOT)
    parser.add_argument("--key")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--hash-model", action="store_true",
                        help="hash the model files (off by default: it costs 1-5 minutes and the run "
                             "is marked nonconforming without the hashes)")
    parser.add_argument("--timeout", type=float, default=30.0)
    args, extra = parser.parse_known_args(argv)

    print()
    print("=== Strata standard benchmark (standard-v1) ===")
    print(f"  mode    : {'profile' if args.profile else 'url'}"
          f"{'  (check only)' if args.check else ''}")
    print(f"  url     : {args.url}")
    print(f"  profile : {args.profile or '-'}")
    print(f"  track   : {args.track}")
    print()

    bundle = find_bundle()
    if not bundle:
        print("[x] no control bundle with the standard suite in", CONTROLS)
        print("    run 'strata update' and try again")
        return 1
    print(f"  bundle  : {bundle}")

    key = args.key or find_key(args.profile or "")
    facts = {"model": "", "quant": "", "gpu": "", "engine": ""}

    if args.profile:
        info = profile_info(args.profile)
        facts = {"model": info["model"], "quant": info["quant"], "gpu": "", "engine": ""}
        # the engine and the card are the machine's, the model is the profile's
        live = server_facts(args.url, key)
        if live.get("loaded"):
            facts["engine"] = live.get("engine") or facts["engine"]
            facts["gpu"] = live.get("gpu") or facts["gpu"]
    else:
        facts = server_facts(args.url, key)
        if not facts["loaded"] and not args.check:
            if not args.profile:
                print(f"[x] nothing is serving at {args.url} and no profile was given.")
                print("    The saved profiles:")
                list_profiles()
                return 2
            if args.no_start:
                print(f"[x] nothing is serving at {args.url} and --no-start was given.")
                return 2
            start_instance(args.profile)
            if not wait_loaded(args.url, key, args.timeout):
                print("[x] the model did not load in time")
                print("    ", LOCAL / "Strata" / "runtime" / f"launch-{args.profile}.server.log")
                return 2
            facts = server_facts(args.url, key)

    quant = args.quant or facts.get("quant") or ""
    notes = args.notes
    if not notes:
        notes = f"engine {facts.get('engine') or '?'}, {facts.get('gpu') or '?'}, model {facts.get('model') or '?'}"
        if quant:
            notes += f", quant {quant}"

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[x] the report root does not exist: {root}")
        return 1
    if args.out:
        out_dir = Path(args.out).resolve()
    else:
        out_dir = root / "bench" / "results"
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"  model   : {facts.get('model') or '-'} {quant}".rstrip())
    print(f"  engine  : {facts.get('engine') or '-'}   gpu: {facts.get('gpu') or '-'}")
    print(f"  report  : {out_dir}")
    print()

    command = strata_cmd(bundle) + ["bench", "run"]
    if args.profile:
        command += ["--profile", args.profile]
    else:
        command += ["--url", args.url]
    command += ["--track", args.track, "--track-notes", notes]
    if quant:
        command += ["--quantization", quant]
    if args.out:
        command += ["--out", str(out_dir)]
    if not args.hash_model and "--skip-model-hash" not in extra:
        command += ["--skip-model-hash"]
    command += extra

    print("  $", " ".join(command[2:]))
    print()

    if args.check:
        print("[i] check only, nothing was started.")
        return 0

    env = dict(os.environ)
    env["STRATA_API_KEY"] = key
    try:
        code = subprocess.run(command, cwd=str(root), env=env).returncode
    except KeyboardInterrupt:
        code = 130

    print()
    print(f"exit code: {code}")
    if args.out:
        print(f"report: {out_dir}")
    else:
        reports = sorted((p for p in out_dir.glob("*standard*") if p.is_dir()),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if reports:
            print(f"report: {reports[0]}")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
