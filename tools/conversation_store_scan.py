#!/usr/bin/env python3
"""Read the store folder's `.idx` files and report what is on the disk.

    python tools/conversation_store_scan.py DIR [--csv out.csv]

The same check the plan uses (CONVERSATION_DISK_STORE_MEASUREMENTS.md, "The prefix check"): for every
entry, the longest token prefix it shares with any other entry, times its own bytes per token. That is
the bytes a store with one copy per prefix would not write. The reader takes nothing from the engine: it
parses the v2 index the tier publishes (`src/core/conversation_disk.cpp:196-218`), and every number it
reports is read off the files, with the folder and the time named.

What comes out:
- entries, MiB split K/V blocks against state blocks, tokens across entries, density;
- the duplicated prefix, in MiB, as a share of the K/V and of the store, the biggest pair, the small
  shared prefixes (the system prompt), and entries fully inside another entry;
- states per conversation (the window question), pinned states, the LRU stamps.
"""
from __future__ import annotations

import argparse
import csv
import os
import struct
import sys
from pathlib import Path

import numpy as np

MAGIC = 0x3258444943525453          # "STRCIDX2", native-endian, as conversation_disk.cpp:132
KV, STATE = 1, 2
GEOMETRY = 18
MIB = 1 << 20


def parse_index(buf: bytes, name: str):
    """The v2 index bytes, in the writer's order. Returns None for a file that is not one."""
    pos = 0
    def get(fmt):
        nonlocal pos
        v = struct.unpack_from(fmt, buf, pos)[0]
        pos += struct.calcsize(fmt)
        return v
    if len(buf) < 8 or get("<Q") != MAGIC:
        return None
    model, config = get("<Q"), get("<Q")
    written, last_used = get("<q"), get("<q")
    cvec = get("<B")
    entry_bytes = get("<Q")
    pos += 8 * GEOMETRY                                   # the fixed geometry array
    tokens = get("<q")

    def key():
        n = get("<I")
        ids = np.frombuffer(buf, dtype="<i4", count=n, offset=pos)
        pos_after = pos + 4 * n
        m = struct.unpack_from("<I", buf, pos_after)[0]
        return ids, pos_after + 4 + 16 * m

    live_ids, pos = key()
    out = {"name": name, "model": model, "config": config, "written_s": written,
           "last_used_s": last_used, "cvec": cvec, "bytes": entry_bytes, "tokens": tokens,
           "ids": live_ids, "states": 0, "kv_mib": 0.0, "state_mib": 0.0, "pinned": 0, "bins": []}
    nck = get("<I")
    for _ in range(nck):
        _, pos = key()
    nstages = get("<I")
    for _ in range(nstages):
        n = get("<I")
        fname = buf[pos:pos + n].decode("utf-8", "replace")
        pos += n
        pos += 20                                          # layer_lo, layer_hi, layers
        nblocks = get("<I")
        out["bins"].append(fname)
        for _ in range(nblocks):
            get("<Q")                                      # offset: the bytes are not read here
            size = get("<Q")
            get("<Q"); get("<Q")                           # hash_lo, hash_hi: not needed here
            kind = get("<I")
            get("<q"); get("<q")                           # tokens_start, tokens_end
            get("<Q"); get("<Q")                           # prefix_digest, used
            pinned = get("<B")
            if kind == KV:
                out["kv_mib"] += size / MIB
            elif kind == STATE:
                out["state_mib"] += size / MIB
                out["states"] += 1
                out["pinned"] += 1 if pinned else 0
    return out


def prefix_len(a: np.ndarray, b: np.ndarray) -> int:
    """The longest token prefix the two key sequences share, as the engine's best() sees it."""
    n = min(len(a), len(b))
    if n == 0:
        return 0
    eq = a[:n] == b[:n]
    first = int(np.argmax(~eq))
    return n if eq[first] else first


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--csv")
    a = ap.parse_args()
    folder = Path(a.dir)
    entries = []
    for p in sorted(folder.glob("*.idx")):
        e = parse_index(p.read_bytes(), p.name)
        if e is None:
            print(f"{p.name}: not a v2 index", file=sys.stderr)
            continue
        e["index_mib"] = p.stat().st_size / MIB
        entries.append(e)

    bin_mib = 0.0
    for p in folder.glob("*.bin"):
        bin_mib += p.stat().st_size / MIB
    total_mib = bin_mib + sum(e["index_mib"] for e in entries)

    print(f"{folder}: {len(entries)} entries, {total_mib:.0f} MiB "
          f"({bin_mib:.0f} MiB .bin, {sum(e['index_mib'] for e in entries):.0f} MiB index)")
    kv = sum(e["kv_mib"] for e in entries)
    st = sum(e["state_mib"] for e in entries)
    tokens = sum(e["tokens"] for e in entries)
    print(f"K/V blocks {kv:.0f} MiB, state blocks {st:.0f} MiB, {tokens} tokens across entries")

    # the prefix check: per entry, the longest shared prefix with any other entry, times its own bytes per token
    dup = 0.0
    best_pair = (0, 0.0, "", "")
    inside = 0
    shared_small = []
    for i, e in enumerate(entries):
        if len(e["ids"]) == 0 or e["tokens"] == 0:
            continue
        per = e["bytes"] / e["tokens"]
        longest = 0
        for j, f in enumerate(entries):
            if i == j:
                continue
            L = prefix_len(e["ids"], f["ids"])
            if L > longest:
                longest = L
                if L == e["tokens"]:
                    inside += 1
        dup += longest * per / MIB
        if longest > best_pair[0]:
            best_pair = (longest, longest * per / MIB, e["name"], f["name"])
        if 0 < longest < 20000:
            shared_small.append(longest)
    print(f"duplicated prefix: {dup:.0f} MiB = {100.0 * dup / kv:.1f}% of the K/V, "
          f"{100.0 * dup / total_mib:.1f}% of the store; fully inside another entry: {inside}")
    print(f"biggest shared pair: {best_pair[0]} tokens ({best_pair[1]:.1f} MiB), "
          f"{best_pair[2]} against {best_pair[3]}")
    if shared_small:
        print("the small shared prefixes (the system prompt, entries with a share under 20 000 tokens): "
              f"{sorted(set(shared_small))}")
    print("states per conversation: " + ", ".join(f"{e['states']}" for e in entries))
    print(f"pinned state blocks: {sum(e['pinned'] for e in entries)}")

    if a.csv:
        fields = ["name", "tokens", "bytes_mib", "kv_mib", "state_mib", "states", "pinned",
                  "written_s", "last_used_s"]
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(fields)
            for e in entries:
                w.writerow([e["name"], e["tokens"], round(e["bytes"] / MIB, 1),
                            round(e["kv_mib"], 1), round(e["state_mib"], 1), e["states"],
                            e["pinned"], e["written_s"], e["last_used_s"]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
