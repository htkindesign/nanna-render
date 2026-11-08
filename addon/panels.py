# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""The Render tab with MoonRay, laid out like Cycles', and the passes in View Layer. Each value reaches MoonRay as moonray:sceneVariable:<name>.

Properties that carry a MoonRay name below default to MoonRay's own value (SceneVariables.json), except Pixel Samples. Left out on purpose:
debug, network, checkpoint, deep image and file output variables, and what nothing in a Blender scene can reach yet: max_geometry_resolution, which
MoonRay only reads for procedurals, the volume variables, as neither a volume shader nor a Volume object gets a material (render_tab/t_vol.py),
enable_displacement and max_hair_depth, as the material translator has no displacement or hair. scene_scale stays out too: lamps are calibrated at
MoonRay's 0.01, 1.0 leaves them 10000 times too dark, and depth of field already matches Cycles at unit scale 1 and 0.01 with it (render_tab/measure2.py).
"""
import os
import tempfile

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty

ENGINE = "MOONRAY_HYDRA"
first_threads = None
PREFIX = "moonray:sceneVariable:"
# Property to MoonRay variable, where the names differ. These were saved in files before this panel existed.
RENAMED = {"max_bounces": "max_depth", "diffuse_bounces": "max_diffuse_depth", "glossy_bounces": "max_glossy_depth",
           "transmission_bounces": "max_mirror_depth", "presence_bounces": "max_presence_depth"}
# ValueConverter.cc takes an enum's place in MoonRay's list, not its value: adaptive is 2 in sampling_mode's list, but 1 here.
ENUM_VALUES = {"UNIFORM": 0, "ADAPTIVE": 1, "BOX": 0, "CUBIC": 1, "QUADRATIC": 2, "OFF": 0, "ON": 1, "SINE": 2, "GGX": 3, "COSINE": 4}
# Blender pass: channels, channel names, node socket type, Hydra AOV, view layer property.
PASSES = {"Depth": (1, "Z", "VALUE", "cameraDepth", "use_pass_z"),
          "Position": (3, "XYZ", "VECTOR", "Wp", "use_pass_position"),
          "Normal": (3, "XYZ", "VECTOR", "normal", "use_pass_normal"),
          "UV": (2, "UV", "VECTOR", "St", "use_pass_uv"),
          "Diffuse Color": (3, "RGB", "COLOR", "blenderDiffuse", "use_pass_diffuse_color"),
          "Diffuse Direct": (3, "RGB", "COLOR", "blenderDiffuseDirect", "use_pass_diffuse_direct"),
          "Diffuse Indirect": (3, "RGB", "COLOR", "blenderDiffuseIndirect", "use_pass_diffuse_indirect"),
          "Glossy Direct": (3, "RGB", "COLOR", "blenderGlossyDirect", "use_pass_glossy_direct"),
          "Glossy Indirect": (3, "RGB", "COLOR", "blenderGlossyIndirect", "use_pass_glossy_indirect"),
          "Transmission Direct": (3, "RGB", "COLOR", "blenderTransmission", "use_pass_transmission_direct")}  # no Emission: the C[LO] LPE stays 0, render_final/check.py allpass


class Settings(bpy.types.PropertyGroup):
    device: EnumProperty(
        name="Device", default="CPU",
        items=(("CPU", "CPU", "MoonRay on the CPU, through Hydra"),
               ("GPU", "GPU", "Nanna on an NVIDIA RTX card, in its own process. F12 only: Pixel Samples squared per pixel, the Max Bounces, Denoise. "
                              "Needs the NVIDIA driver, no CUDA install")),
        description="Where F12 renders", update=lambda self, context: device_changed(self.id_data))
    sampling_mode: EnumProperty(
        name="Mode", default="UNIFORM",
        items=(("UNIFORM", "Uniform", "The same samples in every pixel: Pixel Samples sets how many"),
               ("ADAPTIVE", "Adaptive", "More samples where the picture stays noisy, until Noise Threshold is met")),
        description="How samples are shared between pixels")
    pixel_samples: IntProperty(
        name="Pixel Samples", default=4, min=1, max=64, soft_max=16,
        description="Samples along each side of a pixel: 4 means 16 camera rays. Time grows with the square of this number")
    viewport_samples: IntProperty(
        name="Viewport Samples", default=2, min=1, max=64, soft_max=8,
        description="Pixel Samples of the Rendered viewport, always Uniform: 2 means 4 camera rays. It stops there. Final renders don't use this")
    target_adaptive_error: FloatProperty(
        name="Noise Threshold", default=20, min=0, soft_min=1, soft_max=100, precision=1,
        description="Noise a pixel may keep before it stops. Lower is cleaner and slower")
    min_adaptive_samples: IntProperty(
        name="Min Samples", default=16, min=1, soft_max=256,
        description="Camera rays every pixel gets before it can stop early. Raise it if small details get missed")
    max_adaptive_samples: IntProperty(
        name="Max Samples", default=256, min=1, soft_max=16384,
        description="Most camera rays a pixel can get. Err on the high side: most pixels stop long before this")
    light_samples: IntProperty(
        name="Light Samples", default=2, min=0, max=32, soft_max=8,
        description="Rays to each light where a camera ray lands, along each side: 2 means 4. "
                    "Raise it for grainy shadows. 0 turns off direct light from lights")
    bsdf_samples: IntProperty(
        name="BSDF Samples", default=2, min=0, max=32, soft_max=8,
        description="Rays bounced off the surface where a camera ray lands, along each side: 2 means 4. "
                    "Raise it for grainy reflections and bounced light. 0 turns off bounced light")
    bssrdf_samples: IntProperty(
        name="BSSRDF Samples", default=2, min=0, max=32, soft_max=8,
        description="Rays inside a subsurface material, along each side: 2 means 4. Raise it for grainy skin or wax")
    lock_frame_noise: BoolProperty(
        name="Lock Frame Noise", default=False,
        description="Same noise pattern on every frame. Usually unwanted: the grain looks stuck to the screen")
    light_sampling_mode: EnumProperty(
        name="Light Sampling", default="UNIFORM",
        items=(("UNIFORM", "Uniform", "Every light is sampled at every point"),
               ("ADAPTIVE", "Adaptive", "Only the lights that matter at each point, by Light Quality. For scenes with many lights")),
        description="Which lights are sampled at each point")
    light_sampling_quality: FloatProperty(
        name="Light Quality", default=0.75, min=0, max=1, subtype="FACTOR",
        description="In Adaptive mode, 0 samples one light at each point and 1 samples them all")
    pixel_filter: EnumProperty(
        name="Pixel Filter", default="CUBIC",
        items=(("BOX", "Box", "Hard. A box of width 1 means no filtering"),
               ("CUBIC", "Cubic B-Spline", "Soft"),
               ("QUADRATIC", "Quadratic B-Spline", "Between the two")),
        description="Shape each sample spreads over the pixels around it. MoonRay's own default is Cubic B-Spline")
    pixel_filter_width: FloatProperty(
        name="Filter Width", default=3, min=0.01, soft_min=1, soft_max=8, subtype="PIXEL",
        description="Width of the pixel filter. Larger is softer. MoonRay's own default is 3")
    look: EnumProperty(
        name="Look", default="CYCLES",
        items=(("CYCLES", "Match Cycles", "Materials fitted to look as they do in Cycles"),
               ("NANNA", "Nanna Look", "MoonRay's own coat, sheen, metal and specular, with no fit to Cycles. See Network.__init__ in native.py")),
        description="How Blender's materials become MoonRay's. Takes effect at the next render")
    denoise: BoolProperty(
        name="Denoise", default=True,
        description="Clean the finished frame with Blender's OpenImageDenoise, guided by the surfaces' colour and normals. "
                    "Adds a few seconds per frame and can soften fine detail")

    max_bounces: IntProperty(
        name="Total", default=12, min=0, max=1024, soft_max=32,
        description="Most bounces a ray can make of any kind. It caps Diffuse, Glossy and Transmission")
    diffuse_bounces: IntProperty(
        name="Diffuse", default=4, min=0, max=1024, soft_max=32,
        description="Most diffuse bounces. Fewer is faster and darkens interiors")
    glossy_bounces: IntProperty(
        name="Glossy", default=8, min=0, max=1024, soft_max=32,
        description="Most glossy bounces. Fewer is faster and dims reflections of reflections")
    transmission_bounces: IntProperty(
        name="Transmission", default=6, min=0, max=6,  # measured: above this, glass overflows a render thread's stack and Blender dies
        description="Most bounces through glass and off mirrors. Each pane of glass takes 2, too few turns it black. 6 is the limit here")
    presence_bounces: IntProperty(
        name="Presence", default=16, min=0, max=1024, soft_max=64,
        description="Most layers of cut-out surface, such as leaves, a ray passes through. After that the surface counts as solid")

    sample_clamping_value: FloatProperty(
        name="Sample Clamping", default=10, min=0, soft_max=100,
        description="Brightest value one sample can have, in radiance. Removes fireflies, but dims real highlights. 0 turns it off")
    sample_clamping_depth: IntProperty(
        name="From Bounce", default=1, min=0, soft_max=8,
        description="Clamp only after this many bounces that aren't mirror-like. 0 also clamps what the camera sees directly")
    roughness_clamping_factor: FloatProperty(
        name="Filter Glossy", default=0, min=0, max=1, subtype="FACTOR",
        description="Blurs sharp reflections along a path, up to the roughest surface it met at 1. Removes fireflies from caustics. 0 turns it off, MoonRay's own default")
    russian_roulette_threshold: FloatProperty(
        name="Russian Roulette", default=0.0375, min=0, soft_max=1, step=1, precision=4,
        description="Paths dimmer than this, in luminance, can end at random. Higher is faster and noisier")
    max_subsurface_per_path: IntProperty(
        name="Subsurface Bounces", default=1, min=0, soft_max=8,
        description="Bounces that stay subsurface. Past this, plain diffuse stands in for it")

    enable_dof: BoolProperty(
        name="Depth of Field", default=True,
        description="Off: everything is sharp, whatever the camera's Depth of Field says")
    enable_shadowing: BoolProperty(
        name="Shadows", default=True,
        description="Off: lights pass through objects, so no shadows and a faster render")
    enable_subsurface_scattering: BoolProperty(
        name="Subsurface Scattering", default=True,
        description="Off: subsurface materials render as plain diffuse")
    enable_presence_shadows: BoolProperty(
        name="Cut-out Shadows", default=True,  # not MoonRay's default: Cycles does this, and glass behind Light Path needs it for the sun to come in
        description="On: Alpha and Transparent let shadows through, as in Cycles. Off is faster, and surfaces cast a solid shadow. "
                    "A scene with neither gets the speed anyway")
    lights_visible_in_camera: BoolProperty(
        name="Lights in Camera", default=False,
        description="Lights show up in the picture as bright shapes. Each light's own setting can override this")
    shadow_terminator_fix: EnumProperty(
        name="Shadow Terminator", default="OFF",
        items=(("OFF", "Off", "No correction"),
               ("ON", "On", "MoonRay's own softening. Try this first"),
               ("SINE", "Sine", "A sine variant of Cosine. Try this second"),
               ("GGX", "GGX", "Estevez's method from Ray Tracing Gems. Try this third"),
               ("COSINE", "Cosine", "Chiang's 2019 method. Try this last")),
        description="Softens the hard edge between light and shadow on low-poly smooth surfaces")

    presence_threshold: FloatProperty(
        name="Presence Threshold", default=0.999, min=0, max=1, subtype="FACTOR", precision=3,
        description="A stack of cut-out surfaces this opaque counts as solid, and no ray goes on through it")
    transparency_threshold: FloatProperty(
        name="Transparency Threshold", default=1, min=0, max=1, subtype="FACTOR", precision=3,
        description="A stack of transparent surfaces this opaque counts as solid. 1 follows every layer")

    texture_cache_size: IntProperty(
        name="Texture Cache (MB)", default=4000, min=1, soft_min=256, soft_max=32000,
        description="Memory for textures. More is often faster, on scenes with many textures")
    texture_file_handles: IntProperty(
        name="Open Texture Files", default=24000, min=1, soft_min=256, soft_max=48000,
        description="Most texture files open at once")
    texture_blur: FloatProperty(
        name="Texture Blur", default=0, min=0, soft_max=0.1, subtype="FACTOR", step=1, precision=3,
        description="Extra filtering of every texture, as a share of its width")


def device_changed(scene):
    """Device GPU keeps a pbrt.exe starting or waiting (gpu.py), so F12 skips its start-up. Device CPU lets it go. Not in background Blender."""
    import sys
    if bpy.app.background or scene is None or not hasattr(scene, "moonray_hydra"):
        return
    if scene.moonray_hydra.device == "GPU":
        from . import gpu
        gpu.warm_up()
    elif __package__ + ".gpu" in sys.modules:
        sys.modules[__package__ + ".gpu"].release()


@bpy.app.handlers.persistent
def device_on_load(*_):
    device_changed(bpy.context.scene)


def enabled_passes(view_layer):
    passes = {name: entry for name, entry in PASSES.items() if getattr(view_layer, entry[4])}
    passes.update({f"Combined_{group.name}": (3, "RGB", "COLOR", f"blenderGroup_{group.name}", "") for group in view_layer.lightgroups})
    return passes


def thread_count(scene):
    """0 is MoonRay's own choice, all but one. blender -t N also shows here, as render.threads."""
    render = scene.render
    return render.threads if render.threads_mode == "FIXED" or render.threads != os.cpu_count() else 0


def thread_problems(scene):
    """MoonRay builds its thread pool once per Blender: a second driver in cleanUpGlobalDriver's place keeps the old pool (render_tab/t_threads.py)."""
    global first_threads
    first_threads = first_threads if first_threads is not None else thread_count(scene)
    if thread_count(scene) != first_threads:
        return [f"Threads stays at this Blender's first render ({first_threads or 'Auto'}). Save and restart Blender to change it"]
    return []


def render_settings(scene):
    """Every value this tab sends to MoonRay. A scene property named moonray:sceneVariable:<name> sets any MoonRay variable, and wins over the tab."""
    panel = scene.moonray_hydra
    settings = {}
    for name in Settings.__annotations__:
        if name in {"denoise", "viewport_samples", "look", "device"}:
            continue
        value = getattr(panel, name)
        if isinstance(value, str):
            value = ENUM_VALUES[value]
        settings[PREFIX + RENAMED.get(name, name)] = value
    settings["enableMotionBlur"] = scene.render.use_motion_blur
    settings["blender:frame"] = float(scene.frame_current + scene.frame_subframe)
    settings.update({name: scene[name] for name in scene.keys() if name.startswith(PREFIX)})
    settings[PREFIX + "two_stage_output"] = False  # Blender takes the pixels from memory. With it on, a dump of the scene given to moonray.exe writes to \tmp on the drive root
    settings[PREFIX + "tmp_dir"] = tempfile.gettempdir().replace("\\", "/")  # empty means /tmp, the root of the current drive
    return settings


def viewport_settings(scene):
    """Over render_settings(), for the viewport's delegate. A scene property moonray:sceneVariable: wins there, not here: the limit is the panel's."""
    return {PREFIX + "sampling_mode": ENUM_VALUES["UNIFORM"], PREFIX + RENAMED.get("pixel_samples", "pixel_samples"): scene.moonray_hydra.viewport_samples}


def motion_settings(engine, depsgraph):
    """Blender's Hydra export has no motion, so read where each object ends the shutter. Called before the export, it leaves the frame as it was.
    Objects move, deforming meshes don't: MoonRay would need new points per object. Returns render settings."""
    scene = depsgraph.scene
    if not scene.render.use_motion_blur or scene.render.motion_blur_shutter <= 0:
        return {}
    frame, subframe, half = scene.frame_current, scene.frame_subframe, scene.render.motion_blur_shutter / 2
    kinds = {"MESH", "CURVES", "POINTCLOUD", "CAMERA", "VOLUME"}

    def matrices(offset):
        moment = frame + subframe + offset
        engine.frame_set(int(moment // 1), subframe=moment % 1)
        return {ob.as_pointer(): (ob.name, ob.matrix_world.copy()) for ob in depsgraph.objects if ob.type in kinds}

    begin, end = matrices(-half), matrices(half)
    engine.frame_set(frame, subframe=subframe)
    camera = scene.camera.name if scene.camera else ""
    lines = []
    for pointer, (name, matrix) in end.items():
        if pointer in begin and matrix != begin[pointer][1]:
            numbers = " ".join(repr(value) for pair in (begin[pointer][1], matrix) for row in pair.transposed() for value in row)
            lines.append(f"{'camera' if name == camera else f'O_{pointer:016X}'}\t{numbers}")  # prim names. MoonRay gets Blender's free camera
    return {"blender:motionEnd": "\n".join(lines), "blender:shutter": float(scene.render.motion_blur_shutter)}


def world_normals(engine, depsgraph):
    """MoonRay's normals are in camera space, Blender's Normal pass is in world space."""
    from . import denoise
    layer = depsgraph.view_layer.name
    result = engine.get_result()
    passes = result.layers[layer].passes if result and layer in result.layers else ()
    if "Normal" in passes and depsgraph.scene.camera:
        height, width = result.resolution_y, result.resolution_x
        rotation = np.array(depsgraph.scene.camera.matrix_world.to_3x3().normalized(), np.float32)
        denoise.replace(engine, layer, width, height, {"Normal": denoise.read(passes["Normal"], height, width) @ rotation.T})


def aov_settings(depsgraph):
    settings = {f"blender:lightGroup:O_{ob.as_pointer():016X}": ob.lightgroup for ob in depsgraph.objects if ob.type == "LIGHT" and ob.lightgroup}
    settings.update({f"aovToken:{name}": entry[3] for name, entry in {**PASSES, **enabled_passes(depsgraph.view_layer)}.items()})
    return settings


class RenderPanel:
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"
    COMPAT_ENGINES = {ENGINE}

    @classmethod
    def poll(cls, context):
        return context.engine in cls.COMPAT_ENGINES

    def start(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        return layout, context.scene.moonray_hydra


class RENDER_PT_moonray_look(RenderPanel, bpy.types.Panel):
    bl_label = "Look"

    def draw(self, context):
        layout, panel = self.start(context)
        layout.prop(panel, "device")
        layout.prop(panel, "look")


class RENDER_PT_moonray_sampling(RenderPanel, bpy.types.Panel):
    bl_label = "Sampling"

    def draw(self, context):
        layout, panel = self.start(context)
        layout.prop(panel, "sampling_mode")
        column = layout.column(align=True)
        if panel.device == "GPU":
            column.prop(panel, "pixel_samples")
            column.label(text=f"GPU: {panel.pixel_samples ** 2} samples per pixel, Uniform", icon="INFO")
            return
        if panel.sampling_mode == "UNIFORM":
            column.prop(panel, "pixel_samples")
        else:
            column.prop(panel, "target_adaptive_error")
            column.prop(panel, "min_adaptive_samples")
            column.prop(panel, "max_adaptive_samples")
        column = layout.column(align=True)
        column.prop(panel, "light_samples")
        column.prop(panel, "bsdf_samples")
        column.prop(panel, "bssrdf_samples")
        addon = context.preferences.addons.get(__package__)
        if addon and addon.preferences.experimental_viewport:
            layout.prop(panel, "viewport_samples")


class RENDER_PT_moonray_denoise(RenderPanel, bpy.types.Panel):
    bl_label = "Denoise"
    bl_parent_id = "RENDER_PT_moonray_sampling"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.prop(context.scene.moonray_hydra, "denoise", text="")

    def draw(self, context):
        self.layout.label(text="OpenImageDenoise, on the CPU")


class RENDER_PT_moonray_sampling_lights(RenderPanel, bpy.types.Panel):
    bl_label = "Lights"
    bl_parent_id = "RENDER_PT_moonray_sampling"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout, panel = self.start(context)
        layout.prop(panel, "light_sampling_mode")
        row = layout.row()
        row.active = panel.light_sampling_mode == "ADAPTIVE"
        row.prop(panel, "light_sampling_quality")


class RENDER_PT_moonray_sampling_advanced(RenderPanel, bpy.types.Panel):
    bl_label = "Advanced"
    bl_parent_id = "RENDER_PT_moonray_sampling"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout, panel = self.start(context)
        layout.prop(panel, "lock_frame_noise")


class RENDER_PT_moonray_light_paths(RenderPanel, bpy.types.Panel):
    bl_label = "Light Paths"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        pass


class RENDER_PT_moonray_bounces(RenderPanel, bpy.types.Panel):
    bl_label = "Max Bounces"
    bl_parent_id = "RENDER_PT_moonray_light_paths"

    def draw(self, context):
        layout, panel = self.start(context)
        column = layout.column(align=True)
        column.prop(panel, "max_bounces")
        column = layout.column(align=True)
        column.prop(panel, "diffuse_bounces")
        column.prop(panel, "glossy_bounces")
        column.prop(panel, "transmission_bounces")
        column = layout.column(align=True)
        column.prop(panel, "presence_bounces")


class RENDER_PT_moonray_clamping(RenderPanel, bpy.types.Panel):
    bl_label = "Clamping"
    bl_parent_id = "RENDER_PT_moonray_light_paths"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout, panel = self.start(context)
        column = layout.column(align=True)
        column.prop(panel, "sample_clamping_value")
        column.prop(panel, "sample_clamping_depth")
        layout.prop(panel, "roughness_clamping_factor")
        layout.prop(panel, "russian_roulette_threshold")
        layout.prop(panel, "max_subsurface_per_path")


class RENDER_PT_moonray_motion_blur(RenderPanel, bpy.types.Panel):
    bl_label = "Motion Blur"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.prop(context.scene.render, "use_motion_blur", text="")

    def draw(self, context):
        layout, _ = self.start(context)
        render = context.scene.render
        layout.active = render.use_motion_blur
        layout.prop(render, "motion_blur_shutter")
        column = layout.column(align=True)
        column.label(text="Objects and the camera blur, in a straight line", icon="INFO")
        column.label(text="Deforming meshes stay sharp")


class RENDER_PT_moonray_film(RenderPanel, bpy.types.Panel):
    bl_label = "Film"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout, panel = self.start(context)
        layout.prop(context.scene.render, "film_transparent", text="Transparent")


class RENDER_PT_moonray_film_filter(RenderPanel, bpy.types.Panel):
    bl_label = "Pixel Filter"
    bl_parent_id = "RENDER_PT_moonray_film"

    def draw(self, context):
        layout, panel = self.start(context)
        layout.prop(panel, "pixel_filter", text="Type")
        layout.prop(panel, "pixel_filter_width")


class RENDER_PT_moonray_film_thresholds(RenderPanel, bpy.types.Panel):
    bl_label = "Thresholds"
    bl_parent_id = "RENDER_PT_moonray_film"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout, panel = self.start(context)
        layout.prop(panel, "presence_threshold")
        layout.prop(panel, "transparency_threshold")


class RENDER_PT_moonray_features(RenderPanel, bpy.types.Panel):
    bl_label = "Features"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout, panel = self.start(context)
        column = layout.column(heading="Enable", align=True)
        column.prop(panel, "enable_dof")
        column.prop(panel, "enable_shadowing")
        column.prop(panel, "enable_subsurface_scattering")
        column.prop(panel, "enable_presence_shadows")
        column.prop(panel, "lights_visible_in_camera")
        layout.prop(panel, "shadow_terminator_fix")


class RENDER_PT_moonray_performance(RenderPanel, bpy.types.Panel):
    bl_label = "Performance"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        pass


class RENDER_PT_moonray_threads(RenderPanel, bpy.types.Panel):
    bl_label = "Threads"
    bl_parent_id = "RENDER_PT_moonray_performance"

    def draw(self, context):
        layout, _ = self.start(context)
        render = context.scene.render
        layout.prop(render, "threads_mode")
        row = layout.row()
        row.enabled = render.threads_mode == "FIXED"
        row.prop(render, "threads")
        layout.label(text="Set once per Blender, by its first MoonRay render", icon="INFO")


class RENDER_PT_moonray_memory(RenderPanel, bpy.types.Panel):
    bl_label = "Textures"
    bl_parent_id = "RENDER_PT_moonray_performance"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout, panel = self.start(context)
        layout.prop(panel, "texture_cache_size")
        layout.prop(panel, "texture_file_handles")
        layout.prop(panel, "texture_blur")


class VIEWLAYER_PT_moonray_passes(bpy.types.Panel):
    bl_label = "Passes"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "view_layer"
    COMPAT_ENGINES = {ENGINE}

    @classmethod
    def poll(cls, context):
        return context.engine in cls.COMPAT_ENGINES

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        view_layer = context.view_layer
        column = layout.column(heading="Data", align=True)
        column.prop(view_layer, "use_pass_combined")
        column.prop(view_layer, "use_pass_z", text="Depth")
        column.prop(view_layer, "use_pass_position")
        column.prop(view_layer, "use_pass_normal")
        column.prop(view_layer, "use_pass_uv")
        column = layout.column(heading="Diffuse", align=True)
        column.prop(view_layer, "use_pass_diffuse_direct", text="Direct")
        column.prop(view_layer, "use_pass_diffuse_indirect", text="Indirect")
        column.prop(view_layer, "use_pass_diffuse_color", text="Color")
        column = layout.column(heading="Glossy", align=True)
        column.prop(view_layer, "use_pass_glossy_direct", text="Direct")
        column.prop(view_layer, "use_pass_glossy_indirect", text="Indirect")
        column = layout.column(heading="Other", align=True)
        column.prop(view_layer, "use_pass_transmission_direct", text="Transmission")


class VIEWLAYER_PT_moonray_lightgroups(bpy.types.Panel):
    bl_label = "Light Groups"
    bl_parent_id = "VIEWLAYER_PT_moonray_passes"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "view_layer"
    bl_options = {"DEFAULT_CLOSED"}
    COMPAT_ENGINES = {ENGINE}

    @classmethod
    def poll(cls, context):
        return context.engine in cls.COMPAT_ENGINES

    def draw(self, context):
        layout = self.layout
        view_layer = context.view_layer
        row = layout.row()
        row.template_list("UI_UL_list", "lightgroups", view_layer, "lightgroups", view_layer, "active_lightgroup_index", rows=3)
        column = row.column(align=True)
        column.operator("scene.view_layer_add_lightgroup", icon="ADD", text="")
        column.operator("scene.view_layer_remove_lightgroup", icon="REMOVE", text="")
        layout.label(text="Pick the group on each light, in its Object tab. Lamps only", icon="INFO")


class OBJECT_PT_moonray_lightgroup(bpy.types.Panel):
    bl_label = "Light Group"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_options = {"DEFAULT_CLOSED"}
    COMPAT_ENGINES = {ENGINE}

    @classmethod
    def poll(cls, context):
        return context.engine in cls.COMPAT_ENGINES and context.object is not None and context.object.type == "LIGHT"

    def draw(self, context):
        self.layout.prop_search(context.object, "lightgroup", context.view_layer, "lightgroups", text="Group")


CLASSES = (Settings, RENDER_PT_moonray_look, RENDER_PT_moonray_sampling, RENDER_PT_moonray_denoise, RENDER_PT_moonray_sampling_lights,
           RENDER_PT_moonray_sampling_advanced, RENDER_PT_moonray_light_paths, RENDER_PT_moonray_bounces, RENDER_PT_moonray_clamping,
           RENDER_PT_moonray_motion_blur, RENDER_PT_moonray_film, RENDER_PT_moonray_film_filter, RENDER_PT_moonray_film_thresholds,
           RENDER_PT_moonray_features, RENDER_PT_moonray_performance, RENDER_PT_moonray_threads, RENDER_PT_moonray_memory,
           VIEWLAYER_PT_moonray_passes, VIEWLAYER_PT_moonray_lightgroups, OBJECT_PT_moonray_lightgroup)
