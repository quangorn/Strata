# What is left to check

The state on 2026-11-08, after the merge of `upstream/main` (v0.2.12, engine 0.1.58) into
`engine/conversation-disk-store` (pushed to `origin` as `df5cc636`): the engine package
`0.1.58-local-windows-x86_64-cuda-cuda13` is built and serves (the package serving now is
`0.1.58.1-windows-x86_64-cuda-cuda13`, started 16:03); `tools/deploy_local_code.py` copied the fork files
into `components\control\0.1.56` (six, then two re-runs on 2026-11-08 for `serve/server.py`'s 120 s turn report and
the take-back guard and the new `serve/test_turn_wait_recovery.py` — seven), then **63** files on 2026-11-08 after the deploy's
diff base was changed to the install's own `COMMIT` (`c5ab5d40`, 312 commits behind `upstream/main`), and **20** more after the
rule became a copy of **this checkout's whole tree of the parts** — the parts of `components\control\0.1.56` are now byte for
byte the checkout (identical 318, differ 0, missing 0, install-only 0). With `upstream/main` as the base, every file upstream
added after that release is invisible to the diff and never copied, and the launch code that IS copied imports it — the model
start died with
`ImportError: cannot import name 'arg_layers' from 'strata_control'` (`strata_cli/launch/runner.py:546`; row 11 of
`LOCAL_FORK_CHECKS.md`). With the `COMMIT` base the launch got further and died at
`AttributeError: module 'strata_control.gpu_fit' has no attribute 'strix_q4_stall_note'`. The daily launch runs
from this checkout (`bin\strata`, the alias in `~/.bashrc`), so a model started from the shell runs the checkout's code, and
the server that runs is the code it loaded at its start: a restart is what starts the deployed files. The offline suite answers 30 failed, 2777
passed, 78 skipped, 1 error
and 407 subtests (28 of the failures are conditions of this PC; two are load, see `LOCAL_FORK_CHECKS.md`). The
app's UI tests answer 1505 passed, 0 failed, 110 skipped.

Everything below needs either a real stop/start of the model, or the model stopped for a while. Nothing here is
run by the offline suite. Rows 1, 3 (its last part) and 4 were measured on 2026-11-08 at 20:35-20:40 by the restart
`strata launch --stop` then `strata launch --preset "IQ2_XS low_ram" --no-tui` (ready in 15 s); the rest still need
a run. Say which row to run.

## Checks that need a stop/start of the model (`docs/LOCAL_FORK_CHECKS.md`)

| Row | Command | What must be seen | State |
|---|---|---|---|
| 1 | `strata launch --stop`, then `strata launch --preset iq2_xs-low_ram --no-tui` | The server starts from `components\control\<version>\python\python.exe -m serve.server` **with the fork's files in it** — the deploy is done (83 files copied on 2026-11-08: 63 with the `COMMIT` base, then 20 to make
the parts byte for byte the checkout), so this row is one restart away from being measured. Note the launcher: the shell's alias runs this checkout, so a shell restart measures the checkout's code, not `components\control\<version>`; the shape named here needs a start from the app/terminal (the deploy is in that folder, and its imports and its `launch --plan` are measured — its model start is not run) | measured 2026-11-08 20:36, with the gap this row names: the restart ran and the server came up from `.venv\Scripts\python.exe -m serve.server …` (base interpreter `…\Python312\python.exe`), cwd `C:\Projects\Strata-fork`, `exe: component:strata-engine`; no process from `components\control\0.1.56` and no `strata update`. The `components\control\<version>` shape still needs a start from the app/terminal |
| 4 | `strata launch --preset iq2_xs-low_ram --plan`, then the start | No engine download line, `strata update` never spawned by the launch, no request to the feed for an engine archive in the server's network log | measured 2026-11-08 20:35-20:38: `--plan` rc=0 ("Nothing to download: the model is already on this computer."), the start prints no download line, no `strata update` in the process list, and the 16.6 MB server log matches no `download`/`update`/`feed` string |
| 5 | Delete the newest engine package folder, keep an older one, start | One warn line naming `packaging/build_engine_win_local.py` as the next step, and the start goes on with the older engine; no `strata update` spawned, no refusal | not run on the merged tree |
| 7 | `strata update` after a feed has a newer Control, then `python tools/deploy_local_code.py`, then the start | A new `components\control\<version>` folder appears, the fork files are copied into it, `active.json` selects it, and the start uses it | not run on the merged tree |
| 9 | `strata update --engine --check` | Exit 5, and the version rule refuses `0.1.58-local` for the same reason as `0.1.55-local` (the feed pattern reads digits only). The `0.1.55-local` half is measured 2026-11-02; the 0.1.58 half is static analysis | not run on 0.1.58 |
| 3 (last part) | `strata launch --preset iq2_xs-low_ram --no-tui` with pictures on, then one picture request | A picture request answers through `serve/server.py`'s `with_current_reader`, the reader process from `0.1.58-local…\strata-vision.exe`. The process following the package is measured; the picture request itself was never sent | measured 2026-11-08 20:40: one picture request answered ("A red circle, a blue square, and a green triangle"; 327 prompt tokens, 96 generated) and the reader process is `…\0.1.58.1-windows-x86_64-cuda-cuda13\strata-vision.exe`, which the run config already names (so `with_current_reader` had nothing to move and printed nothing) |

## The full engine ctest

`ctest -C Release` in `build-cuda`, with **the model stopped** (the native `ctest.exe` beside the VS Build Tools
`cmake.exe`; Cygwin's `/usr/bin/ctest` breaks every path into `BAD_COMMAND`). 87 entries.

- The partial run of 2026-11-08, with the model serving, reached 82 of 87: 78 passed, 2 failed, 1 skipped
  (`pinned_alias_test`). Of the two failures only `ple_parity` is real — its fixture path
  `../../Q2_0/Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00002-of-00002.gguf` is not on this PC; `ple_reader_selftest`
  passes alone (100% passed, 0 of 1 failed, 351.87 s). The last five entries were never run.
- With the model stopped, `ctest -R "conversation|session_save"` answered 11 of 11 on 2026-11-05 (measured,
  pre-merge). Re-run it on the merged tree: `conversation_snapshot_test` measures free VRAM and fails run to run
  with a model serving.
- `gdn_rec_parity` with `--long` needs 6.75 GiB of the card: with the model stopped its new guard should let it
  run; with the model serving it should print `needs … on the card, … free - skipped` and exit 77. Neither case has
  been measured — only the RAM skip of `gemm_w16_parity` (8.48 GiB needed, 4.20 GiB free, exit 77) and the
  default run of `gdn_rec_parity` (4.27 GiB, ran, 0 failures) are measured.

## The suite's moving rows

- `test_bench_standard.py`'s three request-outcome cases: the pair moves with the network this PC sees
  (2026-11-02: one case; 2026-11-08: all three). Re-check on a quiet machine and a quiet network, and write the
  row in `LOCAL_FORK_CHECKS.md` with the date.
- `test_pictures.py`'s path-key case (`models\…` against `models/…`) fails from Windows separators in the
  fixture's comparison, not from the code. The fix belongs upstream (change the fixture to compare `Path` parts);
  not done here, not reported.

## Git bookkeeping, not a check

- `main` is behind `engine/conversation-disk-store`; fast-forward it to the branch's tip when the branch is accepted.
- No upstream pull request is opened (the owner's rule, 2026-11-08). The two fixes that were listed as candidates stay
  in this branch and are not sent: `[GUI]` `apps/host/tests/markdown-crlf.test.mjs` passes its text through stdin
  instead of argv (the fork's commit `4319d013`; the app suite then answered 1505 passed, 0 failed, 110 skipped), and
  `[Engine]` the memory guard `include/strata/core/test_memory.hpp` with its use in `gemm_w16_parity` and
  `gdn_rec_parity` (the fork's commit `cfcb93a0`; `gemm_w16_parity` needs 8.52 GiB of the card and 8.48 GiB of RAM
  beside a serving model).
- The branch `server/turn-wait-recovery` is deleted (local and `origin`, 2026-11-08): its tip `0f6cc63b` is an
  ancestor of `engine/conversation-disk-store`, and the merge of upstream (`a40ef613`) kept its hunks in
  `serve/server.py`. The fix is still needed: `upstream/main` keeps no record of who holds the turn, and its #4271
  part 1 (`c08fd2ec`, in the merged tree) ends a blocked socket write, not a generator left with no thread. Its
  threshold is now 120 s (measured, `docs/DETAILS.md`), and `serve/test_turn_wait_recovery.py` covers it: 9 tests,
  no process and no GPU; on the base (`upstream/main`, `146df3c4`) the file answers 4 errors and 2 failures. A review
  of 2026-11-08 found two holes in the fix, both fixed here: the take-back was "whoever holds the turn now", so a
  live holder that took the turn between the report's look and its release lost it (the take-back is now named by the
  holder the report saw, `test_the_take_back_leaves_a_live_holder_that_came_in_between` fails without that guard),
  and the liveness check read the thread id, which Windows reuses (the keeper is now a weakref to the thread;
  measured on this PC: 0 of 200 finished threads had their id given to a live one, so this is latent, not seen).
