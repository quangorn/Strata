#!/usr/bin/env python3
"""Drive the disk tier through the traffic its read gates need, on a server this tool starts itself.

    python tools/phase2_disk_traffic.py CONFIG ARMS... [--port 8091] [--fill 4] [--tokens 100000] [--log out.jsonl]

Arms are the way `tools/ab_engine.py` takes them (`NAME:key=value,...`; `exe` replaces the engine, `xargs`
appends ';'-separated engine arguments), so two arms see the same traffic and only the flags differ. Each
arm starts its own server on a fresh engine log, and the pattern is the same order every time:

1. grow `fill` conversations to about `tokens` tokens each, one request per step (~8000 tokens of tool
   result per step). The RAM cache fills and gives conversations up: that is the eviction side;
2. ask for the first grown conversation again by its stored prefix — the tier's read, a disk hit or a RAM
   hit from the write queue;
3. continue it with one new tool result — an append to the entry the disk already holds;
4. send a conversation the tier has never held (~40000 tokens), so the prompt is read again and the
   refusal lines have a case;
5. ask for the first conversation a second time, after the append, so a read lands on a state below the
   newest K/V.

The prompts are built from this checkout's own files (a system prompt, a task, tool results of real file
text), so anyone with the checkout gets the identical requests and nothing personal is sent. The engine's
own log is what carries the numbers (`tools/phase2_disk_ab.py` reads it out): this script only records the
prompt size and the decode of each request so a report can name the traffic it ran.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ab_engine import wait_ready  # noqa: E402

SYSTEM = ("You are an autonomous coding agent working in the user's repository. You read files, search the "
          "code, edit files and run commands with the tools below. Work step by step, read before changing, "
          "keep the existing style, and explain briefly what you did.")
TASKS = (
    "Summarise what the disk tier of the conversation cache stores, file by file, and list the settings it "
    "reads. Do not change any file.",
    "Explain how the server waits for the engine to be ready, and where the QUIT path ends. Read the code "
    "first; do not invent line numbers.",
    "Describe what the launch presets keep and what the base config keeps, and name the rule that keeps them "
    "from disagreeing.",
    "List every flag that controls the conversation cache and the disk tier, with what each one defaults to.",
)
# the tool results: real text of this checkout, in a fixed order, so the traffic is reproducible
FILES = ("README.md", "serve/server.py", "src/program/generate.cpp", "src/core/conversation_disk.cpp",
         "docs/DETAILS.md", "include/strata/core/conversation_cache.hpp", "setup.py",
         "docs/CONVERSATION_DISK_STORE_PHASE2.md")
# the engine's context is 262144 tokens on this profile: stop growing well before the server refuses a request
MAX_PROMPT = 200000


def chunk(index: int, size: int) -> str:
    text = (ROOT / FILES[index % len(FILES)]).read_text(encoding="utf-8", errors="replace")
    return text[:size]


def post(port: int, msgs: list, max_new: int, seed: int) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps({"messages": msgs, "max_tokens": max_new, "temperature": 1.0, "top_p": 0.95,
                         "top_k": 20, "seed": seed}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # the server's own reason for a refused request belongs in the run's output
        print(f"HTTP {e.code}: {e.read()[:400].decode('utf-8', 'replace')}", flush=True)
        raise


def row(out, step: str, conv: int, r: dict) -> None:
    """The request's own numbers: the full prompt from `usage`, the prefilled part from `timings`."""
    u, t = r.get("usage", {}), r.get("timings", {})
    out.write(json.dumps({"step": step, "conv": conv, "prompt_tokens": u.get("prompt_tokens"),
                          "prefilled": t.get("prompt_n"), "decode": t.get("predicted_per_second")}) + "\n")
    out.flush()


def grow(port: int, conv: int, tokens: int, step: int, out, tag: str = "grow") -> list:
    """One conversation grown to `tokens` prompt tokens, one request per tool result.

    The size the loop tracks is `usage.prompt_tokens`, the whole prompt. `timings.prompt_n` counts only the
    tokens this request prefilled (the reused KV is not in it), so a loop that watches that number never
    reaches its target: measured on this PC, such a loop grew one conversation to 259 886 tokens and the
    server refused the next request with "request exceeds the context window".
    """
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASKS[conv % len(TASKS)]}]
    size, k = 0, 0
    while size < tokens:
        r = post(port, msgs, 48, 1234)
        row(out, tag, conv, r)
        size = r.get("usage", {}).get("prompt_tokens") or size
        if size > MAX_PROMPT:      # the context window is the limit; stop growing before the refusal
            return msgs
        msgs.append({"role": "assistant", "content": r["choices"][0]["message"]["content"]})
        msgs.append({"role": "user", "content": f"tool result (read_file): {chunk(k, step)}"})
        k += 1
    return msgs


def traffic(port: int, fill: int, tokens: int, step: int, out) -> None:
    grown = [grow(port, c, tokens, step, out) for c in range(fill)]
    first = grown[0]
    # 2. the stored prefix: the tier's read of a conversation the cache has given up
    r = post(port, first, 8, 1234)
    row(out, "read-back", 0, r)
    # 3. the append: one new tool result on the entry the disk already holds
    msgs = list(first)
    msgs.append({"role": "assistant", "content": r["choices"][0]["message"]["content"]})
    msgs.append({"role": "user", "content": f"tool result (grep): {chunk(1, step)}"})
    r = post(port, msgs, 48, 1234)
    row(out, "append", 0, r)
    # 4. a conversation the tier never held: the prompt is read again
    grow(port, fill + 9, tokens // 2, step, out, "fresh")
    # 5. the first conversation again, after the append: the read lands below the newest K/V
    r = post(port, msgs, 8, 1234)
    row(out, "read-after-append", 0, r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("arms", nargs="+")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--fill", type=int, default=4)
    ap.add_argument("--tokens", type=int, default=100000)
    ap.add_argument("--step", type=int, default=8000)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--log", default="phase2_disk_traffic.jsonl")
    a = ap.parse_args()
    base = json.loads(Path(a.config).read_text())
    arms = []
    for s in a.arms:
        name, _, kv = s.partition(":")
        arms.append((name, dict(x.split("=", 1) for x in kv.split(",") if x)))
    out = open(a.log, "a", encoding="utf-8")
    for rnd in range(a.rounds):
        for name, env in (arms if rnd % 2 == 0 else arms[::-1]):
            cfg = dict(base)
            e = dict(env)
            if "exe" in e:
                cfg["exe"] = e.pop("exe")
            if "xargs" in e:
                cfg["args"] = list(base["args"]) + e.pop("xargs").split(";")
            cfg["port"] = a.port
            cfg["log"] = str(Path(tempfile.gettempdir()) / f"phase2_traffic_{name}_r{rnd}.log")
            cpath = Path(tempfile.gettempdir()) / f"phase2_traffic_{name}.json"
            cpath.write_text(json.dumps(cfg, indent=1))
            proc = subprocess.Popen([sys.executable, str(ROOT / "serve" / "server.py"), "--engine", "strata",
                                     "--config", str(cpath), "--port", str(a.port)], cwd=str(ROOT),
                                     env=dict(os.environ, **e), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                t0 = time.time()
                wait_ready(a.port, proc)
                print(f"round {rnd} {name}: ready in {time.time() - t0:.0f} s, log {cfg['log']}", flush=True)
                traffic(a.port, a.fill, a.tokens, a.step, out)
            finally:
                proc.terminate()
                try:
                    proc.wait(60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                time.sleep(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
