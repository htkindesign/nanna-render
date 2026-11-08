# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Check hdMoonray's parsed USD and TBB imports against the parsed exports of usd_ms.dll and tbb.dll.

Mangled names carry the USD namespace, so names are compared both as they stand and with the package's namespace
replaced by Blender's. MSVC mangling has no length prefixes, so the replacement is exact.

Usage: python -I check_imports.py <usd json> <tbb json> <package bin> <out json>
"""
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pelib import PE

usd_info, tbb_info, package_bin, out = sys.argv[1:5]
usd = json.load(open(usd_info))["exports"]
tbb = json.load(open(tbb_info))["exports"]
CONSUMERS = ["hd_moonray_debug.dll", "hydramoonray.dll", "hdMoonrayAdapters.dll", "moonrayShaderDiscovery.dll", "moonrayShaderParser.dll"]
BLENDER = "pxrBlender_v25_08__pxrReserved__"
NS = re.compile(r"pxr[A-Za-z0-9_]*?__pxrReserved__")

blender_ns = collections.Counter(m.group(0) for n in usd for m in [NS.search(n)] if m)
print("namespaces in usd_ms exports:", blender_ns.most_common(5))
print("usd_ms exports without a pxr namespace:", sum(1 for n in usd if not NS.search(n)))

report = {}
all_usd, all_tbb = {}, {}
for consumer in CONSUMERS:
    pe = PE(os.path.join(package_bin, consumer))
    usd_syms, tbb_syms, ordinals, others = set(), set(), 0, collections.Counter()
    for dll, names, ords, table in pe.imports():
        low = dll.lower()
        if low.startswith("usd_"):
            usd_syms.update((dll, n) for n in names)
            ordinals += len(ords)
        elif low == "tbb.dll":
            tbb_syms.update(names)
            ordinals += len(ords)
        else:
            others[dll] = len(names)
    pe.close()
    ns = collections.Counter(m.group(0) for _, n in usd_syms for m in [NS.search(n)] if m)
    print(f"\n{consumer}: USD imports {len(usd_syms)}, TBB imports {len(tbb_syms)}, by-ordinal {ordinals}, namespaces {ns.most_common(3)}")
    for item in usd_syms:
        all_usd.setdefault(item[1], set()).add(item[0])
    for s in tbb_syms:
        all_tbb.setdefault(s, set()).add(consumer)
    report[consumer] = {"usd": len(usd_syms), "tbb": len(tbb_syms), "other_dlls": others}


def mapped(name):
    return NS.sub(BLENDER, name)


verbatim = sum(1 for n in all_usd if n in usd)
found = {n for n in all_usd if mapped(n) in usd}
missing = sorted(set(all_usd) - found)
kinds = collections.Counter(usd[mapped(n)] for n in found)
print(f"\nUnion of USD imports: {len(all_usd)}; exact name in usd_ms {verbatim}; after namespace map found {len(found)}, missing {len(missing)}")
print("found by kind", kinds)
per_lib = collections.Counter()
for n in missing:
    for lib in all_usd[n]:
        per_lib[lib] += 1
print("missing by source DLL", per_lib.most_common())
for consumer in CONSUMERS:
    pe = PE(os.path.join(package_bin, consumer))
    names = {n for dll, ns, _, _ in pe.imports() if dll.lower().startswith("usd_") for n in ns}
    pe.close()
    print(f"  {consumer}: found {sum(1 for n in names if mapped(n) in usd)}, missing {sum(1 for n in names if mapped(n) not in usd)}")
core = {n for n in all_usd}
pair = set()
for consumer in ("hd_moonray_debug.dll", "hydramoonray.dll"):
    pe = PE(os.path.join(package_bin, consumer))
    pair |= {n for dll, ns, _, _ in pe.imports() if dll.lower().startswith("usd_") for n in ns}
    pe.close()
print(f"hd_moonray_debug + hydramoonray union: {len(pair)}, found {sum(1 for n in pair if mapped(n) in usd)}, missing {sum(1 for n in pair if mapped(n) not in usd)}")

tbb_found = {n for n in all_tbb if n in tbb}
tbb_missing = sorted(set(all_tbb) - tbb_found)
print(f"TBB imports: {len(all_tbb)}, found {len(tbb_found)}, missing {len(tbb_missing)}; by kind {collections.Counter(tbb[n] for n in tbb_found)}")
for n in sorted(all_tbb):
    print("   tbb import:", n)

with open(out, "w") as f:
    json.dump({"consumers": report, "usd_union": len(all_usd), "usd_found": len(found), "usd_missing": [(n, sorted(all_usd[n])) for n in missing],
               "tbb_union": len(all_tbb), "tbb_found": len(tbb_found), "tbb_missing": tbb_missing}, f, indent=1)
print("\nfirst missing:")
for n in missing[:40]:
    print("  ", sorted(all_usd[n]), n)
