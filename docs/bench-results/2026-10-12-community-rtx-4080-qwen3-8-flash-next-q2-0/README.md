# Community benchmark on NVIDIA GeForce RTX 4080

Measured on 2026-10-12 with `tools/strata_bench.py` (suite 1, 3 runs per test, medians with ranges). Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no correctness check was run.

## Hardware and software

- GPU and VRAM: NVIDIA GeForce RTX 4080, 16 GiB VRAM
- CPU: 13th Gen Intel(R) Core(TM) i7-13700K (16 cores, 24 threads); installed RAM: 47.8 GiB; storage: not measured
- OS: Windows 10 (10.0.19045); GPU driver: 610.88; CUDA: not read
- Strata: project 0.1.43, commit 7695859a; engine 0.1.43
- Background workloads and power limits: not recorded

## Model and configuration

- Model and quantization: qwen3.8-flash-next-q2_0
- Context: 262144 tokens; parallel slots: 1
- Settings: the install's own configuration, unchanged; thinking off for the suite; temperature 0

```text
strata.exe --pack q2_0 --native Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00001-of-00002.gguf --ple-gguf Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00002-of-00002.gguf --expert-profile expert-profile.bin --expert-cache auto --prefill auto --spec 4 --mtp rt --max-context 262144 --kv q4_0 --kv-resident 32768 --vision --vram-reserve-mib 500 --pcie-frac 0.20 --spec-min-p 0.50
```

## Method

Run by `python tools/strata_bench.py` against the HTTP API of a Strata server (started by the benchmark from the install configuration); model loading is not timed. One warm-up chat and a 1K prompt are discarded first. Every prompt starts with a unique tag, so the conversation cache cannot help the fresh-prompt tests.

- short chat: the three questions of `tools/ab_engine.py`, up to 100 new tokens, 3 runs
- 4K / 16K prompts: generated word lists sized by a calibration request, 24 new tokens, 3 fresh prompts each
- agent session: system prompt and four tools, then 5 turns that each add about 1500 words of a project file and continue from the model's own earlier answers (the way agent clients resend a conversation), 64 new tokens per turn, 3 sessions; the cache numbers come from the answer's usage (`cached_tokens`) and timings (`prompt_n`)
- concurrency (when run): N requests sent at the same moment, total tokens over the wall time of the burst, latency over all requests

## Results

| Configuration | Actual prompt tokens | Reused tokens | Generated tokens | Runs | Prompt tok/s median and range | Decode tok/s median and range | TTFT seconds median and range |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| short chat | 46 (43-51) | 0 | 100 (100-100) | 9 | not measured | 116.8 (108.6-143.0) | 0.24 (0.19-0.28) |
| 4K prompt, fresh | 4019 (4019-4019) | 0 (0-0) | 24 cap | 3 | 2088.8 (931.4-2114.8) | not measured | 1.94 (1.92-4.33) |
| 16K prompt, fresh | 16058 (16058-16058) | 0 (0-0) | 24 cap | 3 | 2819.5 (2080.0-2827.1) | not measured | 5.72 (5.72-7.74) |
| agent session, follow-up turns | 14408 (14401-14438) at the last turn | see below | up to 64 | 3 | see below | 111.8 (59.5-154.0) | 1.68 (1.30-6.51) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 68.6 % (68.6-68.7); earlier-turn tokens read again per session: 50 (4-73) (earlier turns that the cache did not hold; the 13633 (13632-13635) new tokens of the files are read once); first turn (nothing cached) first token: 0.89 s (0.87-2.22).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 609 | 0 | 609 | 276.5 | 2.217 | 89.0 |
| 1 | 3711 | 629 | 3082 | 474.5 | 6.513 | 107.3 |
| 2 | 5244 | 3775 | 1469 | 409.3 | 3.606 | 122.3 |
| 3 | 8688 | 5237 | 3451 | 617.3 | 5.611 | 85.6 |
| 4 | 11560 | 8700 | 2860 | 541.0 | 5.309 | 89.0 |
| 5 | 14408 | 11562 | 2846 | 701.7 | 4.077 | 69.9 |

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS; background load and power limits are not recorded. Total time of the suite: 93 s.
