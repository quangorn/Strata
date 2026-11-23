#!/usr/bin/env python3
"""Stamp the BUILD.json of a local engine build in a source checkout (packaging/build_engine_win_local.py).

    python packaging/stamp_local_build.py <stage>/BUILD.json --version 0.1.55 --archs 89 --cuda-root "C:/.../CUDA/v13.4"

Writes the fields setup.py's own stamp writes for a compiled engine (`source`, `version`, `archs`, `cuda_dirs`) plus
the toolkit, so `package_engine.py` has a record to ship beside the binary. `vision_archs` is written by
`package_engine.py --stamp-vision` (the picture-reader recipes call it), and that is what
`package_engine.py --verify` compares with the package's architectures. An existing BUILD.json is kept and only
these fields are added.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def arch_numbers(value) -> list[int]:
    """The CUDA architectures of a list like "89", "75;86;89;120" or ["89-real", "sm_120"], as numbers."""
    found = []
    for item in re.split(r"[;,\s]+", str(value)):
        m = re.fullmatch(r"(?:sm_|compute_)?(\d+)(?:a|f)?(?:-real|-virtual)?", item.strip().lower())
        if m:
            found.append(int(m.group(1)))
    return sorted(set(found))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("build_json", type=Path)
    ap.add_argument("--version", required=True)
    ap.add_argument("--archs", required=True)
    ap.add_argument("--cuda-root", default="", help="the toolkit this engine was built with")
    a = ap.parse_args(argv)

    try:
        data = json.loads(a.build_json.read_text(encoding="utf-8-sig")) if a.build_json.is_file() else {}
    except (OSError, ValueError):
        data = {}
    data["source"] = "local"
    data["version"] = a.version
    data["archs"] = arch_numbers(a.archs)
    toolkit = re.search(r"v?(\d+\.\d+)", a.cuda_root.replace("\\", "/"))
    if toolkit:
        data["toolkit"] = toolkit.group(1)
    if a.cuda_root:
        root = a.cuda_root.replace("\\", "/")
        data["cuda_dirs"] = [f"{root}/bin", f"{root}/bin/x64", f"{root}/lib64"]
    a.build_json.parent.mkdir(parents=True, exist_ok=True)
    a.build_json.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"{a.build_json}: source local, version {a.version}, archs {data['archs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
