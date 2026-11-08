# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Re-apply edit_hdmoonray.py to the copy (hdMoonray_src beside this script), copying only the files that change, so the build stays incremental.

Usage: python -I refresh_hdmoonray.py <hdMoonray clone> <copy>
"""
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile

source, copy = sys.argv[1:3]
here = os.path.dirname(os.path.abspath(__file__))
with tempfile.TemporaryDirectory() as temp:
    fresh = os.path.join(temp, "src")
    subprocess.run([sys.executable, "-I", os.path.join(here, "edit_hdmoonray.py"), source, fresh], check=True)
    for folder, _, files in os.walk(fresh):
        for name in files:
            new = os.path.join(folder, name)
            old = os.path.join(copy, os.path.relpath(new, fresh))
            if not os.path.exists(old) or not filecmp.cmp(new, old, shallow=False):
                os.makedirs(os.path.dirname(old), exist_ok=True)
                shutil.copy2(new, old)
                print("refreshed", os.path.relpath(new, fresh))
