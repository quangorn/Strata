# The fork's checkout launcher: `bin\` is the first entry of the user PATH on this PC, so `strata` here runs
# this checkout. Upstream moved the Terminal into `terminal\`, so this script forwards to the terminal's own
# launcher (`terminal\bin\strata-direct.ps1`), which finds `terminal\strata.py` and the interpreter.
#
# The one thing it adds: the checkout's own `.venv` (it carries `rich` and `textual`, which the screens need) is
# named through STRATA_PYTHON, because the terminal launcher looks for `.venv` beside its own `strata.py`
# (`terminal\.venv`), not at the checkout root.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $here '..\.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venv) { $env:STRATA_PYTHON = $venv }
& (Join-Path $here '..\terminal\bin\strata-direct.ps1') @args
exit $LASTEXITCODE