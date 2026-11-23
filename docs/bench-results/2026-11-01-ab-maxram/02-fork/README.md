# Strata standard benchmark: NVIDIA GeForce RTX 4080

Protocol `strata-qwen38-thinking-v1-draft.1` (draft), run `20261101T103803Z-364295f5`, started 2026-11-01T10:38:03Z. **Status: nonconforming.**

Written by `strata bench run`. The machine-readable result is [results.json](results.json); every request, its raw usage and timings and the client event offsets are in [outputs.jsonl](outputs.jsonl).

This run is not a complete standard result:

- no model file could be hashed

## Hardware and software

- GPU: NVIDIA GeForce RTX 4080 (16.0 GiB dedicated)
- CPU: 13th Gen Intel(R) Core(TM) i7-13700K; RAM: 47.8 GiB
- Storage of the model files: not measured
- OS: Windows 10.0.19045; drivers: gpu-driver 610.88; runtime: cuda 13.3
- Strata commit: `c62d5e3a15b40ea51c96aae9e8d36928965f53ab`; engine 0.1.55-local; release build
- Background work: not measured

## Model and configuration

- Model: swift-iq2_xs; quantization IQ2_XS; revision not measured; source revision not measured
- Files: not measured
- Track: **custom** (A/B max_ram: fork 0.1.55-local (build c62d5e3a-dirty, CUDA 13.4, bundled cuBLAS 13.4); interleaved A/B, fork run 1/10)
- Context 262144 tokens; server capacity 1 slot(s)
- Sampling: temperature 1.0, top_p 0.95, top_k 20, min_p 0.0, presence_penalty 0.0, repetition_penalty 1.0, reasoning_effort xhigh; thinking on, preserved
- Engine options: `config.json`; chat template: `provenance/chat_template.jinja`

## Method

The frozen requests of `bench/prompts/standard-v1` (corpus `e75eff8b9feea04f...`) were sent over loopback HTTP, streaming, one at a time, in this order: 1 warm-up (excluded), 3 repetitions of the short, 4K and 16K prompts, and the twelve sanity checks. Prompt and decode speed are the engine's own `timings`; time to first token is the client's monotonic clock from dispatch to the first nonempty reasoning, answer or tool-call delta. No request was retried. Time after the model loaded: 1 min 26 s.

## Results

| Case | Prompt tok/s | Decode tok/s | TTFT s | First answer s | Total s | Capped |
| --- | --- | --- | --- | --- | --- | --- |
| short | n/a | 129.7 (125.2-140.6) n=3 | 0.44 (0.42-0.45) n=3 | 1.03 (0.97-2.38) n=3 | 3.2 (2.2-4.4) n=3 | 1/3 |
| prompt-4k | 2080.4 (2069.8-2086.0) n=3 | 134.3 (124.0-148.1) n=3 | 1.98 (1.98-2.00) n=3 | 3.66 (3.31-5.09) n=3 | 5.8 (5.4-6.1) n=3 | 3/3 |
| prompt-16k | 2849.0 (2844.5-2852.3) n=3 | 136.4 (128.6-142.5) n=3 | 5.78 (5.78-5.80) n=3 | 7.38 (6.92-9.12) n=3 | 9.2 (8.6-9.8) n=3 | 1/3 |

Cells are median (min-max) n=samples. Short is a prompt of about 80 tokens: its prompt rate is dominated by the fixed cost of a request, so it is not shown; read prompt speed from prompt-4k and prompt-16k. Capped = requests that reached the 512-token cap, reasoning and answer together. A capped request's total time is the time to the cap, not to an answer, and it has no first-answer time when it was still reasoning.

| Phase | Planned | Completed | Truncated | Failed | Skipped |
| --- | ---: | ---: | ---: | ---: | ---: |
| warmup | 1 | 0 | 1 | 0 | 0 |
| performance | 9 | 4 | 5 | 0 | 0 |
| sanity | 12 | 12 | 0 | 0 | 0 |
| concurrency | 0 | 0 | 0 | 0 | 0 |

Sanity checks: **11 of 12**.

| Case | Result | Reason |
| --- | --- | --- |
| I1 | pass | the expected JSON value |
| I2 | pass | the expected JSON value |
| I3 | pass | the expected JSON value |
| I4 | pass | the exact expected text |
| R1 | pass | the exact expected text |
| R2 | pass | the exact expected text |
| R3 | pass | the exact expected text |
| R4 | pass | the expected JSON value |
| T1 | pass | the expected tool call |
| T2 | pass | the expected tool call |
| T3 | fail | expected one structured tool call, got 0 |
| T4 | pass | the expected tool call |

## Failures, exclusions and limitations

- No request failed.
- Prompt-prefix reuse is the shared template prefix only; the cache was not emptied between requests, and the expert cache stays warm and evolves. These are not cold-model measurements.
- Output caps include reasoning; a request that reaches its cap is `truncated` and fails its sanity check.
- The runner cannot see other programs on the machine, the repeat-stop and loop-recovery switches of the server, or the quantization repository's revision; see `missing` in results.json for every value it could not read.
- The server had answered no request before this run (a freshly loaded engine).
