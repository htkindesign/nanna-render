# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Check rebuilt hdMoonray DLLs before anything loads them. Reads the DLLs only.

Fails on an import from a colliding DLL, a package USD DLL, the wrong Python, or a USD or TBB symbol that isn't exported.

Usage: python -I verify_built.py <usd json> <tbb json> <dll> [<dll>...]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from patch_imports import RENAMES
from pelib import PE

usd = json.load(open(sys.argv[1]))["exports"]
tbb = json.load(open(sys.argv[2]))["exports"]
collisions = {k.lower() for k in RENAMES}
bad = 0
for path in sys.argv[3:]:
    pe = PE(path)
    table = pe.imports()
    print(os.path.basename(path), "linker", pe.linker, "imports", sorted(t[0] for t in table))
    for dll, names, ordinals, kind in table:
        low = dll.lower()
        problem = None
        if low in collisions:
            problem = "colliding DLL; expected none in these two targets"
        elif low.startswith("usd_") and low != "usd_ms.dll":
            problem = "package USD DLL; expected usd_ms.dll only"
        elif low.startswith("python") and low != "python313.dll":
            problem = "wrong Python; expected python313.dll"
        elif low == "tbb12.dll":
            problem = "oneTBB; expected tbb.dll, the TBB 2020 runtime the package uses"
        elif low == "usd_ms.dll":
            missing = [n for n in names if n not in usd]
            problem = f"{len(missing)} symbols not exported, first {missing[:3]}" if missing else None
            tbb_typed = [n for n in names if "tbb@@" in n]
            if tbb_typed:
                print("  review: TBB-typed USD imports, layout may disagree with oneTBB:", len(tbb_typed), tbb_typed[:3])
        elif low == "tbb.dll":
            missing = [n for n in names if n not in tbb]
            problem = f"{len(missing)} symbols not exported, first {missing[:3]}" if missing else None
        if problem:
            bad += 1
            print("  FAIL", dll, problem)
    pe.close()
print("FAILED" if bad else "OK", bad)
sys.exit(1 if bad else 0)
