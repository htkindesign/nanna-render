# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Make a private copy of the MoonRay package whose colliding DLLs have same-length private names.

Copies <package>\\bin and <package>\\rdl2dso to the output folder, renames each colliding DLL, then changes only the DLL
name strings found by walking the import, delay-load and bound-import tables. Every changed byte is checked against
the original afterwards. The package itself is only read.

One string outside those tables is changed too: bin\\tbb.dll finds tbbmalloc.dll by name at run time, so patch_embedded
changes that single string. It stops unless the ASCII form occurs exactly once and the UTF-16 forms not at all.

Usage: python -I patch_imports.py <package root> <isolated root>
"""
import filecmp
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pelib import PE, NotPE, DIR_DELAY, DIR_BOUND, DIR_EXPORT, DIR_SECURITY

RENAMES = {  # one character changes, so every string keeps its length
    "OpenImageIO.dll": "OpenImageIm.dll",
    "OpenImageIO_Util.dll": "OpenImageIO_Utim.dll",
    "OpenEXR.dll": "OpenEXm.dll",
    "OpenEXRCore.dll": "OpenEXRCorm.dll",
    "OpenEXRUtil.dll": "OpenEXRUtim.dll",
    "Iex.dll": "Iem.dll",
    "IlmThread.dll": "IlmThream.dll",
    "openvdb.dll": "openvdm.dll",
    "embree4.dll": "embreem.dll",
    "tbbmalloc.dll": "tbbmallom.dll",
    # Blender also loads these names.
    "tbbmalloc_proxy.dll": "tbbmalloc_proxm.dll",
    "OpenImageDenoise.dll": "OpenImageDenoism.dll",
    "OpenImageDenoise_core.dll": "OpenImageDenoise_corm.dll",
}
LOWER = {k.lower(): v for k, v in RENAMES.items()}
EMBEDDED_FILE = os.path.join("bin", "tbb.dll")
EMBEDDED_OLD = "tbbmalloc.dll"
EMBEDDED_NEW = RENAMES[EMBEDDED_OLD]


def pe_files(root):
    for folder, _, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            try:
                pe = PE(path)
            except (NotPE, ValueError):
                continue
            yield path, pe


def patch_file(path, pe):
    """Return [(table, old, new, offset)] after writing the changes, or an empty list."""
    edits = {}
    for table, name, offset in pe.import_names():
        new = LOWER.get(name.lower())
        if new is None:
            continue
        if len(new) != len(name):
            raise ValueError(f"{path}: {name} -> {new} changes length")
        edits.setdefault(offset, (table, name, new))
    pe.close()
    if not edits:
        return []
    with open(path, "r+b") as f:
        for offset, (table, old, new) in edits.items():
            f.seek(offset)
            if f.read(len(old) + 1) != old.encode("latin1") + b"\0":
                raise ValueError(f"{path}: name at {offset:#x} is not {old}")
            f.seek(offset)
            f.write(new.encode("latin1"))
    return [(table, old, new, offset) for offset, (table, old, new) in edits.items()]


def find_forms(data, text):
    """Offsets of text in data, ignoring ASCII case, as {form: [offsets]}."""
    low = data.lower()
    forms = {"ascii": "latin1", "utf16le": "utf-16-le", "utf16be": "utf-16-be"}
    return {form: [m.start() for m in re.finditer(re.escape(text.lower().encode(codec)), low)] for form, codec in forms.items()}


def patch_embedded(path):
    """Change the one embedded EMBEDDED_OLD string in path to EMBEDDED_NEW and return a record of it."""
    with open(path, "rb") as f:
        data = f.read()
    before, new_before = find_forms(data, EMBEDDED_OLD), find_forms(data, EMBEDDED_NEW)
    counts = {form: len(hits) for form, hits in before.items()}
    if counts != {"ascii": 1, "utf16le": 0, "utf16be": 0} or any(new_before.values()):
        raise SystemExit(f"{path}: expected one ASCII {EMBEDDED_OLD}, found {counts}; {EMBEDDED_NEW} found {new_before}")
    offset = before["ascii"][0]
    old, new = EMBEDDED_OLD.encode("latin1"), EMBEDDED_NEW.encode("latin1")
    if len(old) != len(new) or data[offset - 1:offset + len(old) + 1] != b"\0" + old + b"\0":
        raise SystemExit(f"{path}: string at {offset:#x} isn't a whole NUL-terminated {EMBEDDED_OLD}")
    changed = [i for i in range(len(old)) if old[i] != new[i]]
    if len(changed) != 1:
        raise SystemExit(f"{EMBEDDED_OLD} -> {EMBEDDED_NEW} must change one byte")
    pe = PE(path)
    section = next((s for s in pe.sections if s.raw <= offset < s.raw + s.rsize), None)
    signed = pe.directory(DIR_SECURITY)[0] != 0
    checksum = int.from_bytes(pe.d[pe.checksum_offset:pe.checksum_offset + 4], "little")
    pe.close()
    if section is None or section.executable or section.chars & 0x80000000:
        raise SystemExit(f"{path}: {EMBEDDED_OLD} isn't in a read-only data section")
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(new)
    with open(path, "rb") as f:
        after = f.read()
    return {"file": EMBEDDED_FILE, "old": EMBEDDED_OLD, "new": EMBEDDED_NEW, "offset": offset, "section": section.name,
            "changed_offset": offset + changed[0], "old_byte": old[changed[0]], "new_byte": new[changed[0]],
            "counts_before": counts, "counts_after": {form: len(hits) for form, hits in find_forms(after, EMBEDDED_OLD).items()},
            "new_counts_after": {form: len(hits) for form, hits in find_forms(after, EMBEDDED_NEW).items()},
            "authenticode_directory": signed, "pe_checksum_field": checksum}


def diff_positions(a, b):
    """Positions where two same-length files differ."""
    x, y = open(a, "rb").read(), open(b, "rb").read()
    if len(x) != len(y):
        raise ValueError(f"{a}: size changed")
    if x == y:
        return []
    out = []
    step = 1 << 16
    for start in range(0, len(x), step):
        if x[start:start + step] != y[start:start + step]:
            out.extend(i for i in range(start, min(start + step, len(x))) if x[i] != y[i])
    return out


def main():
    package, isolated = sys.argv[1], sys.argv[2]
    if os.path.exists(isolated):
        raise SystemExit(f"{isolated} exists; remove it first")
    os.makedirs(isolated)
    for sub in ("bin", "rdl2dso"):
        shutil.copytree(os.path.join(package, sub), os.path.join(isolated, sub))

    target_bin = os.path.join(isolated, "bin")
    present = {f.lower() for f in os.listdir(target_bin)}
    privates = [v.lower() for v in RENAMES.values()]
    if len(set(privates)) != len(privates) or set(privates) & (present | {k.lower() for k in RENAMES}):
        raise SystemExit("a private name collides")
    renamed = {}
    for old, new in RENAMES.items():
        match = [f for f in os.listdir(target_bin) if f.lower() == old.lower()]
        if not match:
            raise SystemExit(f"{old} is not in bin")
        os.rename(os.path.join(target_bin, match[0]), os.path.join(target_bin, new))
        renamed[new] = match[0]

    log, scanned = [], 0
    for path, pe in list(pe_files(isolated)):
        scanned += 1
        edits = patch_file(path, pe)
        if edits:
            log.append({"file": os.path.relpath(path, isolated), "edits": edits})

    embedded = patch_embedded(os.path.join(isolated, EMBEDDED_FILE))
    entry = next((e for e in log if e["file"] == EMBEDDED_FILE), None)
    table_bytes = {e[3] + i for e in entry["edits"] for i in range(len(e[1]))} if entry else set()
    if entry is None:
        entry = {"file": EMBEDDED_FILE, "edits": []}
        log.append(entry)
    entry["edits"].append(("embedded", EMBEDDED_OLD, EMBEDDED_NEW, embedded["offset"]))
    print("EMBEDDED", json.dumps(embedded))

    # Verify: every differing byte is inside a patched name string, and no colliding import remains.
    changed_bytes = bad = 0
    tbb_before = open(os.path.join(package, EMBEDDED_FILE), "rb").read()
    tbb_after = open(os.path.join(isolated, EMBEDDED_FILE), "rb").read()
    at = embedded["changed_offset"]
    extra = set(diff_positions(os.path.join(package, EMBEDDED_FILE), os.path.join(isolated, EMBEDDED_FILE))) - table_bytes
    embedded["bytes_changed_outside_import_tables"] = len(extra)
    if (extra != {at} or tbb_before[at] != embedded["old_byte"] or tbb_after[at] != embedded["new_byte"]
            or embedded["counts_after"] != {"ascii": 0, "utf16le": 0, "utf16be": 0}
            or embedded["new_counts_after"] != {"ascii": 1, "utf16le": 0, "utf16be": 0}):
        bad += 1
        print("UNEXPECTED EMBEDDED PATCH", sorted(extra)[:20], embedded)
    for entry in log:
        rel = entry["file"]
        name = os.path.basename(rel)
        original = os.path.join(package, os.path.dirname(rel), renamed.get(name, name))
        allowed = {e[3] + i for e in entry["edits"] for i in range(len(e[1]))}
        positions = diff_positions(original, os.path.join(isolated, rel))
        changed_bytes += len(positions)
        if not positions or not set(positions) <= allowed:
            bad += 1
            print("UNEXPECTED DIFF", rel, len(positions))
    left = []
    for path, pe in pe_files(isolated):
        left.extend((path, n) for _, n, _ in pe.import_names() if n.lower() in LOWER)
        pe.close()
    unchanged_ok = 0
    for path, pe in pe_files(isolated):
        pe.close()
        rel = os.path.relpath(path, isolated)
        if rel not in {e["file"] for e in log}:
            name = os.path.basename(rel)
            if filecmp.cmp(os.path.join(package, os.path.dirname(rel), renamed.get(name, name)), path, shallow=False):
                unchanged_ok += 1
            else:
                bad += 1
                print("UNPATCHED FILE DIFFERS", rel)

    by_table = {}
    for entry in log:
        for table, old, *_ in entry["edits"]:
            by_table.setdefault(table, {}).setdefault(old, 0)
            by_table[table][old] += 1
    summary = {"pe_files_scanned": scanned, "files_patched": len(log), "name_strings_patched": sum(len(e["edits"]) for e in log),
               "bytes_changed": changed_bytes, "unpatched_pe_identical": unchanged_ok, "unexpected": bad,
               "colliding_imports_left": left, "by_table": by_table, "renamed": renamed, "embedded_string": embedded}
    with open(os.path.join(os.path.dirname(isolated.rstrip("\\/")), "patch_log.json"), "w") as f:
        json.dump({"summary": summary, "files": log}, f, indent=1)
    print(json.dumps(summary, indent=1))
    return 1 if bad or left else 0


if __name__ == "__main__":
    sys.exit(main())
