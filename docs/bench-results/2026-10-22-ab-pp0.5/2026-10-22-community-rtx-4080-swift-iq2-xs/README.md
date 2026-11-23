# Community benchmark on NVIDIA GeForce RTX 4080

Measured on 2026-10-22 with `tools/strata_bench.py` (suite 1, 3 runs per test, medians with ranges). Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no correctness check was run.

## Hardware and software

- GPU and VRAM: NVIDIA GeForce RTX 4080, 16 GiB VRAM
- CPU: 13th Gen Intel(R) Core(TM) i7-13700K (16 cores, 24 threads); installed RAM: 47.8 GiB; storage: not measured
- OS: Windows 10 (10.0.19045); GPU driver: 610.88; CUDA: not read
- Strata: project 0.1.45.3, commit e88ee39d; engine 0.1.47
- Background workloads and power limits: not recorded

## Model and configuration

- Model and quantization: swift-iq2_xs
- Context: 262144 tokens; parallel slots: 1
- Settings: the install's own configuration, unchanged; thinking off for the suite; temperature 0

## Method

Run by `python tools/strata_bench.py` against the HTTP API of a Strata server (already running); model loading is not timed. One warm-up chat and a 1K prompt are discarded first. Every prompt starts with a unique tag, so the conversation cache cannot help the fresh-prompt tests.

- short chat: the three questions of `tools/ab_engine.py`, up to 100 new tokens, 3 runs
- 4K / 16K prompts: generated word lists sized by a calibration request, 24 new tokens, 3 fresh prompts each
- agent session: system prompt and four tools, then 5 turns that each add about 1500 words of a project file and continue from the model's own earlier answers (the way agent clients resend a conversation), 64 new tokens per turn, 3 sessions; the cache numbers come from the answer's usage (`cached_tokens`) and timings (`prompt_n`)
- concurrency (when run): N requests sent at the same moment, total tokens over the wall time of the burst, latency over all requests

## Results

| Configuration | Actual prompt tokens | Reused tokens | Generated tokens | Runs | Prompt tok/s median and range | Decode tok/s median and range | TTFT seconds median and range |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| short chat | 46 (43-51) | 0 | 100 (100-100) | 9 | not measured | 85.0 (76.8-102.5) | 0.32 (0.27-0.35) |
| 4K prompt, fresh | 4019 (4019-4019) | 0 (0-0) | 24 cap | 3 | 1752.5 (1706.4-1892.1) | not measured | 2.31 (2.16-2.39) |
| 16K prompt, fresh | 16058 (16058-16058) | 0 (0-0) | 24 cap | 3 | 2541.3 (2540.0-2586.2) | not measured | 6.35 (6.24-6.36) |
| agent session, follow-up turns | 14683 (14677-14692) at the last turn | see below | up to 64 | 3 | see below | 99.7 (79.2-116.6) | 2.00 (1.61-2.28) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 69.4 % (69.3-69.5); earlier-turn tokens read again per session: 73 (1-75) (earlier turns that the cache did not hold; the 13814 (13809-13814) new tokens of the files are read once); first turn (nothing cached) first token: 0.96 s (0.92-1.01).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 609 | 0 | 609 | 672.9 | 0.922 | 86.8 |
| 1 | 4108 | 620 | 3488 | 1552.6 | 2.284 | 86.1 |
| 2 | 5635 | 4168 | 1467 | 921.8 | 1.627 | 104.7 |
| 3 | 9068 | 5699 | 3369 | 1536.6 | 2.23 | 99.9 |
| 4 | 11809 | 9128 | 2681 | 1363.3 | 2.004 | 109.7 |
| 5 | 14677 | 11872 | 2805 | 1405.4 | 2.049 | 101.8 |

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS; background load and power limits are not recorded. Total time of the suite: 87 s.
