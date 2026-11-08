# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Device GPU's scene inputs: Export writes what only native.py can translate (materials, textures, lights, the world) as pbrt
blocks for the hdNannaGpu delegate (gpu_hydra.write_inputs), and read_exr reads the delegate's albedo and normal for Denoise.

The scene is written as gpu/pbrt/export_pbrt.py writes it: each material through native.translate as Material "nanna", its maps
from the add-on's .tx copies, Blender's lights, the world as an equal-area EXR.
"""
import math
import os

import bpy
import mathutils
import numpy as np

from . import native
from . import textures as addon_textures


def rgb(color):
    return "[ {:.6f} {:.6f} {:.6f} ]".format(*color[:3])


# NannaMaterial's parameter slots, the same order as nanna_material.inc. Colours take 3 values, floats the first
SLOTS = ["albedo", "metallic_color", "metallic_edge_color", "transmission_color", "clearcoat_attenuation_color",
         "specular", "roughness", "diffuse_roughness", "refractive_index", "metallic", "transmission", "clearcoat",
         "clearcoat_roughness", "clearcoat_refractive_index", "clearcoat_thickness", "input_normal", "clearcoat_bending"]
IGNORED = {"show_clearcoat", "casts_caustics", "presence"}  # presence: reported apart when it isn't 1
OPS = ["add", "subtract", "multiply", "divide", "minimum", "maximum", "power", "abs", "sine", "cosine", "floor", "ceil", "fraction",
       "round", "less_than", "greater_than", "modulo", "acos", "dot", "screen", "overlay"]
TEX_LARGEST = 2048  # the widest mip level kept as the GPU texture's top


class Program:
    """A material's map graph as NannaMaterial's instruction list: one register per map, a Col each, written in dependency order."""

    def __init__(self, export, network, label):
        self.x, self.n, self.label = export, network, label
        self.code, self.regs, self.tex = [], {}, []
        self.kinds = dict(network.nodes)

    def attr(self, node, name):
        return next((a for a in self.n.attrs if a[0] == node and a[1] == name), None)

    def text(self, node, name, default):
        a = self.attr(node, name)
        return a[3] if a else default

    def source(self, node, name):
        return next((s for s, t, a in self.n.binds if t == node and a == name), None)

    def floats(self, node, name, default):
        a = self.attr(node, name)
        return [float(v) for v in a[3].split()] if a else default

    def const(self, values):
        reg = len(self.regs)
        self.regs[("const", reg)] = reg
        v = values if len(values) == 3 else [values[0]] * 3
        self.code += [0, reg, *v]
        return reg

    def operand(self, node, name, default):
        src = self.source(node, name)
        return self.map(src) if src else self.const(self.floats(node, name, default))

    def texture(self, node, name, decode):
        tex = self.x.tx_to_exr(self.attr(node, name)[3], decode)
        if tex not in self.tex:
            self.tex.append(tex)
        return self.tex.index(tex)

    def placement(self, node):
        s, o = self.floats(node, "scale", [1, 1]), self.floats(node, "offset", [0, 0])
        r, c = self.floats(node, "rotation_angle", [0])[0], self.floats(node, "rotation_center", [0, 0])
        if not int(float(self.text(node, "wrap_around", "1"))):
            self.x.unmapped.add(f"{self.label}: {node} not wrapped")
        return [*s[:2], *o[:2], r, *c[:2]]

    def map(self, node):
        if node in self.regs:
            return self.regs[node]
        kind, lost = self.kinds[node], self.x.unmapped
        if kind == "ImageMap":
            if self.attr(node, "alpha_only"):
                lost.add(f"{self.label}: ImageMap alpha_only")
            ins = [1, None, self.texture(node, "texture", self.text(node, "gamma", "on") == "on"), *self.placement(node)]
        elif kind == "ImageNormalMap":
            ins = [2, None, self.texture(node, "tangent_space_normal_texture", False), *self.placement(node)]
        elif kind == "RgbToFloatMap":
            mode = self.text(node, "mode", "luminance")
            ins = [3, None, self.operand(node, "input", [0, 0, 0]), {"luminance": 0, "r": 1, "g": 2, "b": 3}.get(mode, 0)]
        elif kind == "OpMap":
            op = self.text(node, "operation", "add")
            if op not in OPS:
                lost.add(f"{self.label}: OpMap {op}")
            ins = [4, None, self.operand(node, "op1", [0, 0, 0]), self.operand(node, "op2", [1, 1, 1]), OPS.index(op) if op in OPS else 0,
                   float(self.text(node, "clamp", "0"))]
        elif kind == "BlendMap":
            ins = [5, None, self.operand(node, "blend_amount", [0.5]), self.operand(node, "color_A", [0, 0, 0]),
                   self.operand(node, "color_B", [1, 1, 1])]
        elif kind == "RampMap":
            if self.text(node, "ramp_type", "input") != "input":
                lost.add(f"{self.label}: RampMap type")
            pos = self.floats(node, "positions", [0, 1])
            cols = self.floats(node, "colors", [0, 0, 0, 1, 1, 1])
            interp = int(self.floats(node, "interpolations", [1])[0])
            if interp not in (0, 1):
                lost.add(f"{self.label}: RampMap interpolation {interp} as linear")
            ins = [6, None, self.operand(node, "input", [0]), len(pos), interp, *pos, *cols]
        elif kind == "DirectionalMap":
            if self.text(node, "polarity", "") != "parallel" or self.text(node, "clamping_behavior", "") != "absolute":
                lost.add(f"{self.label}: DirectionalMap mode")
            ins = [7, None]
        elif kind == "FloatToRgbMap":
            ins = [8, None, *(self.operand(node, c, [0]) for c in "RGB")]
        elif kind == "ConstantColorMap":
            ins = [0, None, *self.floats(node, "color_value", [0, 0, 0])]
        else:
            lost.add(f"{self.label}: map {kind}")
            ins = [0, None, 0.5, 0.5, 0.5]
        reg = len(self.regs)
        self.regs[node] = reg
        ins[1] = reg
        self.code += ins
        return reg

    def side(self, node):
        values, binds = [], []
        for slot in SLOTS:
            a, src = self.attr(node, slot), self.source(node, slot)
            v = [float(x) for x in a[3].split()] if a else [-1e30]  # -1e30: NannaMaterial's default
            values += (v * 3)[:3]
            binds.append(self.map(src) if src else -1)
        for owner, name, kind, text in self.n.attrs:
            if owner == node and name not in SLOTS and name not in IGNORED:
                self.x.unmapped.add(f"{self.label}: {name}={text[:20]}")
            if owner == node and name == "presence" and text != "1.0":
                self.x.unmapped.add(f"{self.label}: presence {text[:20]}")
        for source, target, name in self.n.binds:
            if target == node and name not in SLOTS:
                self.x.unmapped.add(f"{self.label}: {name} bound")
        return values, binds


def write_ply(path, positions, normals, uvs):
    faces = np.zeros(len(positions) // 3, dtype=[("n", "u1"), ("i", "<i4", 3)])
    faces["n"] = 3
    faces["i"] = np.arange(len(positions)).reshape(-1, 3)
    verts = np.empty(len(positions), dtype=[("p", "<f4", 3), ("n", "<f4", 3), ("uv", "<f4", 2)])
    verts["p"], verts["n"], verts["uv"] = positions, normals, uvs
    with open(path, "wb") as f:
        f.write((f"ply\nformat binary_little_endian 1.0\nelement vertex {len(verts)}\nproperty float x\nproperty float y\nproperty float z\n"
                 f"property float nx\nproperty float ny\nproperty float nz\nproperty float u\nproperty float v\nelement face {len(faces)}\n"
                 "property list uchar int vertex_indices\nend_header\n").encode())
        verts.tofile(f)
        faces.tofile(f)


def equal_area_dirs(n):
    """pbrt's EqualAreaSquareToSphere for every pixel centre of an n x n image, rows top down."""
    y, x = np.mgrid[0:n, 0:n]
    u, v = 2 * (x + .5) / n - 1, 2 * (y + .5) / n - 1
    up, vp = np.abs(u), np.abs(v)
    sd = 1 - (up + vp)
    r = 1 - np.abs(sd)
    phi = np.where(r == 0, 1, (vp - up) / np.maximum(r, 1e-12) + 1) * np.pi / 4
    z = np.copysign(1 - r * r, sd)
    cos_phi, sin_phi = np.copysign(np.cos(phi), u), np.copysign(np.sin(phi), v)
    s = r * np.sqrt(np.maximum(0, 2 - r * r))
    return np.stack([cos_phi * s, sin_phi * s, z], -1)


class Export:
    """export_pbrt.py's scene writer, for one F12. out: an empty folder. look: the panel's Look."""

    def __init__(self, depsgraph, out, look, depths, cancelled):
        self.depsgraph, self.scene, self.out, self.look, self.depths, self.cancelled = depsgraph, depsgraph.scene, out, look, depths, cancelled
        self.unmapped, self.textures, self.texture_lines = set(), {}, []
        self.tiled = {}
        os.makedirs(addon_textures.CACHE, exist_ok=True)  # prepare() makes it on the CPU path
        for image, premultiply in addon_textures._used_images(depsgraph).items():  # the add-on's own .tx copies, as prepare() makes them
            try:
                if image.source == "GENERATED" or image.packed_file or image.is_dirty or image.is_float:
                    self.tiled[image] = addon_textures.convert_pixels(image, premultiply)
                else:
                    self.tiled[image] = addon_textures.convert(addon_textures._disk_path(image), premultiply)
            except (OSError, RuntimeError) as error:
                self.unmapped.add(f"image {image.name}: {error}")

    def tx_to_exr(self, path, decode):
        """The .tx, as a linear RGB EXR pbrt reads, from its largest mip level up to TEX_LARGEST wide. Returns the texture name."""
        key = (path, decode)
        if key not in self.textures:
            import OpenImageIO as oiio
            name = f"t{len(self.textures)}"
            level = 0
            while True:
                buf = oiio.ImageBuf(path, 0, level)
                if buf.spec().width <= TEX_LARGEST or buf.has_error:
                    break
                level += 1
            pixels = buf.get_pixels(oiio.FLOAT)
            rgb3 = pixels[..., :3] if pixels.shape[2] >= 3 else np.repeat(pixels[..., :1], 3, 2)
            if decode and not path.endswith("_linear.tx"):  # ImageMap's gamma "on": sRGB to linear
                rgb3 = np.where(rgb3 <= 0.04045, rgb3 / 12.92, ((np.maximum(rgb3, 0) + 0.055) / 1.055) ** 2.4)
            out_buf = oiio.ImageBuf(oiio.ImageSpec(rgb3.shape[1], rgb3.shape[0], 3, oiio.FLOAT))
            out_buf.set_pixels(oiio.ROI(), np.ascontiguousarray(rgb3, dtype=np.float32))
            out_buf.write(os.path.join(self.out, name + ".exr"))
            self.texture_lines.append(f'Texture "{name}" "spectrum" "imagemap" "string filename" "{name}.exr" "string filter" "trilinear"')
            self.textures[key] = name
        return self.textures[key]

    def nanna(self, material, label):
        output = material.node_tree.get_output_node("CYCLES") if material and material.node_tree else None
        if output is None or not output.inputs["Surface"].is_linked:
            return None, None
        network = native.translate(output.inputs["Surface"], self.tiled, None, None, None, True, None, None, self.look)
        if network.lost:
            self.unmapped.add(f"{label} lost {sorted(network.lost)}")
        prog = Program(self, network, label)
        root, kind = network.nodes[-1]
        while kind == "RaySwitchMaterial":  # the camera's material: what Nanna CPU shows first hand
            root = prog.source(root, "camera_ray_material") or prog.source(root, "default_material")
            kind = prog.kinds[root]
        if kind == "DwaBaseMaterial":
            sides, mix = [prog.side(root)], -1
        elif kind == "DwaMixMaterial":
            a, b = prog.source(root, "material0"), prog.source(root, "material1")
            if prog.kinds[a] != "DwaBaseMaterial" or prog.kinds[b] != "DwaBaseMaterial":
                self.unmapped.add(f"{label}: nested mix")
                return None, network.glow
            sides = [prog.side(a), prog.side(b)]
            mix = prog.operand(root, "mix", [0.5])
        else:
            self.unmapped.add(f"{label}: root {kind}")
            return None, network.glow
        if len(prog.regs) > 16 or len(prog.tex) > 4 or len(prog.code) > 256:
            self.unmapped.add(f"{label}: program too large")
            return None, network.glow
        line = 'Material "nanna"'
        for i, (values, binds) in enumerate(sides):
            line += f' "float side{i}" [ {" ".join(f"{v:.7g}" for v in values)} ] "integer bind{i}" [ {" ".join(map(str, binds))} ]'
        line += f' "integer sides" {len(sides)} "integer mix" {mix} "integer regs" {len(prog.regs)} "integer max_diffuse_depth" {self.depths[0]} "integer max_glossy_depth" {self.depths[1]}'
        if prog.code:
            line += f' "float code" [ {" ".join(f"{v:.7g}" for v in prog.code)} ]'
        for i, tex in enumerate(prog.tex):
            line += f' "texture tex{i}" "{tex}"'
        return line, network.glow

    def surface(self, material):
        """A glow is a two-sided area light over the surface, as MoonRay's MeshLight."""
        gray = 'Material "diffuse" "rgb reflectance" [ 0.5 0.5 0.5 ]'
        if material is None or not material.node_tree:
            return gray
        try:
            line, glow = self.nanna(material, material.name)
        except native.Untranslated as error:
            self.unmapped.add(f"{material.name}: untranslated {error}")
            line, glow = None, None
        if line is None:
            self.unmapped.add(f"{material.name}: gray")
            line = gray
        if glow:
            line = f'AreaLightSource "diffuse" "rgb L" {rgb(glow)} "bool twosided" true\n' + line
        return line

    def world_pixels(self, image):
        """The Environment Texture's pixels as RGB float32, rows top down: its file, else Blender's own pixels (packed, generated or
        only in memory). None, with a warning, if it has none: the world is then left out, not the frame."""
        import OpenImageIO as oiio
        if image is None:
            self.unmapped.add("world: the Environment Texture has no image, the world is left out")
            return None
        path = bpy.path.abspath(image.filepath) if image.filepath else ""
        if image.source == "FILE" and not image.packed_file and path and os.path.isfile(path):
            src = oiio.ImageBuf(path)
            pixels = None if src.has_error else src.get_pixels(oiio.FLOAT)
            if pixels is not None and pixels.ndim == 3:
                return np.ascontiguousarray(np.repeat(pixels[..., :1], 3, 2) if pixels.shape[2] < 3 else pixels[..., :3], np.float32)
        width, height = image.size
        channels = image.channels
        if width and height and channels:
            pixels = np.empty(width * height * channels, np.float32)
            image.pixels.foreach_get(pixels)
            pixels = pixels.reshape(height, width, channels)[::-1]  # Blender's first row is the bottom one
            return np.ascontiguousarray(np.repeat(pixels[..., :1], 3, 2) if channels < 3 else pixels[..., :3], np.float32)
        self.unmapped.add(f"world image {image.name}: file not found ({path or 'no path'}), the world is left out")
        return None

    def world_image(self, lights_sun, n=2048):
        """The world as pbrt's equal-area square: Environment Texture (equirect, Cycles' mapping) times Strength, plus each sun as a disc."""
        import OpenImageIO as oiio
        d = equal_area_dirs(n)
        radiance = np.zeros((n, n, 3), np.float32)
        world = self.scene.world
        if world and world.use_nodes:
            out_node = world.node_tree.get_output_node("CYCLES")
            link = out_node.inputs["Surface"].links[0] if out_node and out_node.inputs["Surface"].is_linked else None
            bg = link.from_node if link else None
            if bg is not None and bg.type == "BACKGROUND":
                strength = bg.inputs["Strength"].default_value
                color = bg.inputs["Color"]
                env = color.links[0].from_node if color.is_linked else None
                if env is not None and env.type == "TEX_ENVIRONMENT" and env.projection == "EQUIRECTANGULAR" and not env.inputs["Vector"].is_linked:
                    img = self.world_pixels(env.image)
                    if img is not None:
                        h, w = img.shape[:2]
                        uu = (0.5 - np.arctan2(d[..., 1], d[..., 0]) / (2 * np.pi)) % 1  # Cycles' direction_to_equirectangular
                        vv = 0.5 + np.arcsin(np.clip(d[..., 2], -1, 1)) / np.pi
                        fx, fy = uu * w - .5, (1 - vv) * h - .5
                        x0, y0 = np.floor(fx).astype(int), np.floor(fy).astype(int)
                        tx, ty = (fx - x0)[..., None], (fy - y0)[..., None]
                        px = lambda yy, xx: img[np.clip(yy, 0, h - 1), xx % w]
                        radiance = ((px(y0, x0) * (1 - tx) + px(y0, x0 + 1) * tx) * (1 - ty) + (px(y0 + 1, x0) * (1 - tx) + px(y0 + 1, x0 + 1) * tx) * ty)
                        radiance = (radiance * strength).astype(np.float32)
                elif not color.is_linked:
                    radiance[:] = np.array(color.default_value[:3]) * strength
                else:
                    self.unmapped.add(f"world colour from {env.type if env else '?'}")
            elif bg is not None:
                self.unmapped.add(f"world {bg.type}")
        elif world:
            radiance[:] = np.array(world.color[:3])
        for direction, irradiance, angle in lights_sun:  # a disc of angle across, carrying exactly the sun's irradiance
            half = max(angle / 2, math.radians(0.25))
            inside = d @ np.array(direction) >= math.cos(half)
            pixels = inside.sum()
            if pixels:
                radiance[inside] += np.array(irradiance) / (pixels * 4 * np.pi / (n * n))
        if not radiance.max() > 0:  # a black infinite light would only take light samples from the rest
            return None
        buf = oiio.ImageBuf(oiio.ImageSpec(n, n, 3, oiio.FLOAT))
        buf.set_pixels(oiio.ROI(), np.ascontiguousarray(radiance))
        buf.write(os.path.join(self.out, "world.exr"))
        return 'LightSource "infinite" "string filename" "world.exr"'

    def light_lines(self):
        """Blender's lights in Cycles' units: a point or spot of P watts has intensity P/(4 pi), an area of P watts radiance P/(area pi)."""
        lines, suns = [], []
        for inst in self.depsgraph.object_instances:
            obj = inst.object
            if obj.type != "LIGHT":
                continue
            light, m = obj.data, inst.matrix_world
            color = np.array(light.color[:3]) * light.energy
            pos = m.translation
            aim = (m.to_3x3() @ mathutils.Vector((0, 0, -1))).normalized()
            if light.type == "SUN":
                suns.append((tuple(-aim), tuple(color), light.angle))
            elif light.type in ("POINT", "SPOT"):
                if light.shadow_soft_size > 1e-4:
                    self.unmapped.add(f"light {obj.name}: radius as a point")
                intensity = color / (4 * math.pi)
                if light.type == "POINT":
                    lines.append(f'AttributeBegin\nLightSource "point" "rgb I" {rgb(intensity)} "point3 from" [ {pos.x} {pos.y} {pos.z} ]\nAttributeEnd')
                else:
                    cone = math.degrees(light.spot_size / 2)
                    cos_half = math.cos(light.spot_size / 2)
                    start = math.degrees(math.acos(min(1.0, cos_half + (1 - cos_half) * light.spot_blend)))
                    to = pos + aim
                    lines.append(f'AttributeBegin\nLightSource "spot" "rgb I" {rgb(intensity)} "point3 from" [ {pos.x} {pos.y} {pos.z} ] '
                                 f'"point3 to" [ {to.x} {to.y} {to.z} ] "float coneangle" {cone} "float conedeltaangle" {cone - start}\nAttributeEnd')
            elif light.type == "AREA":
                sx = light.size
                sy = light.size if light.shape in ("SQUARE", "DISK") else light.size_y
                if light.shape in ("DISK", "ELLIPSE"):
                    self.unmapped.add(f"light {obj.name}: {light.shape} as a rectangle")
                if light.spread < math.pi - 1e-3:
                    self.unmapped.add(f"light {obj.name}: spread")
                area = sx * sy * m.to_scale().x * m.to_scale().y
                radiance = color / (area * math.pi)
                corners = [m @ mathutils.Vector((x * sx / 2, y * sy / 2, 0)) for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
                p = " ".join(f"{c.x} {c.y} {c.z}" for c in corners)
                n = " ".join(f"{aim.x} {aim.y} {aim.z}" for _ in corners)
                lines.append(f'AttributeBegin\nAreaLightSource "diffuse" "rgb L" {rgb(radiance)}\nMaterial "diffuse" "rgb reflectance" [ 0 0 0 ]\n'
                             f'Shape "trianglemesh" "integer indices" [ 0 2 1 0 3 2 ] "point3 P" [ {p} ] "normal N" [ {n} ] "bool nanna_hidden" true\nAttributeEnd')
        world = self.world_image(suns)
        return lines + [world] if world else lines

    def pane_f0(self, material):
        """F0 of the glass if Nanna CPU makes this material a flat pane (native.Network.pane under lights.flat_share), else None."""
        from . import lights as addon_lights
        output = material.node_tree.get_output_node("CYCLES") if material and material.node_tree else None
        if output is None or not output.inputs["Surface"].is_linked:
            return None
        node = output.inputs["Surface"].links[0].from_node
        i = node.inputs
        if node.type == "BSDF_GLASS":
            weight = 1.0
        elif node.type == "BSDF_PRINCIPLED" and not i["Metallic"].is_linked and i["Metallic"].default_value == 0 and not i["Alpha"].is_linked \
                and i["Alpha"].default_value == 1 and not i["Transmission Weight"].is_linked:
            weight = i["Transmission Weight"].default_value
        else:
            return None
        if not weight or i["IOR"].is_linked or i["Roughness"].is_linked or i["Roughness"].default_value > native.PANE_ROUGHNESS or i["Normal"].is_linked:
            return None
        if addon_lights.flat_share(material, self.depsgraph) <= native.PANE_SHARE:
            return None
        ior = i["IOR"].default_value
        return ((ior - 1) / (ior + 1)) ** 2

    def meshes(self):
        groups, materials = {}, {}
        for inst in self.depsgraph.object_instances:
            if self.cancelled():
                return None, None
            obj = inst.object
            if obj.type not in ("MESH", "CURVE", "FONT", "SURFACE", "META"):
                continue
            mesh = obj.to_mesh()
            if mesh is None:
                continue
            mesh.calc_loop_triangles()
            count = len(mesh.loop_triangles)
            if count:
                matrix = np.array(inst.matrix_world, dtype=np.float64)
                normal_matrix = np.linalg.inv(matrix[:3, :3]).T
                corners = np.empty(count * 3, dtype=np.int32)
                mesh.loop_triangles.foreach_get("loops", corners)
                slots = np.empty(count, dtype=np.int32)
                mesh.loop_triangles.foreach_get("material_index", slots)
                vertex_of_loop = np.empty(len(mesh.loops), dtype=np.int32)
                mesh.loops.foreach_get("vertex_index", vertex_of_loop)
                co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
                mesh.vertices.foreach_get("co", co)
                no = np.empty(len(mesh.loops) * 3, dtype=np.float32)
                mesh.corner_normals.foreach_get("vector", no)
                uv = np.zeros((len(mesh.loops), 2), dtype=np.float32)
                layer = next((item for item in mesh.uv_layers if item.active_render), None)
                if layer is not None:
                    flat = np.empty(len(mesh.loops) * 2, dtype=np.float32)
                    layer.data.foreach_get("uv", flat)
                    uv = flat.reshape(-1, 2)
                uv = uv[corners]
                p = co.reshape(-1, 3)[vertex_of_loop[corners]].astype(np.float64)
                n = no.reshape(-1, 3)[corners].astype(np.float64)
                p = p @ matrix[:3, :3].T + matrix[:3, 3]
                n = n @ normal_matrix.T
                n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-20)
                if np.linalg.det(matrix[:3, :3]) < 0:  # keep the corner order consistent with the normals
                    p = p.reshape(-1, 3, 3)[:, ::-1].reshape(-1, 3)
                    n = n.reshape(-1, 3, 3)[:, ::-1].reshape(-1, 3)
                    uv = uv.reshape(-1, 3, 2)[:, ::-1].reshape(-1, 2)
                for slot in np.unique(slots):
                    material = obj.material_slots[slot].material if slot < len(obj.material_slots) else None
                    material = material.original if material else None
                    name = material.name if material else "NONE"
                    materials[name] = material
                    keep = np.repeat(slots == slot, 3)
                    groups.setdefault(name, []).append((p[keep].astype(np.float32), n[keep].astype(np.float32), uv[keep]))
            obj.to_mesh_clear()
        return groups, materials

    def camera(self, width, height, use_dof):
        """pbrt's perspective camera. Its fov is on the short side of the frame."""
        camera = self.scene.camera
        cam = camera.data
        if cam.type != "PERSP":
            raise RuntimeError(f"GPU: only a perspective camera renders, this one is {cam.type.lower()}")
        fit = cam.sensor_fit
        if fit == "AUTO":
            fit = "HORIZONTAL" if width >= height else "VERTICAL"
            sensor = cam.sensor_width
        else:
            sensor = cam.sensor_width if fit == "HORIZONTAL" else cam.sensor_height
        fitted = 2 * math.atan(sensor / 2 / cam.lens)  # across the fitted side
        long_fit = (fit == "HORIZONTAL") == (width >= height)
        fov = 2 * math.atan(math.tan(fitted / 2) * min(width, height) / max(width, height)) if long_fit else fitted
        m = camera.matrix_world
        eye, target, up = m.translation, m @ mathutils.Vector((0, 0, -1)), m.col[1].xyz
        lens_radius = cam.lens * 1e-3 / (2 * cam.dof.aperture_fstop) if cam.dof.use_dof and use_dof else 0
        focus = cam.dof.focus_distance
        if cam.dof.use_dof and cam.dof.focus_object:
            focus = (m.inverted() @ cam.dof.focus_object.matrix_world.translation).length
        if abs(cam.shift_x) > 1e-6 or abs(cam.shift_y) > 1e-6:
            self.unmapped.add("camera shift")
        return ["Scale -1 1 1",
                f"LookAt {eye.x:.6f} {eye.y:.6f} {eye.z:.6f}  {target.x:.6f} {target.y:.6f} {target.z:.6f}  {up.x:.6f} {up.y:.6f} {up.z:.6f}",
                f'Camera "perspective" "float fov" {math.degrees(fov):.6f} "float lensradius" {lens_radius:.6f} "float focaldistance" {max(focus, 1e-3):.6f}']

    def write(self, width, height, spp, maxdepth, gbuffer):
        """scene.pbrt in out. Returns False if cancelled."""
        groups, materials = self.meshes()
        if groups is None:
            return False
        film = "gbuffer" if gbuffer else "rgb"  # gbuffer: colour, albedo and normal (patch_f12.py made it Nanna's RGB)
        head = self.camera(width, height, self.scene.moonray_hydra.enable_dof) + [
            f'Film "{film}" "integer xresolution" {width} "integer yresolution" {height} "string filename" "out.exr"',
            'PixelFilter "gaussian" "float xradius" 1.5 "float yradius" 1.5',
            f'Sampler "zsobol" "integer pixelsamples" {spp}',
            f'Integrator "volpath" "integer maxdepth" {maxdepth}',
            "WorldBegin"]
        body = []
        for index, (name, parts) in enumerate(sorted(groups.items())):
            if self.cancelled():
                return False
            positions = np.concatenate([a for a, _, _ in parts])
            normals = np.concatenate([b for _, b, _ in parts])
            uvs = np.concatenate([c for _, _, c in parts])
            ply = f"m{index:02d}.ply"
            write_ply(os.path.join(self.out, ply), positions, normals, uvs)
            f0 = self.pane_f0(bpy.data.materials.get(name))
            if f0 is not None:  # Nanna CPU's flat pane: a mirror of presence Fresnel, shadow rays pass
                body += ["AttributeBegin", 'Material "conductor" "rgb reflectance" [ 1 1 1 ] "float roughness" 0',
                         f'Shape "plymesh" "string filename" "{ply}" "float alpha" {f0:.6f}', "AttributeEnd"]
                continue
            body += ["AttributeBegin", self.surface(materials[name]), f'Shape "plymesh" "string filename" "{ply}"', "AttributeEnd"]
        if self.scene.world is not None or any(o.type == "LIGHT" for o in self.depsgraph.objects):
            body = self.light_lines() + body
        with open(os.path.join(self.out, "scene.pbrt"), "w", encoding="utf-8") as f:
            f.write("\n".join(head + self.texture_lines + body) + "\n")
        return True



def read_exr(path):
    """Colour, albedo, normal as float32 rows top down, the last two None if the file hasn't them."""
    import OpenImageIO as oiio
    buf = oiio.ImageBuf(path)
    if buf.has_error:
        raise RuntimeError(f"GPU: can't read pbrt's picture: {buf.geterror()}")
    names = list(buf.spec().channelnames)
    pixels = buf.get_pixels(oiio.FLOAT)

    def take(channels):
        if all(c in names for c in channels):
            return np.ascontiguousarray(pixels[..., [names.index(c) for c in channels]], np.float32)
        return None
    normal = take(("Ns.X", "Ns.Y", "Ns.Z"))  # Ns: the shading normal in pbrt's camera space, +z away from the viewer
    if normal is not None:
        normal[..., 2] *= -1  # the CPU path's Denoising Normal: +z toward the viewer (measured, x and y agree)
    return take(("R", "G", "B")), take(("Albedo.R", "Albedo.G", "Albedo.B")), normal
