# Checks that need a real restart (not in the offline suite)

The pytest suite in `terminal/tests/terminal` runs without a GPU, without downloads and without a serving model: every engine, server
and feed it touches is mocked. The checks below cannot be run that way: they need this PC's engine package, its picture reader
and a model that is stopped and started for real. Run them in the order given (the routine in `tools/packaging/README.md`,
"Updating this PC").

`strata` on PATH is this checkout's launcher (`C:\Projects\Strata-fork\bin` is PATH's first entry), so every command below is
typed as `strata`. The model that serves here is `IQ2_XS low_ram`. Every check below stops and starts it, so it costs minutes
(the 68 GB model is loaded from disk).

| # | What is checked | Command | What must be seen |
|---|---|---|---|
| 1 | The deployed Python runs the server | `strata launch --stop`, then `strata launch --preset iq2_xs-low_ram --no-tui` | The server starts from `components\control\<active>\python\python.exe -m serve.server`; the deploy (`tools/deploy_local_code.py`) must have run first, because a running server keeps the code it loaded |
| 2 | A new engine package is the engine that serves | `VERSION=0.1.67.1 python3 tools/packaging/build_engine_win_local.py`, then the start above | `strata components engine` names `0.1.67.1-local-windows-x86_64-cuda-cuda13`; `strata.exe --version` prints `0.1.67-local` (the engine string stays so the conversation disk store survives); the manifest check passes (`strata components engine --full`, no SHA-256 complaint) |
| 3 | The picture reader follows the package | `strata launch --preset iq2_xs-low_ram --no-tui` with pictures on | `terminal/serve/server.py` (`with_current_reader`) points the reader at `strata-vision.exe` inside the new package folder, not at a copy elsewhere; a picture request answers |
| 4 | Nothing downloads an engine | `strata launch --preset iq2_xs-low_ram --plan`, then the start | No download line for the engine, and `strata update` is never spawned by the launch; the network log of the server process shows no request to the feed for an engine archive |
| 5 | A stale engine is warned about, not fetched | Delete the newest package folder, keep an older one, start | One warn line: "older than this model needs (…)", naming `tools/packaging/build_engine_win_local.py` as the next step, and the start goes on with the older engine. No `strata update` is spawned, no refusal |
| 6 | No engine at all is a warning, not a refusal | Move the package folder away, start with `--yes` | The plain "Strata couldn't find an engine for this computer" warning, the model download (nothing to download here), exit code 0, and nothing started |
| 7 | Control update keeps the fork's files | `strata update` after a feed has a newer Control, then `python tools/deploy_local_code.py`, then the start | A new `components\control\<version>` folder appears, the three fork files (`runner.py`, `update_cmd.py`, `updater.py`) are copied into it, `active.json` selects it, and the start uses it |
| 8 | The portable package serves | `tools/packaging/repack_local_portable.sh`, then the start | The package is named with `-local`, its `BUILD.json` records the AVX2 floor and the static runtime, and the model loads on this PC |
| 9 | `strata update --engine` on a local package | `strata update --engine --check` | Exit 5, the version rule refuses the installed package. Measured 2026-11-24 twice on this PC: with `0.1.66-local` installed, `'0.1.66-local' is not a version number (expected like 0.1.46 or 0.1.46.1)`, and after the `0.1.67-local` package went in, the same answer for `'0.1.67-local'`. The feed pattern (`terminal/strata_control/feed.py:62`) reads digits only, so the `-local` suffix is what fails, not the build number (the same answer as the 2026-11-02 check on `0.1.55-local`). This is the known limit, not a bug to fix here: the default path (`update --check`, `update`) must answer 0 with the "not taken from the feed" line |
| 10 | The engine of a model shape comes from the feed, beside a running model | `STRATA_SHAPE_ENGINE_FROM_FEED=1 strata update --no-control` | The shape package is downloaded, its signature and every file hash checked, test-started, and written as a new folder in `components\shapes\<shape>\engine\` beside the older one (`KEEP_VERSIONS` keeps two). No model is stopped and no `Busy` line appears, because the Strata engine step and Control are off. Measured 2026-11-24: inkling2 went `0.1.66` → `0.1.67`, `strata components engine` resolves `engine for inkling2` to the new folder, and `strata-inkling.exe --version` prints `0.1.67` |

Check 2, 3 and 7 are the ones a change to `terminal/serve/server.py`, `terminal/strata_cli/launch/runner.py` or
`terminal/strata_control/updater.py` needs
before a commit: the offline suite covers the logic, these cover the files that run.

A shape engine cannot be built in this checkout: `strata-inkling` from a full build names itself `0.1.67` with no `-local`
suffix, and `strata-inkling.exe --geometry` answers "unknown option" — the install reads that flag and refuses an engine
that cannot name its shape (`terminal/strata_control/shape_engine.py`, check 10). So the tree builds the Strata engine, the
picture reader and the libraries, and a shape engine is taken from the feed. Measured 2026-11-24 on `build-0.1.67-win`
(854 targets, exit 0): `src/inkling2/strata-inkling.exe` is built, `--version` prints `strata-inkling 0.1.67`, `--geometry`
is not an option, and `strata.exe --geometry` prints `flash_next`.

## The offline suite on this PC

`./.venv/Scripts/python.exe -m pytest terminal/tests/terminal -q` (the `.venv` needs `pytest` and `textual==8.2.8`) answers
**3968 passed, 138 skipped, 3 failed and 2142 subtests passed, in 2321.37 s (38.7 min)** — measured 2026-11-24 on this
checkout, after upstream v0.2.21 was merged into it and the fork's test fixes were in. The same run with a full engine
build going on beside it answered 3961 passed, 6 failed in 2439.24 s: the three extra failures are the cases below that
this checkout's test edits close, and they pass on a quiet machine.

Thirteen cases were closed by this checkout's test edits at the 0.2.20 merge; the edits are in this checkout and all thirteen pass in the 0.2.21 run:

| File | Cases | What the test change does |
|---|---|---|
| `test_update_with_model.py` | 9 | its harness never asked for an engine, and this checkout takes an engine from the feed only when asked (`--engine`, or `STRATA_ENGINE_FROM_FEED=1`), so the engine step was skipped and none of the model questions ran. `ModelCase.setUp` asks for the step the way `test_updater.py`'s `Cli` does. The default path, where the engine is not taken from the feed, is checked there by `test_the_engine_step_is_skipped_by_default` |
| `test_launch_stale_engine_update_failed.py` | 3 | rewritten for this fork's rule: a launch never spawns `strata update`, and the engine that runs is the package built in the checkout, so the stale-engine answer names `tools/packaging/build_engine_win_local.py` as the next step |
| `test_launcher_heal.py` | 1 | the launcher header names `bin\strata-direct.ps1`, the file this checkout ships |

The 0.2.21 merge brought six more cases that fail from this checkout's own state; three are closed by this checkout's test edits:

| File | Cases | What the test change does |
|---|---|---|
| `test_drafting_sycl_paths.py` | 2 | the mount rule tries the checkout, then the folder above it, and this PC keeps a data folder beside the checkout (`C:\Projects\Strata-data`, where this fork's models and packs live), so both cases answer with that folder. They now run in a throwaway checkout: `SyclContainerPaths.fake_checkout` patches `drafting.__file__` |
| `test_inkling_unsloth_4gb.py` | 2 | both harnesses mock `setup.free_gb` (disk space) but not `setup.ram_free_gb`, so the Inkling plan reads this PC's real free RAM — 3.7 GiB free under a suite run with the model loaded, and a full-VRAM plan gets a RAM tier it should not have (`--ram-experts-gb 0.77`). Each harness mocks `ram_free_gb` at the RAM its case states |
| `engine/tests/core/dot_x4_bench.cpp` | build | MSVC reads the plain `type_info` as `std::type_info` (`error C2872: type_info: ambiguous symbol`), where GCC picks `strata::quant::type_info` by ADL. The call is qualified, as every other caller in the tree writes it (`tests/quant/quant_parity.cpp:298`). It is a bench, not a ctest case; the target is one of the 854 |

The three left fail on a clean upstream v0.2.21 checkout on this PC as well (checked 2026-11-24 in
`E:\LocalTemp\opencode\pristine-v0221`, upstream commit `d46f5f268`), so none of them comes from a fork change:

| File | Case | Why it fails here | Clean v0.2.21 |
|---|---|---|---|
| `test_tui2_023_install.py` | `WindowsInstaller::test_a_folder_with_accented_letters_is_unpacked` | the installer unpacks inside this PC's temp folder, and Windows refuses that path: "Cannot extract through symlink `\\?\C:\Users\karam\AppData\Local\Temp`", so nothing is installed and the "Installed. Run: strata launch" line never appears | yes |
| `test_tui2_023_install.py` | `WindowsInstaller::test_the_whole_cause_is_printed_not_the_first_six_lines` | the same refusal: the cause that is printed is the unpack error, so the fixture's last line is missing from the output | yes |
| `test_update_bat_stub.py` | `Run::test_without_the_stub_the_old_file_ends_with_the_error_and_exit_1` | `cmd` prints "The batch file cannot be found." in the console's code page (this PC is a Russian Windows, OEM code page 866), and the case reads the child's text as cp1251, so the ASCII sentence is not found in `stderr` | yes |

Two rows of the old table are closed upstream: `test_private_files.py::ElevatedRunLockoutTests::test_default_dacl_allows_reopening_child_process_for_qos`
(the child Python had no path to the checkout's `terminal` folder) and `test_test_start_missing_library.py::BinarySmoke::test_the_default_test_start_with_no_model_keeps_the_log`
(`updater.default_test_start` reading `component.id` from a `SimpleNamespace` that gives none). Measured 2026-11-24: both
pass in this checkout and in the clean v0.2.21 checkout.

The groups that check the engine step of the launch and of `strata update` pass whole: `test_launch_engine_first.py`,
`test_updater.py`, `test_round8_fixes.py`, `test_no_acquisition.py`, `test_component_start.py`, `test_make_guard.py`,
`test_profiles.py`, `test_launch_monitor.py`, `test_issues_022.py`, `test_update_with_model.py` — 250 passed, 3 skipped,
14 subtests in 462.76 s (measured 2026-11-24, the ten files run together). The shape-engine suite, `test_shape_engine.py`
and this checkout's `test_shape_engine_feed_only.py`, answers 62 passed, 5 skipped, 2 subtests in 43.95 s.

## The engine parity tests (ctest)

The build folder for 0.1.67 is `build-0.1.67-win`, configured from `engine/` with Ninja, the `.venv/Scripts` copies of
`cmake` and `ninja`, CUDA 13.4's `nvcc`, `ARCHS=89` and `-DSTRATA_BUILD_TESTS=ON`. The cache of `build-c13-win` was made
from the pre-split source root (`C:/Projects/Strata-fork/CMakeLists.txt`), and cmake refuses to reconfigure it for
`engine/`: "the source … does not match the source … used to generate cache". A full build inside the vcvars64 environment
(`ninja -k 0 -j 24`) answers **854 of 854 targets, exit 0**, with 330 `.exe` files in the folder.

`ctest -N` registers **409 tests** in this configuration (`engine/cmake/strata_gpu_engine.cmake` has 165 `add_test` lines
at v0.2.21 against 162 at v0.2.20, and `engine/CMakeLists.txt` adds 25 more). Keep `STRATA_BUILD_TESTS=ON`: a tests-OFF
cache registers some tests from a subfolder's `CMakeLists.txt` without building their executables —
`engine/src/inkling2/CMakeLists.txt:270` registers `sk_tier_test` as `src/inkling2/sk_tier_test.exe`, which a tests-OFF
build never compiles, and ctest answers "Not Run" (measured 2026-11-24: 92 tests registered in the tests-OFF cache,
97% passed with that one Not Run).

The serial run `./.venv/Scripts/ctest.exe --test-dir build-0.1.67-win` (the serving model `IQ2_XS low_ram` up on
127.0.0.1:8080) answers **`99% tests passed, 4 tests failed out of 409`** — 390 Passed, 15 Skipped, 4 Failed — in
**1703.49 s** (measured 2026-11-24). The four:

| Test | Time | What it printed | A clean v0.2.21 on this PC |
|---|---|---|---|
| `ple_parity` | 0.01 s | `ple_parity: required PLE table is missing or incompatible: ../../Q2_0/Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00002-of-00002.gguf (cannot open …)`: the test wants a Q2_0 expert pack that is not in this tree | Failed |
| `no_mmq_symbols` | 0.42 s | `bytes CLEAN; nm CLEAN; cuobjdump NOT RUN (it read no device function …)` and `2 of 3 files not fully checked`: `tools/check_no_mmq.py` is a Linux-tool check, and on this PC the device-code look never runs | Failed |
| `prefill_fused_q36_test` | 0.08 s | `prefill fused MoE (Qwen3.6 shape) parity failed: Q6_K + Q8_0: not covered by the Qwen3.6 copy of the kernels` | Failed |
| `pool_tasks_test` | 60.80 s | every check it prints passes, then `strata: the CPU expert pool stalled after a phase (4 of 5 workers parked) - stopping the engine …` with exit code `0xc0000409`. A moving result: run alone it answers Passed in 9.40 s (measured 2026-11-24) | Passed |

`kv_window_parity`, the moving result of the 0.1.66 run, passed this time. The 15 Skipped tests use `SKIP_RETURN_CODE "77"`:
`device_error_exit_test`, `pinned_alias_test`, `kq512_parity`, `prefill_fused_iq_test_sm75`, `prefill_fused_iq_test_ws`,
`prefill_fp4_tc_test`, `nvfp4_decode_sm120_test`, `expert_multi_test`, `native_hadamard_test`, `tq_cpu_parity`,
`tq_cpu_parity_avx2`, `expert_parity`, `pool_test`, `isa_guard_check`, `cuda_spill_guard`.

A clean upstream v0.2.21 checkout does not build on this PC: seven targets fail for the MSVC-only causes the fork fixes,
measured 2026-11-24 in `E:\LocalTemp\opencode\pristine-v0221\build-pristine` (851 of 854, ninja stopped on the errors) —
`flag_order_test.cu` (`__ATOMIC_SEQ_CST`, `__atomic_load_n` undefined), `gemm_w16_parity.cu` (`setenv` undefined),
`dot_x4_bench.cpp` (`type_info` is an ambiguous symbol), and `LNK2019: unresolved external symbol strata::kernels::cpu::expert_layout()`
for `pinned_shared_win_test.exe`, `strata-load.exe`, `strata-overlap.exe`, `strata-concurrent.exe`. This checkout builds
all 854. The fixes are the fork's pattern, not new machinery: `_InterlockedCompareExchange` in `flag_order_test.cu` (the
branch `include/strata/core/flag_order.hpp` already uses), `setenv`/`unsetenv` mapped to `_putenv_s` under `_WIN32`
(the pattern of `engine/src/kernels/hc_persist_parity.cpp`), `$<TARGET_NAME_IF_EXISTS:strata_kernels_cpu>` on the four link
lines, and the qualified `strata::quant::type_info`. The `<cstdint>` that `engine/include/strata/kernels/native_mmvq.hpp`
needed for MSVC is upstream's own line now: `git diff upstream/main main` for that file is empty, so that fix left the fork.

What to do: configure the build folder from `engine/` with `-DSTRATA_BUILD_TESTS=ON`, build it fully inside vcvars, and run
`ctest` in it. The packaging script reconfigures the folder with tests OFF, so a ctest run straight after packaging sees the
shorter list. The build does not stop the serving model, and the run is done with the model up: 409 tests, 330 executables,
about 28 minutes.
