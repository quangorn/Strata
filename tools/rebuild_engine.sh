#!/bin/sh
# The fork's own update routine on this PC, in one run:
#   1. git pull --ff-only                 upstream code into the checkout
#   2. strata launch --stop              a serving engine holds strata.exe and its DLLs: the package folder cannot be rewritten
#   3. strata update                     Control from the signed feed; the engine step is off here (STRATA_ENGINE_FROM_FEED=1 asks for one)
#   4. deploy_local_code.py              this checkout's own Python changes into components/control/<active>: the folder that runs
#   5. build_engine_win_local.py         engine, picture reader, and the package <version>-local-windows-x86_64-cuda-cuda13
#   6. strata launch --preset ...        the newest package is the engine, the reader ships in its folder
#
# Nothing downloads an engine: `strata launch` reads the component folder and says in plain words when the package there is
# older than a model needs, and `strata update` skips the engine by default (see packaging/README.md, "Updating this PC").
# Set PRESET to the model you serve (the default is the one running now). STRATA_PORTABLE=ON is passed through to step 5.
set -e
cd /cygdrive/c/Projects/Strata-fork
PY=./.venv/Scripts/python.exe
# the build runs with the cygwin interpreter: it turns every Windows path into its `/cygdrive/c/...` form, which a
# Windows interpreter cannot open, so `python3` here and not the `.venv` python (see the script's `upath()`)
PRESET="${PRESET:-iq2_xs-low_ram}"

git pull --ff-only

$PY strata.py launch --stop

$PY strata.py update

$PY tools/deploy_local_code.py

python3 packaging/build_engine_win_local.py

$PY strata.py launch --preset "$PRESET"
