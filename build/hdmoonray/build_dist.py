# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Build the installable add-on zip: the add-on folder, the MoonRay runtime as runtime, and optionally pbrt.exe's folder as runtime\\gpu,
built and validated with Blender's own extension tool. Never touches the user's Blender: the build runs on its own user folder.

Usage: python -I build_dist.py [output zip]
Roots (config.example.bat at the top of the repo), defaults in brackets:
  NANNA_BLENDER      blender.exe of Blender 5.1.2 [C:\\Program Files\\Blender Foundation\\Blender 5.1\\blender.exe]
  NANNA_ADDON        the add-on folder, the repo's addon [required]
  NANNA_RUNTIME      the MoonRay runtime, deploy.bat's isolated folder [isolated beside this script]
  NANNA_GPU_RUNTIME  pbrt.exe, its VERSION.txt and the MSVC runtime DLLs; optional, no Device GPU without it [not included]
  NANNA_DIST         output folder [dist beside this script]
Writes <dist>\\moonray_hydra-<version>-windows-x64.zip and checks every add-on file in it byte for byte.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BLENDER = os.environ.get("NANNA_BLENDER") or r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
ADDON = os.environ.get("NANNA_ADDON") or sys.exit('NANNA_ADDON is not set: set it in config.bat, then run "call config.bat".')
RUNTIME = os.environ.get("NANNA_RUNTIME") or os.path.join(HERE, "isolated")
GPU = os.environ.get("NANNA_GPU_RUNTIME", "")
DIST = os.environ.get("NANNA_DIST") or os.path.join(HERE, "dist")
STAGE = os.path.join(DIST, "stage", "moonray_hydra")

home = os.path.join(DIST, "home_build")  # an empty user folder, so the build can't start the user's add-ons
env = dict(os.environ, BLENDER_USER_RESOURCES=home)
env.pop("HDMOONRAY_ROOT", None)
os.makedirs(home, exist_ok=True)
manifest = open(os.path.join(ADDON, "blender_manifest.toml"), encoding="utf-8").read()
version = re.search(r'^version = "(.+)"', manifest, re.M)[1]
output = sys.argv[1] if len(sys.argv) > 1 else os.path.join(DIST, f"moonray_hydra-{version}-windows-x64.zip")
shutil.rmtree(os.path.dirname(STAGE), ignore_errors=True)
os.makedirs(STAGE)
files = sorted(n for n in os.listdir(ADDON) if os.path.isfile(os.path.join(ADDON, n)) and not n.endswith(".pyc"))
for name in files:
    shutil.copy2(os.path.join(ADDON, name), STAGE)
shutil.copytree(RUNTIME, os.path.join(STAGE, "runtime"))
if GPU:
    shutil.copytree(GPU, os.path.join(STAGE, "runtime", "gpu"))
for command in (["build", "--source-dir", STAGE, "--output-filepath", output], ["validate", output]):
    code = subprocess.call([BLENDER, "--command", "extension", *command], env=env, timeout=600)
    print("extension", command[0], "exit", code, flush=True)
    if code:
        sys.exit(code)
shutil.rmtree(os.path.dirname(STAGE))
with zipfile.ZipFile(output) as z:
    names = set(z.namelist())
    missing = [n for n in files if n not in names]
    different = [n for n in files if n in names and z.read(n) != open(os.path.join(ADDON, n), "rb").read()]
print("add-on files", len(files), "missing", missing, "different", different)
digest = hashlib.sha256()
with open(output, "rb") as f:
    for block in iter(lambda: f.read(2**22), b""):
        digest.update(block)
print(output, os.path.getsize(output), "bytes sha256", digest.hexdigest())
sys.exit(1 if missing or different else 0)
