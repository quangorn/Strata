# AGENTS.md

This is the **quangorn/Strata** fork of `Niko1221/Strata` (remote `upstream`). This PC runs its own engine, its own
launch code, and one update routine: nothing here downloads an engine, every package is built on this PC.
Version 0.2.21, engine 0.1.67. The public model families are Qwen3.8-Flash-Next and Qwen3.6-35B-A3B. Read
**[docs/AI_REFERENCE.md](docs/AI_REFERENCE.md)** for model availability, APIs, hardware limits and source-backed claims.
Read **[docs/LOCAL_FORK_CHECKS.md](docs/LOCAL_FORK_CHECKS.md)** before changing `terminal/serve/server.py`,
`terminal/strata_cli/launch/runner.py` or `terminal/strata_control/updater.py`: those checks need real restarts and
are not in the offline suite.

## Layout (upstream 0.2.21)

Upstream split the repository into four trees; the fork's checkout is the same shape:

- `engine/` — the C++/CUDA/HIP/SYCL/Metal engine: `engine/CMakeLists.txt`, `engine/src`, `engine/include`,
  `engine/metal`, `engine/sycl`.
- `terminal/` — the Terminal and the server: `terminal/strata.py`, `terminal/strata_cli`, `terminal/strata_control`,
  `terminal/serve`, `terminal/bin` (the upstream launchers), `terminal/tests`.
- `app/` — the desktop app (`app/desktop`, `app/host`); `npm test` runs in `app/host`.
- `tools/` — packaging and helper scripts: `tools/packaging`, `tools/check`, `tools/layout`, `tools/bench`.

The fork's own files live inside these trees: `tools/packaging/build_engine_win_local.py`,
`tools/packaging/stamp_local_build.py`, `tools/packaging/repack_local_portable.sh`,
`tools/deploy_local_code.py`, `tools/rebuild_engine.sh`, `docs/LOCAL_FORK_CHECKS.md`, and the root `bin/` launcher
layer that points at `terminal/` (`strata`, `strata.cmd`, `strata.ps1` + `strata-root.txt`).

## This PC

- RTX 4080 (sm_89), driver 610.88, CUDA Toolkit 13.4, Visual Studio 2022 Build Tools (MSVC 14.44.35207), 48 GB RAM,
  Cygwin bash as the daily shell.
- `strata` on PATH is this checkout's launcher (`bin/` is PATH's first entry). The serving model is `IQ2_XS low_ram`:
  its run config, tuning and log are `%LOCALAPPDATA%\Strata\launch\iq2_xs-*`, its model, pack and picture files are
  `C:\Projects\Strata-data` beside the checkout, and it runs on the engine package in `components\engine`. The Inkling-2-Mini
  run config `strata-inkling-2-mini-sq-15.json` sits in the checkout root; that model's engine is the shape component
  `components\shapes\inkling2\engine`, which is taken from the feed (`STRATA_SHAPE_ENGINE_FROM_FEED=1`), because a local build
  of it cannot be installed: `strata-inkling` built in the tree names itself `0.1.67` without the `-local` suffix and answers
  no `--geometry` flag, and an engine that cannot name its shape is refused at install (measured 2026-11-24).
- `.venv` in the checkout is the Python for setup, tests and packaging; `cmake` and `ninja` used by the packaging
  scripts come from it. The Cygwin `/usr/bin/cmake` is **not** a native build: it cannot make VS solutions, and ninja
  is not on the Cygwin PATH.

## Building the engine

The canonical path is `python3 tools/packaging/build_engine_win_local.py` from cygwin bash (any Python 3.10+):
`plan` shows what it would do, `engine` builds only `strata.exe` + `strata_kq.dll`, `vision` the picture reader,
`package` the component package. A running server does not block compilation: `engine` and `vision` write only to the
build folders. The package step writes a folder named by version, and `package_engine.py` refuses an existing one
("packages are immutable") — and a folder with `strata.exe`/`strata-vision.exe` running inside cannot be removed on
Windows. So packaging needs a fresh version, never a stop, and compilation needs neither: no `strata launch --stop`
before the build.

The fork's version rule: the engine's compiled-in version string (`STRATA_VERSION`, from `engine/CMakeLists.txt` +
`STRATA_VERSION_SUFFIX`) is part of the conversation identity. The parked conversation-disk tier matches every saved
index by model + settings fingerprint, which includes `engine_version`, and a start deletes every index whose identity
differs. Measured 2026-11-02: bumping `project(VERSION …)` to `0.1.55.1` emptied the disk store of all saved
conversations at the next start. Put the build number in `VERSION` only — `VERSION=0.1.67.1 python3 …` names the
package folder and the manifest, `resolve()` picks `0.1.67.1-local` over `0.1.67-local` by the manifest version,
and the engine keeps naming itself `0.1.67-local`. Upstream's 0.2.21 raised `project(VERSION)` to `0.1.67` itself,
so the merge moved the engine string and the package with it: the build of this checkout names itself `0.1.67-local`
and its package is `0.1.67-local-windows-x86_64-cuda-cuda13` (measured 2026-11-24: `strata.exe --version`, and the
package verified by `package_engine.py --verify`). The next model start takes the new engine; restart the preset when
the work is done, not during the build. (The disk tier and the conversation-cache rules it depends on are parked on
`fork/conversation-cache-disk`; until they are ported, the identity rule protects nothing on disk.)

Environment knobs, all optional: `ARCHS` (default 89 = this card; the release cuda13 list is `75;86;89;120`),
`BUILD_JOBS` (default 24; a 24-job build of this tree needed at most 4 GB RAM), `VCVARS_VER` (CUDA 13.4 accepts MSVC
`_MSC_VER` below 1960, so this PC's toolset 14.44.35207 compiles without it — set the knob only for a toolkit that
rejects it), `VERSION_SUFFIX` (default `-local`: a forked build names itself `strata 0.1.67-local`), `VERSION` (the
build number: package folder and manifest only, never bump the engine string). Build folders are kept between runs;
delete one when its generator or cached flags change — and when its source folder moves, because the cache records
where the source is: `build-c13-win`'s cache came from the pre-split root `CMakeLists.txt`, and `build-vision-cuda13-win`
refused to reconfigure after the picture reader moved to `terminal/tools/vision` (measured 2026-11-24: both answer
"the source … does not match the source … used to generate cache", and the folders are `build-0.1.67-win` and a fresh
vision folder now). `STRATA_PORTABLE=ON` is the release-shape build (AVX2 floor, static MSVC runtime), repacked over
the local package with `tools/packaging/repack_local_portable.sh`.
Do not add `--use_fast_math` to `EXTRA_FLAGS` for a whole build: it changes rounding outside the kernel files.

A hand CMake build (no packaging): use a native Windows CMake, the VS Build Tools copy works
(`C:/Program Files (x86)/Microsoft Visual Studio/2022/BT2022/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe`,
3.31.6, `ninja.exe` beside it):

```sh
cmake -S engine -B build-cuda -G "Visual Studio 17 2022" -A x64 \
      -DSTRATA_ENABLE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=native
cmake --build build-cuda --config Release --parallel
```

The engine is `build-cuda/Release/strata.exe` (without `STRATA_VERSION_SUFFIX` it names itself plain `0.1.67`); the
test executables sit in the same folder and `ctest` runs them, with `-DSTRATA_BUILD_TESTS=ON`: some tests are registered
by a subfolder's `CMakeLists.txt` (`engine/src/inkling2/CMakeLists.txt:270` registers `sk_tier_test` in `src/inkling2/`)
and a tests-OFF cache registers the test without building its executable, which ctest answers "Not Run" (measured
2026-11-24). A `--parallel` VS build leaves its MSBuild node-reuse workers alive after the build ends — kill the stray
`MSBuild.exe` once the build is done. CUDA kernels compile for `native` (sm_89 here); the guard in
`engine/CMakeLists.txt` refuses anything below sm_75.

MSVC has no `setenv`/`unsetenv`: tests that set the environment use the `#if defined(_WIN32)` + `_putenv_s`
pattern (`engine/src/kernels/hc_persist_parity.cpp`, `engine/src/prefill/kva_test.cpp`). Follow it in new tests.
Warnings C4244 and C4996 (`strerror`) are tolerated, `STRATA_WERROR` is off by default.

## Deploying the fork's code

`python tools/deploy_local_code.py` copies the fork's launch and control files (`terminal/serve/server.py`,
`terminal/strata_cli/**`, `terminal/strata_control/**`) into the active component folder. A running server keeps the
code it loaded, so deploy first, then stop and start the model.

`strata update` takes Control from the signed feed and skips the engine (the package built in the checkout is the one
that runs). `STRATA_SHAPE_ENGINE_FROM_FEED=1` opens the model-shape engines: `strata update` brings an installed shape
engine (Inkling-2-Mini, Hy4-Flash) to the newest of the feed beside the older one, in `components/shapes/<shape>/engine/`,
without touching the Strata engine (`terminal/strata_cli/update_cmd.py:feed_steps`). This is the only way to get a shape
engine on this PC: a local build of one cannot be installed. `--engine` opens both steps, and on this PC it fails on the
version rule ("`0.1.67-local` is not a version number"), which is the known limit, not a bug to fix here.

## Tests

- The Terminal and Control suite: `./.venv/Scripts/python.exe -m pytest terminal/tests/terminal -q` runs without a
  GPU, without downloads and with a serving model stopped. On this PC (measured 2026-11-24, after the upstream
  0.2.20 merge): 3781 passed, 138 skipped, 1666 subtests passed in 2583 s, with 5 failures. All five fail on a clean
  upstream v0.2.20 checkout on this PC too; their causes are listed in `docs/LOCAL_FORK_CHECKS.md`.
- Setup's own tests run without a GPU or downloads: `python terminal/tools/test_setup_<name>.py` (for example
  `terminal/tools/test_setup_amd.py`, `terminal/tools/test_setup_choices.py`).
- The app's UI tests: `npm test` in `app/host`. Engine parity tests: `ctest` in the build folder.
- How the engine works, every measured number, the API and all settings: [docs/DETAILS.md](docs/DETAILS.md) and
  the [paper](docs/paper/Strata-Paper.pdf). The Terminal: [docs/TERMINAL.md](docs/TERMINAL.md) and
  [docs/TERMINAL_LAUNCH.md](docs/TERMINAL_LAUNCH.md). Packaging and the update feed:
  [tools/packaging/README.md](tools/packaging/README.md) and [tools/packaging/UPDATES.md](tools/packaging/UPDATES.md).

## Contributing a change or report

The rules are in **[.github/CONTRIBUTING.md](.github/CONTRIBUTING.md)**; read it before you open an issue or a pull
request, the fork sends changes upstream. The short version:

- Start every issue and pull request title with its area: `[Engine]`, `[GUI]`, `[TUI]`, `[Benchmark]`, `[Docs]`
  or `[Other]`. A check on GitHub fails a pull request whose title has no area.
- Search the open and closed issues and pull requests first, and add to a thread that already covers your point.
- One problem per pull request. Lead with the problem and the result, then the approach, then the validation
  (passed, failed and not run, kept apart) and the limits.
- A new feature is opt-in, and the default path stays byte-identical to the last release.
- Never submit measurements, logs or reproduction steps you did not produce. Mark a finding from reading code as
  unconfirmed static analysis.

## Style

Keep the docs' style: plain words, short sentences, written for non-technical users first. Give measured numbers
with what they were measured on. Make no claim without a measurement.