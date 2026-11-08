# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Experimental Rendered viewport, off unless the add-on's preference is on. The engine calls update and draw only then, and stop_for_render() only sets a flag with no viewport live.

One delegate is kept across edits: Blender's Hydra sends what changed, and MoonRay restarts its progressive frame on it. MoonRay has one render driver and the plugin
refuses a second delegate, so F12 takes over: the viewport's delegate is freed first, on the main thread, and made again when the render ends.

What a delegate can't take in, it gets by being replaced: a MeshLight loads once per delegate and doesn't follow the camera, and render settings are read at the start.
Those restarts wait for things to stay still, see wanted(), so a camera drag or a playing timeline shows the old lights until it stops. A size change can't wait.
"""
import threading
import time
import traceback
import weakref

import bpy

from . import panels, runtime

PAUSED = "MoonRay: the Rendered viewport is paused for the render"
WAIT_SECONDS = 30  # what a render waits for the viewport to let go. Stopping a frame can sit in MoonRay's render prep for 10 s or more
SETTLE = 0.3  # seconds of no change before a restart
SETTLE_MOST = 3.0  # a slow scene waits as long as its last start took, up to this

live = None  # weak: Blender frees the viewport's engine, and a strong reference would stop HydraRenderEngine.__del__ freeing the delegate
held = False  # a final render owns the driver
release_wanted = False
creating = False  # start() is making a delegate. With held it closes the race with a render: each side sets its own flag, then reads the other's
waiting = False  # a viewport found itself paused and needs a redraw when the render ends


def enabled():
    addon = bpy.context.preferences.addons.get(__package__)
    return bool(addon and addon.preferences.experimental_viewport)


def paused():
    """On the main thread. held can't be cleared by poll() alone: it isn't running if no viewport was live when the render began."""
    global held
    running = bpy.app.is_job_running("RENDER")
    if held and not running:
        held = False
    return held or running


def viewport_engine():
    return live() if live else None


def redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def watch():
    if not bpy.app.timers.is_registered(poll):
        bpy.app.timers.register(poll, persistent=True)


def poll():
    """On the main thread, as a timer: the delegate must go there, as Blender's own viewport teardown does. Registering a timer from the render thread isn't safe, so this runs all along."""
    global held, release_wanted, waiting
    engine = viewport_engine()
    if release_wanted:
        if engine is not None:
            engine.release()
        release_wanted = False
        return 0.02
    if (held or waiting) and not bpy.app.is_job_running("RENDER"):
        held = waiting = False
        redraw()
    elif engine is not None and engine.engine_ptr and not held:
        scene = bpy.data.scenes.get(engine.scene_seen)
        if scene and settings_state(scene) != engine.settings_seen:  # here, not in edit(): Blender calls view_update on a depsgraph change, and a panel edit needn't make one
            engine.settings_seen = settings_state(scene)
            wanted(engine, "settings")
        if engine.restart_since is not None:  # a finished frame draws no more, and the restart happens in draw()
            redraw()
    if engine is None and not held and not waiting:
        return None
    return 0.1


def stop_for_render():
    """A final render's first step. It runs on the render thread after F12 and on the main thread with EXEC_DEFAULT, where no draw can be running."""
    global held, release_wanted
    held = True  # before anything is read: start() reads this after it sets creating
    began = time.monotonic()
    if threading.current_thread() is threading.main_thread():
        engine = viewport_engine()
        if engine is not None and engine.engine_ptr:
            engine.release()
        return
    while creating and time.monotonic() < began + WAIT_SECONDS:
        time.sleep(0.02)
    engine = viewport_engine()
    if not creating and (engine is None or not engine.engine_ptr):
        return
    release_wanted = True
    while release_wanted and time.monotonic() < began + WAIT_SECONDS:
        time.sleep(0.02)
    if creating or release_wanted:
        raise RuntimeError("the Rendered viewport didn't stop in time. Switch it to Solid, then render again")
    print(f"[moonray_hydra] viewport stopped for the render in {time.monotonic() - began:.2f} s", flush=True)


def settings_state(scene):
    """What a running delegate won't pick up. The frame is sent on its own, see edit(). The original scene: depsgraph.scene can lag behind the panel."""
    scene = scene.original
    settings = panels.render_settings(scene)
    settings.pop("blender:frame", None)
    return settings, panels.viewport_settings(scene), scene.render.film_transparent


def glow_state(engine, depsgraph):
    """The MeshLights as this depsgraph has them. Their file is read once per delegate, so a change to this needs a new one."""
    if not engine.has_glow:
        return None
    from . import textures
    return textures.lights.glow_state(depsgraph, textures.emitters)


def remember(engine, depsgraph):
    scene = depsgraph.scene
    engine.scene_seen = scene.original.name
    engine.frame_seen = scene.frame_current + scene.frame_subframe
    engine.settings_seen = settings_state(scene)
    engine.glow_seen = glow_state(engine, depsgraph)
    engine.restart_since = None


def start(engine, context, depsgraph):
    global live, creating
    began = time.perf_counter()
    creating = True
    try:
        if paused():
            return
        engine.ready(depsgraph)
        try:
            bpy.types.HydraRenderEngine.view_update(engine, context, depsgraph)  # with engine.depsgraph set, this prepares textures and the world
        except Exception:
            engine.release()
            raise
        engine.depsgraph = None
        if not engine.engine_ptr:
            raise RuntimeError("the Hydra delegate could not be created, see the console")
        if held:  # the render came while this ran, and is waiting on creating. It finds no delegate left
            engine.release()
            return
        live = weakref.ref(engine)
        remember(engine, depsgraph)
    finally:
        creating = False
    watch()
    engine.start_seconds = time.perf_counter() - began
    print(f"[moonray_hydra] viewport started in {engine.start_seconds:.2f} s", flush=True)


def wanted(engine, reason):
    """A restart is needed, from now if something keeps changing. draw() does it once nothing has for a while."""
    engine.restart_since = time.monotonic()
    engine.restart_reason = reason


def due(engine):
    return engine.restart_since is not None and time.monotonic() - engine.restart_since >= max(SETTLE, min(engine.start_seconds, SETTLE_MOST))


def edit(engine, context, depsgraph):
    """A camera or object move and a light edit are all Hydra's. A material or the world also needs the add-on's manifest, the only way native networks reach Material.cc."""
    import _bpy_hydra
    began = time.perf_counter()
    scene = depsgraph.scene
    shading = any(isinstance(update.id, (bpy.types.Material, bpy.types.NodeTree, bpy.types.World)) for update in depsgraph.updates)
    frame = scene.frame_current + scene.frame_subframe
    if shading:
        engine.depsgraph = depsgraph
        try:
            bpy.types.HydraRenderEngine.view_update(engine, context, depsgraph)
        finally:
            engine.depsgraph = None
    else:
        _bpy_hydra.engine_update(engine.engine_ptr, depsgraph, context)
    if frame != engine.frame_seen:  # what moves came through Hydra above. This is MoonRay's noise seed, and it's what a full view_update would send
        _bpy_hydra.engine_set_render_setting(engine.engine_ptr, "blender:frame", float(frame))
        engine.frame_seen = frame
    glow = glow_state(engine, depsgraph)
    changed = glow != engine.glow_seen
    if changed:
        engine.glow_seen = glow
        wanted(engine, "emitters")
    print(f"[moonray_hydra] viewport edit, shading {shading}, emitters changed {changed}, {time.perf_counter() - began:.2f} s", flush=True)


def update(engine, context, depsgraph):
    global waiting
    if paused():
        waiting = True
        watch()
        engine.update_stats("", PAUSED)
        return
    try:
        (edit if engine.engine_ptr else start)(engine, context, depsgraph)
    except Exception as error:
        traceback.print_exc()
        engine.update_stats("", f"MoonRay: {error}")


def draw(engine, context, depsgraph):
    """Runs inside Blender's draw, where start() runs anyway on a viewport's first frame."""
    global waiting
    if paused():
        waiting = True
        watch()
        engine.update_stats("", PAUSED)
        return
    size = (context.region.width, context.region.height)
    view = tuple(tuple(row) for row in context.region_data.perspective_matrix)
    moved = engine.view_state not in (None, view)  # an orbit, pan or zoom
    if engine.engine_ptr and engine.view_size not in (None, size):  # a delegate reused at a new size crashes RenderTaskDelegate::read_aov, see release()
        engine.release()
    elif engine.engine_ptr and engine.has_glow and moved:
        wanted(engine, "camera")  # Scene::preFrame leaves a MeshLight where it was when only the camera moves. On a test interior a new delegate is as quick as an edit
    if engine.engine_ptr and due(engine):
        print(f"[moonray_hydra] viewport restart for {engine.restart_reason}", flush=True)
        engine.release()
    if not engine.engine_ptr:
        update(engine, context, depsgraph)
    engine.view_size, engine.view_state = size, view
    if engine.engine_ptr:
        if moved:  # before Hydra sends the new view and MoonRay restarts: the last view's picture isn't shown on the new one
            runtime.blank_until_fresh()
        bpy.types.HydraRenderEngine.view_draw(engine, context, depsgraph)
