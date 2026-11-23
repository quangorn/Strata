#!/usr/bin/env python3
"""Builds this PC's own CUDA engine in a source checkout, and packages it into the component folder.

Run it from cygwin bash (any Python 3.10+), or from cmd / PowerShell with the checkout's `.venv` python:

    python3 packaging/build_engine_win_local.py            engine, picture reader, package
    python3 packaging/build_engine_win_local.py plan       show what it would do (nothing is built)
    python3 packaging/build_engine_win_local.py engine     only strata.exe + strata_kq.dll (pictures stay off)
    python3 packaging/build_engine_win_local.py vision     only the picture reader, into the last build
    python3 packaging/build_engine_win_local.py package    only the package from the last build

Needs Visual Studio 2022 Build Tools with the C++ workload, and a CUDA Toolkit (13.4 on this PC). Nothing is
downloaded, nothing is uploaded, and nothing in the release path is changed.

Settings are environment variables, all optional:

    ARCHS          CUDA architectures, semicolons. Default 89 (RTX 4080); the release cuda13 list is 75;86;89;120
    BUILD_JOBS     compile jobs (default 24). A 24-job build of this tree on this PC (48 GB RAM) needed at most
                   4 GB, so the default is fine here; `packaging/build_vision_windows.bat:17` uses 4 for the reason
                   it names ("the CUDA compiler uses RAM"), and I have not measured what one nvcc job needs on a
                   smaller machine. Drop this if the PC starts paging: BUILD_JOBS=8 is enough to keep 24 cores busy
                   on the C++ side
    CUDA_ROOT      the toolkit (default CUDA_PATH, then the newest CUDA\\v* folder)
    STRATA_VCVARS  a vcvars64.bat that vswhere cannot find (setup.py reads the same name)
    VCVARS_VER     an MSVC toolset to pick, e.g. 14.42 (`vcvars64.bat -vcvars_ver=...`). CUDA needs MSVC's
                   crt/host_config.h; the newest toolset on this PC (14.44.35207) has no crt headers, so nvcc's
                   check fails without this. Install the toolset in the Visual Studio Installer first
    MIN_DRIVER     the driver the package is recorded for (default this PC's, from nvidia-smi; the release
                   value 580.65 is CUDA 13.0's, so do not use it for a build of a newer toolkit)
    VERSION        default the version in CMakeLists.txt
    VERSION_SUFFIX the suffix the engine adds to its own version string (default `-local`), so a forked build names
                   itself `strata 0.1.55-local` and its INFO line reads `engine=0.1.55-local`; the web monitor shows
                   that (`serve/server.py:1156-1158` takes the engine's INFO line over the manifest, and
                   `serve/web/app.js:575` prints `v<version>`). Set it empty for a binary that reports exactly the
                   release's `0.1.55`; BUILD.json and the package manifest keep a plain numeric version
    BUILD_DIR / VISION_BUILD_DIR / STAGE / OUT (default %LOCALAPPDATA%\\Strata\\components\\engine)
                   the build folders are kept between runs: `cmake --build` recompiles only what changed, so a run
                   after a source edit costs the changed files, not all 169 CUDA and C++ targets. Only `strata.exe`
                   is removed before the relink (`packaging/build_engine_win_local.py:277`), and a cmake reconfigure
                   on the same folder keeps the object files. Delete a build folder when its generator or its cached
                   flags change, as setup.py does (`setup.py:5786` `fresh_if_other_generator`)
    STRATA_PORTABLE  ON builds for other PCs (AVX2 floor, static MSVC runtime); default OFF: this PC
    EXTRA_FLAGS      more compiler flags for the Release build, appended to C, C++ and nvcc. Default empty

Optimization: the build is `CMAKE_BUILD_TYPE=Release`, which is what the release recipes use
(`packaging/build_cuda_linux.sh:34`): MSVC gets its Release flags (`/O2 /Ob2 /DNDEBUG`, plus `/Zi` and
`/OPT:REF /OPT:ICF` for the PDB, `CMakeLists.txt:31`), nvcc its default `-O2`, and ggml-cpu is built for this CPU
(`STRATA_NATIVE_EXPERTS=ON`, `GGML_NATIVE ON`). The project's own fast-math flags are per file
(`--use_fast_math` on the CUDA kernel files, `CMakeLists.txt:650`, and on the MMQ libraries, `CMakeLists.txt:2253`);
do NOT add `--use_fast_math` to EXTRA_FLAGS for the whole build: it changes rounding outside those files
(`CMakeLists.txt:652-655` keeps Q8_K's multiply and magic-bias rounding as separate FP32 operations).

CPU floor: a ready-made x86 engine needs AVX2 (`CMakeLists.txt:35-38`), and Strata's AVX-512 kernels are per file
behind a run-time CPU check (`CMakeLists.txt:36`, `/arch:AVX512` on `iq_avx512.cpp` and `nvfp4_avx512.cpp`), so
building on a CPU without AVX-512 is the normal case: those kernels are compiled and never run. A CPU with no
AVX2 needs the experimental floor `STRATA_ISA_FLOOR=avx` (Sandy/Ivy Bridge) or `none` (SSE4.2), which setup.py
passes as `-DSTRATA_ISA_FLOOR` (`setup.py:5842`); leave it empty for AVX2.

What has been run on this PC (RTX 4080 sm_89, driver 610.88, CUDA 13.4, MSVC 14.44, 48 GB RAM): the engine
(169 ninja targets) and the picture reader (275 targets), at 24 jobs (peak RAM 4 GB) and at 8, the package
(`package_engine.py --verify`: ok, 6 files, manifest sha256 `36d385581b5a…`), `strata components engine --full`
(the local package is chosen, nothing downloaded with `STRATA_NO_ACQUIRE=1`), and `strata.exe --version` printing
`strata 0.1.55-local`. Not measured: a build with `VCVARS_VER` set (the toolset that ships crt headers came from the
toolkit's extra components).

MSVC is used through `vcvars64.bat`: its environment is read once and given to cmake, ninja and the picture
reader, so the build works from a cygwin shell with no "x64 Native Tools" prompt. `packaging/build_vision_windows.bat`
is called as it is (it stamps `vision_archs` into the stage's `BUILD.json`), and `packaging/package_engine.py` runs on
the checkout's own Windows Python, because a package check uses Windows path rules (`strata_control/longpath.py`).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
import stamp_local_build          # the BUILD.json stamp (packaging/stamp_local_build.py)


def wpath(p) -> str:
    """`p` as a path Windows understands (the cygwin form /cygdrive/c/... becomes C:\\...): what cmd, cmake,
    nvcc and `package_engine.py` are given."""
    s = str(p)
    if re.match(r"^[A-Za-z]:[\\/]", s):
        return s.replace("/", "\\")
    try:
        got = subprocess.run(["cygpath", "-w", s], capture_output=True, text=True, timeout=20).stdout.strip()
        return got or s
    except OSError:
        return s


def upath(p) -> str:
    """`p` as a path THIS Python opens. A cygwin Python cannot open `C:\\...` (the env values it inherits from
    Windows are Windows paths), so every path kept in a Path goes through here; on a Windows Python nothing changes."""
    s = str(p)
    if re.match(r"^[A-Za-z]:[\\/]", s):
        try:
            got = subprocess.run(["cygpath", "-u", s], capture_output=True, text=True, timeout=20).stdout.strip()
            return got or s.replace("\\", "/")
        except OSError:
            return s.replace("\\", "/")
    return s


def prog(name) -> str:
    """A program, in the form THIS Python can launch. A cygwin Python inherits Windows PATH values and its exec
    does not search them, so a bare name is resolved with `which`; a Windows path (`C:\\...`) is turned into the
    `/cygdrive/c/...` form, which is the one that launches. A path already in the cygwin form is left as it is."""
    n = str(name)
    if re.match(r"^[A-Za-z]:[\\/]", n):
        return upath(n)
    return shutil.which(n) or n


def run(cmd, env=None, cwd=ROOT) -> int:
    """A command with a printed line, and its exit code. The program is launched in the form THIS Python can
    resolve (a cygwin Python needs `/cygdrive/c/...`; it does not turn arguments into Windows paths for a
    Windows program, so the arguments stay in the form cmake, nvcc and cmd read).
    The working directory is the checkout by default: cmd inherits cygwin's `/cygdrive/c/...` cwd, and a bat that
    resolves its own folder (`build_vision_windows.bat:25` takes `%~dp0..` through cwd) then builds
    `/cygdrive/c/.../C:/Projects/...` and cmake fails with a source directory that does not exist."""
    argv = [str(c) for c in cmd]
    argv[0] = prog(argv[0])
    print("  " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(argv, env=env, cwd=str(cwd) if cwd else None).returncode


def vcvars() -> Path | None:
    """vcvars64.bat. The same search setup.py:2252 makes: STRATA_VCVARS, then vswhere with the C++ x64 tools."""
    hand = os.environ.get("STRATA_VCVARS", "").strip()
    if hand:
        v = Path(upath(hand))
        return v if v.exists() else None
    for b in [os.environ.get("ProgramFiles(x86)", ""), "C:/Program Files (x86)"]:
        vswhere = Path(upath(b)) / "Microsoft Visual Studio/Installer/vswhere.exe" if b else None
        if not vswhere.exists():
            continue
        found = subprocess.run([upath(vswhere), "-latest", "-products", "*",
                                "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                                "-property", "installationPath"], capture_output=True, text=True, timeout=60).stdout.strip()
        if found:
            v = Path(upath(found)) / "VC/Auxiliary/Build/vcvars64.bat"
            if v.exists():
                return v
    return None


def vcvars_env(v: Path, work: Path) -> dict:
    """The environment vcvars64.bat makes (PATH, INCLUDE, LIB), read once and reused for every step.
    The picture-reader recipe looks for `cl` on PATH (`build_vision_windows.bat:43`), and its own search knows only
    the standard BuildTools / Community folders - a nonstandard install folder needs this environment.
    cygwin quotes the arguments it hands a Windows program, so a command line with a quoted path inside it reaches
    cmd as `""path""` and stops with "is not recognized"; the call goes in a file, the way setup.py:5816 writes its
    build script."""
    work.mkdir(parents=True, exist_ok=True)
    bat = work / "strata-vcvars-env.bat"
    # an older toolset is chosen with -vcvars_ver (VCVARS_VER=14.42). CUDA's include/host_config.h includes
    # crt/host_config.h, and MSVC 14.44.35207 has no crt headers on this PC, so nvcc's compiler check fails with
    # "crt/host_config.h: No such file or directory" until an older v143 toolset is installed and named here.
    sel = f' -vcvars_ver={os.environ["VCVARS_VER"]}' if os.environ.get("VCVARS_VER") else ""
    bat.write_text(f'@echo off\r\ncall "{wpath(v)}"{sel} >nul\r\nset\r\n', encoding="utf-8")
    out = subprocess.run([prog("cmd"), "/c", wpath(bat)], capture_output=True, env=os.environ, timeout=300)
    env = {}
    # vcvars answers in the system's code page (cp1251 / cp866 on a Russian Windows), not UTF-8
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key] = value
    return env


def cuda_root() -> Path | None:
    """The toolkit build folder: CUDA_ROOT, CUDA_PATH, then the newest CUDA\\v* (setup.py:2238 does the same)."""
    hand = os.environ.get("CUDA_ROOT", "").strip() or os.environ.get("CUDA_PATH", "").strip()
    if hand:
        root = Path(upath(hand))
        if (root / "bin" / "nvcc.exe").exists():
            return root
    base = Path(upath("C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA"))
    kits = sorted((p for p in base.iterdir() if (p / "bin" / "nvcc.exe").exists()), key=lambda p: p.name) if base.is_dir() else []
    return kits[-1] if kits else None


def driver() -> str:
    """This PC's NVIDIA driver, from nvidia-smi (the header is kept: cygwin splits arguments on commas)."""
    out = subprocess.run([prog("nvidia-smi"), "--query-gpu=driver_version", "--format=csv"],
                         capture_output=True, text=True, timeout=60).stdout.splitlines()
    return out[1].strip() if len(out) > 1 else ""


def version() -> str:
    m = re.search(r"project\(strata VERSION (\d+\.\d+(?:\.\d+)*)", (ROOT / "CMakeLists.txt").read_text(encoding="utf-8"))
    return m.group(1) if m else "0.1.55"


def main(argv=None) -> int:
    argv = list(sys.argv) if argv is None else list(argv)
    step = (argv[1] if len(argv) > 1 else "all").lower()
    archs = [int(x) for x in re.split(r"[;,\s]+", os.environ.get("ARCHS", "89")) if x.strip()]
    jobs = int(os.environ.get("BUILD_JOBS", "24"))
    portable = (os.environ.get("STRATA_PORTABLE", "OFF").upper() == "ON")
    build_dir = Path(upath(os.environ.get("BUILD_DIR") or ROOT / "build-c13-win"))
    stage = Path(upath(os.environ.get("STAGE") or build_dir / "stage"))
    vision_dir = Path(upath(os.environ.get("VISION_BUILD_DIR") or ROOT / "build-vision-cuda13-win"))
    out = Path(upath(os.environ.get("OUT") or Path(os.environ.get("LOCALAPPDATA", "")) / "Strata/components/engine"))
    # "-local" keeps the package beside the release one instead of on top of it: the release package 0.1.55 is
    # signed and immutable, and a package step on the same version refuses to overwrite it. resolve() picks the
    # newest version, and "0.1.55-local" is newer than "0.1.55" (strata_control/engine_component.py:252),
    # with the driver tie-break (610.88 vs 580.65) going the same way.
    ver = os.environ.get("VERSION") or (version() + "-local")
    # the engine's own version string, compiled in (`CMakeLists.txt:27` defines STRATA_VERSION; `src/program/generate.cpp:2921`
    # prints it for --version, and `src/program/generate.cpp:12266` writes the INFO engine= line the web monitor shows).
    # The suffix marks a forked build; BUILD.json and the manifest keep the plain numeric version, because setup.py reads
    # a binary's version with a digits-only pattern (`setup.py:6824-6831`) and compares it (`setup.py:6842-6850`).
    suffix = os.environ.get("VERSION_SUFFIX", "-local")
    min_driver = os.environ.get("MIN_DRIVER") or driver()
    isafloor = os.environ.get("STRATA_ISA_FLOOR", "").strip()
    extra = os.environ.get("EXTRA_FLAGS", "").strip()
    v = vcvars()
    root = cuda_root()
    if v is None:
        print("no Visual Studio 2022 C++ tools: install the workload 'Desktop development with C++', "
              "or set STRATA_VCVARS to a vcvars64.bat")
        return 1
    if root is None:
        print("no CUDA Toolkit: install one, or set CUDA_ROOT to its folder")
        return 1

    print(f"source  : {ROOT}")
    print(f"vcvars  : {wpath(v)}")
    print(f"cuda    : {wpath(root)}")
    print(f"archs   : {';'.join(str(a) for a in archs)}   jobs: {jobs}   portable: {'ON' if portable else 'OFF'}")
    print(f"flags   : Release (MSVC /O2 /Ob2 /DNDEBUG, nvcc -O2)   extra: {extra or '(none)'}   "
          f"CPU floor: {isafloor or 'AVX2'}   native ggml-cpu: {'OFF' if portable else 'ON'}")
    print(f"engine  : names itself {version() + suffix} in --version and the INFO engine= line   "
          f"BUILD.json version {version()}   package version {ver}")
    print(f"build   : {build_dir}   stage: {stage}")
    print(f"package : {out / (ver + '-windows-x86_64-cuda-cuda13')}  (min driver nvidia={min_driver})")
    if step == "plan":
        return 0

    env = vcvars_env(v, build_dir)             # PATH / INCLUDE / LIB of the MSVC tools, plus the toolkit
    env["CUDA_PATH"] = wpath(root)
    env["BUILD_JOBS"] = str(jobs)
    env["VISION_BUILD_DIR"] = wpath(vision_dir)
    # the picture-reader bat calls bare `cmake` and `ninja`: on a cygwin PATH that is cygwin's cmake, which reads a
    # Windows -S path as relative to its own root (/cygdrive/c/... + C:/...) and reports "source directory does not
    # exist". The pip cmake from requirements-build.txt goes first.
    venv_bin = wpath(ROOT / ".venv/Scripts")
    if (Path(upath(venv_bin)) / "cmake.exe").exists():
        env["PATH"] = venv_bin + ";" + env.get("PATH", "")

    # ---------------------------------------------------------------- engine
    if step in ("all", "engine"):
        cmake = ROOT / ".venv/Scripts/cmake.exe"
        ninja = ROOT / ".venv/Scripts/ninja.exe"
        if not cmake.exists():
            cmake, ninja = "cmake", "ninja"
        real = ";".join(f"{a}-real" for a in archs)
        # cmake wants forward slashes, and the Windows form of the path (a cygwin /cygdrive path is not one)
        cuda_fwd = wpath(root).replace("\\", "/")
        ggml_fwd = wpath(ROOT / "third_party/llama.cpp").replace("\\", "/")
        flag_defs = [f"-DCMAKE_{which}_FLAGS_RELEASE={extra}" for which in ("C", "CXX", "CUDA")] if extra else []
        if run([wpath(cmake), "-G", "Ninja", "-S", wpath(ROOT), "-B", wpath(build_dir),
                "-DCMAKE_MAKE_PROGRAM=" + wpath(ninja), "-DCMAKE_BUILD_TYPE=Release",
                "-DSTRATA_ENABLE_CUDA=ON", "-DSTRATA_BUILD_TESTS=OFF", "-DSTRATA_Q6K_EXPERTS=ON",
                "-DSTRATA_MMQ_KQUANTS=ON", "-DSTRATA_NATIVE_EXPERTS=ON", "-DSTRATA_EXPERIMENTAL_SM60=OFF",
                f"-DSTRATA_PORTABLE={'ON' if portable else 'OFF'}", f"-DSTRATA_ISA_FLOOR={isafloor}",
                f"-DSTRATA_VERSION_SUFFIX={suffix}",
                *flag_defs,
                f"-DCMAKE_CUDA_ARCHITECTURES={real};{archs[-1]}-virtual",
                "-DCMAKE_C_COMPILER=cl", "-DCMAKE_CXX_COMPILER=cl",
                f"-DCMAKE_CUDA_COMPILER={cuda_fwd}/bin/nvcc.exe",
                f"-DCUDAToolkit_ROOT={cuda_fwd}",
                f"-DSTRATA_GGML_DIR={ggml_fwd}"], env=env) != 0:
            return 1
        exe = build_dir / "strata.exe"
        exe.unlink(missing_ok=True)           # a binary left from an older commit must not pass for this build
        if run([wpath(cmake), "--build", wpath(build_dir), "--target", "strata", "--target", "strata_kq",
                "-j", str(jobs)], env=env) != 0:
            return 1
        if not exe.exists():
            print(f"no {exe}")
            return 1
        subprocess.run([upath(exe), "--version"])
        stage.mkdir(parents=True, exist_ok=True)
        shutil.copy2(exe, stage / "strata.exe")
        if (build_dir / "strata_kq.dll").exists():
            shutil.copy2(build_dir / "strata_kq.dll", stage / "strata_kq.dll")
        stamp_local_build.main([str(stage / "BUILD.json"), "--version", version(), "--archs",
                                ";".join(str(a) for a in archs), "--cuda-root", wpath(root)])
        if step == "engine":
            return 0

    # ---------------------------------------------------------------- picture reader
    if step in ("all", "vision"):
        env["VISION_ARCHS"] = ";".join(str(a) for a in archs)
        bat = wpath(ROOT / "packaging/build_vision_windows.bat")
        if run(["cmd", "/c", bat, "cuda13", wpath(stage)], env=env) != 0:
            return 1
        if step == "vision":
            return 0

    # ---------------------------------------------------------------- package
    py = ROOT / ".venv/Scripts/python.exe"
    tool = wpath(ROOT / "packaging/package_engine.py")      # a Windows Python reads the script by its own path form
    args = [str(py) if py.exists() else sys.executable, tool, "--src", wpath(stage), "--backend", "cuda",
            "--platform", "windows-x86_64", "--version", ver, "--variant", "cuda13"]
    for a in archs:
        args += ["--arch", f"sm_{a}"]
    args += ["--arch", f"compute_{archs[-1]}"]   # the PTX the build carries for newer cards (build_cuda_linux.sh:63)
    args += ["--min-driver", f"nvidia={min_driver}", "--out", wpath(out)]
    # the DLLs the engine imports must travel in the package, or a clean PC fails with 0xC0000135 (binary_deps.py,
    # packaging/binary_deps.py:6). A local build's strata.exe imports the CUDA cuBLAS and the MSVC C++ runtime, so
    # the packager copies them from these folders; the release recipe ships the same cuBLAS DLLs.
    crt = Path(upath(env.get("VCToolsRedistDir", ""))) / "x64/Microsoft.VC143.CRT"
    for d in (root / "bin/x64", root / "bin", crt):
        if d.is_dir():
            args += ["--search", wpath(d)]
    if run(args, env=env) != 0:
        return 1
    return run([str(py) if py.exists() else sys.executable, tool,
                "--verify", wpath(out / (ver + "-windows-x86_64-cuda-cuda13"))], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
