# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Device GPU: F12 and the Rendered viewport through the hdNannaGpu Hydra delegate (runtime\\gpu_hydra), which runs Nanna's nanna_gpu.exe
(a modified pbrt-v4) as a long-lived server process (runtime\\gpu, --nanna-serve). Device CPU never comes here and stays on hdMoonray.

The delegate does meshes, instances and the camera from Hydra, keeps one pbrt server per viewport (a camera move is a camera command,
about 1 s to a first picture) and fills Blender's render buffers. Python writes what only native.py can translate, with gpu.Export:
one pbrt block per material keyed by Hydra's material name, textures, lights and world, into a folder the delegate is told about.
A pbrt fault is a failed frame, not a Blender crash: pbrt stays a child process. Nothing is killed at exit: every server gets its
stdin closed and exits by itself.
"""
import atexit
import ctypes
import hashlib
import os
import shutil
import tempfile
import time

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(HERE, "runtime", "gpu_hydra")
PLUGIN = "hdNannaGpu"
DELEGATE_ID = "HdNannaGpuRendererPlugin"
SERVER_VERSION = "nanna_gpu pbrt-v4 b0bdf46+patch_pbrt+f12+server+memory+aux 2026-11-08"  # first line of runtime\gpu\VERSION.txt
SERVER_SHA256 = "b121a7e8f18473418e19f6635a4300585053f09c049405b861b62eac43aa1ff0"  # gpu\delegate\pbrt build9 (patch_server.py + patch_memory.py + patch_aux.py)
_registered = False
_checked = {}  # path -> (size, mtime) that passed the hash check
_viewport_folders = []  # newest last; one folder per viewport input change, so a running pbrt never reads a half-written file


def find_server():
    """runtime\\gpu\\nanna_gpu.exe if it is the server build this add-on was made with. Raises the reason it can't run."""
    folder = os.path.join(HERE, "runtime", "gpu")
    exe = os.path.join(folder, "nanna_gpu.exe")
    if not os.path.isfile(exe):
        raise RuntimeError("GPU: no nanna_gpu.exe in the add-on's runtime\\gpu folder")
    try:
        with open(os.path.join(folder, "VERSION.txt"), encoding="utf-8") as f:
            version = f.readline().strip()
    except OSError:
        version = ""
    if version != SERVER_VERSION:
        raise RuntimeError(f"GPU: runtime\\gpu is '{version or 'unversioned'}', this add-on needs '{SERVER_VERSION}'")
    stat = os.stat(exe)
    if _checked.get(exe) != (stat.st_size, stat.st_mtime):
        with open(exe, "rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != SERVER_SHA256:
                raise RuntimeError("GPU: nanna_gpu.exe isn't the build this add-on was made with. Reinstall the add-on")
        _checked[exe] = (stat.st_size, stat.st_mtime)
    if not os.path.isfile(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvcuda.dll")):
        raise RuntimeError("GPU: no NVIDIA driver found (nvcuda.dll). Device GPU needs an NVIDIA RTX card and its driver")
    return exe


def setup():
    """Hands the plugin to Blender's USD once. The Blender version and usd_ms.dll checks are the CPU path's: same Blender, same USD."""
    global _registered
    if _registered:
        return
    from . import runtime
    runtime.check_blender()
    import pxr.Plug
    registry = pxr.Plug.Registry()
    registry.RegisterPlugins(os.path.join(RUNTIME, "plugin"))
    if registry.GetPluginWithName(PLUGIN) is None:
        raise RuntimeError(f"GPU: the Hydra plugin wasn't found under {RUNTIME}. Reinstall the add-on")
    _registered = True


def plugin_loaded():
    return bool(ctypes.windll.kernel32.GetModuleHandleW(PLUGIN + ".dll"))


def plugin_dll():
    return ctypes.WinDLL(os.path.join(RUNTIME, "bin", PLUGIN + ".dll"))  # loaded by USD by now: this only finds it


def warm(exe):
    """The delegate keeps a pbrt server started and waiting (its pool), so the next F12 or viewport skips the start-up (about 10 s)."""
    if plugin_loaded():
        plugin_dll().nannaGpuWarm(exe.encode("utf-8"))


def warm_up():
    """Device GPU chosen or a file with it opened: the plugin is loaded through USD and a server starts now. Raises the reason it can't."""
    setup()
    exe = find_server()
    import pxr.Plug
    pxr.Plug.Registry().GetPluginWithName(PLUGIN).Load()
    warm(exe)


def last_error():
    if not plugin_loaded():
        return ""
    dll = plugin_dll()
    dll.nannaGpuLastError.restype = ctypes.c_char_p
    return explain(dll.nannaGpuLastError().decode("utf-8", "replace"))


MIN_DRIVER = "551.61"  # the Windows driver of CUDA 12.4, which nanna_gpu.exe is built with (README, Requirements)
# What CUDA's runtime/driver API and OptiX print when the card or driver can't run nanna_gpu.exe (pbrt's CUDA_CHECK, CU_CHECK, OPTIX_CHECK)
DRIVER_ERRORS = ("insufficient for cuda runtime", "no cuda-capable device", "cuda_error_no_device", "cudaerrornodevice",
                 "cudaerrorinsufficientdriver", "cuda_error_system_driver_mismatch", "unsupported toolchain", "unsupported_ptx_version",
                 "unsupportedptxversion", "ptx jit", "no kernel image", "no_binary_for_gpu", "nobinaryforgpu", "optixinit",
                 "optix_error_unsupported_abi_version", "optix_error_library", "code 7801", "code 7804", "unsupported abi version",
                 "nvoptix", "cuda_error_not_initialized", "cudaerrorinitializationerror", "initialization error")


def explain(error):
    """A driver or card that can't run nanna_gpu.exe gets the one thing to do about it in front of pbrt's own words."""
    if error and any(key in error.lower() for key in DRIVER_ERRORS):
        return f"Device GPU needs an NVIDIA RTX card and driver {MIN_DRIVER} or newer. ({error})"
    return error


def pool_info():
    if not plugin_loaded():
        return "not loaded"
    dll = plugin_dll()
    dll.nannaGpuPoolInfo.restype = ctypes.c_char_p
    return dll.nannaGpuPoolInfo().decode()


def shutdown():
    """Device CPU, unregister and Blender's exit: every pbrt server gets its stdin closed and exits by itself (a rendering one after its
    current sample pass). Nothing waits, nothing is killed."""
    if plugin_loaded():
        plugin_dll().nannaGpuShutdown()
    while _viewport_folders:
        shutil.rmtree(_viewport_folders.pop(), ignore_errors=True)


atexit.register(shutdown)


def same_inputs(old, new):
    """Every file write_inputs made byte-equal to the folder before. The delegate's own files are left out."""
    import filecmp
    names = sorted(n for n in os.listdir(new) if os.path.isfile(os.path.join(new, n)))
    return all(os.path.isfile(os.path.join(old, n)) and filecmp.cmp(os.path.join(old, n), os.path.join(new, n), shallow=False) for n in names)


def write_inputs(depsgraph, folder, cancelled):
    """materials.txt and world.pbrt for the delegate. Returns the problems found."""
    from . import gpu
    scene = depsgraph.scene
    panel = scene.moonray_hydra
    export = gpu.Export(depsgraph, folder, panel.look, (panel.diffuse_bounces, panel.glossy_bounces), cancelled)
    blocks = []
    for block in depsgraph.ids:
        if not isinstance(block, bpy.types.Material):
            continue
        name = f"M_{block.as_pointer():016X}"  # Blender's Hydra prim name, as textures._native_lines keys Material.cc's manifest
        original = block.original
        f0 = export.pane_f0(original)
        if f0 is not None:
            text = 'Material "conductor" "rgb reflectance" [ 1 1 1 ] "float roughness" 0\n' + f'shape: "float alpha" {f0:.6f}'
        else:
            text = export.surface(original)
        blocks.append(f"@{name}\n{text}\n")
    with open(os.path.join(folder, "materials.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.writelines(blocks)
    world = []
    if scene.world is not None or any(o.type == "LIGHT" for o in depsgraph.objects):
        world = export.light_lines()
    with open(os.path.join(folder, "world.pbrt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(export.texture_lines + world) + "\n")
    return sorted(export.unmapped)


# Updates that don't change what the inputs hold (measured: making or removing any image gives Collection, Object, Scene without these flags)
QUIET = {"Image", "Scene", "Screen", "WindowManager", "WorkSpace", "Text", "Brush", "Palette", "Collection"}


def render_settings(engine, engine_type):
    """The engine's get_render_settings for Device GPU. engine.gpu_folder holds the inputs folder of this engine."""
    settings = {"aovToken:Combined": "color"}
    depsgraph = engine.depsgraph
    if depsgraph is None:
        return settings
    scene, panel = depsgraph.scene, depsgraph.scene.moonray_hydra
    exe = find_server()
    if engine_type == "VIEWPORT":
        # Called on every view_update (edits, not camera moves): new inputs in a new folder only if they changed.
        started = time.perf_counter()
        changed = [u for u in depsgraph.updates if type(u.id).__name__ not in QUIET and
                   (u.is_updated_shading or u.is_updated_geometry or u.is_updated_transform)]
        if engine.gpu_folder is None or not os.path.isdir(engine.gpu_folder) or changed:
            folder = tempfile.mkdtemp(prefix="nanna_vp_")
            problems = write_inputs(depsgraph, folder, lambda: False)
            if engine.gpu_folder and os.path.isdir(engine.gpu_folder) and same_inputs(engine.gpu_folder, folder):
                shutil.rmtree(folder, ignore_errors=True)
            else:
                _viewport_folders.append(folder)
                while len(_viewport_folders) > 3:
                    shutil.rmtree(_viewport_folders.pop(0), ignore_errors=True)
                engine.gpu_folder = folder
                print(f"[moonray_hydra] GPU viewport inputs in {time.perf_counter() - started:.3f} s", "unmapped:" if problems else "", problems or "", flush=True)
        warm(exe)
        settings.update({"nanna:sceneDir": engine.gpu_folder, "nanna:pbrt": exe, "nanna:viewport": True,
                         "nanna:spp": max(1, panel.viewport_samples) ** 2, "nanna:firstSpp": 1,
                         "nanna:seed": 0, "nanna:maxdepth": panel.max_bounces, "nanna:dof": bool(panel.enable_dof)})
    else:
        if engine.gpu_folder and engine.gpu_folder not in _viewport_folders:  # an earlier call's inputs (another view layer, a retry)
            shutil.rmtree(engine.gpu_folder, ignore_errors=True)
        engine.gpu_folder = tempfile.mkdtemp(prefix="nanna_hd_")
        problems = write_inputs(depsgraph, engine.gpu_folder, engine.test_break)
        if problems:
            engine.warn(f"GPU: {problem}" for problem in problems)
        warm(exe)
        settings.update({"nanna:sceneDir": engine.gpu_folder, "nanna:pbrt": exe, "nanna:viewport": False, "nanna:spp": panel.pixel_samples ** 2,
                         "nanna:seed": 0 if panel.lock_frame_noise else scene.frame_current, "nanna:maxdepth": panel.max_bounces,
                         "nanna:dof": bool(panel.enable_dof), "nanna:aux": bool(panel.denoise)})
    return settings


def read_aux(folder, width, height):
    """Albedo and normal for Denoise from the F12's out_<run>_aux.exr (nanna:aux: pbrt's gbuffer film, patch_aux.py), as 0.9.2's gpu.py
    read them: rows bottom up like Blender's result, float32 rows by columns by 3. None if there is none or its size is wrong."""
    import numpy as np
    from . import gpu
    found = []
    for name in os.listdir(folder) if folder and os.path.isdir(folder) else ():
        stem = name[4:-8] if name.startswith("out_") and name.endswith("_aux.exr") else ""
        if stem.isdigit():
            found.append((int(stem), name))
    if not found:
        return None
    _, albedo, normal = gpu.read_exr(os.path.join(folder, max(found)[1]))  # the last run's; Ns z turned toward the viewer there
    if albedo is None or normal is None or albedo.shape[:2] != (height, width):
        return None
    return np.ascontiguousarray(albedo[::-1]), np.ascontiguousarray(normal[::-1])
