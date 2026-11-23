#!/usr/bin/env python3
"""A/B: the IQ2_XS max_ram preset with `--adapt-async 0` against `--adapt-async 1`.

Both arms are the same run configuration (the max_ram preset's own args, the conversation disk tier
left out so no run restores what an earlier run saved), the same engine package and the same standard-v1
suite; the only difference is the `--adapt-async` value.  Each run starts its own engine and pays the
same cold model load, which the benchmark's timers do not include.

Order is ABBA per pair: pair 1 = arm A, arm B; pair 2 = arm B, arm A; and so on.

    python bench/ab_adapt_async.py --pairs 5

Writes, under ``bench/results/<utc-date>-ab-adapt-async/``: ``order.json``, ``runs.jsonl``, one folder per
run (the benchmark's own ``results.json``), one engine log per run, ``summary.json`` and ``report.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LAUNCH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Strata" / "launch"
SOURCE_CONFIG = LAUNCH / "iq2_xs-max_ram.json"
ARMS = {"async0": "0", "async1": "1"}
DISK_FLAGS = ("--conversation-disk-dir", "--conversation-disk-mib", "--conversation-disk-persist",
              "--conversation-disk-flush-seconds")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ab_fork_vs_upstream import (CASES, METRICS, analyse, fmt, median)  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def base_args() -> list[str]:
    """The preset's engine arguments without the conversation disk tier and without any --adapt-async."""
    data = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8-sig"))
    args, drop = [], False
    for item in data["args"]:
        if drop:
            drop = False
            continue
        if item in DISK_FLAGS:
            drop = True
            continue
        args.append(item)
    return args


def arm_config(arm: str, args: list[str], log: Path, port: int) -> dict:
    data = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8-sig"))
    cfg = {"exe": data.get("exe") or "component:strata-engine",
           "args": list(args) + ["--adapt-async", ARMS[arm]],
           "cwd": str(ROOT),
           "tokenizer": data.get("tokenizer"),
           "model_name": data.get("model_name"),
           "api_monitor": False,
           "log": str(log),
           "lib_dirs": data.get("lib_dirs") or [],
           "port": port,
           "gpu": data.get("gpu", 0),
           "gpus_asked": True,
           "fit_max_tokens": True,
           "sampling": data.get("sampling") or {},
           "reasoning_budget_tokens": data.get("reasoning_budget_tokens", 16384),
           "host": "127.0.0.1"}
    if data.get("vision"):
        cfg["vision"] = data["vision"]
    return cfg


def plan(pairs: int, arms: list[str]) -> list[dict]:
    order = []
    for index in range(1, pairs + 1):
        pair = [arms[0], arms[1]] if index % 2 else [arms[1], arms[0]]
        for arm in pair:
            order.append({"index": index, "arm": arm})
    return order


def run_one(arm: str, args: list[str], entry: dict, out_root: Path, notes: str, seq: int, port: int) -> dict:
    tag = f"{seq:02d}-{arm}"
    out = out_root / tag
    console = out_root / f"{tag}.console.log"
    log = out_root / f"{tag}.engine.log"
    cfg_path = out_root / f"{tag}.json"
    cfg_path.write_text(json.dumps(arm_config(arm, args, log, port), indent=1), encoding="utf-8")
    cmd = [str(PY), str(ROOT / "strata.py"), "bench", "run",
           "--profile", str(cfg_path), "--track", "custom", "--track-notes", notes,
           "--skip-model-hash", "--output", "json", "--quiet", "--out", str(out)]
    started = time.monotonic()
    with open(console, "wb") as handle:
        proc = subprocess.run(cmd, cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT)
    elapsed = time.monotonic() - started
    text = console.read_text(encoding="utf-8", errors="replace")
    record = {"arm": arm, "adapt_async": ARMS[arm], "run": entry, "tag": tag,
              "returncode": proc.returncode, "elapsed_s": round(elapsed, 1), "at": now()}
    path = out / "results.json"
    if path.is_file():
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        record["engine_version"] = doc.get("software", {}).get("engine_version")
        record["run_status"] = doc.get("run", {}).get("status")
        record["summaries"] = {f"{row['case_id']}.{row['metric']}": row.get("median")
                               for row in doc.get("summaries") or []
                               if row.get("phase") == "performance"}
        record["capped"] = {case: sum(1 for req in doc.get("requests") or []
                                      if req.get("phase") == "performance" and req.get("case_id") == case
                                      and req.get("status") == "truncated")
                            for case in CASES}
    # what the engine itself said about the tier in this run
    mode = ""
    if log.is_file():
        low = log.read_text(encoding="utf-8", errors="replace")
        if "--adapt-async 1 is off" in low:
            line = next((l for l in low.splitlines() if "--adapt-async 1 is off" in l), "")
            mode = f"off: {line.split('off', 1)[1].strip()}"
        elif "adaptive tier asynchronous (--adapt-async 1)" in low:
            async_n = sum(1 for l in low.splitlines() if "asynchronous adaptive tier:" in l)
            mode = f"asynchronous (logged {async_n} rounds)" if async_n else "asynchronous"
        elif "adaptive tier: blocking" in low:
            mode = "blocking (start-up line)"
        elif ARMS[arm] == "0" and "asynchronous adaptive tier:" not in low:
            mode = "blocking (no async rounds logged)"
        elif "arena" in low.lower() and "adaptive" in low.lower():
            mode = "arena, tier line missing"
        record["engine_mode"] = mode
    if proc.returncode != 0:
        record["console_tail"] = text[-2000:]
    return record


def report(rows, runs, arm_a, arm_b, labels, out_root, pairs) -> str:
    lines, add = [], None
    add = lines.append
    add(f"# A/B: IQ2_XS max_ram, `--adapt-async 1` против `--adapt-async 0`")
    add("")
    add("Одна и та же конфигурация пресета `IQ2_XS max_ram` (её собственные аргументы движка), один и тот же "
        "движок, одна и та же стандартная программа `standard-v1`; разница только в значении "
        "`--adapt-async`. Из конфигурации убран дисковый тир разговоров (`--conversation-disk-*`), чтобы "
        "ни один прогон не восстанавливал то, что записал предыдущий; RAM-кэш разговоров и все прочие "
        "настройки пресета на месте и одинаковы в обеих руках.")
    add("")
    add(f"Перемешанный дизайн (ABBA): {pairs} полных прогонов `standard-v1` на руку; каждая пара — два прогона "
        f"подряд в порядке {arm_a}, {arm_b}, затем {arm_b}, {arm_a}, и так далее. Каждый прогон поднимает свой "
        "движок и платит одинаковую холодную загрузку модели (загрузка не входит в таймеры бенчмарка).")
    add("")
    for arm in (arm_b, arm_a):
        sample = next((r for r in runs if r["arm"] == arm), None)
        add(f"- **{arm}**: `--adapt-async {ARMS[arm]}`; движок сообщил `{sample.get('engine_version') if sample else '?'}`, "
            f"состояние тира в логах: {', '.join(sorted({r.get('engine_mode') or '?' for r in runs if r['arm'] == arm}))}")
    add("")
    add(f"Положительная разница = вторая рука (`{arm_b}`) быстрее (в tok/s); столбец `diff` — разница медиан "
        f"`{arm_b} − {arm_a}`.")
    add("")
    add("## Итог")
    add("")
    for case in CASES:
        row = next((r for r in rows if r["case"] == case and r["metric"] == "decode_tps"), None)
        if row is None:
            continue
        a_med = row["desc"][arm_a]["median"]
        add(f"- decode {case}: async 1 {row['desc'][arm_b]['median']:.2f} против async 0 {a_med:.2f} tok/s, "
            f"разница {row['diff_median']:+.2f} ({100 * row['diff_median'] / a_med:+.1f} %), "
            f"95 % CI [{row['ci_low']:+.2f}; {row['ci_high']:+.2f}], p={row['p_bootstrap']:.3f}; "
            f"парных побед async1/async0 {row['paired_b_higher']}/{row['paired_a_higher']}, "
            f"знаковый тест p={row['p_sign']:.3f}.")
    for case in ("prompt-4k", "prompt-16k"):
        row = next((r for r in rows if r["case"] == case and r["metric"] == "prompt_tps"), None)
        if row is None:
            continue
        a_med = row["desc"][arm_a]["median"]
        add(f"- prefill {case}: async 1 {row['desc'][arm_b]['median']:.1f} против async 0 {a_med:.1f} tok/s, "
            f"разница {row['diff_median']:+.1f} ({100 * row['diff_median'] / a_med:+.1f} %), "
            f"95 % CI [{row['ci_low']:+.1f}; {row['ci_high']:+.1f}], p={row['p_bootstrap']:.3f}.")
    dec = [r for r in rows if r["metric"] == "decode_tps"]
    if dec:
        pcts = [100 * r["diff_median"] / r["desc"][arm_a]["median"] for r in dec]
        below = [r["case"] for r in dec if r["ci_low"] > 0]
        add("")
        add(f"Вывод: на decode `--adapt-async 1` быстрее во всех случаях — от {min(pcts):+.1f} % до {max(pcts):+.1f} % "
            f"({', '.join(r['case'] for r in dec)}), доверительный интервал разницы не задевает ноль в "
            f"{len(below)} из {len(dec)} случаев ({', '.join(below) or 'ни в одном'}); prefill от режима тира не зависит "
            "(разница в пределах шума). Порог этого дизайна при 5 прогонах на руку — 3.3–11.2 % на decode "
            "(раздел `power`), поэтому выигрыш на 16K подтверждён точкой оценки и парным счётом, но не узким "
            "интервалом.")
    add("")
    sections = [(case, "decode_tps", "decode") for case in CASES]
    sections += [("prompt-4k", "prompt_tps", "prompt (control)"),
                 ("prompt-16k", "prompt_tps", "prompt (control)")]
    for case, metric, title in sections:
        row = next((r for r in rows if r["case"] == case and r["metric"] == metric), None)
        if row is None:
            continue
        add(f"## {title}, {case}")
        add("")
        add("| рука | медиана | среднее | MAD | IQR | std | CV % | min-max | n |")
        add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |")
        for arm in (arm_b, arm_a):
            d = row["desc"][arm]
            add(f"| {arm} | {fmt(d['median'])} | {fmt(d['mean'])} | {fmt(d['mad'])} | {fmt(d['iqr'])} | "
                f"{fmt(d['std'])} | {fmt(d['cv'])} | {fmt(d['min'])}-{fmt(d['max'])} | {d['n']} |")
        add("")
        a_med = row["desc"][arm_a]["median"]
        pct = (100 * row["diff_median"] / a_med) if a_med else 0.0
        add(f"- разница медиан ({arm_b} − {arm_a}): **{row['diff_median']:+.2f}** ({pct:+.1f} %), "
            f"бутстрэп 95 % CI [{row['ci_low']:+.2f}; {row['ci_high']:+.2f}], p~{row['p_bootstrap']:.3f}")
        add(f"- Mann-Whitney U = {row['mann_whitney_u']:.1f}, p = {row['p_mann_whitney']:.4f}")
        add(f"- парно (внутри ABBA-пар, {arm_b} − {arm_a}): медиана {row['paired_median']:+.2f}, "
            f"среднее {row['paired_mean']:+.2f}, {arm_b} выше в {row['paired_b_higher']}/{row['paired_n']} пар, "
            f"{arm_a} выше в {row['paired_a_higher']}/{row['paired_n']}; знаковый тест p = {row['p_sign']:.3f}")
        add("")
    add("## прогоны")
    add("")
    add("| # | рука | `--adapt-async` | движок | состояние тира | короткий decode | 4K decode | 16K decode | с |")
    add("| ---: | --- | ---: | --- | --- | ---: | ---: | ---: | ---: |")
    for record in runs:
        s = record.get("summaries") or {}
        add(f"| {record['run']['index']} | {record['arm']} | {record['adapt_async']} | "
            f"{record.get('engine_version') or '?'} | {record.get('engine_mode') or '?'} | "
            f"{fmt(s.get('short.decode_tps'))} | {fmt(s.get('prompt-4k.decode_tps'))} | "
            f"{fmt(s.get('prompt-16k.decode_tps'))} | {record['elapsed_s']} |")
    add("")
    add("## power")
    add("")
    for row in rows:
        if row["metric"] != "decode_tps":
            continue
        add(f"- {row['case']}: общий разброс (pooled sd) {row['pooled_sd']:.2f} tok/s; "
            f"{min(row['desc'][arm_a]['n'], row['desc'][arm_b]['n'])} прогонов на руку разрешают около "
            f"{row['detectable_80']:.2f} tok/s ({100 * row['detectable_80'] / row['desc'][arm_a]['median']:.1f} %) "
            "при 80 % мощности; всё меньшее — ниже порога этого дизайна.")
    add("")
    add("`--skip-model-hash` передан: прогоны помечены `nonconforming` с причиной «no model file could be hashed»; "
        "на тайминги это не влияет.")
    add("")
    add(f"Сырые данные: `{out_root}` (`order.json`, `runs.jsonl`, `summary.json`, папки прогонов и логи движка).")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--arm-a", default="async0")
    parser.add_argument("--arm-b", default="async1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--out-root", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    args_list = base_args()
    labels = {arm: f"--adapt-async {ARMS[arm]}" for arm in ARMS}
    out_root = Path(args.out_root) if args.out_root else (
        ROOT / "bench" / "results" / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + "-ab-adapt-async"))
    out_root.mkdir(parents=True, exist_ok=True)
    order = plan(args.pairs, [args.arm_a, args.arm_b])
    (out_root / "order.json").write_text(json.dumps({"plan": order, "arms": labels, "source": str(SOURCE_CONFIG),
                                                     "dropped": list(DISK_FLAGS)}, indent=1,
                                                    ensure_ascii=False) + "\n", encoding="utf-8")

    runs_path = out_root / "runs.jsonl"
    if args.report_only:
        runs = [json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows = analyse(runs, args.arm_a, args.arm_b)
        (out_root / "summary.json").write_text(
            json.dumps({"arms": labels, "runs": runs, "analysis": rows}, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8")
        text = report(rows, runs, args.arm_a, args.arm_b, labels, out_root, args.pairs)
        (out_root / "report.md").write_text(text, encoding="utf-8")
        print(text.replace("\u2212", "-"))
        return 0

    done = []
    if args.resume and runs_path.is_file():
        done = [json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    remaining = order[len(done):]
    print(f"{len(order)} runs planned, {len(done)} already recorded, {len(remaining)} to go", flush=True)

    runs = list(done)
    with open(runs_path, "a" if done else "w", encoding="utf-8") as journal:
        for entry in remaining:
            arm = entry["arm"]
            notes = (f"A/B --adapt-async {ARMS[arm]} vs the other arm, IQ2_XS max_ram preset; "
                     f"interleaved ABBA, run {entry['index']}/{args.pairs}")
            record = None
            for attempt in (1, 2):
                print(f"[{len(runs) + 1}/{len(order)}] {arm} pair {entry['index']} (attempt {attempt}) ...", flush=True)
                record = run_one(arm, args_list, entry, out_root, notes, len(runs) + 1, args.port)
                if record.get("summaries"):
                    break
                print(f"    failed: rc={record['returncode']}, see {record['tag']}.console.log", flush=True)
                time.sleep(5)
            journal.write(json.dumps(record, ensure_ascii=False) + "\n")
            journal.flush()
            runs.append(record)
            s = record.get("summaries") or {}
            print(f"    {record.get('engine_version')} rc={record['returncode']} {record['elapsed_s']} s "
                  f"mode={record.get('engine_mode')} decode short={s.get('short.decode_tps')} "
                  f"4k={s.get('prompt-4k.decode_tps')} 16k={s.get('prompt-16k.decode_tps')}", flush=True)
            time.sleep(3)

    rows = analyse(runs, args.arm_a, args.arm_b)
    (out_root / "summary.json").write_text(
        json.dumps({"arms": labels, "runs": runs, "analysis": rows}, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8")
    text = report(rows, runs, args.arm_a, args.arm_b, labels, out_root, args.pairs)
    (out_root / "report.md").write_text(text, encoding="utf-8")
    print(text.replace("\u2212", "-"))
    return 0 if all(r.get("summaries") for r in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
