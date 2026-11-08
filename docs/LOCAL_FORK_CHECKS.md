# Checks that need a real restart (not in the offline suite)

The pytest suite in `tests/terminal` runs without a GPU, without downloads and without a serving model: every engine, server
and feed it touches is mocked. The checks below cannot be run that way: they need this PC's engine package, its picture reader
and a model that is stopped and started for real. Run them in the order given (the routine in `packaging/README.md`,
"Updating this PC").

`strata` on PATH is this checkout's launcher (`C:\Projects\Strata-fork\bin` is PATH's first entry), so every command below is
typed as `strata`. The model that serves here is `IQ2_XS low_ram`. Every check below stops and starts it, so it costs minutes
(the 68 GB model is loaded from disk).

| # | What is checked | Command | What must be seen |
|---|---|---|---|
| 1 | The deployed Python runs the server | `strata launch --stop`, then `strata launch --preset iq2_xs-low_ram --no-tui` | The server starts from `components\control\<version>\python\python.exe -m serve.server`; the deploy (`tools/deploy_local_code.py`) must have run first, because a running server keeps the code it loaded. Measured 2026-11-08: after the merge the model was started by the app, and the server ran from `components\control\0.1.56` (the upstream release's Control); the deploy copied 6 patched files into that folder (`serve/server.py`, `strata_cli/launch/runner.py`, `strata_cli/launch/settings.py`, `strata_cli/update_cmd.py`, `strata_control/engine_update.py`, `strata_control/updater.py`). This row is not run on the merged tree: the restart that starts the deployed code is still to be done |
| 2 | A new engine package is the engine that serves | `VERSION=0.1.58.1 python3 packaging/build_engine_win_local.py`, then the start above | `strata components engine` names the new package; `strata.exe --version` prints the engine string — on the merged tree (`upstream/main`, v0.2.12) it follows `project(VERSION)`, so the first start drops every index in the disk store (the identity check, `generate.cpp:10507`) and the long prompts re-warm; the manifest check passes. Measured 2026-11-08 on the merged tree: `package` built `0.1.58-local-windows-x86_64-cuda-cuda13` (687.2 MiB, 11 files, manifest sha256 `7b14a6a8…`, `package_engine.py --verify` ok), `strata components engine` answers `engine … 0.1.58-local-windows-x86_64-cuda-cuda13\strata.exe (version 0.1.58-local)` and `strata.exe --version` prints `strata 0.1.58-local`. The model was started on it at 11:26; `E:\strata-conv-data` held 0 files at 11:30 (every index dropped at the start, as the rule predicts), and by 11:38 the tier had written 3 entries back from live traffic: 5068 MiB, 457,458 tokens (`tools/conversation_store_scan.py`). A build number in `VERSION` (a `.1`) was not used here, so this row's "names `0.1.58.1-local…`" part is not run |
| 3 | The picture reader follows the package | `strata launch --preset iq2_xs-low_ram --no-tui` with pictures on | `serve/server.py` (`with_current_reader`) points the reader at `strata-vision.exe` inside the new package folder, not at a copy elsewhere; a picture request answers. Measured 2026-11-08: the run config `AppData\Local\Strata\launch\iq2_xs-low_ram.json` still names `…\0.1.55.12-windows-x86_64-cuda-cuda13\strata-vision.exe`, and the process that started is `…\0.1.58-local-windows-x86_64-cuda-cuda13\strata-vision.exe` — the reader followed the package. A picture request was not sent |
| 4 | Nothing downloads an engine | `strata launch --preset iq2_xs-low_ram --plan`, then the start | No download line for the engine, and `strata update` is never spawned by the launch; the network log of the server process shows no request to the feed for an engine archive |
| 5 | A stale engine is warned about, not fetched | Delete the newest package folder, keep an older one, start | One warn line: "older than this model needs (…)", naming `packaging/build_engine_win_local.py` as the next step, and the start goes on with the older engine. No `strata update` is spawned, no refusal |
| 6 | No engine at all is a warning, not a refusal | Move the package folder away, start with `--yes` | The plain "Strata couldn't find an engine for this computer" warning, the model download (nothing to download here), exit code 0, and nothing started |
| 7 | Control update keeps the fork's files | `strata update` after a feed has a newer Control, then `python tools/deploy_local_code.py`, then the start | A new `components\control\<version>` folder appears, the four fork files are copied into it, `active.json` selects it, and the start uses it |
| 8 | The portable package serves | `packaging/repack_local_portable.sh`, then the start | The package is named with `-local`, its `BUILD.json` records the AVX2 floor and the static runtime, and the model loads on this PC |
| 9 | `strata update --engine` on a local package | `strata update --engine --check` | Exit 5, the version rule refuses the installed package. Measured 2026-11-02 on `0.1.55-local`: `'0.1.55-local' is not a version number (expected like 0.1.46 or 0.1.46.1)`. The feed pattern (`strata_control/feed.py:62`) reads digits only, so the `-local` suffix is what fails, not the build number: `0.1.55.1-local` must fail the same way (static analysis, not run). On the merged tree the same rule reads `0.1.58-local` (static analysis, not run on 0.1.58). This is the known limit, not a bug to fix here: the default path (`update --check`, `update`) must answer 0 with the "not taken from the feed" line |
| 10 | A heavy GPU parity test asks for room before it takes it | `build-cuda/Release/gemm_w16_parity.exe` and `gdn_rec_parity.exe` with the model serving | Each prints its need and the free figure it measured and exits 77 (ctest's skip) without allocating, so the serving model keeps its card. Measured 2026-11-08: `gemm_w16_parity` prints the RAM term and
exits 77, `gdn_rec_parity` fits and runs (0 failures). The numbers are in "The GPU parity tests ask for room" below |

Check 2, 3 and 7 are the ones a change to `serve/server.py`, `strata_cli/launch/runner.py` or `strata_control/updater.py` needs
before a commit: the offline suite covers the logic, these cover the files that run.

## Engine tests and the serving model

The engine tests in `build-*/Release` (and `ctest -R "conversation|session_save"`) must run with the serving model
stopped. `conversation_snapshot_test` compares `cudaMemGetInfo` before and after six restores, with 4 MiB of slack, to
prove that a restore allocates nothing on the card (#2409). With a model serving, the free figure moves by the driver's
own pool blocks (0 to 16 blocks of 10.9 MiB on this PC) and the check fails run to run with `six more restores allocate
nothing on the card`. Measured on 2026-11-05: with `iq2_xs-low_ram` serving, 9-13 of 20 runs failed in each of the
plain, `STRATA_CKPT_ASYNC=1` and `STRATA_CKPT_VRAM_SLOTS=1` variants; with the model stopped, 60 of 60 passed and
`ctest -R "conversation|session_save"` answered 11 of 11. The test itself is unchanged: the failure is the measurement,
not an engine fault, and on the Linux CUDA path the check stays as it was.

## The GPU parity tests ask for room

`gemm_w16_parity` builds the dense projections of Qwen3.8-Flash-Next in four quant types and, at its last shape (the
248320-row output head) and T 4096, holds `Y[0]` and `Y[1]` on the card at 3.79 GiB each and `out[0]` and `out[1]` in RAM
at the same size, beside 0.90 GiB of weights: **8.52 GiB of the card and 8.48 GiB of RAM**, 17 GiB together (the byte
count of the test's own `cudaMalloc` sizes and its two output vectors). Measured 2026-11-08:
the test ran (and passed, 207.19 s) with `iq2_xs-low_ram` serving, the PC went into heavy swap, and the engine stopped
with `verify: timed out at layer 14; its GPU waits were released but the GPU did not finish within 5 s`. The test came in
with upstream's commit `62853e78` (the FP16 weight cache and its parity test, part of v0.2.12); it had never run on this
PC before, because the tests were not built here. `gdn_rec_parity` builds the same kind of arrays: 4.27 GiB of the card at
T 32768 (the longest in its default run), 6.75 GiB at T 51820 with `--long` — the file's own header says "needs ~7 GB" for
that case, which the byte count of `Inputs` + `check_bits` reproduces. No other ctest entry passes 3 GiB: the largest
after these are `gemm_dequant_side_parity` at about 1 GiB (`Y[0]`, `Y[1]` at T 512 over 8 shapes), `gemm_f32_parity` and
`dequant_wide_parity` at about 0.9 GiB and 0.3 GiB (computed from their `cudaMalloc` sizes, not measured).

Both heavy tests now ask before they allocate, through `include/strata/core/test_memory.hpp`:
`test_memory_enough(need_device, need_host, name)` for `gemm_w16_parity` (its executable links `strata_prefill`, which
links `strata_engine`, so `available_host_bytes()` is there), and `test_device_enough(need_device, name)` for
`gdn_rec_parity` (it links only the CUDA runtime, so the RAM term is not available to it). A test that cannot fit prints
the need and the free figure it measured and exits 77, which ctest reports as a skip. Measured on 2026-11-08 with
`iq2_xs-low_ram` serving: `gemm_w16_parity.exe` answers `gemm_w16_parity: needs 8.48 GiB of RAM, 4.20 GiB free - skipped`
and exits 77 (its card term passes: the low_ram preset leaves more than 8.52 GiB of the card free), and
`gdn_rec_parity.exe` — 4.27 GiB, which is free — runs its whole default series, `gdn_rec_parity: 0 failures`, exit 0, with
the model answering through it (`strata launch --status` is `ready at http://127.0.0.1:8080` after it). A test that fits
runs; a test that cannot fit is skipped, and the engine's card and RAM stay as they were.

## The engine ctest on this PC (partial, 2026-11-08)

`ctest -C Release` in `build-cuda` (the native `ctest.exe`, 87 entries) reached 82 of 87 before it was stopped:
**78 passed, 2 failed, 1 skipped** (`pinned_alias_test`, a Linux-path case), the last five entries not run. Of the two
failures one is a fixture this PC does not have: `ple_parity` answers, in 0.01 s and on a single-test re-run,
`required PLE table is missing or incompatible: ../../Q2_0/Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00002-of-00002.gguf
(cannot open …)` — the checkout's `bench/micro` layout does not carry that Q2_0 shard. The second failure,
`ple_reader_selftest` after 404 s, is recorded with its output in the next section. The run that reached 82 ran with
the model serving, which is the load that stopped the engine; a full `ctest` needs the model stopped.

## The offline suite on this PC

`./.venv/Scripts/python.exe -m pytest tests/terminal -q` (the `.venv` needs `pytest` and `textual==8.2.8`) answers
**30 failed, 2777 passed, 78 skipped, 1 error, 407 subtests passed in 2121.59 s** (measured 2026-11-08 on the merged
tree, `upstream/main` v0.2.12, with `iq2_xs-low_ram` serving). 28 of the failures are conditions of this PC; two
(`test_procs.py`, `test_runtime.py`) are load from the `ctest` run that ran beside this suite — they pass in a re-run of
the four files alone (measured 2026-11-08: 4 failed, 153 passed, 3 skipped in 335 s). Every listed case is a condition of
this PC, not the fork's code.

| File | Cases | Why it fails here |
|---|---|---|
| `test_split_parallel.py` | 12 (the split plans, the app's plan, the draft-layer fetch) | the all-cards split rule reads the cards only on Linux: `strata_control/split_engine.expected` sets `cards = []` when `sys.platform` is not linux, so every plan on this PC answers `dflash2` where the fixture expects `split` and `mtp` |
| `test_fix3_0212.py` | 3 (the same split rule on busy and free cards) | the same rule: `split_expected()` answers False on Windows, so `(split, choice)` is `(False, "dflash2")` against the fixture's `(True, "mtp")` |
| `test_launch_dsv41.py` | 4 plan and list cases | the fixtures describe 4 AMD cards and 251 GB, and the plan answers with this PC's Windows placement rule ("on Windows a model runs on one AMD card"); one verdict is `tight` where the fixture expects `fits` |
| `test_private_files.py` | 1 (a locked file read after the repair) | the ACL repair runs on a temp folder under `D:\tmp\cygwin`, and this PC locks it differently |
| `test_bench_standard.py` | 3 request-outcome cases | the outcome (`connection_TimeoutError` against `timeout`) depends on the network this PC sees; the pair moves — 2026-11-02: the round-total case fails, the other passes; 2026-11-08 all three fail |
| `test_pictures.py` | 1 (the picture file in the run config's data folder) | the tree the case compares is built with `/` path keys (`f"models/{NAME}"`), and on Windows `Path`'s parts give `\`: `models\mmproj-Tiny.gguf` against the expected `models/mmproj-Tiny.gguf` |
| `test_update_notice.py` | 1 (a monitor beside a hanging feed) | the temp folder it makes cannot be removed: `.updates/update.lock` is unlinked by the cleanup with `PermissionError 13` |
| `test_discovery.py`, `test_shared_models_folder.py` | 3 | they expect a machine with no Strata install visible, and this checkout answers as one: its root holds the launch files (`strata-swift-iq2_xs.json` and two more), so `Catalog.root` is the checkout |
| `test_procs.py`, `test_runtime.py` | 1 + 1 (and 1 error) | load, not the code: the suite ran with the `ctest` of the GPU parity tests beside it, and both cases read this PC's live process list (a live engine of this executable, a reused PID). Measured 2026-11-08 in a re-run of these four files alone: `test_procs.py` and `test_runtime.py` pass with no failure and no error |

None of the eleven says anything about the engine step of the launch or of `strata update`. The groups that do test them pass:
`test_launch_engine_first.py` (20 cases), `test_updater.py` (49), `test_round8_fixes.py` (25), `test_no_acquisition.py` (13),
`test_component_start.py`, `test_make_guard.py`, `test_profiles.py`, `test_launch_monitor.py` (81 together), `test_issues_022.py`
and `test_tui2_023_install.py` (34).

## The app's UI tests on this PC

`npm test` in `apps/host` needs two things this checkout had not set up:

- `npm ci` in `apps/host` (the folder had no `node_modules`; one test imports `esbuild`). Measured 2026-11-08: the run
  without it ended with every module failing on `Cannot find package 'esbuild'`.
- the working tree in LF. `core.autocrlf` was `true` here, so `.gitattributes` (which says nothing about `.ts` / `.tsx`)
  checked the app's sources out with CRLF. Measured on the merged tree: 1503 passed, 2 failed with the tree in CRLF
  (`ui-do.test.mjs` reads `src/lib/store.ts` against a regex that wants `\n`, and the composer case is the same class);
  after `git config core.autocrlf false` and a re-checkout from the index, 1504 passed, 1 failed. The third case
  (`markdown-crlf.test.mjs`) was a Windows limit, not a line ending, and is fixed in commit `4319d013`. With both:
  **1505 passed, 0 failed, 110 skipped** (the skipped group is `STRATA_HARNESS_DIR` — no built Harness on this PC).

`bin/strata.ps1`, `*.cmd` and `*.bat` keep CRLF by `.gitattributes`; the re-checkout did not touch them.
