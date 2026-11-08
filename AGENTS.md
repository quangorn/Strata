# AGENTS.md

This is the **quangorn/Strata** fork of `Niko1221/Strata` (remote `upstream`). This PC runs its own engine, its own
launch code, and one update routine: nothing here downloads an engine, every package is built on this PC.
Version 0.2.12, engine 0.1.58 (the merged upstream line, taken 2026-11-08). The public model families are
DeepSeekV4.1-Flash-1017, Qwen3.8-Flash-Next and Qwen3.6-35B-A3B; this PC serves Qwen3.8-Flash-Next (`IQ2_XS low_ram`).
Read **[docs/AI_REFERENCE.md](docs/AI_REFERENCE.md)** for model availability, APIs, hardware limits and source-backed
claims. Read **[docs/LOCAL_FORK_CHECKS.md](docs/LOCAL_FORK_CHECKS.md)** before changing `serve/server.py`,
`strata_cli/launch/runner.py` or `strata_control/updater.py`: those checks need real restarts and are not in the
offline suite.

## This PC

- RTX 4080 (sm_89), driver 610.88, CUDA Toolkit 13.4, Visual Studio 2022 Build Tools (MSVC 14.44.35207), 48 GB RAM,
  Cygwin bash as the daily shell.
- `strata` on PATH is this checkout's launcher (`bin/` is PATH's first entry). The serving model is `IQ2_XS low_ram`;
  its launch files (`strata-swift-iq2_xs.json` and two more) sit in the checkout root, so `Catalog.root` is the checkout.
- `.venv` in the checkout is the Python for setup, tests and packaging; `cmake` and `ninja` used by the packaging
  scripts come from it. The Cygwin `/usr/bin/cmake` is **not** a native build: it cannot make VS solutions, and ninja
  is not on the Cygwin PATH.

## Building the engine

The canonical path is `python3 packaging/build_engine_win_local.py` from cygwin bash (any Python 3.10+):
`plan` shows what it would do, `engine` builds only `strata.exe` + `strata_kq.dll`, `vision` the picture reader,
`package` the component package. A running server does not block compilation: `engine` and `vision` write only to the
build folders. The package step writes a folder named by version, and `package_engine.py` refuses an existing one
("packages are immutable") — and a folder with `strata.exe`/`strata-vision.exe` running inside cannot be removed on
Windows. So packaging needs a fresh version, never a stop, and compilation needs neither: no `strata launch --stop`
before the build. The fork's version rule: a package build never changes the engine's compiled-in version string
(`STRATA_VERSION`, from `CMakeLists.txt` + `VERSION_SUFFIX`), because it is part of the conversation identity: the disk
tier matches every saved index by model + settings fingerprint, which includes `engine_version`
(`generate.cpp:10507`), and a start deletes every index whose identity differs (`conversation_disk.cpp:210-211`,
`247-248`). Measured 2026-11-02: bumping `project(VERSION …)` to `0.1.55.1` emptied the disk store of all saved
conversations at the next start. Put the build number in `VERSION` only — `VERSION=0.1.55.1 python3 …` names the
package folder and the manifest, `resolve()` picks `0.1.55.1-local` over `0.1.55-local` by the manifest version,
and the engine keeps naming itself `0.1.55-local` so the disk store survives. The exception is a merge of upstream's
release line: taking upstream's `project(VERSION …)` bump is the only case where the engine string changes, and it
empties the store on purpose. Pinning the old string over changed kernels would let indexes written by the old engine
be read back by the new one, which is a data risk, not a versioning rule. The merge of `upstream/main`
(v0.2.12, engine 0.1.58) on 2026-11-08 takes the bump, so the store is expected to drop every index at the first
start on `0.1.58-local` and re-warm; this is static analysis until check 2 of `docs/LOCAL_FORK_CHECKS.md` is run on
the merged tree, and its measured numbers go in that file. The next model
start takes the new engine; restart the preset when the work
is done, not during the build. Environment knobs, all optional: `ARCHS` (default 89 = this card; the release
cuda13 list is `75;86;89;120`), `BUILD_JOBS` (default 24; a 24-job build of this tree needed at most 4 GB RAM),
`VCVARS_VER` (CUDA 13.4 accepts MSVC `_MSC_VER` below 1960, so this PC's toolset 14.44.35207 compiles without it —
set the knob only for a toolkit that rejects it), `VERSION_SUFFIX` (default `-local`: a forked build names itself
`strata 0.1.58-local`), `VERSION` (the build number: package folder and manifest only, never bump the engine
string on a package build). Build folders are kept
between runs; delete one when its generator or cached flags change. `STRATA_PORTABLE=ON` is the release-shape
build (AVX2 floor, static MSVC runtime), repacked over the local package with `packaging/repack_local_portable.sh`.
Do not add `--use_fast_math` to `EXTRA_FLAGS` for a whole build: it changes rounding outside the kernel files.

A hand CMake build (no packaging): use a native Windows CMake, the VS Build Tools copy works
(`C:/Program Files (x86)/Microsoft Visual Studio/2022/BT2022/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe`,
3.31.6, `ninja.exe` beside it):

```sh
cmake -S . -B build-cuda -G "Visual Studio 17 2022" -A x64 \
      -DSTRATA_ENABLE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=native
cmake --build build-cuda --config Release --parallel
```

Measured on this PC (2026-11-02): 96 executables, exit 0. The engine is `build-cuda/Release/strata.exe` (without
`STRATA_VERSION_SUFFIX` it names itself plain `0.1.55`); the test executables sit in the same folder and `ctest`
runs them. A `--parallel` VS build leaves its MSBuild node-reuse workers alive after the build ends — kill the
stray `MSBuild.exe` once the build is done. CUDA kernels compile for `native` (sm_89 here); the guard in
`CMakeLists.txt` refuses anything below sm_75.

MSVC has no `setenv`/`unsetenv`: tests that set the environment use the `#if defined(_WIN32)` + `_putenv_s`
pattern (`src/prefill/kva_test.cpp`). Follow it in new tests. Warnings C4244 and C4996 (`strerror`) are tolerated,
`STRATA_WERROR` is off by default.

## Deploying the fork's code

`python tools/deploy_local_code.py` copies the fork's launch and control files into the active component folder.
A running server keeps the code it loaded, so deploy first, then stop and start the model.

## Tests

- The Terminal and Control suite: `./.venv/Scripts/python.exe -m pytest tests/terminal -q` runs without a GPU,
  without downloads and with a serving model stopped. On this PC: 2034 passed, 62 skipped, 11 known failures
  (listed with their causes in `docs/LOCAL_FORK_CHECKS.md`).
- Setup's own tests run without a GPU or downloads: `python tools/test_setup_<name>.py` (for example
  `tools/test_setup_amd.py`, `tools/test_setup_choices.py`).
- The app's UI tests: `npm test` in `apps/host`. Engine parity tests: `ctest` in the build folder.
- How the engine works, every measured number, the API and all settings: [docs/DETAILS.md](docs/DETAILS.md) and
  the [paper](docs/paper/Strata-Paper.pdf). The Terminal: [docs/TERMINAL.md](docs/TERMINAL.md) and
  [docs/TERMINAL_LAUNCH.md](docs/TERMINAL_LAUNCH.md). Packaging and the update feed:
  [packaging/README.md](packaging/README.md) and [packaging/UPDATES.md](packaging/UPDATES.md).

## Contributing a change or report

The rules are in **[CONTRIBUTING.md](CONTRIBUTING.md)**; read it before you open an issue or a pull request, the
fork sends changes upstream. The short version:

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
