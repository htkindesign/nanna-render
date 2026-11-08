# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Tiled copies of Blender's images, for MoonRay, whose texture shaders only sample tiled, mip-mapped files.

Runs at render time. The user's files, images and .blend are only read. Copies go to CACHE, named by a hash of the source bytes,
so an edited image gets a new copy and an unchanged one is converted once. The delegate gets a manifest of original path to copy,
because the file path inside Blender's material network can't be changed from Python.
"""
import hashlib
import math
import os
import tempfile
import time

import bpy
import numpy as np
import OpenImageIO as oiio

from . import lights, native

CACHE = os.path.join(tempfile.gettempdir(), "hdmoonray_tx")
FORMAT = 3  # part of the file name: raise it when the conversion below changes
LINEAR = "_linear"  # ends the name of a copy that holds scene linear: the delegate then turns off MoonRay's colour conversion
CACHE_LIMIT = 4 * 2**30  # bytes. A scene's textures alone can pass 1 GB (a furnished interior). Going over costs a reconversion of images not used lately, nothing else
CACHE_RECENT = 3600  # seconds. Files used this recently stay even over the limit: this frame, or another open Blender, may need them
TX_SETTINGS = {"maketx:filtername": "box", "maketx:highlightcomp": 0, "maketx:constant_color_detect": 0}
# What Blender's Hydra export hands over: the first of these two shaders in the tree, wherever it sits, with its image textures.
# Measured: Mix, Math and Bump arrive as defaults, and a Diffuse BSDF on the output loses to an unused Principled beside it
MATERIAL_SHADERS = {"BSDF_PRINCIPLED", "BSDF_DIFFUSE"}
MATERIAL_CARRIED = MATERIAL_SHADERS | {"TEX_IMAGE", "NORMAL_MAP", "UVMAP", "TEX_COORD", "MAPPING", "REROUTE"}
WORLD_CARRIED = {"TEX_ENVIRONMENT", "TEX_IMAGE", "TEX_COORD", "REROUTE"}

HEIGHT_SIZE = 2048  # a Bump's height is worked out at no more than this
_bumps = set()  # normal images this frame wrote or used
_digests = {}  # (path, size, mtime) -> hash, so an unchanged file isn't read again on every frame
_manifests = 0
emitters = {}  # {material: radiance} of the last prepare()
clear_shadow = []  # the last prepare()'s material names with Network.clear_shadow
presence_free = False  # the last prepare() saw every material as a native network, none with a presence. Shadow rays then skip presence tests, a third of the render on a test interior


def shaded(material):
    """Same test as Cycles. use_nodes is no guard: it's always True in Blender 5, and a Grease Pencil material isn't shaded."""
    return not material.grease_pencil and bool(material.node_tree)


def _digest(path):
    stat = os.stat(path)
    file_id = (path, stat.st_size, stat.st_mtime_ns)
    if file_id not in _digests:
        with open(path, "rb") as f:
            _digests[file_id] = hashlib.file_digest(f, "blake2b").hexdigest()[:32]
    return _digests[file_id]


def _is_tiled(path):
    """A good copy has tiles and, unless it's one pixel, a second mip level. A bad copy must not be trusted from the cache."""
    source = oiio.ImageInput.open(path)
    if not source:
        oiio.geterror()
        return False
    spec = source.spec()
    tiled = spec.tile_width > 0 and (max(spec.width, spec.height) == 1 or source.seek_subimage(0, 1))
    source.close()
    return bool(tiled)


def _settled(path):
    """_is_tiled, with time for another Blender's os.replace of the same copy: a read during one fails for a moment."""
    for delay in (0, 0.1, 0.4, 1):
        time.sleep(delay)
        if _is_tiled(path):
            return True
    return False


def _write(source, target, spec):
    partial = f"{target}.{os.getpid()}.tx"  # another Blender may be converting the same image
    if not oiio.ImageBufAlgo.make_texture(oiio.MakeTxTexture, source, partial, spec):
        raise OSError(oiio.geterror())
    try:
        os.replace(partial, target)
    except PermissionError:  # Windows, while another Blender renders from a finished copy of this image. The name holds its hash: that copy will do
        os.remove(partial)
    if not _settled(target):
        raise OSError("the converted file isn't tiled")
    return target


def _tx_settings(premultiply):
    spec = oiio.ImageSpec()
    spec.attribute("oiio:UnassociatedAlpha", 0 if premultiply else 1)
    for name, value in TX_SETTINGS.items():
        spec.attribute(name, value)
    return spec


def convert(path, premultiply):
    """Path of the tiled copy of an 8-bit image file. Raises OSError if there can't be one."""
    target = os.path.join(CACHE, f"{_digest(path)}_{int(premultiply)}_{FORMAT}.tx")
    if os.path.exists(target) and _is_tiled(target):
        return target
    spec = _tx_settings(premultiply)
    source = oiio.ImageBuf(path, 0, 0, spec)
    if source.has_error:
        raise OSError(source.geterror())
    if not premultiply:  # a TIFF reader multiplies by any channel marked alpha, unless told not to, and MoonRay can't be told
        source.specmod().alpha_channel = -1
        source.specmod().erase_attribute("oiio:UnassociatedAlpha")
    return _write(source, target, spec)


def convert_pixels(image, premultiply):
    """Path of the tiled copy of an image as it is in memory. Byte pixels are still in the image's colour space, as in a file.
    Float pixels are scene linear, whatever the file's colour space was, so these copies are marked for MoonRay to leave alone:
    its UsdUVTexture gets the sRGB curve wrong on anything but 8-bit files."""
    width, height = image.size
    channels = image.channels
    if not width or not height:
        raise OSError("the image has no pixels")
    pixels = np.empty(width * height * channels, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    digest = hashlib.blake2b(pixels.tobytes(), digest_size=16)
    digest.update(f"{width} {height} {channels} {image.is_float} {premultiply} {image.alpha_mode}".encode())
    target = os.path.join(CACHE, f"{digest.hexdigest()}_{FORMAT}{LINEAR if image.is_float else ''}.tx")
    if os.path.exists(target) and _is_tiled(target):
        return target
    pixels = pixels.reshape(height, width, channels)[::-1].copy()  # Blender's first row is the bottom one
    if channels == 4 and image.alpha_mode in {"STRAIGHT", "PREMUL"}:
        alpha = pixels[..., 3:]
        if premultiply and not image.is_float:  # Blender keeps bytes straight and floats premultiplied
            pixels[..., :3] *= alpha
        elif image.is_float and not premultiply:
            pixels[..., :3] /= np.where(alpha > 0, alpha, 1)
    if not image.is_float:
        pixels = np.rint(pixels * 255.0).astype(np.uint8)
    buffer = oiio.ImageBuf(oiio.ImageSpec(width, height, channels, "float" if image.is_float else "uint8"))
    buffer.set_pixels(oiio.ROI(0, width, 0, height), pixels)
    return _write(buffer, target, _tx_settings(False))


def _image_nodes(tree, seen):
    """Image Texture nodes in a node tree, through groups."""
    if tree is None or tree in seen:
        return
    seen.add(tree)
    for node in tree.nodes:
        if node.type == "TEX_IMAGE" and node.image:
            yield node
        elif node.type == "GROUP":
            yield from _image_nodes(node.node_tree, seen)


def _used_images(depsgraph):
    """Images this frame uses, each with whether Color is multiplied by Alpha. Cycles does that unless the Alpha output is linked."""
    seen, images = set(), {}
    for block in depsgraph.ids:
        if isinstance(block, bpy.types.Material) and shaded(block):
            for node in _image_nodes(block.node_tree, seen):
                image = node.image.original
                images[image] = images.get(image, True) and not node.outputs["Alpha"].is_linked
    return {image: multiply and image.alpha_mode in {"STRAIGHT", "PREMUL"} for image, multiply in images.items()}


def _disk_path(image):
    return os.path.normpath(bpy.path.abspath(image.filepath, library=image.library))


def _memory_names(image):
    """Manifest keys for an image with no file of its own. Blender's export writes "<file or image name>.<format>", relative and
    resolving to nothing, so the delegate looks up the stem with a * in front. An edited file keeps its real path."""
    names = {"*" + image.name, "*" + os.path.splitext(image.name)[0]}
    if image.filepath:
        names.add("*" + os.path.splitext(os.path.basename(image.filepath.replace("\\\\", "/")))[0])
    if image.filepath and not image.packed_file:  # edited, or float: Blender's export names the real file
        names.add(_disk_path(image))
    return names


def _output(block):
    return block.node_tree.get_output_node("CYCLES") or block.node_tree.get_output_node("ALL")


def _surface(material):
    """The Principled BSDF that drives this material's output, if that's what it is."""
    output = _output(material)
    links = output.inputs["Surface"].links if output else ()
    node = links[0].from_node if links else None
    return node if node is not None and node.type == "BSDF_PRINCIPLED" else None


def _surface_lines(depsgraph):
    """Materials MoonRay's UsdPreviewSurface can't show, those with transmission or alpha, by the name Blender's Hydra export gives
    them. See refractiveNetwork() in Material.cc. An alpha of -1 means it's linked."""
    lines = []
    for block in depsgraph.ids:
        if not isinstance(block, bpy.types.Material) or not shaded(block):
            continue
        surface = _surface(block)
        if surface is None:
            continue
        transmission = surface.inputs["Transmission Weight"]
        weight = 1.0 if transmission.is_linked else transmission.default_value
        alpha = surface.inputs["Alpha"]
        opacity = -1.0 if alpha.is_linked else alpha.default_value
        if weight > 0 or opacity < 1:
            lines.append(f"material\tM_{block.as_pointer():016X}\t{weight}\t{opacity}\n")
    return lines


class _AnyImage(dict):
    """For the warnings, which run without the tiled copies: an image that failed has its own warning."""
    def __contains__(self, key):
        return key is not None

    def __getitem__(self, key):
        return ""


def _world_per_uv(material, depsgraph):
    """World size of one UV unit on what this material covers: the square root of the area ratio. One number, so a material on meshes
    of different scale gets the mean."""
    world, uv = 0.0, 0.0
    material = material.original
    for instance in depsgraph.objects:
        if instance.type != "MESH" or not instance.data.uv_layers.active:
            continue
        slots = [index for index, slot in enumerate(instance.material_slots) if slot.material and slot.material.original == material]
        if not slots:
            continue
        mesh = instance.data
        mesh.calc_loop_triangles()
        count = len(mesh.loop_triangles)
        if not count:
            continue
        vertices, loops, indices = np.empty(count * 3, np.int32), np.empty(count * 3, np.int32), np.empty(count, np.int32)
        mesh.loop_triangles.foreach_get("vertices", vertices)
        mesh.loop_triangles.foreach_get("loops", loops)
        mesh.loop_triangles.foreach_get("material_index", indices)
        keep = np.isin(indices, slots)
        positions = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.vertices.foreach_get("co", positions)
        coords = np.empty(len(mesh.loops) * 2, np.float32)
        mesh.uv_layers.active.uv.foreach_get("vector", coords)
        points = positions.reshape(-1, 3)[vertices.reshape(-1, 3)[keep]] @ np.array(instance.matrix_world)[:3, :3].T.astype(np.float32)
        uvs = coords.reshape(-1, 2)[loops.reshape(-1, 3)[keep]]
        edges = points[:, 1:] - points[:, :1]
        world += float(np.linalg.norm(np.cross(edges[:, 0], edges[:, 1]), axis=-1).sum()) / 2
        flat = uvs[:, 1:] - uvs[:, :1]
        uv += float(np.abs(flat[:, 0, 0] * flat[:, 1, 1] - flat[:, 0, 1] * flat[:, 1, 0]).sum()) / 2
    return math.sqrt(world / uv) if uv > 0 else 1.0


_linear = {}  # image -> its linear pixels, for one frame's Bump heights


def _linear_pixels(image):
    """Scene-linear pixels, bottom row first, at most HEIGHT_SIZE wide: a Bump doesn't need more."""
    if image not in _linear:
        width, height = image.size
        channels = image.channels
        pixels = np.empty(width * height * channels, dtype=np.float32)
        image.pixels.foreach_get(pixels)
        pixels = pixels.reshape(height, width, channels)
        step = max(1, max(width, height) // HEIGHT_SIZE)
        pixels = pixels[::step, ::step].copy()
        if not image.is_float and image.colorspace_settings.name == "sRGB":
            pixels[..., :3] = native.srgb_to_linear(pixels[..., :3])
        _linear[image] = pixels
    return _linear[image]


def _height_images(network, socket):
    """Image Texture nodes under a height."""
    found, stack = [], [socket]
    while stack:
        for link in stack.pop().links:
            if link.from_node.type == "TEX_IMAGE":
                found.append(link.from_node)
            elif link.from_node.type != "MAPPING":
                stack.extend(link.from_node.inputs)
    return found


def _texture_extent(material, depsgraph):
    """Mean size of the texture space of the meshes this material is on: the object-space length of Generated's 0 to 1."""
    sizes = []
    material = material.original
    for instance in depsgraph.objects:
        if instance.type == "MESH" and any(slot.material and slot.material.original == material for slot in instance.material_slots):
            sizes.append(2 * sum(instance.data.texspace_size) / 3)
    return sum(sizes) / len(sizes) if sizes else 1.0


def _pixel_size(material, depsgraph):
    """World size of a pixel on the nearest object this material is on, across the width of the frame."""
    scene = depsgraph.scene
    camera, width = scene.camera, scene.render.resolution_x * scene.render.resolution_percentage / 100
    if camera is None:
        return 0.0
    if camera.data.type == "ORTHO":
        return camera.data.ortho_scale / width
    material = material.original
    near = [(instance.matrix_world.translation - camera.matrix_world.translation).length for instance in depsgraph.objects
            if instance.type == "MESH" and any(slot.material and slot.material.original == material for slot in instance.material_slots)]
    return min(near, default=0.0) * camera.data.sensor_width / camera.data.lens / width


def _bake_bump(material, depsgraph):
    """The bake argument of native.translate: writes a Bump's height as a tiled normal image, in the cache."""
    def bake(network, node, distance, strength):
        socket = node.inputs["Height"]
        images = _height_images(network, socket)
        if not images or not all(item.image for item in images):
            network.lost.add("Bump height with no image")
            return None
        world = _world_per_uv(material, depsgraph)
        transforms = {network.uv_mapping(item.inputs["Vector"]) for item in images}
        largest = max(_linear_pixels(item.image.original).shape[:2] for item in images)
        digest = hashlib.blake2b(f"{sorted(transforms)} {world} {distance} {strength} {os.path.getmtime(native.__file__)}".encode(), digest_size=16)
        for item in images:
            digest.update(_linear_pixels(item.image.original).tobytes())
        if len(transforms) == 1:  # one mapping for all: the height tiles as the images do, and the normal map takes over that mapping
            scale, offset, degrees = transforms.pop()
            field = _OneTile(network)
            if degrees or scale[0] != scale[1]:
                network.lost.add("Bump over a rotated or uneven Mapping")
            texel = (world / (abs(scale[0]) * largest[1]), world / (abs(scale[1]) * largest[0]))
        else:
            scale, offset, degrees = (1.0, 1.0), (0.0, 0.0), 0.0
            size = min(max(largest) * 2, HEIGHT_SIZE)
            largest = (size, size)
            field = network
            texel = (world / size, world / size)
        # A small evaluation of the height stands for its nodes, ramps and groups included: the full one is most of a heavy scene's prep
        digest.update(native.height_field(field, socket, (64, 64), _linear_pixels).tobytes())
        target = os.path.join(CACHE, f"{digest.hexdigest()}_{FORMAT}_normal.tx")
        if not (os.path.exists(target) and _is_tiled(target)):
            height = native.height_field(field, socket, largest, _linear_pixels)
            pixels = native.bump_normals(height, texel, distance, strength)
            rows, columns = pixels.shape[:2]
            buffer = oiio.ImageBuf(oiio.ImageSpec(columns, rows, 3, "uint16"))
            buffer.set_pixels(oiio.ROI(0, columns, 0, rows), pixels[::-1].copy())
            try:
                _write(buffer, target, _tx_settings(False))
            except OSError as error:
                network.lost.add(f"Bump, its normal image failed: {error}")
                return None
        _bumps.add(target)
        return target, scale, offset, degrees, world
    return bake


class _OneTile:
    """A network whose images all read at plain UV: the height is made over one tile."""
    def __init__(self, network):
        self.network = network
        self.lost = network.lost

    def source(self, socket):
        return self.network.source(socket)

    def uv_mapping(self, socket):
        return (1.0, 1.0), (0.0, 0.0), 0.0


def native_network(block, tiled, depsgraph=None):
    """The native translation of this material, or None if it stays on the Hydra network."""
    if os.environ.get("HDMOONRAY_NATIVE") == "0":  # the control for tests: what Hydra alone gives
        return None
    output = _output(block)
    if output is None or not output.inputs["Surface"].is_linked:
        return None
    try:
        return native.translate(output.inputs["Surface"], tiled, _bake_bump(block, depsgraph) if depsgraph else None,
                                (lambda: lights.mean_chord(block, depsgraph)) if depsgraph else None,
                                (lambda: lights.flat_share(block, depsgraph)) if depsgraph else None,
                                getattr(getattr(depsgraph.scene, "cycles", None), "caustics_refractive", True) if depsgraph else True,
                                (lambda: _texture_extent(block, depsgraph)) if depsgraph else None,
                                (lambda: _pixel_size(block, depsgraph)) if depsgraph else None,
                                depsgraph.scene.moonray_hydra.look if depsgraph else "CYCLES")
    except native.Untranslated:
        return None


def _native_lines(depsgraph, tiled):
    """Each material's native network, by the name Blender's Hydra export gives it, and {material: radiance} for those that have a MeshLight.
    See nativeNetwork() in Material.cc."""
    global presence_free, clear_shadow
    lines, emitters = [], {}
    clear_shadow = []
    translated = True
    masked = False
    for block in depsgraph.ids:
        if isinstance(block, bpy.types.Material) and shaded(block):
            network = native_network(block, tiled, depsgraph)
            translated = translated and bool(network)
            if network:
                name = f"M_{block.as_pointer():016X}"
                written = native.lines(name, network)
                lines += written
                if network.clear_shadow:
                    clear_shadow.append(name)
                else:  # MoonRay's presence path ignores a ShadowReceiverSet, so this glass needs presence shadows off, and its own Transparent is no reason to keep them
                    masked = masked or any("\tpresence" in line and not line.endswith("\t1.0\n") for line in written)  # 1.0 is what native writes for solid
                if network.glow:
                    emitters[block.original] = network.glow
                print(f"[moonray_hydra] native {block.name} {name}: {len(network.nodes)} nodes, lost {sorted(network.lost)}", flush=True)
    _linear.clear()
    presence_free = translated and not masked
    return lines, emitters


def _source(socket):
    """The node feeding a socket, past reroutes and past nothing else."""
    links = socket.links
    while links and links[0].from_node.type == "REROUTE":
        links = links[0].from_node.inputs[0].links
    return links[0].from_node if links else None


def _upstream(node, seen):
    """Every node feeding this one, at any depth."""
    for socket in node.inputs:
        for link in socket.links:
            if link.from_node not in seen:
                seen.add(link.from_node)
                _upstream(link.from_node, seen)
    return seen


def material_problems(material):
    """Names of what in this material doesn't reach MoonRay. Empty if it all does."""
    output = _output(material)
    source = _source(output.inputs["Surface"]) if output else None
    if source is None:
        return ["no shader on the Surface output"]
    lost = {node.bl_label for node in _upstream(source, set()) if node.type not in MATERIAL_CARRIED}
    if source != next((node for node in material.node_tree.nodes if node.type in MATERIAL_SHADERS), None):
        lost.add(source.bl_label)
    lost.update(f"{name} output" for name in ("Volume", "Displacement") if output.inputs[name].is_linked)
    return sorted(lost)


def world_problems(world):
    """Names of what in this world doesn't reach MoonRay. Hydra takes a Background with a colour or an image, and nothing else."""
    if world is None or not world.node_tree:
        return []
    output = _output(world)
    source = _source(output.inputs["Surface"]) if output else None
    if source is None:
        return ["no shader on the Surface output"]
    lost = {node.bl_label for node in _upstream(source, set()) if node.type not in WORLD_CARRIED}
    if source.type != "BACKGROUND":
        lost.add(source.bl_label)
    elif source.inputs["Strength"].is_linked:
        lost.add("linked Strength")
    return sorted(lost)


def unsupported(depsgraph):
    """Warnings for what this frame's materials and world lose, one each, then the total. Empty if nothing."""
    lines = []
    materials = 0
    for block in depsgraph.ids:
        if isinstance(block, bpy.types.Material) and shaded(block):
            network = native_network(block, _AnyImage())
            lost = sorted(network.lost) if network else material_problems(block)
            if lost:
                materials += 1
                lines.append(f"material {block.name}: not as in Cycles: {', '.join(lost)}" if network else
                             f"material {block.name}: unsupported: {', '.join(lost)}. It renders with default values")
    if lines:
        lines.append(f"{materials} material{'' if materials == 1 else 's'} won't render as in Cycles")
    return lines


def trim_cache(used, limit=CACHE_LIMIT):
    """Oldest first, down to the limit. A file another process has open fails to delete and stays."""
    now = time.time()
    for path in used:
        try:
            os.utime(path)
        except OSError:  # gone or refused, because of another Blender. A cache must not stop a frame
            pass
    files = []
    for entry in os.scandir(CACHE):
        try:
            if entry.is_file():
                stat = entry.stat()
                files.append((stat.st_mtime, stat.st_size, entry.path))
        except OSError:  # another Blender removed it after the listing
            pass
    files.sort()
    total = sum(size for _, size, _ in files)
    for modified, size, path in files:
        if total <= limit or now - modified < CACHE_RECENT:
            break
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass


def bake_world(depsgraph, cancelled, threads):
    """The EXR of a world Hydra can't carry, or "", and a list of problems."""
    lost = world_problems(depsgraph.scene.world)
    if not lost:
        return "", []
    os.makedirs(CACHE, exist_ok=True)
    return lights.bake_world(depsgraph, cancelled, threads, lost, CACHE)


def prepare(depsgraph, cancelled):
    """Convert what this frame uses. Returns the manifest's path, the MeshLight file's or "", and a list of problems for the user."""
    global _manifests, emitters
    entries, problems, tiled = {}, [], {}
    os.makedirs(CACHE, exist_ok=True)
    for image, premultiply in sorted(_used_images(depsgraph).items(), key=lambda item: item[0].name):
        if cancelled():
            break
        try:
            if image.source not in {"FILE", "GENERATED"}:
                raise OSError(f"{image.source.lower()} images aren't supported")
            exists = os.path.exists(_disk_path(image))
            if image.source == "GENERATED" or image.packed_file or image.is_dirty or (exists and image.is_float):
                target = convert_pixels(image, premultiply)
                names = _memory_names(image)
                clashes = names & entries.keys()
                if clashes and any(entries[name] != target for name in clashes):
                    problems.append(f"{image.name}: another packed or generated image has the same name, one of them will be wrong")
                entries.update((name, target) for name in names)
                tiled[image] = target
            elif exists:
                entries[_disk_path(image)] = tiled[image] = convert(_disk_path(image), premultiply)
            else:
                raise OSError(f"file not found, {_disk_path(image)}")
        except (OSError, RuntimeError) as error:
            problems.append(f"{image.name}: can't be used, it won't render as in Cycles: {error}")
    for old in (f"manifest_{os.getpid()}_{_manifests}.txt", f"glow_{os.getpid()}_{_manifests}.bin"):
        try:
            os.remove(os.path.join(CACHE, old))
        except OSError:  # none yet, or held: trim_cache gets it later
            pass
    _manifests += 1
    manifest = os.path.join(CACHE, f"manifest_{os.getpid()}_{_manifests}.txt")
    native_lines, emitters = _native_lines(depsgraph, tiled)
    with open(manifest, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(f"{path}\t{target}\n" for path, target in entries.items())
        f.writelines(_surface_lines(depsgraph))
        f.writelines(native_lines)
    glows = os.path.join(CACHE, f"glow_{os.getpid()}_{_manifests}.bin") if emitters else ""
    if emitters:
        lights.write_glows(depsgraph, emitters, glows)
    trim_cache(set(entries.values()) | _bumps | {manifest} | ({glows} if glows else set()))
    _bumps.clear()
    return manifest, glows, problems
