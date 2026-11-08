# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Runs in a background Blender with factory settings, started by lights.bake_world: renders one world with Cycles through an equirect camera.

Usage (after --): <blend with the world> <world name> <output exr> <width> <height> <frame> <samples>
"""
import math
import sys

import bpy

blend, name, output, width, height, frame, samples = sys.argv[sys.argv.index("--") + 1:]
with bpy.data.libraries.load(blend) as (source, target):
    target.worlds = [name]
scene = bpy.context.scene
scene.world = target.worlds[0]
for block in list(scene.objects):
    bpy.data.objects.remove(block)
# A world that shows the camera something else is lit by its other branch. That's the one a dome needs: it lights the scene.
for tree in [scene.world.node_tree, *bpy.data.node_groups]:
    for link in list(tree.links):
        if link.from_node.type == "LIGHT_PATH" and link.from_socket.identifier == "Is Camera Ray":
            zero = tree.nodes.new("ShaderNodeValue")
            zero.outputs[0].default_value = 0.0
            tree.links.new(zero.outputs[0], link.to_socket)

data = bpy.data.cameras.new("bake")
data.type = "PANO"
data.panorama_type = "EQUIRECTANGULAR"
camera = bpy.data.objects.new("bake", data)
camera.rotation_euler = (math.pi / 2, 0, -math.pi / 2)  # the image lines up with an Environment Texture
scene.collection.objects.link(camera)
scene.camera = camera

render = scene.render
render.engine = "CYCLES"
render.resolution_x, render.resolution_y = int(width), int(height)
render.resolution_percentage = 100
render.film_transparent = False
render.use_compositing = False
render.use_sequencer = False
render.filepath = output
render.image_settings.file_format = "OPEN_EXR"
render.image_settings.color_mode = "RGB"
render.image_settings.color_depth = "32"
render.image_settings.exr_codec = "ZIP"
cycles = scene.cycles
cycles.device = "CPU"
cycles.samples = int(samples)
cycles.use_adaptive_sampling = False
cycles.use_denoising = False
cycles.sample_clamp_direct = 0.0
cycles.sample_clamp_indirect = 0.0
cycles.pixel_filter_type = "BOX"
cycles.filter_width = 1.0
scene.frame_set(int(frame))
bpy.ops.render.render(write_still=True)
