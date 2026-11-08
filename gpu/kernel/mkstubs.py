#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""The package has its ISPC stubs flat in shading/ispc/, the sources include them from subfolders.
Write one-line forwarding headers into out/stubs so the MoonRay headers compile. Nothing else is touched."""
import os, re, pathlib
HERE = pathlib.Path(__file__).resolve().parent
for var in ('NANNA_HD', 'NANNA_MOONRAY_PKG'):
    if not os.environ.get(var): raise SystemExit(var + ' is not set: set it in config.bat, then run "call config.bat".')
HD = pathlib.Path(os.environ['NANNA_HD']).resolve()  # its src/moonray is the MoonRay source checkout
PKG = pathlib.Path(os.environ['NANNA_MOONRAY_PKG']).resolve()
flat = {p.name: p for p in (PKG/'include').rglob('*_ispc_stubs.h')}
src = HD/'src/moonray/lib/rendering/shading'
out = HERE/'out/stubs'
seen = set()
for f in list(src.rglob('*.h')) + list(src.rglob('*.hh')):
    for m in re.finditer(r'#include\s+<(moonray/[^>]*_ispc_stubs\.h)>', f.read_text(errors='ignore')):
        seen.add(m.group(1))
miss = []
for inc in sorted(seen):
    name = inc.rsplit('/', 1)[1]
    if name not in flat: miss.append(inc); continue
    d = out/inc
    if flat[name].as_posix().endswith(inc): continue
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text('#include "%s"\n' % flat[name].as_posix())
print('forwarded', len(seen)-len(miss), 'missing', miss)
