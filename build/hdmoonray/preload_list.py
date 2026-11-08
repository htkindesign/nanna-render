# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Write addon\\preload.txt: the DLLs runtime.py loads ahead of USD, in order. Reads files only.

First the plugin, whose imports bring in the core. Then what only the shader DSOs in rdl2dso need: MoonRay opens those at render time,
when nothing puts bin on the search path. A DLL that needs the package's USD 25.05 is left out: it can't load in Blender, see BUILD.md, Known limits.
Rerun it whenever isolated changes.

Usage: python -I preload_list.py <isolated folder> <output file>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pelib import PE


def imports(path):
    pe = PE(path)
    names = {name.lower() for _, name, _ in pe.import_names()}
    pe.close()
    return names


root, output = sys.argv[1:3]
folder = os.path.join(root, "bin")
in_bin = {name.lower(): name for name in os.listdir(folder) if name.lower().endswith(".dll")}


def closure(start):
    """Everything in bin that start reaches through import tables."""
    seen, stack = set(), list(start)
    while stack:
        name = stack.pop()
        if name in in_bin and name not in seen:
            seen.add(name)
            stack.extend(imports(os.path.join(folder, in_bin[name])))
    return seen


plugin = closure(["hd_moonray_debug.dll"])
dsos = set()
shaders = [name for name in os.listdir(os.path.join(root, "rdl2dso")) if name.endswith(".so")]
for name in shaders:
    dsos |= imports(os.path.join(root, "rdl2dso", name))


def needs_old_usd(name):
    return any(n.startswith("usd_") for reached in closure([name]) for n in imports(os.path.join(folder, in_bin[reached])))


extra = {name for name in closure(dsos) - plugin if not needs_old_usd(name)}
with open(output, "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(["hd_moonray_debug.dll"] + [in_bin[name] for name in sorted(extra)]) + "\n")
print(f"plugin closure {len(plugin)}, {len(shaders)} shaders, {len(extra)} DLLs only they need:", sorted(extra))
