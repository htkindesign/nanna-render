# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Add the licence texts to a built add-on zip: LICENSE (Nanna Render's GPL-3.0 text), THIRD_PARTY.md and third_party\\ go into the
add-on folder of the zip (the folder holding blender_manifest.toml), written to a new zip and validated with Blender's own extension
tool. THIRD_PARTY.md and third_party\\ come from beside this script; LICENSE from beside this script, else from its parent folder (the
repo root). The input zip is only read. Any LICENSE, THIRD_PARTY.md or third_party/ already in it is replaced. Never touches the user's Blender: the
validation runs on its own empty user folder.

Usage: python -I add_third_party.py <add-on zip> [output zip]
  output defaults to <input name>-third_party.zip beside the input
  NANNA_BLENDER  blender.exe of Blender 5.1.2 [C:\\Program Files\\Blender Foundation\\Blender 5.1\\blender.exe]
Checks every original entry and every added file in the output byte for byte; exit 0 only if that and validation pass.
"""
import os
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BLENDER = os.environ.get("NANNA_BLENDER") or r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
MD = os.path.join(HERE, "THIRD_PARTY.md")
TREE = os.path.join(HERE, "third_party")
LICENSE = next((p for p in (os.path.join(HERE, "LICENSE"), os.path.join(os.path.dirname(HERE), "LICENSE")) if os.path.isfile(p)), None)

if len(sys.argv) not in (2, 3):
    sys.exit(__doc__)
source = os.path.abspath(sys.argv[1])
output = os.path.abspath(sys.argv[2]) if len(sys.argv) == 3 else os.path.splitext(source)[0] + "-third_party.zip"
if output == source:
    sys.exit("output must differ from the input zip")
if not os.path.isfile(MD) or not os.path.isdir(TREE):
    sys.exit(f"missing {MD} or {TREE}")
with open(LICENSE or os.devnull, encoding="utf-8") as f:
    if not LICENSE or not f.read(200).strip().startswith("GNU GENERAL PUBLIC LICENSE\n                       Version 3"):
        sys.exit(f"no GPL-3.0 LICENSE beside {HERE} or in its parent folder")

# What to add, as zip names relative to the add-on folder.
added = {"LICENSE": LICENSE, "THIRD_PARTY.md": MD}
for folder, _, names in os.walk(TREE):
    for name in names:
        path = os.path.join(folder, name)
        added[os.path.relpath(path, HERE).replace(os.sep, "/")] = path

with zipfile.ZipFile(source) as zin:
    manifests = [n for n in zin.namelist() if n.rsplit("/", 1)[-1] == "blender_manifest.toml"]
    if len(manifests) != 1:
        sys.exit(f"expected one blender_manifest.toml in {source}, found {manifests}")
    prefix = manifests[0][: -len("blender_manifest.toml")]  # "" when the add-on files are at the zip root
    ours = lambda n: n in (prefix + "LICENSE", prefix + "THIRD_PARTY.md") or n.startswith(prefix + "third_party/")
    kept = [i for i in zin.infolist() if not ours(i.filename)]
    replaced = len(zin.infolist()) - len(kept)
    tmp = output + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in kept:
            zout.writestr(info, zin.read(info), compress_type=info.compress_type)
        for name in sorted(added):
            zout.write(added[name], prefix + name)
os.replace(tmp, output)
print(f"{output}: kept {len(kept)} entries, replaced {replaced}, added {len(added)} under '{prefix or '<zip root>'}'", flush=True)

problems = []
with zipfile.ZipFile(source) as zin, zipfile.ZipFile(output) as zout:
    names = set(zout.namelist())
    for info in kept:  # by name: writestr() moved these ZipInfo offsets to the output
        if info.filename not in names or (not info.is_dir() and zout.read(info.filename) != zin.read(info.filename)):
            problems.append("changed or missing: " + info.filename)
    for name, path in added.items():
        if prefix + name not in names or zout.read(prefix + name) != open(path, "rb").read():
            problems.append("not added correctly: " + name)
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
