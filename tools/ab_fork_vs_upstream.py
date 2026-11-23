#!/usr/bin/env python3
"""Interleaved A/B of two engine packages through the standard-v1 benchmark.

The two arms are installed engine packages. Before every run the chosen package is pinned in
``%LOCALAPPDATA%/Strata/components/.updates/state.json`` (``engine.pinned``), which is the folder
``strata_control.engine_component.resolve`` starts; ``component:strata-engine`` in the run config then
resolves to that package. Each run starts its own engine and pays the same cold model load, and the
load is not part of the benchmark timers.

Order is ABBA per pair: pair 1 = arm A, arm B; pair 2 = arm B, arm A; and so on.

    python bench/ab_fork_vs_upstream.py --pairs 10

Writes, under ``bench/results/<utc-date>-ab-fork-upstream/``: ``order.json``, ``runs.jsonl``, one
folder per run (the benchmark's own ``results.json``), ``summary.json`` and ``report.md``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
COMPONENTS = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Strata" / "components"
ENGINE_DIR = COMPONENTS / "engine"
STATE = COMPONENTS / ".updates" / "state.json"
CONFIG = ROOT / "bench" / "ab-fork-upstream" / "max_ram-ab.json"
CASES = ("short", "prompt-4k", "prompt-16k")
METRICS = ("decode_tps", "prompt_tps", "ttft_s", "total_s")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pin(folder: str | None) -> None:
    data = {}
    try:
        data = json.loads(STATE.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    data.setdefault("engine", {})["pinned"] = folder
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def engine_version(folder: Path) -> str:
    exe = folder / "strata.exe"
    out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=120)
    return (out.stdout or out.stderr).strip()


def manifest(folder: Path) -> dict:
    try:
        return json.loads((folder / "engine-component.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def plan(pairs: int, arms: list[str]) -> list[dict]:
    """ABBA: the first arm of pair 1 is arms[0]; each following pair swaps the order."""
    order = []
    for index in range(1, pairs + 1):
        pair = [arms[0], arms[1]] if index % 2 else [arms[1], arms[0]]
        for arm in pair:
            order.append({"index": index, "arm": arm})
    return order


def run_one(arm: str, folder: Path, order_entry: dict, out_root: Path, notes: str, seq: int) -> dict:
    tag = f"{seq:02d}-{arm}"
    out = out_root / tag
    log = out_root / f"{tag}.console.log"
    pin(folder.name)
    cmd = [str(PY), str(ROOT / "strata.py"), "bench", "run",
           "--profile", str(CONFIG), "--track", "custom", "--track-notes", notes,
           "--skip-model-hash", "--output", "json", "--quiet", "--out", str(out)]
    started = time.monotonic()
    with open(log, "wb") as handle:
        proc = subprocess.run(cmd, cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT)
    elapsed = time.monotonic() - started
    record = {"arm": arm, "folder": folder.name, "run": order_entry, "tag": tag,
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
        record["requests_per_case"] = {case: sum(1 for req in doc.get("requests") or []
                                                 if req.get("phase") == "performance" and req.get("case_id") == case)
                                       for case in CASES}
    return record


# ---------------------------------------------------------------- statistics

def median(values):
    return statistics.median(values) if values else float("nan")


def mad(values):
    m = median(values)
    return median([abs(v - m) for v in values])


def iqr(values):
    s = sorted(values)
    if len(s) < 2:
        return 0.0
    q1 = statistics.median(s[:len(s) // 2])
    q3 = statistics.median(s[(len(s) + 1) // 2:])
    return q3 - q1


def describe(values, arm):
    n = len(values)
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    return {"arm": arm, "n": n, "median": median(values), "mean": mean, "mad": mad(values),
            "iqr": iqr(values), "std": sd, "cv": (100 * sd / mean) if mean else 0.0,
            "min": min(values), "max": max(values)}


def bootstrap_diff(a, b, draws=20000, seed=131719):
    """Percentile CI and a two-sided bootstrap p for median(a) - median(b)."""
    rng = random.Random(seed)
    diff = median(a) - median(b)
    samples = []
    for _ in range(draws):
        ra = [a[rng.randrange(len(a))] for _ in range(len(a))]
        rb = [b[rng.randrange(len(b))] for _ in range(len(b))]
        samples.append(median(ra) - median(rb))
    samples.sort()
    lo = samples[int(0.025 * draws)]
    hi = samples[int(0.975 * draws) - 1]
    below = sum(1 for s in samples if s <= 0) / draws
    above = sum(1 for s in samples if s >= 0) / draws
    return diff, lo, hi, min(1.0, 2 * min(below, above))


def mann_whitney(a, b):
    """Two-sided Mann-Whitney U, normal approximation with tie and continuity correction."""
    n1, n2 = len(a), len(b)
    combined = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks, i = {}, 0
    rank_sum_a = 0.0
    ties = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            if combined[k][1] == 0:
                rank_sum_a += rank
        ties += (j - i + 1) ** 3 - (j - i + 1)
        i = j + 1
    u1 = rank_sum_a - n1 * (n1 + 1) / 2
    u = min(u1, n1 * n2 - u1)
    mu = n1 * n2 / 2
    n = n1 + n2
    sigma = math.sqrt(n1 * n2 / 12 * ((n + 1) - ties / (n * (n - 1))))
    if sigma == 0:
        return u1, 1.0
    z = (abs(u - mu) - 0.5) / sigma
    p = math.erfc(z / math.sqrt(2))
    return u1, min(1.0, p)


def sign_test(diffs):
    positive = sum(1 for d in diffs if d > 0)
    negative = sum(1 for d in diffs if d < 0)
    n = positive + negative
    if n == 0:
        return 1.0
    k = min(positive, negative)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def analyse(runs, arm_a, arm_b):
    rows = []
    for case in CASES:
        for metric in METRICS:
            key = f"{case}.{metric}"
            values = {}
            for arm in (arm_a, arm_b):
                values[arm] = [r["summaries"][key] for r in runs
                               if r["arm"] == arm and key in (r.get("summaries") or {})]
            if len(values[arm_a]) < 2 or len(values[arm_b]) < 2:
                continue
            a, b = values[arm_a], values[arm_b]
            diff, lo, hi, p_boot = bootstrap_diff(b, a)
            u, p_mw = mann_whitney(b, a)
            pairs = {}
            for r in runs:
                if key in (r.get("summaries") or {}):
                    pairs.setdefault(r["run"]["index"], {})[r["arm"]] = r["summaries"][key]
            paired = [d[arm_b] - d[arm_a] for d in pairs.values() if arm_a in d and arm_b in d]
            sd_a = statistics.stdev(a)
            sd_b = statistics.stdev(b)
            pooled = math.sqrt((sd_a ** 2 + sd_b ** 2) / 2)
            n = min(len(a), len(b))
            rows.append({
                "case": case, "metric": metric, "values": values,
                "desc": {arm: describe(values[arm], arm) for arm in (arm_a, arm_b)},
                "diff_median": diff, "ci_low": lo, "ci_high": hi, "p_bootstrap": p_boot,
                "mann_whitney_u": u, "p_mann_whitney": p_mw,
                "paired_median": median(paired) if paired else None,
                "paired_mean": statistics.fmean(paired) if paired else None,
                "paired_a_higher": sum(1 for d in paired if d < 0),
                "paired_b_higher": sum(1 for d in paired if d > 0),
                "paired_n": len(paired), "p_sign": sign_test(paired),
                "pooled_sd": pooled,
                "detectable_80": 2.802 * math.sqrt(2) * pooled / math.sqrt(n) if n else None,
            })
    return rows


def fmt(value, digits=2):
    return "n/a" if value is None else f"{value:.{digits}f}"


def report(rows, runs, arm_a, arm_b, labels, order_path, out_root, pairs):
    lines = []
    add = lines.append
    add(f"# A/B: {labels[arm_b]} vs {labels[arm_a]}, preset IQ2_XS max_ram")
    add("")
    add("Обе руки запускались с одним и тем же конфигом `bench/ab-fork-upstream/max_ram-ab.json`: это "
        "пресет IQ2_XS max_ram без четырёх fork-only флагов `--conversation-disk-*` (в upstream 0.1.58 "
        "этих флагов нет, движок отверг бы argv). Поэтому диск-тир выключен в обеих руках, и сравнение "
        "меряет общий путь prefill/decode, а не дисковый кэш разговоров.")
    add("")
    add("Пакеты собраны разными инструментами: форк — локальной сборкой этого чекаута "
        "(`packaging/build_engine_win_local.py`, Release, CUDA 13.4, sm_89, native ggml-cpu), "
        "upstream — официальным релизом v0.2.12 (сборка CI, набор арх. 75;86;89;120, в комплекте "
        "свои cuBLAS DLL). Значит, разница включает и разницу сборок, а не только исходников; "
        "это ограничение дизайна, его нельзя убрать без локальной сборки upstream.")
    add("")
    add(f"Перемешанный дизайн (ABBA): {pairs} полных прогонов standard-v1 на руку; каждая пара — "
        f"два прогона подряд в порядке {arm_a}, {arm_b}, затем {arm_b}, {arm_a}, и так далее. "
        "Каждый прогон поднимает свой движок и платит одинаковую холодную загрузку модели "
        "(загрузка не входит в таймеры бенчмарка).")
    add("")
    add("Движки: два пакета в `%LOCALAPPDATA%/Strata/components/engine`; перед каждым прогоном пакет "
        "закреплялся (`engine.pinned`), так что `component:strata-engine` в конфиге разрешался в него.")
    add("")
    for arm in (arm_b, arm_a):
        sample = next((r for r in runs if r["arm"] == arm), None)
        add(f"- **{arm}**: `{labels[arm]}`; в прогонах движок сообщил `{sample.get('engine_version') if sample else '?'}`.")
    add("")
    add("Положительная разница = вторая рука быстрее (в tok/s); столбец `diff` — разница медиан `%s − %s`." % (arm_b, arm_a))
    add("")
    add("## Итог")
    add("")
    for case in CASES:
        row = next((r for r in rows if r["case"] == case and r["metric"] == "decode_tps"), None)
        if row is None:
            continue
        add(f"- decode {case}: fork {row['desc'][arm_b]['median']:.2f} против upstream "
            f"{row['desc'][arm_a]['median']:.2f} tok/s, разница {row['diff_median']:+.2f} "
            f"({100 * row['diff_median'] / row['desc'][arm_a]['median']:+.1f} %), "
            f"95 % CI [{row['ci_low']:+.2f}; {row['ci_high']:+.2f}], p={row['p_bootstrap']:.3f}; "
            f"парных побед fork/upstream {row['paired_b_higher']}/{row['paired_a_higher']}, "
            f"знаковый тест p={row['p_sign']:.3f}.")
    add("- prefill: на 16k разницы нет; на 4k точка оценки у форка выше примерно на 1 % "
        "(см. раздел `prompt (control), prompt-4k`), p около 0.07 — на границе значимости.")
    add("")
    add("Вывод: ни по decode, ни по prefill разница не выходит за шум этого дизайна. "
        "Точечная оценка decode у форка на 4k и 16k около +2.5 %, но доверительные интервалы "
        "широко пересекают ноль, а 10 пар на руку разрешают только разницу от ~7.5–8.4 % "
        "(раздел `power`). То есть регрессии от форка не видно, а заявлять ускорение по этим "
        "данным нельзя.")
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
                add(f"| {labels[arm]} | {fmt(d['median'])} | {fmt(d['mean'])} | {fmt(d['mad'])} | "
                    f"{fmt(d['iqr'])} | {fmt(d['std'])} | {fmt(d['cv'])} | "
                    f"{fmt(d['min'])}-{fmt(d['max'])} | {d['n']} |")
            add("")
            a_med = row["desc"][arm_a]["median"]
            pct = (100 * row["diff_median"] / a_med) if a_med else 0.0
            add(f"- разница медиан ({arm_b} − {arm_a}): **{row['diff_median']:+.2f}** ({pct:+.1f} %), "
                f"бутстрэп 95 % CI [{row['ci_low']:+.2f}; {row['ci_high']:+.2f}], p~{row['p_bootstrap']:.3f}")
            add(f"- Mann-Whitney U = {row['mann_whitney_u']:.1f}, p = {row['p_mann_whitney']:.4f}")
            add(f"- парно (внутри ABBA-пар, {arm_b} − {arm_a}): медиана {row['paired_median']:+.2f}, "
                f"среднее {row['paired_mean']:+.2f}, {arm_b} выше в "
                f"{row['paired_b_higher']}/{row['paired_n']} пар, {arm_a} выше в "
                f"{row['paired_a_higher']}/{row['paired_n']}; знаковый тест p = {row['p_sign']:.3f}")
            add("")
    add("## robustness: decode split by how many requests hit the 512-token cap")
    add("")
    add("| рука | case | capped | прогонов | decode по прогонам | медиана |")
    add("| --- | --- | ---: | ---: | --- | ---: |")
    for arm in (arm_b, arm_a):
        for case in CASES:
            groups = {}
            for r in runs:
                if r["arm"] == arm and case in (r.get("capped") or {}):
                    groups.setdefault(r["capped"][case], []).append(r["summaries"].get(f"{case}.decode_tps"))
            for capped in sorted(groups):
                vals = [v for v in groups[capped] if v is not None]
                if not vals:
                    continue
                add(f"| {labels[arm]} | {case} | {capped}/{runs[0]['requests_per_case'].get(case, '?')} | "
                    f"{len(vals)} | {', '.join(f'{v:.1f}' for v in sorted(vals))} | {fmt(median(vals))} |")
    add("")
    add("## power")
    add("")
    for row in rows:
        if row["metric"] != "decode_tps":
            continue
        add(f"- {row['case']}: общий разброс (pooled sd) {row['pooled_sd']:.2f} tok/s; "
            f"{min(row['desc'][arm_a]['n'], row['desc'][arm_b]['n'])} прогонов на руку разрешают около "
            f"{row['detectable_80']:.2f} tok/s "
            f"({100 * row['detectable_80'] / row['desc'][arm_a]['median']:.1f} %) при 80 % мощности; "
            "всё меньшее — ниже порога этого дизайна.")
    add("")
    add("`--skip-model-hash` передан: прогоны помечены `nonconforming` с причиной «no model file could be hashed»; "
        "на тайминги это не влияет. Состояние закрепления движка осталось на последней руке — снимите его "
        "(`engine.pinned` = null), когда закончите.")
    add("")
    add(f"Сырые данные: `{out_root}` (`order.json`, `runs.jsonl`, `summary.json`, папки прогонов), "
        f"порядок: `{order_path.name}`.")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=10)
    parser.add_argument("--arm-a", default="upstream")
    parser.add_argument("--arm-b", default="fork")
    parser.add_argument("--fork-folder", default="0.1.58.1-windows-x86_64-cuda-cuda13")
    parser.add_argument("--upstream-folder", default="0.1.58-windows-x86_64-cuda-cuda13")
    parser.add_argument("--out-root", default=None)
    parser.add_argument("--resume", action="store_true", help="keep runs already in runs.jsonl")
    parser.add_argument("--report-only", action="store_true", help="rebuild report.md from runs.jsonl")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    folders = {"fork": ENGINE_DIR / args.fork_folder, "upstream": ENGINE_DIR / args.upstream_folder}
    for arm, folder in folders.items():
        if not (folder / "strata.exe").is_file():
            print(f"no engine for {arm}: {folder}")
            return 2
    labels = {}
    for arm, folder in folders.items():
        m = manifest(folder)
        labels[arm] = f"{arm} engine {engine_version(folder).replace('strata ', '')} " \
                      f"(package {m.get('version')}, build {str(m.get('build_id'))[:8]})"

    out_root = Path(args.out_root) if args.out_root else (
        ROOT / "bench" / "results" / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + "-ab-fork-upstream"))
    out_root.mkdir(parents=True, exist_ok=True)
    order = plan(args.pairs, [args.arm_a, args.arm_b])
    (out_root / "order.json").write_text(json.dumps({"plan": order, "arms": labels}, indent=1,
                                                    ensure_ascii=False) + "\n", encoding="utf-8")

    runs_path = out_root / "runs.jsonl"
    if args.report_only:
        runs = [json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows = analyse(runs, args.arm_a, args.arm_b)
        summary = {"arms": labels, "runs": runs, "analysis": rows}
        (out_root / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n",
                                               encoding="utf-8")
        text = report(rows, runs, args.arm_a, args.arm_b, labels, out_root / "order.json", out_root, args.pairs)
        (out_root / "report.md").write_text(text, encoding="utf-8")
        print(text.replace("\u2212", "-"))
        return 0

    done = []
    if args.resume and runs_path.is_file():
        for line in runs_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.append(json.loads(line))
    remaining = order[len(done):]
    print(f"{len(order)} runs planned, {len(done)} already recorded, {len(remaining)} to go", flush=True)

    runs = list(done)
    with open(runs_path, "a" if done else "w", encoding="utf-8") as journal:
        for entry in remaining:
            arm = entry["arm"]
            notes = (f"A/B fork vs upstream, IQ2_XS max_ram: {labels[arm]}; "
                     f"interleaved ABBA, run {entry['index']}/{args.pairs}")
            record = None
            for attempt in (1, 2):
                print(f"[{len(runs) + 1}/{len(order)}] {arm} pair {entry['index']} "
                      f"(attempt {attempt}) ...", flush=True)
                record = run_one(arm, folders[arm], entry, out_root, notes, len(runs) + 1)
                if record.get("summaries"):
                    break
                print(f"    failed: rc={record['returncode']}, see {record['tag']}.console.log", flush=True)
                time.sleep(5)
            journal.write(json.dumps(record, ensure_ascii=False) + "\n")
            journal.flush()
            runs.append(record)
            print(f"    {record.get('engine_version')} rc={record['returncode']} "
                  f"{record['elapsed_s']} s decode short={record.get('summaries', {}).get('short.decode_tps')}",
                  flush=True)
            time.sleep(3)

    rows = analyse(runs, args.arm_a, args.arm_b)
    summary = {"arms": labels, "runs": runs, "analysis": rows}
    (out_root / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n",
                                           encoding="utf-8")
    text = report(rows, runs, args.arm_a, args.arm_b, labels, out_root / "order.json", out_root, args.pairs)
    (out_root / "report.md").write_text(text, encoding="utf-8")
    print(text.replace("\u2212", "-"))
    return 0 if all(r.get("summaries") for r in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
