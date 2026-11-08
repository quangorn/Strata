#!/usr/bin/env python3
"""Summarise a disk tier run from the engine's own log lines: the phase 2 validation numbers.

    python tools/phase2_disk_ab.py LOG... [--csv out.csv]

Every number comes from a line the engine prints, so a run needs no instrumentation: one log file can hold
several runs, split at each `engine started:` line, and each run is reported on its own with the flags it
started with (`--conversation-disk-flush-seconds`, the folder, the budget). What the phase 2 plan asks for
(`docs/CONVERSATION_DISK_STORE_PHASE2.md`, "Validation on hardware") is here:

- the shape of every write: `fill write` (a new file or fewer than seven states: the window is taken),
  `append write` (one end state past a file that already holds seven) and `idle flush write` (a debounced
  park's end state), with the older logs' `forced write, the whole image` and `debounced write, ...`
  understood too, the rule-3 `skipped N tokens: ...` lines counted as writes that wrote nothing, and the
  bytes split K/V, states, index;
- tokens past the deepest stored state, which is the number that decides "one state per flush" against
  "the whole chain";
- writes, RAM hits, disk hits, tokens given back, waits for RAM, budget removals, failures — the tier's
  own `totals:` line;
- the QUIT save (`saved for the next start in N s`) and every `waited N ms for N queued writes`;
- the park's own duration (median, p95), which the plan gates the shared pointer on;
- reads the tier refused (`N tokens not restored`) and the "not read" heuristic.

A `--csv` file gets one row per run, for an A/B table. Nothing here is measured by this tool: it only reads
what a run wrote to its log, and a report of any number must say which run and which PC produced it.
"""
from __future__ import annotations

import argparse
import csv
import re
import statistics
import sys
from pathlib import Path

RUN_START = re.compile(r"engine started: (\S+\.exe)(.*)$")
TOTALS = re.compile(
    r"conversation disk: totals: (\d+) queued, (\d+) written \((\d+) MiB, (\d+) MB/s\), (\d+) RAM hits, "
    r"(\d+) disk hits \((\d+) MiB, (\d+) MB/s\), (\d+) prompt tokens given back, (\d+) waits for RAM \((\d+) ms\), "
    r"(\d+) removed for the budget, (\d+)/(\d+) writes/reads failed, (\d+) reads fell back on damage, "
    r"(\d+) dropped on RAM refusals; on disk (\d+) \((\d+) MiB\), queue (\d+)")
WRITE = re.compile(
    r"wrote (\d+) tokens( \(appended\))?: ([\d.]+) MiB appended in (\d+) blocks \((\d+) new kv, ([\d.]+) MiB; "
    r"(\d+) new state, ([\d.]+) MiB\), index ([\d.]+) MiB rewritten, (\d+) file")
SHAPE = re.compile(r"; (forced|debounced|fill|append|idle flush) write, ([^;]+); (\d+) tokens"
                   r"(?: past the deepest stored state)?(?:, the entry held no state before)?")
# rule 3 of docs/CONVERSATION_DISK_STATE_WINDOW.md: a write under the window writes nothing at all
SKIP = re.compile(r"skipped (\d+) tokens: (\d+) tokens past the newest stored state and --prompt-cache-every is (\d+)")
PARK = re.compile(r"conversation cache: parked (\d+) tokens in ([\d.]+) ms")
QUIT = re.compile(r"conversation disk: saved for the next start in ([\d.]+) s")
WAITED = re.compile(r"waited (\d+) ms for (\d+) queued write")
HIT = re.compile(r"disk hit: (\d+) tokens, ([\d.]+) MiB in (\d+) file, (\d+) ms \((\d+) MB/s\)")
NOT_READ = re.compile(r"conversation disk: not read: its (\d+) more tokens")
NOT_RESTORED = re.compile(r"conversation disk: (\d+) tokens not restored \(([^)]*)\)")
# the request's own reuse line: how much of the prompt came back and how much was read again
REUSE = re.compile(r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in [\d.]+ ms \(([\d.]+) tok/s\),"
                   r" (\d+) generated in [\d.]+ ms \(([\d.]+) tok/s\)")
RESTORED = re.compile(r"conversation cache: restored (\d+) tokens \((live|disk)\) in ([\d.]+) ms")


def flags(args_line: str) -> dict:
    """`--conversation-disk-*` and the flush flag as the engine's command line has them."""
    out, want = {}, ("conversation-disk-flush-seconds", "conversation-disk-dir",
                     "conversation-disk-mib", "conversation-disk-persist")
    parts = args_line.split()
    for i, p in enumerate(parts):
        key = p.lstrip("--")
        if key in want and i + 1 < len(parts):
            out[key] = parts[i + 1]
    return out


def percentile(values, q):
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(q * (len(s) - 1)))]


class Run:
    def __init__(self, exe: str, args_line: str, started: str):
        # the log prints a Windows path (`C:\...\0.1.55.13-...cuda13\strata.exe`): normalise it so the package
        # folder name comes out on any Python
        self.exe, self.flags, self.started = exe.replace("\\", "/"), flags(args_line), started
        self.queues = self.writes = 0
        self.kv_mib = self.state_mib = self.index_mib = 0.0
        self.appended = self.forced = self.debounced = self.states = self.appended_states = 0
        self.skipped = 0        # rule 3: writes that found the end under the window and wrote nothing
        self.skip_past = []     # tokens past the newest stored state on those skipped writes
        self.new_states = 0     # new state blocks all writes added, the fill's window included
        self.past = []          # tokens past the deepest stored state, per write
        self.totals = None      # the last `totals:` line of the run
        self.park_ms = []
        self.quit_s = None
        self.waited = []        # (ms, jobs) for every queued-write wait
        self.hits = 0
        self.hit_tokens = self.hit_mib = 0
        self.not_read = []
        self.not_restored = []
        self.reuse = []         # (prompt, reused, read again) for every request the engine reported
        self.restored = []      # (tokens, "live" or "disk", ms) for every restore

    def add_write(self, line: str):
        m = WRITE.search(line)
        if not m:
            return
        self.writes += 1
        self.appended += 1 if "(appended)" in line else 0
        self.kv_mib += float(m.group(6))
        self.new_states += int(m.group(7))
        self.state_mib += float(m.group(8))
        self.index_mib += float(m.group(9))
        s = SHAPE.search(line)
        if s:
            what = s.group(1)
            if what in ("forced", "fill"):        # the whole image (an older log) / the fill branch's window
                self.forced += 1
            elif what in ("debounced", "idle flush"):
                self.debounced += 1
            else:                                  # "append": one end state past a file holding the window
                self.appended_states += 1
            self.states += 1 if "end state written" in s.group(2) else 0
            self.past.append(int(s.group(3)))

    def add_skip(self, line: str):
        m = SKIP.search(line)
        if not m:
            return
        self.skipped += 1
        self.skip_past.append(int(m.group(2)))

    def report(self) -> list[str]:
        f = self.flags
        out = [f"run started {self.started}  engine {Path(self.exe).parent.name}"
               f"  flush {f.get('conversation-disk-flush-seconds', 'absent = 0')} s"
               f"  dir {f.get('conversation-disk-dir', '-')}  budget {f.get('conversation-disk-mib', '-')} MiB"
               f"  persist {f.get('conversation-disk-persist', '-')}"]
        out.append(f"  writes {self.writes} ({self.appended} appended): {self.kv_mib:.0f} MiB K/V, "
                   f"{self.state_mib:.0f} MiB states, {self.index_mib:.0f} MiB index; shapes: "
                   f"{self.forced} fill (or an older log's whole image), {self.appended_states} append, "
                   f"{self.debounced} idle flush (or an older log's debounced), {self.states} wrote an end state, "
                   f"{self.new_states} new state blocks, {self.skipped} skipped under the window")
        if self.skip_past:
            out.append(f"  tokens past the newest stored state on the skipped writes: "
                       f"median {int(statistics.median(self.skip_past))}, "
                       f"p95 {int(percentile(self.skip_past, 0.95))}, max {max(self.skip_past)} over "
                       f"{len(self.skip_past)} skips")
        if self.past:
            out.append(f"  tokens past the deepest stored state: median {int(statistics.median(self.past))}, "
                       f"p95 {int(percentile(self.past, 0.95))}, max {max(self.past)} over {len(self.past)} writes")
        if self.park_ms:
            out.append(f"  park duration over {len(self.park_ms)} parks: median {statistics.median(self.park_ms):.1f} ms, "
                       f"p95 {percentile(self.park_ms, 0.95):.1f} ms, max {max(self.park_ms):.1f} ms")
        if self.quit_s is not None:
            out.append(f"  QUIT save: {self.quit_s:.1f} s")
        if self.waited:
            out.append(f"  waits for queued writes: {len(self.waited)}, "
                       f"{sum(ms for ms, _ in self.waited):.0f} ms total, max {max(ms for ms, _ in self.waited):.0f} ms")
        t = self.totals
        if t:
            out.append(f"  totals snapshot (the last line the tier printed, not the run's end): queued {t['queued']}, "
                       f"written {t['written']} ({t['written_mib']} MiB), RAM hits {t['ram_hits']}, "
                       f"disk hits {t['disk_hits']} ({t['disk_mib']} MiB), {t['given_back']} tokens given back, "
                       f"waits for RAM {t['waits']} ({t['wait_ms']} ms), {t['removed']} removed for the budget, "
                       f"{t['failed']} writes/reads failed, {t['damage']} reads fell back on damage, "
                       f"{t['ram_drops']} dropped on RAM refusals, on disk {t['on_disk']} ({t['disk_budget_mib']} MiB)")
        out.append(f"  reads: {self.hits} disk hits ({self.hit_mib:.0f} MiB, {self.hit_tokens} tokens), "
                   f"{len(self.not_read)} 'not read' refusals ({sum(self.not_read)} tokens), "
                   f"{len(self.not_restored)} 'not restored' ({sum(n for n, _ in self.not_restored)} tokens)")
        if self.reuse:
            out.append(f"  reuse over {len(self.reuse)} requests: {sum(r[1] for r in self.reuse)} tokens reused, "
                       f"{sum(r[2] for r in self.reuse)} read again, {sum(1 for r in self.reuse if r[1] > 0)} "
                       f"requests got a reuse")
        if self.restored:
            out.append(f"  restores: {len(self.restored)} ({sum(1 for s in self.restored if s[1] == 'disk')} from "
                       f"the disk, {sum(1 for s in self.restored if s[1] == 'live')} live), "
                       f"{sum(s[2] for s in self.restored):.1f} ms")
        return out

    def csv_row(self) -> dict:
        t = self.totals or {}
        return {"started": self.started, "engine": Path(self.exe).parent.name,
                "flush_s": self.flags.get("conversation-disk-flush-seconds", "0"),
                "writes": self.writes, "appended": self.appended, "forced": self.forced,
                "debounced": self.debounced, "wrote_state": self.states,
                "fill": self.forced, "append": self.appended_states, "idle": self.debounced,
                "skipped": self.skipped, "new_states": self.new_states,
                "kv_mib": round(self.kv_mib), "state_mib": round(self.state_mib),
                "past_median": int(statistics.median(self.past)) if self.past else 0,
                "park_median_ms": round(statistics.median(self.park_ms), 1) if self.park_ms else 0,
                "park_p95_ms": round(percentile(self.park_ms, 0.95), 1) if self.park_ms else 0,
                "quit_s": round(self.quit_s, 1) if self.quit_s is not None else "",
                "queue_waits_ms": sum(ms for ms, _ in self.waited),
                "disk_hits": self.hits, "disk_mib": round(self.hit_mib),
                "not_read": len(self.not_read), "not_restored": len(self.not_restored),
                "reused": sum(r[1] for r in self.reuse), "read_again": sum(r[2] for r in self.reuse),
                "restore_disk": sum(1 for s in self.restored if s[1] == "disk"),
                "restore_live": sum(1 for s in self.restored if s[1] == "live"),
                "ram_waits": t.get("waits", 0), "ram_wait_ms": t.get("wait_ms", 0),
                "removed": t.get("removed", 0)}


def parse(text: str) -> list[Run]:
    runs: list[Run] = []
    started, exe, args_line = None, "", ""
    for line in text.splitlines():
        m = RUN_START.search(line)
        if m:
            stamp = (re.search(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line) or [None, ""])[1]
            runs.append(Run(m.group(1), m.group(2), stamp))
            continue
        if not runs:
            continue
        r = runs[-1]
        if "wrote " in line and " MiB appended" in line:
            r.add_write(line)
        elif "skipped " in line and " tokens past the newest stored state" in line:
            r.add_skip(line)
        elif "conversation disk: totals:" in line:
            t = TOTALS.search(line)
            if t:
                # the snapshot the tier printed, not the run's end: a run that is still going has no final line
                r.totals = {"queued": int(t.group(1)), "written": int(t.group(2)), "written_mib": int(t.group(3)),
                            "ram_hits": int(t.group(5)), "disk_hits": int(t.group(6)), "disk_mib": int(t.group(7)),
                            "given_back": int(t.group(9)), "waits": int(t.group(10)), "wait_ms": int(t.group(11)),
                            "removed": int(t.group(12)), "failed": f"{t.group(13)}/{t.group(14)}",
                            "damage": int(t.group(15)), "ram_drops": int(t.group(16)),
                            "on_disk": int(t.group(17)), "disk_budget_mib": int(t.group(18))}
        elif "conversation cache: parked " in line:
            p = PARK.search(line)
            if p:
                r.park_ms.append(float(p.group(2)))
        elif "saved for the next start in" in line:
            q = QUIT.search(line)
            if q:
                r.quit_s = float(q.group(1))
        elif "ms for" in line and "queued write" in line:
            w = WAITED.search(line)
            if w:
                r.waited.append((float(w.group(1)), int(w.group(2))))
        elif "disk hit:" in line:
            h = HIT.search(line)
            if h:
                r.hits += 1
                r.hit_tokens += int(h.group(1))
                r.hit_mib += float(h.group(2))
        elif "not read: its" in line:
            n = NOT_READ.search(line)
            if n:
                r.not_read.append(int(n.group(1)))
        elif "tokens not restored" in line:
            n = NOT_RESTORED.search(line)
            if n:
                r.not_restored.append((int(n.group(1)), n.group(2)))
        elif "prompt " in line and " reused + " in line:
            u = REUSE.search(line)
            if u:
                r.reuse.append((int(u.group(1)), int(u.group(2)), int(u.group(3))))
        elif "conversation cache: restored " in line:
            s = RESTORED.search(line)
            if s:
                r.restored.append((int(s.group(1)), s.group(2), float(s.group(3))))
    return runs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--csv")
    a = ap.parse_args()
    runs: list[Run] = []
    for log in a.logs:
        p = Path(log)
        if not p.is_file():
            print(f"{p}: not there", file=sys.stderr)
            continue
        runs += parse(p.read_text(encoding="utf-8", errors="replace"))
    for r in runs:
        for line in r.report():
            print(line)
        print()
    if a.csv:
        fields = list(Run("x", "", "").csv_row())
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fields)
            w.writeheader()
            for r in runs:
                w.writerow(r.csv_row())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
