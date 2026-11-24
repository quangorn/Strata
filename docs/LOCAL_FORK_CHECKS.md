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
| 1 | The deployed Python runs the server | `strata launch --stop`, then `strata launch --preset iq2_xs-low_ram --no-tui` | The server starts from `components\control\0.1.66\python\python.exe -m serve.server`; the deploy (`tools/deploy_local_code.py`) must have run first, because a running server keeps the code it loaded |
| 2 | A new engine package is the engine that serves | `VERSION=0.1.66.1 python3 tools/packaging/build_engine_win_local.py`, then the start above | `strata components engine` names `0.1.66.1-local-windows-x86_64-cuda-cuda13`; `strata.exe --version` prints `0.1.66-local` (the engine string stays so the conversation disk store survives); the manifest check passes (`strata components engine --full`, no SHA-256 complaint) |
| 3 | The picture reader follows the package | `strata launch --preset iq2_xs-low_ram --no-tui` with pictures on | `terminal/serve/server.py` (`with_current_reader`) points the reader at `strata-vision.exe` inside the new package folder, not at a copy elsewhere; a picture request answers |
| 4 | Nothing downloads an engine | `strata launch --preset iq2_xs-low_ram --plan`, then the start | No download line for the engine, and `strata update` is never spawned by the launch; the network log of the server process shows no request to the feed for an engine archive |
| 5 | A stale engine is warned about, not fetched | Delete the newest package folder, keep an older one, start | One warn line: "older than this model needs (…)", naming `tools/packaging/build_engine_win_local.py` as the next step, and the start goes on with the older engine. No `strata update` is spawned, no refusal |
| 6 | No engine at all is a warning, not a refusal | Move the package folder away, start with `--yes` | The plain "Strata couldn't find an engine for this computer" warning, the model download (nothing to download here), exit code 0, and nothing started |
| 7 | Control update keeps the fork's files | `strata update` after a feed has a newer Control, then `python tools/deploy_local_code.py`, then the start | A new `components\control\<version>` folder appears, the three fork files (`runner.py`, `update_cmd.py`, `updater.py`) are copied into it, `active.json` selects it, and the start uses it |
| 8 | The portable package serves | `tools/packaging/repack_local_portable.sh`, then the start | The package is named with `-local`, its `BUILD.json` records the AVX2 floor and the static runtime, and the model loads on this PC |
| 9 | `strata update --engine` on a local package | `strata update --engine --check` | Exit 5, the version rule refuses the installed package. Measured 2026-11-02 on `0.1.55-local`: `'0.1.55-local' is not a version number (expected like 0.1.46 or 0.1.46.1)`. The feed pattern (`terminal/strata_control/feed.py:62`) reads digits only, so the `-local` suffix is what fails, not the build number: `0.1.66.1-local` must fail the same way (static analysis, not run). This is the known limit, not a bug to fix here: the default path (`update --check`, `update`) must answer 0 with the "not taken from the feed" line |

Check 2, 3 and 7 are the ones a change to `terminal/serve/server.py`, `terminal/strata_cli/launch/runner.py` or
`terminal/strata_control/updater.py` needs
before a commit: the offline suite covers the logic, these cover the files that run.

## The offline suite on this PC

`./.venv/Scripts/python.exe -m pytest terminal/tests/terminal -q` (the `.venv` needs `pytest` and `textual==8.2.8`) answers 3781 passed,
138 skipped, 5 failed and 1666 subtests passed, in 2583.47 s (43 min) — measured 2026-11-24 on this checkout, after upstream v0.2.20
was merged into it and the fork's test fixes were in.

The merge brought 18 failures. Thirteen are closed by this checkout's own test edits, five are left. The thirteen:

| File | Cases | What the test change does |
|---|---|---|
| `test_update_with_model.py` | 9 | its harness never asked for an engine, and this checkout takes an engine from the feed only when asked (`--engine`, or `STRATA_ENGINE_FROM_FEED=1`), so the engine step was skipped and none of the model questions ran. `ModelCase.setUp` asks for the step the way `test_updater.py`'s `Cli` does. The default path, where the engine is not taken from the feed, is checked there by `test_the_engine_step_is_skipped_by_default` |
| `test_launch_stale_engine_update_failed.py` | 3 | rewritten for this fork's rule: a launch never spawns `strata update`, and the engine that runs is the package built in the checkout, so the stale-engine answer names `tools/packaging/build_engine_win_local.py` as the next step |
| `test_launcher_heal.py` | 1 | the launcher header names `bin\strata-direct.ps1`, the file this checkout ships |

The five left fail on a clean upstream v0.2.20 checkout on this PC as well (checked 2026-11-24 in `E:\LocalTemp\opencode\pristine-v0220`,
upstream commit `a34979fc9`), so none of them comes from a fork change:

| File | Case | Why it fails here | Clean v0.2.20 |
|---|---|---|---|
| `test_private_files.py` | `ElevatedRunLockoutTests::test_default_dacl_allows_reopening_child_process_for_qos` | the case runs its script in a child Python, and that child has no path to the checkout's `terminal` folder: the `.venv` has no `.pth` file and the case sets no `PYTHONPATH`, so the child answers `ModuleNotFoundError: No module named 'strata_control'` | yes |
| `test_test_start_missing_library.py` | `BinarySmoke::test_the_default_test_start_with_no_model_keeps_the_log` | `updater.default_test_start` reads `component.id`, and the case's `SimpleNamespace` for the component gives none: `AttributeError` (upstream's own test bug, not a fork one) | yes |
| `test_tui2_023_install.py` | `WindowsInstaller::test_a_folder_with_accented_letters_is_unpacked` | the installer unpacks inside this PC's temp folder, and Windows refuses that path: "Cannot extract through symlink `\\?\C:\Users\karam\AppData\Local\Temp`", so nothing is installed and the "Installed. Run: strata launch" line never appears | yes |
| `test_tui2_023_install.py` | `WindowsInstaller::test_the_whole_cause_is_printed_not_the_first_six_lines` | the same refusal: the cause that is printed is the unpack error, so the fixture's last line is missing from the output | yes |
| `test_update_bat_stub.py` | `Run::test_without_the_stub_the_old_file_ends_with_the_error_and_exit_1` | `cmd` prints "The batch file cannot be found." in the console's code page (this PC is a Russian Windows, OEM code page 866), and the case reads the child's text as cp1251, so the ASCII sentence is not found in `stderr` | yes |

None of the seven rows the old table listed fails any more. The six files it named that are in this checkout — `test_bundle_imports.py`,
`test_bench_standard.py`, `test_launch_tui.py`, `test_discovery.py`, `test_shared_models_folder.py`, `test_no_acquisition.py` (248 cases) —
pass in this run, and they pass in the clean v0.2.20 checkout too (248 passed, 9 subtests, no failures). `test_launch_dsv41.py` is not in
this checkout, and not in upstream v0.2.20: the row cannot fall any more. The checkout facts the old rows blamed — the launch files in the
checkout's root, the missing llama.cpp tree, this PC's default folder — do not break these cases any more.

The groups that check the engine step of the launch and of `strata update` pass whole: `test_launch_engine_first.py` (20),
`test_updater.py` (56), `test_round8_fixes.py` (26), `test_no_acquisition.py` (12), `test_component_start.py` (8),
`test_make_guard.py` (22), `test_profiles.py` (21), `test_launch_monitor.py` (11), `test_issues_022.py` (33) and
`test_update_with_model.py` (40).

## The engine parity tests (ctest)

The fork's build script `tools/packaging/build_engine_win_local.py` builds the engine: its `engine` step compiles `strata.exe`
and `strata_kq.dll`, the `vision` step the picture reader `strata-vision.exe`. It builds no test executable, so a `ctest` run
in `build-c13-win` straight after that script answers `0% tests passed, 88 tests failed out of 88` — every test is "Not Run",
because none of the test executables is in the folder (measured 2026-11-24: `ctest -N` lists 88 tests and prints
"Could not find executable …" for every one of them).

The 88 tests come from `engine/cmake/strata_gpu_engine.cmake`, which registers them when CUDA is on. The folder's cache has
`STRATA_BUILD_TESTS=OFF` (read from `build-c13-win/CMakeCache.txt`, 2026-11-24), and its generator is Ninja: a full build of the
folder with `ninja -k 0 -j 24` inside the vcvars64 environment builds the test executables anyway. `cl.exe` needs vcvars, and
`cmake`, `ctest` and `ninja` here are this checkout's `.venv/Scripts` copies.

A full build of `build-c13-win` (measured 2026-11-24): `build.ninja` has 108 executable targets, 100 of them built. `strata.exe`,
the libraries and `strata-device.exe` — the binary of `cuda_device_selftest` — build. The tests need 76 unique executables, and
73 of them build. Eight targets do not build; three of them are test executables:

| Target | Why it does not build (the message as printed) |
|---|---|
| `flag_order_test.exe` | `flag_order_test.cu(31): error: identifier "__ATOMIC_SEQ_CST" is undefined` and `identifier "__atomic_load_n" is undefined`: the test uses GCC's atomic builtins, which nvcc with the MSVC host compiler has none of |
| `gemm_w16_parity.exe` | `gemm_w16_parity.cu(101): error: identifier "setenv" is undefined`: the file calls POSIX `setenv` with no `_WIN32` branch. The fork's pattern is `_putenv_s` (`engine/src/kernels/hc_persist_parity.cpp`, `engine/src/prefill/kva_test.cpp`) |
| `pinned_shared_win_test.exe` | link fails: `strata_core.lib(pinned.cu.obj) : error LNK2019: unresolved external symbol strata::kernels::cpu::expert_layout()`, referenced in `numa_arena_usable`; `fatal error LNK1120: 1 unresolved externals`. The link line has no CPU-expert library |

The same `LNK2019` takes down three binaries that are not tests: `strata-load.exe`, `strata-overlap.exe`, `strata-concurrent.exe`.
Two more targets fail for the same kind of reason and are not tests either: `dflash2_ring_test.cpp(190)` and `(193)` —
`error C3861: setenv: identifier not found` and `unsetenv`, and `q8_gemv_bench.cpp` —
`native_mmvq.hpp(115): error C4430` and `error C2143` (MSVC reads a GCC-only construct in the header).

All eight are fixed in this checkout (measured 2026-11-24, the same folder, the same command). The fixes are the fork's
own pattern, not new machinery: `engine/tests/core/flag_order_test.cu` takes the `_InterlockedCompareExchange` branch
that `include/strata/core/flag_order.hpp` already uses; `engine/src/prefill/gemm_w16_parity.cu` and
`engine/src/core/dflash2_ring_test.cpp` map `setenv`/`unsetenv` to `_putenv_s` under `_WIN32`, as
`engine/src/kernels/hc_persist_parity.cpp` does; `engine/cmake/strata_gpu_engine.cmake` adds
`$<TARGET_NAME_IF_EXISTS:strata_kernels_cpu>` to the four link lines (the pattern from
`engine/src/split/CMakeLists.txt:112`; the target is made in `engine/sycl/CMakeLists.txt:176` when native experts are
on); `engine/include/strata/kernels/native_mmvq.hpp` declares `int8_t` and only included `<cstddef>`, so it gets
`<cstdint>`. A full build of `build-c13-win` then builds 108 of 108 executable targets, all 76 test executables, exit 0.
The ctest run that follows answers **86 Passed, 1 Skipped, 2 Failed, 0 Not Run** — `98% tests passed, 2 tests failed out
of 88` in 413.91 s. `kv_window_parity`, which failed in the first run (23.26 s, 11 `FAIL int8` lines), passed in this
run: it is the one parity test whose result moved between runs, and the model was up both times.

The run after the full build, with this checkout's `.venv/Scripts/ctest.exe --test-dir build-c13-win` (measured 2026-11-24):
**81 Passed, 1 Skipped, 3 Failed, 3 Not Run**, and `ctest` answers `93% tests passed, 6 tests failed out of 88` (it counts a
"Not Run" as a failure; the Skipped test is not counted). `Total Test time (real) = 422.88 sec`.

The three that Failed:

| Test | Time | What it printed |
|---|---|---|
| `gr_parity` | 0.61 s | its first line is `q8 sync: an illegal memory access was encountered`; every check it prints passes ("real dims 2560/4/320 BF16 pass", "… FP32 pass", "native pinned GR pass", "fused GR read + q8_1 (STRATA_QFUSE), T 1..8, direct and graph replays: pass"), and the exit code is nonzero |
| `kv_window_parity` | 23.26 s | 11 `FAIL int8` lines (`streamed attention differs (140, 2)`, `ring attention differs (140, 2)`, … `(1459, 6)`) and `FAIL int8: a host copy differs from the resident reference (4, 0)`; its counters end with `int8 graph : … host/ring-host bad 4` |
| `ple_parity` | 0.01 s | `ple_parity: required PLE table is missing or incompatible: ../../Q2_0/Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00002-of-00002.gguf (cannot open …)`: the test needs a Q2_0 expert pack that is not in this tree |

The Skipped test is `pinned_alias_test`: `pinned_alias_test: SKIP (Windows HIP only)`, return code 77. Seventeen tests are
registered with `SKIP_RETURN_CODE "77"` (read from `CTestTestfile.cmake`, 2026-11-24); in this run one used it.
`cuda_device_selftest` (`strata-device.exe --selftest`) passed.

The GPU part: 73 of the 76 test executables link the CUDA runtime (read from `build.ninja`, 2026-11-24), so they need a GPU; the
three CPU-only ones are `auto_cache_test`, `ple_residency_test`, `qwen35_sampling_test`. The serving model is up here
(`IQ2_XS low_ram` on 127.0.0.1:8080) and holds about 14.3 GiB of the card's 16 GiB, so about 1.6 GiB is free — measured
2026-11-24, the model was not stopped for any of this. No test printed an out-of-memory message (searched
`build-c13-win/Testing/Temporary/LastTest.log`, 2026-11-24): the only CUDA error line in the whole run is `gr_parity`'s
"an illegal memory access was encountered", and its checks all pass. The GPU parity tests run on synthetic data in a small
buffer, so 1.6 GiB of free memory is enough for them. None of the 88 tests starts the engine or loads a model, and none needs
the model stopped; the one test that wants a model file (`ple_parity`) fails by saying the file is missing, not by touching the
serving model.

What to do: run `ctest` after a full build of the build folder. `tools/packaging/build_engine_win_local.py` builds the engine
and the reader only, and its folders have no test executables, so a `ctest` run there answers 88 failures out of 88 and says
nothing about the engine. A full build needs vcvars for `cl.exe`; the build does not stop the serving model, and the run is done
with the model up.
