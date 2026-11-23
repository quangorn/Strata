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
