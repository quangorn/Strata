#!/bin/sh
# Replace the local engine package with the STRATA_PORTABLE=ON build (CUDA 13.4, sm_89), version 0.1.55-local.
# Run after the serving engine is stopped: `strata launch --stop` (the running engine loads strata.exe and
# cublas64_13.dll from the package folder, so its files cannot be removed while it serves).
set -e
cd /cygdrive/c/Projects/Strata-fork
STRATA_NO_ACQUIRE=1 ./.venv/Scripts/python.exe strata.py launch --stop

base="/cygdrive/c/Users/karam/AppData/Local/Strata/components/engine"
stage="C:/Projects/Strata-fork/build-c13-portable/stage"
for name in 0.1.55-local-windows-x86_64-cuda-cuda13 0.1.55-local-portable-windows-x86_64-cuda-cuda13; do
    [ -d "$base/$name" ] && rm -rf "$base/$name"
done

./.venv/Scripts/python.exe packaging/package_engine.py --src "$stage" --backend cuda --platform windows-x86_64 \
    --version 0.1.55-local --variant cuda13 --arch sm_89 --arch compute_89 --min-driver nvidia=610.88 \
    --out "C:/Users/karam/AppData/Local/Strata/components/engine" \
    --search "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.4/bin/x64" \
    --search "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.4/bin" \
    --search "C:/Program Files (x86)/Microsoft Visual Studio/2022/BT2022/VC/Redist/MSVC/14.44.35112/x64/Microsoft.VC143.CRT"

./.venv/Scripts/python.exe packaging/package_engine.py --verify \
    "C:/Users/karam/AppData/Local/Strata/components/engine/0.1.55-local-windows-x86_64-cuda-cuda13"
STRATA_NO_ACQUIRE=1 ./.venv/Scripts/python.exe strata.py components engine --full
