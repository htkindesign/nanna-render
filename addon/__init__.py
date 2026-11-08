# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""MoonRay through Hydra in Blender: engine class, panel and registration.

Registering loads no MoonRay code and changes nothing for other engines. runtime.setup() runs the first time this engine renders.
Cleanup isn't here on purpose. The delegate registers it with Py_AtExit when it starts the render driver, so it runs on every
normal exit, whether or not this module is still enabled. See RndrRenderer.cc.
"""
import os
import traceback

import bpy

from . import panels, runtime, viewport

ENGINE = "MOONRAY_HYDRA"
VIEWPORT_OFF = "MoonRay: viewport rendering is off for now. Use F12, or a Solid or Material Preview viewport"
# Hydra exports what these edit. Blender's own Light panel only has the type, so the EEVEE one stands in, as Cycles has its own.
SHARED_PANELS = ("EEVEE_MATERIAL_PT_context_material", "EEVEE_MATERIAL_PT_surface", "MATERIAL_PT_preview", "EEVEE_WORLD_PT_surface",
                 "DATA_PT_EEVEE_light", "NODE_DATA_PT_EEVEE_light", "DATA_PT_camera_dof", "DATA_PT_camera_dof_aperture")
REPLACED_PANELS = ("DATA_PT_light", "NODE_DATA_PT_light")
DENOISE_AOVS = {"Denoising Normal": "normal", "Denoising Albedo": "blenderAlbedo"}  # Blender pass to Hydra AOV, read by denoise.py


class MoonRayHydraPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    runtime_path: bpy.props.StringProperty(
        name="Runtime Folder", subtype="DIR_PATH",
        description="Folder holding MoonRay's bin, plugin and rdl2dso. Leave empty for the one inside the add-on. "
                    "A change applies after restarting Blender if MoonRay has already rendered")
    experimental_viewport: bpy.props.BoolProperty(
        name="Experimental: Rendered viewport", default=False,
        description="Render the viewport with MoonRay in Rendered shading. F12 stops it first and it starts again after the render. Not ready for real work")

    def draw(self, context):
        self.layout.prop(self, "runtime_path")
        self.layout.prop(self, "experimental_viewport")


class MoonRayHydraEngine(bpy.types.HydraRenderEngine):
    bl_idname = ENGINE
    bl_label = "Nanna Render"
    bl_use_preview = False
    bl_use_eevee_viewport = True  # Material Preview. Without it, that mode calls view_update too and shows VIEWPORT_OFF
    bl_use_gpu_context = False
    bl_use_materialx = False
    bl_delegate_id = "HdMoonrayRendererDebugPlugin"

    film_transparent = False
    depsgraph = None
    motion = {}
    reported = ""
    warned = frozenset()
    setup_error = ""
    view_size = None
    view_state = None
    has_glow = False
    restart_since = None  # the rest of these are viewport.py's
    restart_reason = ""
    start_seconds = 0.0
    frame_seen = None
    settings_seen = None
    scene_seen = ""
    glow_seen = None

    def ready(self, depsgraph):
        """Raises the reason MoonRay can't run. Blender's update has then created no delegate."""
        self.film_transparent = depsgraph.scene.render.film_transparent
        self.depsgraph = depsgraph
        addon = bpy.context.preferences.addons.get(__package__)
        runtime.setup(addon.preferences.runtime_path if addon else "")
        os.environ["HDMOONRAY_THREADS"] = str(panels.thread_count(depsgraph.scene))  # read by RndrRenderer.cc
        if not self.engine_ptr and runtime.delegate_live():
            raise RuntimeError("another MoonRay render is still running in this Blender. Wait for it to end, then render again")

    def release(self):
        """Every view layer and frame gets a new delegate, in every mode, so Persistent Data keeps nothing of MoonRay's. A reused delegate
        crashes in RenderTaskDelegate::read_aov after a resolution change, and keeps a failed frame's state into the next render
        (seen in window tests). Freed here, while the depsgraph Blender's Hydra read is still alive."""
        if self.engine_ptr:
            import _bpy_hydra
            _bpy_hydra.engine_free(self.engine_ptr)
            self.engine_ptr = None
        self.depsgraph = None

    def first_layer(self, depsgraph):
        scene = depsgraph.scene.original
        names = [layer.name for layer in scene.view_layers if layer.use]
        return scene.render.use_single_layer or not names or depsgraph.view_layer.name == names[0]

    def update(self, data, depsgraph):
        """Any failure here is held for render(), which reports it and shows no picture."""
        self.setup_error = ""
        try:
            self.release()  # render() has done this, unless Blender skipped it for a cancelled layer
            viewport.stop_for_render()
            if self.first_layer(depsgraph):  # with Persistent Data this object lives through several renders
                self.warned = frozenset()
            if depsgraph.scene.moonray_hydra.device == "GPU":  # no delegate: render() hands the frame to gpu.py
                self.depsgraph = depsgraph
                return
            self.ready(depsgraph)
            self.motion = panels.motion_settings(self, depsgraph)
            super().update(data, depsgraph)
        except Exception as error:
            traceback.print_exc()
            self.setup_error = str(error) if isinstance(error, RuntimeError) else f"can't prepare the render, {type(error).__name__}: {error}"

    def view_update(self, context, depsgraph):
        """Blocked unless the Experimental preference is on: a viewport is a second delegate on MoonRay's one render driver beside F12. See viewport.py."""
        if viewport.enabled():
            viewport.update(self, context, depsgraph)
        else:
            self.update_stats("", VIEWPORT_OFF)

    def view_draw(self, context, depsgraph):
        if viewport.enabled():
            viewport.draw(self, context, depsgraph)

    def tell(self, level, message):
        """Also to the console: with a window, Blender's reports only reach the Info editor."""
        print(f"[moonray_hydra] {level}: {message}", flush=True)
        self.report({level}, "MoonRay: " + message)

    def warn(self, problems):
        """Each once, also when a render has several view layers."""
        for problem in problems:
            if problem not in self.warned:
                self.warned = self.warned | {problem}
                self.tell("WARNING", problem)

    def get_render_settings(self, engine_type):
        settings = {"blender:filmTransparent": self.film_transparent}
        if self.depsgraph:  # Blender calls this after engine_update, which is when its image cache is written
            from . import textures  # pulls in OpenImageIO, which only this engine needs
            self.update_stats("", "MoonRay: preparing textures")
            manifest, glows, problems = textures.prepare(self.depsgraph, self.test_break)
            settings["blender:textureManifest"] = manifest
            settings["blender:meshLights"] = glows
            self.has_glow = bool(glows)
            settings["blender:clearShadowMaterials"] = " ".join(textures.clear_shadow)
            self.warn(problems)
            self.reported = " ".join(problems)
            self.warn(textures.unsupported(self.depsgraph))
            self.warn(f"light {block.name}: Normalize off isn't supported, it renders as if on"
                      for block in self.depsgraph.ids if isinstance(block, bpy.types.Light) and not block.normalize)
            self.warn(camera_problems(self.depsgraph.scene))
            self.warn(panels.thread_problems(self.depsgraph.scene))
            settings.update(panels.render_settings(self.depsgraph.scene.original if engine_type == "VIEWPORT" else self.depsgraph.scene))
            # A viewport render can change mode or lose its last light while it runs, and the delegate loads the anchor only once
            adaptive = settings[panels.PREFIX + "light_sampling_mode"] == panels.ENUM_VALUES["ADAPTIVE"]
            settings["blender:lightTreeAnchor"] = engine_type == "VIEWPORT" or (adaptive and not textures.lights.has_bounded_light(self.depsgraph))
            if textures.presence_free and panels.PREFIX + "enable_presence_shadows" not in self.depsgraph.scene:
                settings[panels.PREFIX + "enable_presence_shadows"] = False
            if engine_type == "VIEWPORT":
                settings.update(panels.viewport_settings(self.depsgraph.scene.original))
            settings.update(self.motion)
            settings["blender:worldStrength"] = world_strength(self.depsgraph.scene.world)
            self.update_stats("", "MoonRay: baking the world")
            baked, problems = textures.bake_world(self.depsgraph, self.test_break, panels.thread_count(self.depsgraph.scene))
            self.warn(problems)
            if baked:  # holds the strength already
                settings["blender:worldTexture"] = baked
                settings["blender:worldStrength"] = 1.0
        if engine_type != "VIEWPORT":
            settings["aovToken:Combined"] = "color"
            settings.update({f"aovToken:{name}": aov for name, aov in DENOISE_AOVS.items()})
        if self.depsgraph and engine_type != "VIEWPORT":
            settings.update(panels.aov_settings(self.depsgraph))
        return settings

    def update_render_passes(self, scene, render_layer):
        if render_layer.use_pass_combined:
            self.register_pass(scene, render_layer, "Combined", 4, "RGBA", "COLOR")
        if scene.moonray_hydra.denoise:
            self.register_pass(scene, render_layer, "Denoising Normal", 3, "XYZ", "VECTOR")
            self.register_pass(scene, render_layer, "Denoising Albedo", 3, "RGB", "COLOR")
        for name, (channels, ids, kind, *_) in panels.enabled_passes(render_layer).items():
            self.register_pass(scene, render_layer, name, channels, ids, kind)

    def render(self, depsgraph):
        if depsgraph.scene.moonray_hydra.device == "GPU" and not self.setup_error:
            try:
                from . import gpu
                gpu.render(self, depsgraph)
            except Exception as failure:
                traceback.print_exc()
                self.tell("ERROR", str(failure) if isinstance(failure, RuntimeError) else f"the GPU render failed, {type(failure).__name__}: {failure}")
            self.depsgraph = None
            return
        if self.setup_error or not self.engine_ptr:  # Blender leaves a blank picture and no message
            self.release()
            self.tell("ERROR", self.setup_error or "the Hydra delegate could not be created, see the console")
            return
        try:
            if depsgraph.scene.moonray_hydra.denoise:  # update_render_passes only names them for the compositor
                for name, channels in (("Denoising Normal", "XYZ"), ("Denoising Albedo", "RGB")):
                    self.add_pass(name, 3, channels, layer=depsgraph.view_layer.name)
            for name, (channels, ids, *_) in panels.enabled_passes(depsgraph.view_layer).items():
                self.add_pass(name, channels, ids, layer=depsgraph.view_layer.name)
            super().render(depsgraph)
            unmapped = (path for path in runtime.unmapped_textures() if os.path.basename(path) not in self.reported)
            self.warn(f"{path}: no tiled copy, it will render black" for path in unmapped)
            error = runtime.last_error()
            if not error:
                panels.world_normals(self, depsgraph)
        except Exception as failure:
            traceback.print_exc()
            error = f"the render failed, {type(failure).__name__}: {failure}"
        self.release()
        if error:
            self.tell("ERROR", error)  # error_set() alone still saves a blank picture
        elif depsgraph.scene.moonray_hydra.denoise and not self.test_break():
            self.denoise(depsgraph.view_layer.name)

    def denoise(self, layer):
        """A failure here leaves the frame as rendered, with a warning."""
        self.update_stats("", "MoonRay: denoising")
        try:
            from . import denoise
            if not denoise.apply(self, layer):
                self.tell("WARNING", "denoise found no picture to work on, the frame stays as rendered")
        except Exception as failure:
            traceback.print_exc()
            self.tell("WARNING", f"denoise failed, the frame stays as rendered. {type(failure).__name__}: {failure}")


def camera_problems(scene):
    """An orthographic camera shows only the World if this Blender's first MoonRay frame was perspective. The scene MoonRay gets is the same
    as a good one, and orthographic first then anything is right, so the cause is inside MoonRay. Not found."""
    global first_camera
    kind = scene.camera.data.type if scene.camera else None
    first_camera = first_camera or kind
    if kind == "ORTHO" and first_camera != "ORTHO":
        return ["an orthographic camera after a perspective render can come out empty. Save, restart Blender and render it first"]
    return []


def world_strength(world):
    """Background strength, which Blender's Hydra export drops when the colour comes from an image. 1.0 if there's no plain
    Background: textures.world_problems() warns about those."""
    if world is None or not world.node_tree:
        return 1.0
    output = world.node_tree.get_output_node("CYCLES") or world.node_tree.get_output_node("ALL")
    links = output.inputs["Surface"].links if output else ()
    node = links[0].from_node if links else None
    if node is None or node.type != "BACKGROUND" or node.inputs["Strength"].is_linked:
        return 1.0
    return node.inputs["Strength"].default_value


def standard_panels():
    panels = [panel for panel in bpy.types.Panel.__subclasses__()
              if "BLENDER_RENDER" in getattr(panel, "COMPAT_ENGINES", ()) and panel.__name__ not in REPLACED_PANELS]
    return panels + [getattr(bpy.types, name) for name in SHARED_PANELS if hasattr(bpy.types, name)]


first_camera = None
CLASSES = (MoonRayHydraPreferences, *panels.CLASSES, MoonRayHydraEngine)


def register():
    if bpy.app.timers.is_registered(remove_when_idle):  # disabled and enabled again inside one render: it never left
        bpy.app.timers.unregister(remove_when_idle)
        return
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.moonray_hydra = bpy.props.PointerProperty(type=panels.Settings)
    for panel in standard_panels():
        panel.COMPAT_ENGINES.add(ENGINE)
    bpy.app.handlers.load_post.append(panels.device_on_load)  # a file saved with Device GPU starts its pbrt


def remove():
    import sys
    if __package__ + ".gpu" in sys.modules:  # the warm pbrt.exe goes with the add-on
        sys.modules[__package__ + ".gpu"].stop()
    if panels.device_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(panels.device_on_load)
    for panel in standard_panels():
        panel.COMPAT_ENGINES.discard(ENGINE)
    del bpy.types.Scene.moonray_hydra
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


def remove_when_idle():
    if bpy.app.is_job_running("RENDER"):
        return 0.5
    remove()


def unregister():
    """Removing the engine class under a running render crashes Blender in engine_render_view_layer, so that waits for the render's end."""
    if bpy.app.is_job_running("RENDER"):
        bpy.app.timers.register(remove_when_idle, persistent=True)
    else:
        remove()
