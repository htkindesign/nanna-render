# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Find the MoonRay runtime and hand it to Blender's USD, the first time MoonRay is used. Until then nothing here touches the process.

Nothing here touches Blender's install or user folders, or PATH. No environment variable has to be set first. The runtime is, in order: the
add-on's Runtime Folder preference, HDMOONRAY_ROOT (also how a developer runs the add-on from its source folder), the runtime folder inside the add-on.
"""
import ctypes
import hashlib
import os

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_NAME = "hd_moonray_debug"
BLENDER_VERSION = (5, 1, 2)  # the DLLs are built against this release's usd_ms.dll, and fit no other
USD_MS_SHA256 = "90103a5f628f1cac02f84c11e4a728bfafd42f73db5728f5bd96b3d87bca4663"
LOAD_WITH_ALTERED_SEARCH_PATH = 0x8

_root = None  # set by setup(). A process keeps the first runtime it loaded: a DLL can't be swapped under a running Blender


def _is_runtime(root):
    return os.path.isfile(os.path.join(root, "bin", PLUGIN_NAME + ".dll"))


def find_root(setting=""):
    """A folder named on purpose must work: a typo shouldn't quietly run a different MoonRay."""
    named = setting or os.environ.get("HDMOONRAY_ROOT", "")
    if named:
        if _is_runtime(named):
            return named
        raise RuntimeError(f"no MoonRay runtime in {named}: {PLUGIN_NAME}.dll is not in its bin folder")
    root = os.path.join(HERE, "runtime")
    if _is_runtime(root):
        return root
    raise RuntimeError("no MoonRay runtime found: set Runtime Folder in the add-on's preferences")


def root_bin():
    return os.path.join(_root, "bin")


def check_blender():
    """Refuse before anything loads. A mismatched USD doesn't fail cleanly: it fails inside Blender."""
    wanted = ".".join(map(str, BLENDER_VERSION))
    if bpy.app.version != BLENDER_VERSION:
        raise RuntimeError(f"this build needs Blender {wanted}, this is {bpy.app.version_string}. Nothing was loaded")
    path = os.path.join(os.path.dirname(bpy.app.binary_path), "blender.shared", "usd_ms.dll")
    try:
        with open(path, "rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
    except OSError as error:
        raise RuntimeError(f"can't read Blender's usd_ms.dll, so can't check it fits. Nothing was loaded: {error}")
    if digest != USD_MS_SHA256:
        raise RuntimeError(f"Blender's usd_ms.dll isn't the one from Blender {wanted} this build needs. Nothing was loaded")


def load_runtime(root):
    """USD opens the plugin with a plain LoadLibrary, and MoonRay its shaders, which find their neighbours only through PATH. Putting
    bin there would pass Qt, OpenSSL and zlib to every process Blender starts. These flags find them beside each DLL instead, and
    once loaded, a later plain load matches them by name. preload.txt is made by build\\hdmoonray\\preload_list.py."""
    with open(os.path.join(HERE, "preload.txt"), encoding="utf-8") as f:
        names = f.read().split()
    for name in names:
        ctypes.WinDLL(os.path.join(root, "bin", name), winmode=LOAD_WITH_ALTERED_SEARCH_PATH)


def setup(setting=""):
    """Must run before the delegate loads."""
    global _root
    if _root:
        return
    root = find_root(setting)
    check_blender()
    import pxr.Plug  # Blender's own USD. Imported here so that enabling the add-on loads nothing
    os.environ["RDL2_DSO_PATH"] = os.path.join(root, "rdl2dso")
    os.environ["REZ_MOONRAY_ROOT"] = root
    os.environ["HDMOONRAY_BLENDER_UNITS"] = "1"  # read by Light.cc, see blenderUnitScale()
    registry = pxr.Plug.Registry()
    registry.RegisterPlugins(os.path.join(root, "plugin", "pxr"))
    if registry.GetPluginWithName(PLUGIN_NAME) is None:
        raise RuntimeError(f"MoonRay plugin not found under {root}")
    load_runtime(root)
    _root = root


def last_error():
    """The reason the delegate's last frame failed, or an empty string."""
    plugin = ctypes.WinDLL(os.path.join(_root, "bin", PLUGIN_NAME + ".dll"))  # already loaded, so this only finds it
    plugin.hdMoonrayLastError.restype = ctypes.c_char_p
    return plugin.hdMoonrayLastError().decode("utf-8", "replace")


def blank_until_fresh():
    """The viewport's view changed: the frame that starts next shows nothing until it has pixels, not the last view's picture.
    False with a runtime older than 0.9.2, which keeps the old picture up."""
    plugin = ctypes.WinDLL(os.path.join(_root, "bin", PLUGIN_NAME + ".dll"))  # already loaded, so this only finds it
    try:
        plugin.hdMoonrayBlankUntilFresh()
    except AttributeError:
        return False
    return True


def delegate_live():
    """True while a MoonRay delegate exists in this process. Asks only if the plugin is loaded: this must not load it."""
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    if not kernel32.GetModuleHandleW(PLUGIN_NAME + ".dll"):
        return False
    return bool(ctypes.WinDLL(os.path.join(_root, "bin", PLUGIN_NAME + ".dll")).hdMoonrayDelegateLive())


def unmapped_textures():
    """Textures the delegate was given that had no tiled copy, which MoonRay can't sample."""
    delegate = ctypes.WinDLL(os.path.join(_root, "bin", "hydramoonray.dll"))  # already loaded, so this only finds it
    delegate.hdMoonrayUnmappedTextures.restype = ctypes.c_char_p
    return delegate.hdMoonrayUnmappedTextures().decode("utf-8", "replace").splitlines()
