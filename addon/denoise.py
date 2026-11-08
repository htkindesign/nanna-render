# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""The Denoise checkbox: Blender's own OpenImageDenoise on the finished frame, on the CPU.

MoonRay only denoises while writing a file (RenderOutput's denoise attribute), and a Hydra render writes none, so this runs after the render
instead. It reads Blender's result and replaces it, the way the frame is written. Runs on the render thread, so it touches no bpy.data.
Imported the first time a frame is denoised.
"""
import ctypes
import os

import bpy
import numpy as np

FLOAT3 = 3  # OIDNFormat
CPU = 1  # OIDNDeviceType
oidn = None


def library():
    """Blender's copy, by full path. MoonRay's own is renamed and never loaded."""
    global oidn
    if oidn is None:
        path = os.path.join(os.path.dirname(bpy.app.binary_path), "blender.shared", "OpenImageDenoise.dll")
        dll = ctypes.WinDLL(path)
        handle = ctypes.c_void_p
        size = ctypes.c_size_t
        name = ctypes.c_char_p
        for function, result, arguments in (
                ("oidnNewDevice", handle, [ctypes.c_int]),
                ("oidnCommitDevice", None, [handle]),
                ("oidnGetDeviceError", ctypes.c_int, [handle, ctypes.POINTER(ctypes.c_char_p)]),
                ("oidnReleaseDevice", None, [handle]),
                ("oidnNewFilter", handle, [handle, name]),
                ("oidnSetSharedFilterImage", None, [handle, name, handle, ctypes.c_int, size, size, size, size, size]),
                ("oidnSetFilterBool", None, [handle, name, ctypes.c_bool]),
                ("oidnCommitFilter", None, [handle]),
                ("oidnExecuteFilter", None, [handle]),
                ("oidnReleaseFilter", None, [handle])):
            getattr(dll, function).restype = result
            getattr(dll, function).argtypes = arguments
        oidn = dll
    return oidn


def run(colour, normal, albedo):
    """colour: float32 rows by columns by 4, changed in place, alpha kept. normal, albedo: rows by columns by 3, or None."""
    lib = library()
    height, width = colour.shape[:2]
    output = np.empty((height, width, 3), np.float32)
    device = lib.oidnNewDevice(CPU)
    filter_ = None
    try:
        lib.oidnCommitDevice(device)
        filter_ = lib.oidnNewFilter(device, b"RT")
        lib.oidnSetSharedFilterImage(filter_, b"color", colour.ctypes.data, FLOAT3, width, height, 0, 16, 0)
        lib.oidnSetSharedFilterImage(filter_, b"output", output.ctypes.data, FLOAT3, width, height, 0, 12, 0)
        if normal is not None and albedo is not None:  # OIDN takes albedo alone, or both: without them it smears textures
            lib.oidnSetSharedFilterImage(filter_, b"albedo", albedo.ctypes.data, FLOAT3, width, height, 0, 12, 0)
            lib.oidnSetSharedFilterImage(filter_, b"normal", normal.ctypes.data, FLOAT3, width, height, 0, 12, 0)
        lib.oidnSetFilterBool(filter_, b"hdr", True)
        lib.oidnCommitFilter(filter_)
        lib.oidnExecuteFilter(filter_)
        message = ctypes.c_char_p()
        if lib.oidnGetDeviceError(device, ctypes.byref(message)):
            raise RuntimeError((message.value or b"unknown error").decode(errors="replace"))
    finally:
        if filter_:
            lib.oidnReleaseFilter(filter_)
        lib.oidnReleaseDevice(device)
    colour[..., :3] = output


def read(render_pass, height, width):
    pixels = np.empty((height, width, render_pass.channels), np.float32)
    render_pass.rect.foreach_get(pixels.reshape(-1))
    return pixels


def replace(engine, layer_name, width, height, pixels):
    """Writes these passes into the result. A new result is blank in every other pass, and ending it merges all of them, so they go back too."""
    result = engine.get_result()
    keep = {render_pass.name: read(render_pass, height, width) for render_pass in result.layers[layer_name].passes}
    keep.update(pixels)
    out = engine.begin_result(0, 0, width, height, layer=layer_name)
    for render_pass in out.layers[0].passes:
        if render_pass.name in keep:
            render_pass.rect.foreach_set(keep[render_pass.name].reshape(-1))
    engine.end_result(out)


def apply(engine, layer_name):
    """Replaces the Combined pass of this view layer. Returns False if there's no result to work on."""
    result = engine.get_result()
    layer = result.layers.get(layer_name) if result else None
    combined = layer.passes.get("Combined") if layer else None
    if combined is None:
        return False
    height, width = result.resolution_y, result.resolution_x
    colour = read(combined, height, width)
    normal, albedo = (read(layer.passes[name], height, width) if name in layer.passes else None for name in ("Denoising Normal", "Denoising Albedo"))
    run(colour, normal, albedo)
    replace(engine, layer_name, width, height, {"Combined": colour})
    return True
