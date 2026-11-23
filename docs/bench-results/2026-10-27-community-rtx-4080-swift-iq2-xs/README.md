# Community benchmark on NVIDIA GeForce RTX 4080

Measured on 2026-10-27 with `tools/strata_bench.py` (suite 1, 3 runs per test, medians with ranges). Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no correctness check was run.

## Hardware and software

- GPU and VRAM: NVIDIA GeForce RTX 4080, 16 GiB VRAM
- CPU: 13th Gen Intel(R) Core(TM) i7-13700K (16 cores, 24 threads); installed RAM: 47.8 GiB; storage: TOSHIBA DT01ACA300 HDD/SATA; Samsung SSD 980 PRO 1TB SSD/NVMe
- OS: Windows 10 (10.0.19045); GPU driver: 610.88; CUDA (driver supports up to): 13.3
- Strata: project ?, commit not a git checkout; engine 0.1.52
- GPU detail (after the run, near idle): power_limit_w=340.00, power_draw_w=174.23, pcie=gen4 x16, pstate=P2, temp_c=57
- Background workloads: not recorded (GPU power limit/draw read above)

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
| short chat | 46 (43-51) | 0 | 100 (100-100) | 9 | not measured | 130.4 (108.0-147.3) | 0.25 (0.20-0.31) |
| 4K prompt, fresh | 4019 (4019-4019) | 0 (0-0) | 24 cap | 3 | 2009.2 (2008.3-2025.6) | not measured | 2.02 (2.00-2.02) |
| 16K prompt, fresh | 16058 (16058-16058) | 0 (0-0) | 24 cap | 3 | 2708.8 (2703.7-2710.9) | not measured | 5.95 (5.95-5.96) |
| agent session, follow-up turns | 9022 (8986-9055) at the last turn | see below | up to 64 | 3 | see below | 141.2 (112.4-191.1) | 1.41 (1.38-1.45) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 70.9 % (70.3-71.0); earlier-turn tokens read again per session: 5 (3-214) (earlier turns that the cache did not hold; the 8207 (8207-8207) new tokens of the files are read once); first turn (nothing cached) first token: 0.83 s (0.80-0.84).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 609 | 0 | 609 | 777.0 | 0.798 | 126.3 |
| 1 | 2262 | 620 | 1642 | 1197.1 | 1.386 | 114.6 |
| 2 | 3968 | 2325 | 1643 | 1179.2 | 1.408 | 136.1 |
| 3 | 5674 | 4031 | 1643 | 1181.5 | 1.414 | 183.2 |
| 4 | 7364 | 5722 | 1642 | 1179.3 | 1.409 | 152.1 |
| 5 | 9022 | 7380 | 1642 | 1173.0 | 1.415 | 113.5 |

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS; background load is not recorded. Total time of the suite: 68 s.
