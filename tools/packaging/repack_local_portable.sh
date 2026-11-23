#!/bin/sh
# Replace the installed engine package with the STRATA_PORTABLE=ON build (CUDA 13.4, sm_89): the engine, the picture reader
# and the package, built and packaged by tools/packaging/build_engine_win_local.py, which takes its version from
# engine/CMakeLists.txt (the package is named <version>-local-<platform>-<backend>), its driver from nvidia-smi and its
# MSVC redist folder from vcvars. Nothing here repeats those numbers.
#
# Run it from cygwin bash. It stops the serving engine first: a running engine loads strata.exe and cublas64_13.dll from
# the package folder, so its files cannot be removed while it serves.
set -e
cd "$(dirname "$0")/.."
PY=./.venv/Scripts/python.exe

$PY terminal/strata.py launch --stop

# the packages this checkout built for this PC (the native and the portable build share one package name)
base="$LOCALAPPDATA/Strata/components/engine"
ver="$($PY -c 'import re,sys;print(re.search(r"project\(strata VERSION (\d+\.\d+(?:\.\d+)*)", open("engine/CMakeLists.txt",encoding="utf-8").read()).group(1) + "-local")')"
for folder in "$base"/*-windows-x86_64-cuda-cuda13; do
    [ -d "$folder" ] && [ "$(basename "$folder")" != "${ver}-windows-x86_64-cuda-cuda13" ] && rm -rf "$folder"
done

# the cygwin interpreter, not the .venv one: the build script turns Windows paths into /cygdrive/c/... (its `upath`)
STRATA_PORTABLE=ON BUILD_DIR="$PWD/build-c13-portable" \
    VISION_BUILD_DIR="$PWD/build-vision-portable" \
    STAGE="$PWD/build-c13-portable/stage" \
    python3 tools/packaging/build_engine_win_local.py

$PY tools/packaging/package_engine.py --verify "$base/${ver}-windows-x86_64-cuda-cuda13"
STRATA_NO_ACQUIRE=1 $PY terminal/strata.py components engine --full