# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""The light sources Blender's Hydra export loses: a World that isn't a plain Background, and meshes that emit.

World: bake_world() has Cycles render the world to an equirect EXR in a background Blender, and the delegate uses that as the dome's texture.
Hydra's dome only takes a colour or one image. The cache name is a hash of the world's nodes, so an edit makes a new bake and no edit costs nothing.

Emission: MoonRay only samples what's a light. Constant emission comes off the surface (native.Network.glow), and write_glows() writes the
triangles to a file the delegate turns into MeshLights, RenderDelegate::loadMeshLights() in edit_hdmoonray.py. File, little-endian:
    uint32 lights, then per light: float rgb[3], uint32 points, uint32 triangles, float xyz[points][3], int32 corners[triangles][3]
"""
import hashlib
import math
import os
import struct
import subprocess
import time

import bmesh
import bpy
import numpy as np
from mathutils.bvhtree import BVHTree

BAKE_WIDTH, BAKE_LARGEST = 2048, 4096  # wider only for an image in the world, up to the largest
BAKE_SAMPLES = 32
BAKE_TIMEOUT = 180  # seconds
FORMAT = 2  # part of the bake's name: raise it when bake_world_child.py changes what it renders
THIN_GLOW = 0.05  # metres on the scene's Unit Scale. Two emissive faces closer than this are one sheet, see glow_layers().
LAYER_CHECK_LIMIT = 200000  # triangles of one mesh. More than this writes both sides without looking, which costs light samples, not light
_skipped = {"rna_type", "name_full", "location", "location_absolute", "width", "height", "dimensions", "select", "color", "color_tag",
            "use_custom_color", "show_options", "show_preview", "show_texture", "hide", "parent", "node", "bl_icon", "warning_propagation"}


def _describe(item, depth=0):
    """Text of everything that can change what a node renders, by RNA, so that a new property counts without a change here."""
    if depth > 6:
        return ""
    parts = []
    for prop in item.bl_rna.properties:
        name = prop.identifier
        if name in _skipped or name.startswith("debug"):
            continue
        value = getattr(item, name)
        if prop.type == "POINTER":
            if isinstance(value, bpy.types.Image):
                parts.append(_image_id(value))
            elif isinstance(value, bpy.types.NodeTree):
                parts.append(_tree_text(value))
            elif value is not None and not isinstance(value, bpy.types.ID):
                parts.append(_describe(value, depth + 1))
        elif prop.type == "COLLECTION":
            parts.extend(_describe(entry, depth + 1) for entry in value)
        else:
            parts.append(f"{name}={list(value) if getattr(prop, 'is_array', False) else value}")
    return " ".join(parts)


def _image_id(image):
    if image.is_dirty or image.packed_file or image.source == "GENERATED":
        pixels = np.empty(len(image.pixels), dtype=np.float32)
        image.pixels.foreach_get(pixels)
        return hashlib.blake2b(pixels.tobytes(), digest_size=16).hexdigest()
    path = bpy.path.abspath(image.filepath, library=image.library)
    stat = os.stat(path) if os.path.exists(path) else None
    return f"{path} {stat and (stat.st_size, stat.st_mtime_ns)}"


def _tree_text(tree):
    parts = [node.bl_idname + " " + node.name + " " + _describe(node) for node in tree.nodes]
    parts += [f"{link.from_node.name}.{link.from_socket.identifier}>{link.to_node.name}.{link.to_socket.identifier}" for link in tree.links]
    return "\n".join(sorted(parts))


def _trees(tree, seen):
    if tree not in seen:
        seen.add(tree)
        for node in tree.nodes:
            if node.type == "GROUP" and node.node_tree:
                _trees(node.node_tree, seen)
    return seen


def bake_world(depsgraph, cancelled, threads, lost, cache):
    """Path of this world's EXR, or "" if there's none, with a list of problems. lost: what textures.world_problems() named."""
    world = depsgraph.scene.world.original
    trees = _trees(world.node_tree, set())
    animated = any(tree.animation_data for tree in trees) or world.animation_data
    frame = depsgraph.scene.frame_current
    key = hashlib.blake2b(f"{FORMAT} {bpy.app.version_string} {frame if animated else ''}\n{_tree_text(world.node_tree)}".encode(), digest_size=16)
    target = os.path.join(cache, f"{key.hexdigest()}_world.exr")
    problems = []
    if any(node.type == "LIGHT_PATH" for tree in trees for node in tree.nodes):
        problems.append(f"world {world.name}: Light Path is baked for what the scene is lit by, so the camera sees that too")
    if os.path.exists(target):
        os.utime(target)
        return target, problems
    widths = [node.image.size[0] for tree in trees for node in tree.nodes if node.type in {"TEX_ENVIRONMENT", "TEX_IMAGE"} and node.image]
    width = max(BAKE_WIDTH, min(max(widths, default=0), BAKE_LARGEST))
    scratch = f"{target}.{os.getpid()}"
    blend, partial, log = scratch + ".blend", scratch + ".exr", scratch + ".log"
    try:
        bpy.data.libraries.write(blend, {world}, path_remap="ABSOLUTE", fake_user=True)
        child = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bake_world_child.py")
        command = [bpy.app.binary_path, "--background", "--factory-startup", "-t", str(threads), "--python-exit-code", "9", "--python", child,
                   "--", blend, world.name, partial, str(width), str(width // 2), str(frame), str(BAKE_SAMPLES)]
        started = time.time()
        with open(log, "wb") as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        while process.poll() is None:
            if cancelled() or time.time() - started > BAKE_TIMEOUT:
                process.kill()
                process.wait()
                if not cancelled():
                    problems.append(f"world {world.name}: its bake took over {BAKE_TIMEOUT} s and was stopped, so its light is wrong")
                return "", problems
            time.sleep(0.05)
        print(f"[moonray_hydra] world {world.name} baked in {time.time() - started:.1f} s, {'+'.join(lost)}", flush=True)
        if process.returncode or not os.path.exists(partial):
            problems.append(f"world {world.name}: its bake failed, exit {process.returncode}, so its light is wrong")
            return "", problems
        os.replace(partial, target)
        return target, problems
    except OSError as error:
        if os.path.exists(target):  # os.replace onto a copy another Blender reads: that copy will do
            return target, problems
        problems.append(f"world {world.name}: its bake failed, so its light is wrong: {error}")
        return "", problems
    finally:
        for path in (blend, partial, log):
            try:
                os.remove(path)
            except OSError:  # never made
                pass


def _reversed_polygons(mesh, slots):
    """The polygons that wind into their island of faces, as a closed mesh with reversed normals does."""
    shape = bmesh.new()
    shape.from_mesh(mesh)
    faces = [face for face in shape.faces if face.material_index in slots]
    bmesh.ops.recalc_face_normals(shape, faces=faces)
    found = {face.index for face in faces if face.normal.dot(mesh.polygons[face.index].normal) < 0}
    shape.free()
    return found


def glow_layers(points, triangles, reversed_sides, thin):
    """An emitter's light as (points, triangles). Cycles emits from both faces and a MeshLight from one, and the surface stops the side that
    shouldn't get through, so each triangle gets a layer on each side, on its own face normal. The outer side is always written. The inner is left
    out when an emissive triangle lies closer behind it than thin: rays to a light stop just short of it, so its light would pass through the
    far wall and add to that wall's own. The gap is what a 32-bit float can hold this far from the origin."""
    corners = points[triangles]
    way = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    length = np.linalg.norm(way, axis=1)
    keep = length > 0
    triangles, corners, way, reversed_sides = triangles[keep], corners[keep], way[keep] / length[keep, None], reversed_sides[keep]
    gap = max(20.0, float(np.abs(points).max())) * 5e-7
    outward = np.where(reversed_sides[:, None], -way, way)
    clear = np.ones(len(triangles), dtype=bool)
    if len(triangles) <= LAYER_CHECK_LIMIT:
        tree = BVHTree.FromPolygons(points.tolist(), triangles.tolist(), all_triangles=True)
        centres = corners.mean(axis=1) - outward * gap
        for index, (centre, inward) in enumerate(zip(centres.tolist(), (-outward).tolist())):
            clear[index] = tree.ray_cast(centre, inward, thin)[0] is None
    swap = [0, 2, 1]
    outer = np.where(reversed_sides[:, None, None], corners[:, swap], corners) + outward[:, None] * gap
    near = np.where(reversed_sides[clear][:, None, None], corners[clear], corners[clear][:, swap]) - outward[clear][:, None] * gap
    layers = np.concatenate((outer, near))
    return layers.reshape(-1, 3), np.arange(len(layers) * 3, dtype=np.int32).reshape(-1, 3)


def _instance_glow(depsgraph, emitters, thin):
    """Yields (material, points, triangles) for each mesh on screen that has an emitter's slots."""
    for instance in depsgraph.object_instances:
        block = instance.object
        if block.type != "MESH" or not (instance.is_instance or instance.show_self):
            continue
        slots = {}
        for index, slot in enumerate(block.material_slots):
            if slot.material and slot.material.original in emitters:
                slots.setdefault(slot.material.original, []).append(index)
        if not slots:
            continue
        matrix = np.array(instance.matrix_world, dtype=np.float64)
        mesh = block.data
        mesh.calc_loop_triangles()
        count = len(mesh.loop_triangles)
        corners, material_index, polygon = np.empty(count * 3, np.int32), np.empty(count, np.int32), np.empty(count, np.int32)
        mesh.loop_triangles.foreach_get("vertices", corners)
        mesh.loop_triangles.foreach_get("material_index", material_index)
        mesh.loop_triangles.foreach_get("polygon_index", polygon)
        corners = corners.reshape(-1, 3)
        points = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.vertices.foreach_get("co", points)
        points = points.reshape(-1, 3).astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3]
        for material, indices in slots.items():
            keep = np.isin(material_index, indices)
            if not keep.any():
                continue
            flipped = _reversed_polygons(mesh, set(indices)) if keep.sum() <= LAYER_CHECK_LIMIT else set()
            sides = np.isin(polygon[keep], list(flipped))
            triangles = corners[keep]
            if np.linalg.det(matrix[:3, :3]) < 0:  # a mirror turns the winding inside out
                triangles = triangles[:, [0, 2, 1]]
            yield (material, *glow_layers(points, triangles, sides, thin))


def _triangles(material, depsgraph):
    """World-space corners a, b, c, as arrays, of the triangles this material covers, one yield per mesh."""
    material = material.original
    for block in depsgraph.objects:
        if block.type != "MESH":
            continue
        slots = [index for index, slot in enumerate(block.material_slots) if slot.material and slot.material.original == material]
        if not slots:
            continue
        mesh = block.data
        mesh.calc_loop_triangles()
        count = len(mesh.loop_triangles)
        corners, indices, points = np.empty(count * 3, np.int32), np.empty(count, np.int32), np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.loop_triangles.foreach_get("vertices", corners)
        mesh.loop_triangles.foreach_get("material_index", indices)
        mesh.vertices.foreach_get("co", points)
        matrix = np.array(block.matrix_world, dtype=np.float64)
        points = points.reshape(-1, 3).astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3]
        yield points[corners.reshape(-1, 3)[np.isin(indices, slots)]].transpose(1, 0, 2)


def mean_chord(material, depsgraph):
    """Mean length of a straight line through what this material covers, 4 V / A, in world units. 0.0 if there's no closed volume to measure."""
    volume, area = 0.0, 0.0
    for a, b, c in _triangles(material, depsgraph):
        normals = np.cross(b - a, c - a)
        volume += abs(float((a * normals).sum()) / 6)
        area += float(np.linalg.norm(normals, axis=1).sum()) / 2
    return 4 * volume / area if area else 0.0


def flat_share(material, depsgraph):
    """Share of this material's area in the six face directions that hold the most, either side counting as one, directions within about 2 degrees
    together. Near 1 for panes, however many. Small for anything curved."""
    normals = [np.cross(b - a, c - a) for a, b, c in _triangles(material, depsgraph)]
    normals = np.concatenate(normals) if normals else np.zeros((0, 3))
    area = np.linalg.norm(normals, axis=1)
    normals, area = normals[area > 0], area[area > 0]
    if not len(area):
        return 0.0
    unit = normals / area[:, None]
    unit *= np.sign(unit[np.arange(len(unit)), np.abs(unit).argmax(axis=1)])[:, None]
    groups = np.unique(np.rint(unit * 30).astype(np.int32), axis=0, return_inverse=True)[1].ravel()
    return float(np.sort(np.bincount(groups, area))[-6:].sum() / area.sum())


def has_bounded_light(depsgraph):
    """True if a light Blender sends is sure to be in MoonRay's light tree: not a Sun, not a MeshLight, not one MoonRay turns off for being black or for
    an uneven scale. False asks for the delegate's anchor, RenderDelegate::loadMeshLights(), which costs next to nothing, so anything in doubt counts as no."""
    for block in depsgraph.objects:
        if block.type == "LIGHT" and block.data.type != "SUN" and block.data.energy > 0 and any(block.data.color):
            scale = block.matrix_world.to_scale()
            if min(abs(value) for value in scale) > max(abs(value) for value in scale) * 0.999:
                return True
    return False


def glow_state(depsgraph, emitters):
    """A digest of what write_glows() would write."""
    digest = hashlib.sha1()
    thin = THIN_GLOW / (depsgraph.scene.unit_settings.scale_length or 1)
    for material, points, triangles in _instance_glow(depsgraph, emitters, thin):
        digest.update(repr((material.name, emitters[material])).encode())
        digest.update(np.asarray(points).tobytes())
        digest.update(np.asarray(triangles).tobytes())
    return digest.digest()


def write_glows(depsgraph, emitters, path):
    """Writes the MeshLights of emitters, {material: radiance}. Returns the number of lights."""
    thin = THIN_GLOW / (depsgraph.scene.unit_settings.scale_length or 1)
    gathered = {}
    for material, points, triangles in _instance_glow(depsgraph, emitters, thin):
        gathered.setdefault(material, []).append((points, triangles))
    with open(path, "wb") as f:
        f.write(struct.pack("<I", len(gathered)))
        for material, pieces in gathered.items():
            start = np.cumsum([0] + [len(points) for points, _ in pieces])
            points = np.concatenate([item for item, _ in pieces])
            triangles = np.concatenate([item + first for (_, item), first in zip(pieces, start)])
            f.write(struct.pack("<3fII", *emitters[material], len(points), len(triangles)))
            f.write(points.astype("<f4").tobytes())
            f.write(triangles.astype("<i4").tobytes())
            print(f"[moonray_hydra] MeshLight {material.name}: {len(triangles)} triangles, radiance {emitters[material]}", flush=True)
    return len(gathered)
