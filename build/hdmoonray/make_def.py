# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Write a .def from a DLL's parsed export table, tagging exports in non-executable sections DATA. Reads the DLL only.

Usage: python -I make_def.py <dll> <output .def> <output json>
The json holds the counts, the DLL's imports, and the name and section kind of each export, for check_imports.py.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pelib import PE, data_export, write_def

dll, out, info = sys.argv[1:4]
for path in (out, info):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
pe = PE(dll)
library = os.path.basename(dll)
counts = write_def(pe, library, out)
exports = {name: ("data" if data_export(pe, rva) else "code") for _, name, rva, forward in pe.exports()[2] if name and not forward}
sections = [{"name": s.name, "chars": hex(s.chars), "executable": s.executable, "vsize": s.vsize} for s in pe.sections]
imports = sorted(n for _, n, _ in pe.import_names())
result = {"dll": dll, "bytes": os.path.getsize(dll), "linker": pe.linker, "timestamp": pe.timestamp, "export_dir_name": pe.exports()[0],
          "counts": counts, "sections": sections, "imports": imports}
with open(info, "w") as f:
    json.dump({**result, "exports": exports}, f)
print(json.dumps(result, indent=1))
