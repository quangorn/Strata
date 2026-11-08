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
| 1 | The deployed Python runs the server | `strata launch --stop`, then `strata launch --preset iq2_xs-low_ram --no-tui` | The server starts from `components\control\0.1.55\python\python.exe -m serve.server`; the deploy (`tools/deploy_local_code.py`) must have run first, because a running server keeps the code it loaded |
| 2 | A new engine package is the engine that serves | `VERSION=0.1.58.1 python3 packaging/build_engine_win_local.py`, then the start above | `strata components engine` names `0.1.58.1-local-windows-x86_64-cuda-cuda13`; `strata.exe --version` prints `0.1.58-local` — on the merged tree (`upstream/main`, v0.2.12) the engine string follows `project(VERSION)`, so the first start drops every index in the disk store (the identity check, `generate.cpp:10507`) and the long prompts re-warm; the manifest check passes (`strata components engine --full`, no SHA-256 complaint). Measured on the merged tree: run this row, then write the numbers here |
| 3 | The picture reader follows the package | `strata launch --preset iq2_xs-low_ram --no-tui` with pictures on | `serve/server.py` (`with_current_reader`) points the reader at `strata-vision.exe` inside the new package folder, not at a copy elsewhere; a picture request answers |
| 4 | Nothing downloads an engine | `strata launch --preset iq2_xs-low_ram --plan`, then the start | No download line for the engine, and `strata update` is never spawned by the launch; the network log of the server process shows no request to the feed for an engine archive |
| 5 | A stale engine is warned about, not fetched | Delete the newest package folder, keep an older one, start | One warn line: "older than this model needs (…)", naming `packaging/build_engine_win_local.py` as the next step, and the start goes on with the older engine. No `strata update` is spawned, no refusal |
| 6 | No engine at all is a warning, not a refusal | Move the package folder away, start with `--yes` | The plain "Strata couldn't find an engine for this computer" warning, the model download (nothing to download here), exit code 0, and nothing started |
| 7 | Control update keeps the fork's files | `strata update` after a feed has a newer Control, then `python tools/deploy_local_code.py`, then the start | A new `components\control\<version>` folder appears, the four fork files are copied into it, `active.json` selects it, and the start uses it |
| 8 | The portable package serves | `packaging/repack_local_portable.sh`, then the start | The package is named with `-local`, its `BUILD.json` records the AVX2 floor and the static runtime, and the model loads on this PC |
| 9 | `strata update --engine` on a local package | `strata update --engine --check` | Exit 5, the version rule refuses the installed package. Measured 2026-11-02 on `0.1.55-local`: `'0.1.55-local' is not a version number (expected like 0.1.46 or 0.1.46.1)`. The feed pattern (`strata_control/feed.py:62`) reads digits only, so the `-local` suffix is what fails, not the build number: `0.1.55.1-local` must fail the same way (static analysis, not run). On the merged tree the same rule reads `0.1.58-local` (static analysis, not run on 0.1.58). This is the known limit, not a bug to fix here: the default path (`update --check`, `update`) must answer 0 with the "not taken from the feed" line |

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

## The offline suite on this PC

`./.venv/Scripts/python.exe -m pytest tests/terminal -q` (the `.venv` needs `pytest` and `textual==8.2.8`) answers 2036 passed
and 62 skipped, with eleven failures (measured 2026-11-02, after the engine build-number rule). Seven are upstream's: they fail
at `upstream/main` with no fork change, on this PC.

| File | Case | Why it fails here |
|---|---|---|
| `test_launch_dsv41.py` | four plan and list cases | the fixtures describe 4 AMD cards and 251 GB, and the plan answers with this PC's Windows placement rule ("on Windows a model runs on one AMD card"); one verdict is `tight` where the fixture expects `fits` |
| `test_private_files.py` | a locked file read after the repair | the ACL repair runs on a temp folder under `D:\tmp\cygwin`, and this PC locks it differently |
| `test_bundle_imports.py` | setup.py tools import inside the bundle | the checkout has no `third_party/llama.cpp` and `STRATA_GGUF_PY` is not set, so `gguf-py` is not found |
| `test_bench_standard.py` | one request-outcome case | the outcome (`connection_TimeoutError` against `timeout`) depends on the network this PC sees; the pair moves — 2026-11-02: the round-total case fails, the other passes |
| `test_launch_tui.py` | `FolderStep::test_default_folder_and_free_space_and_change` | seen 2026-11-02: the step reads this PC's default folder and free space, which is not what the fixture describes; the same nature as the checkout cases below |

Three more are this checkout, not the code. `test_discovery.py` and `test_shared_models_folder.py` expect a machine with no
Strata install visible, and this checkout answers as one: its root holds the launch files (`strata-swift-iq2_xs.json` and two
more), so `Catalog.root` is the checkout. `test_no_acquisition.py` scans the modules a start loads by their path under the
checkout's root, so a module under `.venv/Lib/site-packages` was counted as project code; that case skips `.venv` now.

None of the eleven says anything about the engine step of the launch or of `strata update`. The groups that do test them pass:
`test_launch_engine_first.py` (20 cases), `test_updater.py` (49), `test_round8_fixes.py` (25), `test_no_acquisition.py` (13),
`test_component_start.py`, `test_make_guard.py`, `test_profiles.py`, `test_launch_monitor.py` (81 together), `test_issues_022.py`
and `test_tui2_023_install.py` (34).
