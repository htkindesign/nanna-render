# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Generate the USD headers hdMoonray needs in place of the source tree's, with Blender's USD configuration.

Writes <include dir>\\pxr\\pxr.h from pxr\\pxr.h.in and <include dir>\\pxr\\base\\work\\impl.h from pxr\\base\\work\\impl.h.in.
Also writes <include dir>\\pxr\\base\\arch\\attributes.h, the stock header with Blender's section names.

Usage: python -I gen_pxr_h.py <OpenUSD dir> <include dir>
"""
import os
import re
import sys

PXR_H = {
    "PXR_MAJOR_VERSION": "0",
    "PXR_MINOR_VERSION": "25",
    "PXR_PATCH_VERSION": "8",
    "PXR_VERSION": "2508",
    "PXR_USE_NAMESPACES": "1",
    "PXR_EXTERNAL_NAMESPACE": "pxr",
    "PXR_INTERNAL_NAMESPACE": "pxrBlender_v25_08",
    "PXR_PYTHON_SUPPORT_ENABLED": "1",
    "PXR_PREFER_SAFETY_OVER_SPEED": "1",
}
IMPL_H = {"PXR_WORK_IMPL_HEADER": '"pxr/base/work/workTBB/impl.h"'}


def configure(source, target, values):
    with open(source, newline="") as f:
        text = f.read()
    used = set(re.findall(r"@(\w+)@", text))
    if used != set(values):
        raise SystemExit(f"{source}: unset {used - set(values)}, unused {set(values) - used}")
    text = re.sub(r"@(\w+)@", lambda m: values[m.group(1)], text)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", newline="") as f:
        f.write(text)
    print("wrote", target, len(text), "bytes; variables", len(used))


usd, include = sys.argv[1:3]
configure(os.path.join(usd, "pxr", "pxr.h.in"), os.path.join(include, "pxr", "pxr.h"), PXR_H)
configure(os.path.join(usd, "pxr", "base", "work", "impl.h.in"), os.path.join(include, "pxr", "base", "work", "impl.h"), IMPL_H)
# Blender patches its USD to scan .pxbctor/.pxbdtor. A plugin built from the stock header puts its TF_REGISTRY_FUNCTIONs in
# .pxrctor, nothing scans that, and USD reports "missing TfType registration". Measured in usd_ms.dll's section names.
ATTRIBUTES_NOTE = (
    "// Generated copy of OpenUSD v25.08 pxr/base/arch/attributes.h. Blender patches USD to scan .pxbctor/.pxbdtor, not .pxrctor/.pxrdtor,\n"
    "// so on MSVC a plugin built from the stock header never has its TF_REGISTRY_FUNCTIONs run. Measured in usd_ms.dll sections.\n"
)


def blender_attributes(source, target):
    with open(source, newline="") as f:
        text = f.read()
    for section in ("ctor", "dtor"):
        if f".pxr{section}" not in text:
            raise SystemExit(f"{source}: no .pxr{section}")
        text = text.replace(f".pxr{section}", f".pxb{section}")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", newline="") as f:
        f.write(ATTRIBUTES_NOTE + text)
    print("wrote", target, len(text), "bytes")


blender_attributes(os.path.join(usd, "pxr", "base", "arch", "attributes.h"), os.path.join(include, "pxr", "base", "arch", "attributes.h"))
