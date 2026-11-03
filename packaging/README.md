# Strata packaging: targets, recipes and locks

This page is for developers who build the desktop app, the Terminal archive and the engine packages. To install Strata, use
[docs/INSTALL.md](../docs/INSTALL.md). It describes the current release (app and Terminal 0.2.5, Control and engines 0.1.50).
How packages are signed and published is in [UPDATES.md](UPDATES.md), and the package format is in [ENGINE-COMPONENT.md](ENGINE-COMPONENT.md).

Release assets show what was published; the platform guides and retained reports show what was tested. A published package does not
qualify every GPU, model or feature on its platform.

## Quick start

| I want to | See |
| --- | --- |
| Build the desktop app | [Desktop app](#desktop-app) |
| Build an engine package | [ENGINE-COMPONENT.md](ENGINE-COMPONENT.md#building-a-package) |
| Build the Terminal archive | [The Terminal archive](#the-terminal-archive) |
| Sign and publish a release | [UPDATES.md](UPDATES.md#releasing) |
| Test an update against a local feed | [UPDATES.md](UPDATES.md#testing-locally) |

## The target matrix

Each row is an OS, CPU ABI and backend. A row is `qualified` only after the acceptance checklist
([below](#what-a-platform-must-produce-before-it-is-qualified)) has run on it.

| Target | App artifact | Engine package recipe | Status |
| --- | --- | --- | --- |
| `macos-arm64-metal` | `.app` and `.dmg` (Tauri) | Metal `strata`, bundled with `strata-vision` (`packaging/build_vision_mac.sh`) | Built. Self-test passes from the mounted DMG. Inference measurements are in [MAC.md](../docs/MAC.md). Signing: `packaging/mac/sign-and-notarize.sh` ([mac/README.md](mac/README.md)). |
| `darwin-arm64` Terminal | self-contained `.tar.gz` | any | Built and verified. Runs from a moved directory with no system Python. |
| `windows-x86_64-cuda` | NSIS `.exe` (Tauri) | `cuda13` and `cuda12` | Built and tested on a Windows PC with an NVIDIA card: silent install with a System32-only PATH, a real model, an agent chat. |
| `windows-x86_64-hip` | NSIS `.exe` (Tauri) | `tools/hip/build_windows.bat` | Published. Strix Halo inference is recorded in [STRIX_HALO.md](../docs/STRIX_HALO.md). This does not qualify every discrete Radeon card ([AMD_HIP.md](../docs/AMD_HIP.md)). |
| `linux-x86_64-cuda` | AppImage (Tauri) | `packaging/build_cuda_linux.sh`, built in a glibc 2.35 chroot | `cuda13` built and tested on RTX 3060 and RTX A4000 (app in the browser, a real model, a picture). `cuda12` built. |
| `linux-x86_64-hip` | AppImage (Tauri) | `packaging/build_hip_linux.sh` | `rx9070` (gfx1200 / gfx1201) built and tested on a Radeon AI PRO R9700. `rx7900` (gfx1100 / 1101 / 1102) built, not run on an RX 7000 card. `rx6000` (gfx1030 / 1031) built, not tested on a real RX 6000 card. |
| `linux-aarch64-cuda` | none (no AppImage for arm64) | `packaging/build_cuda_linux_aarch64.sh`, built on the DGX Spark (`cuda13`, `sm_121`) | Engine packages (Qwen with `strata-dsv41` inside, and the separate DeepSeek one) and the Terminal archive built and tested on one DGX Spark ([DGX_SPARK_LINUX.md](../docs/DGX_SPARK_LINUX.md)). Not tried on any other arm64 machine. |
| `linux-x86_64-sycl` | none | `packaging/build_sycl_linux.sh` (one JIT package for Alchemist and Battlemage) | Engine package built and tested on an Arc A750. Battlemage is supported and untested by us. |
| `windows-x86_64-sycl` | none | none | Gated. No native Windows SYCL path exists upstream. |
| `macos-x86_64-*` | none | none | Unsupported. The macOS engine is Apple Silicon only. |

**DeepSeek V4.1-Flash.** The signed feed's `dsv41` member carries the DeepSeek engine `strata-dsv41` as its own package: Windows
NVIDIA `cuda13`, Linux NVIDIA `cuda13`, Linux AMD `rx9070`, and since 0.2.7 Windows AMD `rdna3` (Ryzen AI Max, gfx1151). All other
engine packages carry no DeepSeek engine. Build `strata-dsv41` with `packaging/build_dsv41_cuda.sh` (Linux) or
`packaging/build_dsv41_windows.bat` (Windows), then package it with `package_engine.py --require-dsv41`; or pack it as the separate
download (`feed_tool.py pack-engine --prefix strata-dsv41`, [UPDATES.md](UPDATES.md#the-feed)).

Windows AMD (gfx1151): build `strata-dsv41` with `tools\dsv41\win\build_hip_win.bat` (the ROCm venv, default `gfx1151`) and stage
it with the ROCm runtime the Qwen Windows `rdna3` package already carries: the HIP and C++ runtime DLLs beside the program,
`rocm/bin` (hipBLAS / rocBLAS / hipBLASLt and their imports), `rocm/bin/rocblas/library/gfx1151`,
`rocm/bin/hipblaslt/library/gfx1151`, `rocm/.kpack/blas_lib_gfx1151.kpack`, `rocm/licenses`, and a `BUILD.json` that names
`"lib_dirs": ["rocm/bin"]`. Package it with `package_engine.py --backend hip --platform windows-x86_64 --arch gfx1151 --entrypoint
strata-dsv41.exe --variant rdna3` (about 240 MiB, 280 files). Strata puts the package's `lib_dirs` on the engine's library path
(`strata_control/dsv41_engine.py lib_dirs`), so the HIP runtime is found the same way the Qwen engine finds it.

Cross-compilation is not attempted for the desktop app. Tauri bundles are per OS, and the Windows installer is built on Windows.
The Python contracts, the owner transport, the checks and the engine build recipes are portable.

## A local Windows CUDA engine (this PC)

`packaging/build_engine_win_local.py` builds the CUDA engine from this checkout on this PC and puts it in the per-user component
folder. It is the quick way to try a branch or a fix on a real model: nothing is downloaded or uploaded, and the build is kept
between runs, so a second run after a source edit recompiles only what changed.

```sh
/usr/bin/python3 packaging/build_engine_win_local.py plan     # show paths, archs and versions; build nothing
/usr/bin/python3 packaging/build_engine_win_local.py engine   # strata.exe and strata_kq.dll only
/usr/bin/python3 packaging/build_engine_win_local.py vision   # only the picture reader, into the last build
/usr/bin/python3 packaging/build_engine_win_local.py package  # the engine, the picture reader, then the package
/usr/bin/python3 packaging/build_engine_win_local.py          # all three: engine, picture reader, package
```

Run it in cygwin bash with cygwin's `/usr/bin/python3`. In cmd or PowerShell the checkout's own
`.venv\Scripts\python.exe` works instead; the cygwin shell must use cygwin Python because of the path conversion it does
(`cygpath` on `PATH` makes a Windows Python treat `/cygdrive/c/...` as a real path).

It finds Visual Studio through `vswhere` (install the Desktop development with C++ workload; `STRATA_VCVARS` names
`vcvars64.bat` by hand) and the CUDA toolkit through `CUDA_PATH` / `CUDA_ROOT`, then the newest `CUDA\v*` folder. The MSVC
environment is read once and given to cmake, ninja and nvcc, so no "x64 Native Tools" prompt is needed. The builds are
`Release` with `-DSTRATA_ENABLE_CUDA=ON`, and `BUILD_DIR` defaults to `build-c13-win`.

Settings are environment variables, all optional:

| Variable | Default | What it changes |
| --- | --- | --- |
| `ARCHS` | `89` (RTX 4080) | CUDA architectures, semicolons; the release cuda13 list is `75;86;89;120` |
| `BUILD_JOBS` | `24` | compile jobs; a 24-job build of this tree on this PC (48 GB) needed at most 4 GB |
| `BUILD_DIR` | `build-c13-win` | the build folder, kept between runs |
| `STAGE` | `<build>/stage` | where `strata.exe`, `strata_kq.dll` and `BUILD.json` are staged |
| `OUT` | `%LOCALAPPDATA%\Strata\components\engine` | where the package is written |
| `VERSION_SUFFIX` | `-local` | the suffix the engine prints (`strata 0.1.55-local`); empty reports the release version |
| `STRATA_PORTABLE` | `OFF` | `ON` builds for other PCs (AVX2 floor, static MSVC runtime) |
| `STRATA_ISA_FLOOR` | AVX2 | `avx` or `none` for a CPU without AVX2 |
| `EXTRA_FLAGS` | empty | more Release flags for C, C++ and nvcc |
| `VCVARS_VER` | newest | an older MSVC toolset, e.g. `14.42` |
| `CUDA_ROOT` / `STRATA_VCVARS` | found automatically | the toolkit / `vcvars64.bat` to use |

To test a branch without touching the installed component, point the build, stage and output at a scratch folder:

```sh
BUILD_DIR=/cygdrive/c/Projects/Strata-fork/build-c13-win \
STAGE='D:\tmp\my-branch\stage' OUT='D:\tmp\my-branch\out' VERSION_SUFFIX=-mybranch \
  /usr/bin/python3 packaging/build_engine_win_local.py engine
```

The script removes `strata.exe` before the relink, so a binary left from an older commit cannot pass for the new build, and it
runs the new `strata.exe --version` before staging. A run config can then name that binary directly in `"exe"`; the installed
`component:strata-engine` package is not rebuilt unless `package` (or the default `all`) writes into `OUT`. The script needs a
Windows PC with an NVIDIA card; Linux builds use `packaging/build_cuda_linux.sh`, and the Mac uses `packaging/build_vision_mac.sh`.

**2026-11-03, measured on this PC**: `package` used to skip both builds, so a run after a source edit packed the last
build's binaries. Two packages of one commit (`0.1.55.5`, `0.1.55.6`) shipped the previous engine: the three exes of
`0.1.55.4`, `0.1.55.5`, `0.1.55.6` were byte-identical, md5 `2d347b43a8952e6f88cc773884c585da`, and the manifest
stamp `build_id` is the git commit, not the compiled code. `package` now runs the engine and the picture reader
first; its own relink deletes `strata.exe` in the build folder, so an old binary cannot pass for the new build.

### Updating this PC (the fork's own routine)

The engine and the picture reader are built here; everything else comes from upstream as binaries, so nothing on this PC
downloads an engine, and an engine is never fetched by `strata launch` (`strata_cli/launch/runner.py` reads the component
folder and says in plain words when the package there is older than a model needs; the next step is to rebuild it here).
`strata update` takes Control from the signed feed and skips the engine, which is the default in this checkout
(`STRATA_ENGINE_FROM_FEED=1` asks for an engine package from the feed, and `--engine` says the same for one run).

```sh
git pull --ff-only                       # 1. upstream code
strata launch --stop                     # 2. a serving engine holds strata.exe and its DLLs: the package folder cannot be rewritten
strata update                            # 3. Control from the feed; the engine step is off
python tools/deploy_local_code.py        # 4. this checkout's own Python changes into components/control/<active>: the folder that
                                         #    runs the server. The terminal is not touched: its upstream release is an older code
                                         #    base, and the launch runs from this checkout (`bin\strata.ps1` with its `.venv`)
python3 packaging/build_engine_win_local.py   # 5. engine, picture reader, package 0.1.55-local-windows-x86_64-cuda-cuda13. The cygwin
                                         #    interpreter: the script turns Windows paths into /cygdrive/c/..., which a Windows
                                         #    interpreter cannot open. `tools/rebuild_engine.sh` is this routine in one file.
strata launch --preset iq2_xs-low_ram    # 6. the engine is the newest package, the reader ships in its folder
```

`tools/deploy_local_code.py plan` lists the files that differ without writing them. The set it copies is what git says this
checkout changed against upstream (`git diff --name-only upstream/main HEAD`, plus the files edited and not committed), limited
to `strata.py`, `setup.py`, `strata_cli/`, `strata_control/` and `serve/`: four files today. Run step 4 after every Control
update: a new Control version arrives as a new folder, and the patched files must go in there too. A Control folder's files are
not hashed when it starts (`components/control/active.json` records the archive's SHA-256), so copying them is not refused; an
engine package's files are checked against their manifest on every start, which is why the engine reaches this PC only as a
package, built here. `packaging/repack_local_portable.sh` is the same routine for the `STRATA_PORTABLE=ON` build: it stops the
model, deletes the packages this checkout built, builds and packages the portable one, and verifies it.

`docs/LOCAL_FORK_CHECKS.md` lists the checks that the offline suite cannot make: each one stops the serving model and starts
it again, so they are run by hand, one at a time, when the machine is free.

## The launch on PATH (the fork's launcher, first entry)

User PATH's **first** entry is `C:\Projects\Strata-fork\bin`, and the entry `terminal\current\bin` was removed. `strata` and
`strata launch` therefore run this checkout's code (its `bin\strata.cmd`, `.ps1`, `strata`), not the upstream terminal's.
Nothing in the terminal needs to be changed, and nothing has to be re-made after an update: `packaging/install.ps1:142-148`
is the only place Strata writes PATH, and it appends `terminal\current\bin` after what is already there, so a new terminal
release cannot take `strata` away from this checkout. The junction `current` is moved to a new version folder by the same
script (`install.ps1:127-129`), which is also why the terminal's bin is not the place for a fork's launcher: a symlink there
is deleted when the same version is reinstalled (`install.ps1:122-126`) and bypassed when `current` moves, and cmd and
PowerShell look for `strata.py` next to the link, not next to what the link points at, so the launcher needs a
`strata-root.txt` marker in that folder (the launchers here read it, and the shell launcher follows the link itself). The
launchers upstream shipped in `terminal\current\bin` are untouched, and the copies from the 0.1.48.1 archive are kept in
`terminal\upstream-bin-0.1.48.1`.

The Python each launcher picks needs the screens' packages: the `.cmd` and `.ps1` take this checkout's `.venv` (it has
`rich` and `textual`), and the shell launcher `bin/strata` takes a Python 3.10+ from PATH, so `rich==15.0.0` and
`textual==8.2.8` (the pin in `requirements-monitor.txt`) are installed in the cygwin interpreter with `python3 -m pip install
--user`.

```sh
# the one change made, in PowerShell: the checkout's bin first, the terminal entry dropped, entries unique
powershell -NoProfile -Command "$parts=[Environment]::GetEnvironmentVariable('Path','User') -split ';'";
  "$new=@('C:\Projects\Strata-fork\bin') + ($parts | Where-Object { $_ -and $_ -ne 'C:\Users\karam\AppData\Local\Strata\terminal\current\bin' });
  $u=@(); foreach($p in $new){ if($p -and ($u -notcontains $p)){ $u += $p } };
  [Environment]::SetEnvironmentVariable('Path', ($u -join ';'), 'User')"
```

Check it in a new terminal window: `strata --version` must say `strata 0.2.9 (build 0.1.55; python ... C:\Projects\Strata-fork\.venv\Scripts\python.exe)`.

## The private Node runtime

The desktop bundle ships a pinned standalone Node beside the App Host, so an end user supplies no runtime. It is not committed
(about 117 MB, an input rather than source). Fetch and verify it:

```sh
apps/desktop/scripts/fetch-node.sh                 # host platform
apps/desktop/scripts/fetch-node.sh --target linux-x64
apps/desktop/scripts/fetch-node.sh --check         # verify what is installed
```

The script refuses an archive whose SHA-256 differs from the digest recorded for that target. The digests come from nodejs.org's
`SHASUMS256.txt` for the pinned version. To bump the version, change `STRATA_NODE_VERSION`, add the new digests and run `--check`.
`fetch-node.ps1` is the Windows equivalent.

## Desktop app

```sh
cd apps/desktop
./scripts/fetch-node.sh                                                      # the sidecar (once)
npm install --prefix ../host && npm --prefix ../host run build                # the UI
cargo tauri build --bundles app                                              # macOS: Strata.app
./scripts/make-dmg.sh                                                        # macOS DMG (headless; Tauri's default step needs a Finder session)
cargo tauri build --bundles nsis                                             # Windows, on Windows: per-user NSIS installer
cargo tauri build --bundles appimage deb                                     # Linux, on Linux
```

The Windows installer is per-user and needs no elevation (`installMode: currentUser` in `tauri.conf.json`). Models, profiles and
sessions live outside the install directory (`~/.strata` and the platform state directories), so uninstalling the app does not remove them.

Build boxes build without a signing key: the default `tauri.conf.json` has `createUpdaterArtifacts: false`. Signing happens on the
release PC ([UPDATES.md](UPDATES.md#releasing)).

## Strata Control and the engine in the installed app

The installer carries Control, and on a Mac the Metal engine too. Neither fetches anything when it runs.

**Control** (`<install>/control`, built by `apps/desktop/scripts/stage-control.py`) is the same bundle as the Terminal archive:
the pinned CPython 3.12.15, the hash-locked wheels, `strata_control`, `strata_cli` and `serve`, plus a `BUNDLE.json` with the
interpreter and lock digests. The host finds it beside itself or through `STRATA_CONTROL_DIR`, runs it on its own interpreter
(`PYTHONNOUSERSITE=1`, no `PYTHONHOME` or `PYTHONPATH`) and starts a model with `strata profile create|use-config` and
`strata runtime start`. The engine then runs under the owner runtime (loopback TCP and a 256-bit token on Windows, a private
socket elsewhere; a Job Object or process group; leases), and a second client such as the Terminal can attach to the same instance.

The app starts each engine with `runtime start --die-with-pid <host>`, so the engine ends with the host. The host ends with the shell
(a kill-on-close Job Object on Windows; the host watches its parent everywhere). The app is a single instance: a second launch focuses
the open window. One app can run several engines (profiles `app-1`, `app-2`, ...). Every close path is in [docs/CLOSING.md](../docs/CLOSING.md).

**The engine.** The Windows and Linux installers carry no engine. On the first start the app shows the engine for the card with its size
and one Download button (with progress and resume), or installs an engine file the person already has ("I already have the file").
The Mac dmg keeps its small Metal engine in `<install>/engine-bundle/<package folder>`. Either way the engine ends up in the per-user
component folder, after the package is checked against its manifest (`%LOCALAPPDATA%\Strata\components`, `$XDG_DATA_HOME/strata/components`, or `STRATA_COMPONENTS_DIR`),
where Control finds it like any engine that `strata update` installed:

    components/engine/<version>-<platform>-<backend>[-<variant>]/
        engine-component.json     the manifest (see ENGINE-COMPONENT.md)
        strata[.exe]  *.dll | *.so   the binary and the GPU runtime libraries beside it

A run config names it as `"exe": "component:strata-engine"`. At start, `strata_control/engine_component.py` picks the package for this PC
(platform, then the backend its GPU needs, found with nvidia-smi, rocminfo or sycl-ls, or `STRATA_ENGINE_BACKEND`), checks every
file's size and SHA-256 and puts the package folder first on the library path. A mismatch is `EngineIntegrityError` and nothing starts.

When no package matches, `strata serve` ends with exit 5 and "No engine for this PC was found". It never fetches. Before that point
everything is a warning: `strata launch` says it could not find an engine, asks whether to go on, downloads the model and ends with
the next step (exit 0, nothing started). The app shows the same warning with an "Update engine" button in Settings.
`packaging/package_engine.py` makes a local package from a build folder.

**No acquisition at run time.** `STRATA_NO_ACQUIRE=1` (set by the Tauri shell for the host, and by the host for Control) makes
`strata_control/acquisition.py` refuse pip, downloads and compiles. On the host side `server/acquisition.mjs` refuses the llama.cpp
and cloudflared fetches (a connector placed in `components/cloudflared/` is used as it is). Models the user chooses to download are a
separate, explicit action. `apps/host/tests/no-acquire.test.mjs` fails on any outbound request or installer process.
`apps/desktop/scripts/installed-flow.mjs` drives an installed app end to end.

## The Terminal archive

The Terminal bundles a private Python and its dependencies for `windows-x86_64`, `linux-x86_64`, `linux-aarch64` (DGX Spark) and `darwin-arm64`
(`package_terminal.py --target ...`, built on the target OS).

- The interpreter is python-build-standalone CPython 3.12.15, digest-checked at build time.
- Packages come from `packaging/locks/runtime-<target>-cp312.txt` and are installed with
  `pip install --require-hashes --no-deps --only-binary=:all:` at build time only. `lock_wheels.py` makes the lock; review its diff when a pin changes.
- Wrappers: `bin/strata.cmd` and `bin/strata.ps1` on Windows, `bin/strata` on POSIX. `START-HERE.bat`, `SETUP.bat`, `UPDATE.bat`, `setup.sh`
  and `update.sh` use a `python/` folder beside them when there is one.
- `smoke_terminal.py <archive>` extracts an archive into a clean environment (a path with a space, a PATH with no Python) and runs
  `--version`, `doctor`, `profile list`, `install` and `serve --engine mock`.
- Check each archive's `BUNDLE.json` and lock digests. An old archive is not upgraded when the source lock changes.

## What a platform must produce before it is qualified

1. The app launches and the window serves the supplied UI from the host's origin.
2. The owner transport works: one owner per installation, a second client attaches, a stale owner can be recovered, and no client can stop a process it does not own.
3. Real inference on a supported pack, with the engine build hash and the model hashes recorded.
4. `docs/work/QUALIFICATION-KIT.md` run end to end, with raw evidence kept.

## Release checks and gaps

- CI covers scripts, Terminal checks, Metal builds and Docker images ([.github/workflows](../.github/workflows)). It does not replace native desktop
  packaging, signing or real-GPU acceptance on every target. Some recipes still run by hand.
- Signing needs the owner's credentials, which are never in this tree. The feed and every engine package are signed with the feed key
  (`feed_tool.py`); the app installers with the Tauri updater key ([UPDATES.md](UPDATES.md)). The Mac app and Terminal archive are signed and
  notarised on a Mac that holds the Developer ID certificate. No Windows code-signing certificate is configured, so Windows may warn about the installer.
- Check the licenses and provenance of bundled fonts and assets when packaging. The early D05 checklist lacked redistribution evidence;
  treat that as an open checklist finding, not an audit of every release asset.

## History / older measurements

- **Tauri config:** an early draft used `perMachine`, `oneClick` and `deleteAppDataOnUninstall`. The Tauri schema rejects them; the
  current config uses `installMode: currentUser`.
- **Repository size:** `apps/desktop/src-tauri/target` and the Node sidecar were briefly committed, which left about 324 MB in the git object
  store. They are untracked and ignored now. History was not rewritten.
- **Mac packaging tests:** the first DMG self-test (8/8) and the engine's 17 kernel-parity suites ran on an Apple M4 GPU in a 16 GiB Mac, with service stand-ins for signing.
  Later inference measurements on larger Macs are in [MAC.md](../docs/MAC.md).
- **Intel Arc:** official since 0.1.40.2. The 2026-10 B70 tests used the source build, not the engine package. `sycl/setup_intel.py` (Docker) remains the source-build route.
