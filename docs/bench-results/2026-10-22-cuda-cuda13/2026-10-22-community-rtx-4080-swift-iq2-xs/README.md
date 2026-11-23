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
| short chat | 46 (43-51) | 0 | 100 (100-100) | 9 | not measured | 87.4 (76.0-105.4) | 0.32 (0.29-0.38) |
| 4K prompt, fresh | 4019 (4019-4019) | 0 (0-0) | 24 cap | 3 | 1823.3 (1713.1-1833.4) | not measured | 2.24 (2.23-2.38) |
| 16K prompt, fresh | 16058 (16058-16058) | 0 (0-0) | 24 cap | 3 | 2622.4 (2612.3-2623.9) | not measured | 6.16 (6.14-6.18) |
| agent session, follow-up turns | 14679 (14653-14687) at the last turn | see below | up to 64 | 3 | see below | 97.6 (78.9-119.8) | 1.99 (1.53-2.27) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 69.4 % (69.3-69.5); earlier-turn tokens read again per session: 73 (1-74) (earlier turns that the cache did not hold; the 13813 (13811-13813) new tokens of the files are read once); first turn (nothing cached) first token: 0.95 s (0.92-0.99).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 609 | 0 | 609 | 682.7 | 0.922 | 80.3 |
| 1 | 4109 | 623 | 3486 | 1590.3 | 2.229 | 94.0 |
| 2 | 5642 | 4173 | 1469 | 979.0 | 1.535 | 80.6 |
| 3 | 9075 | 5635 | 3440 | 1538.3 | 2.273 | 82.1 |
| 4 | 11822 | 9138 | 2684 | 1360.1 | 1.992 | 103.9 |
| 5 | 14679 | 11872 | 2807 | 1423.1 | 2.009 | 95.7 |

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS; background load and power limits are not recorded. Total time of the suite: 85 s.
