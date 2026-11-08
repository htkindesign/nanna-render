# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Leave out of a built add-on zip the runtime files that exclude.txt (beside this script) lists, written to a new zip and validated
with Blender's own extension tool. The input zip is only read. Never touches the user's Blender: validation runs on its own empty user folder.

Usage: python -I apply_exclude.py <add-on zip> [output zip]
         output defaults to <input name>-trimmed.zip beside the input
       python -I apply_exclude.py --folder <runtime folder>
         deletes the matching files from that folder instead (for a test copy; never point it at a source folder)
  NANNA_BLENDER  blender.exe of Blender 5.1.2 [C:\\Program Files\\Blender Foundation\\Blender 5.1\\blender.exe]
exclude.txt: one glob per line relative to the add-on's runtime\\ folder, '#' starts a comment; '*' and '?' stay within one folder, '**'
crosses folders; matching ignores case. Exit 0 only if every pattern matched something, every kept entry is byte-identical, nothing a
pattern matches is left, the Hydra plugin is still there, and validation passes.
"""
import os
import re
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BLENDER = os.environ.get("NANNA_BLENDER") or r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
MUST_KEEP = "bin/hd_moonray_debug.dll"  # the Hydra plugin: a list that removes it is wrong


def patterns():
    out = []
    with open(os.path.join(HERE, "exclude.txt"), encoding="utf-8") as f:
        for line in f:
            glob = line.split("#", 1)[0].strip()
            if glob:
                regex = "".join(".*" if part == "**" else "[^/]*" if part == "*" else "[^/]" if part == "?" else re.escape(part)
                                for part in re.split(r"(\*\*|\*|\?)", glob))
                out.append((glob, re.compile(regex + r"\Z", re.IGNORECASE)))
    return out


def excluded(relative, globs, hits):
    """relative: a path inside runtime\\ with '/' separators."""
    found = False
    for glob, regex in globs:
        if regex.match(relative):
            hits[glob] = hits.get(glob, 0) + 1
            found = True
    return found


def check(globs, hits, kept_names):
    problems = [f"pattern matched nothing: {g}" for g, _ in globs if not hits.get(g)]
    if MUST_KEEP not in {n.lower() for n in kept_names}:
        problems.append(f"{MUST_KEEP} is missing or excluded")
    return problems


globs = patterns()
hits = {}
if len(sys.argv) == 3 and sys.argv[1] == "--folder":
    root = os.path.abspath(sys.argv[2])
    removed, size, kept = [], 0, []
    for folder, _, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if excluded(relative, globs, hits):
                size += os.path.getsize(path)
                removed.append(relative)
                os.remove(path)
            else:
                kept.append(relative)
    for folder, subfolders, names in os.walk(root, topdown=False):  # folders the removal emptied
        if folder != root and not os.listdir(folder):
            os.rmdir(folder)
    for glob, _ in globs:
        print(f"{hits.get(glob, 0):4} {glob}")
    print(f"removed {len(removed)} files, {size / 2**20:.1f} MiB; kept {len(kept)}")
    problems = check(globs, hits, kept)
    print("problems:", problems or "none")
    sys.exit(1 if problems else 0)

if len(sys.argv) not in (2, 3):
    sys.exit(__doc__)
source = os.path.abspath(sys.argv[1])
output = os.path.abspath(sys.argv[2]) if len(sys.argv) == 3 else os.path.splitext(source)[0] + "-trimmed.zip"
if output == source:
    sys.exit("output must differ from the input zip")

with zipfile.ZipFile(source) as zin:
    manifests = [n for n in zin.namelist() if n.rsplit("/", 1)[-1] == "blender_manifest.toml"]
    if len(manifests) != 1:
        sys.exit(f"expected one blender_manifest.toml in {source}, found {manifests}")
    runtime = manifests[0][: -len("blender_manifest.toml")] + "runtime/"
    kept, dropped, size = [], [], 0
    for info in zin.infolist():
        inside = info.filename.startswith(runtime) and not info.is_dir()
        if inside and excluded(info.filename[len(runtime):], globs, hits):
            dropped.append(info.filename)
            size += info.file_size
        else:
            kept.append(info)
    tmp = output + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in kept:
            zout.writestr(info, zin.read(info), compress_type=info.compress_type)
os.replace(tmp, output)
for glob, _ in globs:
    print(f"{hits.get(glob, 0):4} {glob}")
print(f"{output}: kept {len(kept)} entries, left out {len(dropped)} ({size / 2**20:.1f} MiB uncompressed) under '{runtime}'", flush=True)

problems = check(globs, hits, [i.filename[len(runtime):] for i in kept if i.filename.startswith(runtime)])
with zipfile.ZipFile(source) as zin, zipfile.ZipFile(output) as zout:
    names = set(zout.namelist())
    for info in kept:  # by name: writestr() moved these ZipInfo offsets to the output
        if info.filename not in names or (not info.is_dir() and zout.read(info.filename) != zin.read(info.filename)):
            problems.append("changed or missing: " + info.filename)
    for name in dropped:
        if name in names:
            problems.append("still present: " + name)
    bad = zout.testzip()
    if bad:
        problems.append("CRC error: " + bad)

with tempfile.TemporaryDirectory(prefix="nanna_validate_") as home:  # an empty user folder, so validation can't start the user's add-ons
    env = dict(os.environ, BLENDER_USER_RESOURCES=home)
    env.pop("HDMOONRAY_ROOT", None)
    code = subprocess.call([BLENDER, "--command", "extension", "validate", output], env=env, timeout=600)
print("extension validate exit", code)
if code:
    problems.append(f"blender extension validate exit {code}")
print("problems:", problems or "none")
sys.exit(1 if problems else 0)
