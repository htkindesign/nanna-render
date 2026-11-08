# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Export the evaluated Blender scene to a pbrt-v4 scene for Nanna GPU. Never saves the .blend.
Usage: blender --background <blend> --python export_pbrt.py -- <out dir> <width> [lens radius override]
Every material goes through the add-on's translator (native.translate, Look NANNA): DwaBase, or a DwaMix of two, as Material "nanna",
its maps as an instruction list (nanna_material.inc), images from the add-on's .tx files (textures.py) as linear EXRs.
A material's glow is a two-sided area light, as MoonRay's MeshLight. Blender lights: sun baked into the world as a 2 degree disc (MoonRay's
sun has an angle, pbrt's distant light has none), area as a one-sided quad light, point and spot as pbrt's. World: an Environment Texture
times Strength, or a colour, as pbrt's equal-area infinite light. Anything not carried is on the EXPORT UNMAPPED line."""
import math
import os
import sys

import bpy
import mathutils
import numpy as np

sys.dont_write_bytecode = True  # the add-on is only read
HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_PARENT = os.environ.get("NANNA_ADDON_PARENT") or next(  # the folder holding addon/: the repo's root, else the development layout's
    p for p in (os.path.join(HERE, "..", ".."), os.path.join(HERE, "..", "..", "..", "hdbuild")) if os.path.isfile(os.path.join(p, "addon", "native.py")))
sys.path.insert(0, os.path.abspath(ADDON_PARENT))
import addon.native as native  # MoonRay's translator, what Nanna CPU renders
import addon.textures as addon_textures  # the .tx copies MoonRay samples

args = sys.argv[sys.argv.index("--") + 1:]
out, width = args[0], int(args[1])
os.makedirs(out, exist_ok=True)
scene = bpy.context.scene
for obj in bpy.data.objects:  # render levels, in memory only. The depsgraph here is the viewport's, and MoonRay and Cycles get the render's
    for mod in obj.modifiers:
        if mod.type == "SUBSURF":
            mod.levels = mod.render_levels
        elif mod.type == "MULTIRES":
            mod.levels = mod.render_levels
        mod.show_viewport = mod.show_render
depsgraph = bpy.context.evaluated_depsgraph_get()
unmapped = set()

# The add-on's own .tx copies, as prepare() makes them: image -> path
tiled = {}
for image, premultiply in addon_textures._used_images(depsgraph).items():
    try:
        if image.source == "GENERATED" or image.packed_file or image.is_dirty or image.is_float:
            tiled[image] = addon_textures.convert_pixels(image, premultiply)
        else:
            tiled[image] = addon_textures.convert(addon_textures._disk_path(image), premultiply)
    except (OSError, RuntimeError) as error:
        unmapped.add(f"image {image.name}: {error}")


def rgb(color):
    return "[ {:.6f} {:.6f} {:.6f} ]".format(*color[:3])


# NannaMaterial's parameter slots, the same order as nanna_material.inc. Colours take 3 values, floats the first
SLOTS = ["albedo", "metallic_color", "metallic_edge_color", "transmission_color", "clearcoat_attenuation_color",
         "specular", "roughness", "diffuse_roughness", "refractive_index", "metallic", "transmission", "clearcoat",
         "clearcoat_roughness", "clearcoat_refractive_index", "clearcoat_thickness", "input_normal", "clearcoat_bending"]
IGNORED = {"show_clearcoat", "casts_caustics", "presence"}  # presence: reported apart when it isn't 1
OPS = ["add", "subtract", "multiply", "divide", "minimum", "maximum", "power", "abs", "sine", "cosine", "floor", "ceil", "fraction",
       "round", "less_than", "greater_than", "modulo", "acos", "dot", "screen", "overlay"]
textures = {}  # (tx path, decode sRGB) -> pbrt texture name
texture_lines = []
DEPTHS = [int(v) for v in os.environ.get("NANNA_DEPTHS", "4,8").split(",")]  # the add-on's Diffuse and Glossy bounces
TEX_LARGEST = 2048  # the widest mip level kept as the GPU texture's top: the frames here are small


def tx_to_exr(path, decode):
    """The .tx the add-on made, as a linear RGB EXR pbrt reads, from its largest mip level up to TEX_LARGEST wide. Returns the texture name."""
    key = (path, decode)
    if key not in textures:
        import OpenImageIO as oiio
        name = f"t{len(textures)}"
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
        out_buf.write(os.path.join(out, name + ".exr"))
        texture_lines.append(f'Texture "{name}" "spectrum" "imagemap" "string filename" "{name}.exr" "string filter" "trilinear"')
        textures[key] = name
        print(f"EXPORT texture {name} {os.path.basename(path)} level {level} {rgb3.shape[1]}x{rgb3.shape[0]} decode {decode}")
    return textures[key]


class Program:
    """A material's map graph as NannaMaterial's instruction list: one register per map, a Col each, written in dependency order."""

    def __init__(self, network, label):
        self.n, self.label = network, label
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
        """The register holding an input: its map's, or a constant."""
        src = self.source(node, name)
        return self.map(src) if src else self.const(self.floats(node, name, default))

    def texture(self, node, name, decode):
        tex = tx_to_exr(self.attr(node, name)[3], decode)
        if tex not in self.tex:
            self.tex.append(tex)
        return self.tex.index(tex)

    def placement(self, node):
        s, o = self.floats(node, "scale", [1, 1]), self.floats(node, "offset", [0, 0])
        r, c = self.floats(node, "rotation_angle", [0])[0], self.floats(node, "rotation_center", [0, 0])
        if not int(float(self.text(node, "wrap_around", "1"))):
            unmapped.add(f"{self.label}: {node} not wrapped")
        return [*s[:2], *o[:2], r, *c[:2]]

    def map(self, node):
        if node in self.regs:
            return self.regs[node]
        kind = self.kinds[node]
        if kind == "ImageMap":
            if self.attr(node, "alpha_only"):
                unmapped.add(f"{self.label}: ImageMap alpha_only")
            ins = [1, None, self.texture(node, "texture", self.text(node, "gamma", "on") == "on"), *self.placement(node)]
        elif kind == "ImageNormalMap":
            ins = [2, None, self.texture(node, "tangent_space_normal_texture", False), *self.placement(node)]
        elif kind == "RgbToFloatMap":
            mode = self.text(node, "mode", "luminance")
            ins = [3, None, self.operand(node, "input", [0, 0, 0]), {"luminance": 0, "r": 1, "g": 2, "b": 3}.get(mode, 0)]
        elif kind == "OpMap":
            op = self.text(node, "operation", "add")
            if op not in OPS:
                unmapped.add(f"{self.label}: OpMap {op}")
            ins = [4, None, self.operand(node, "op1", [0, 0, 0]), self.operand(node, "op2", [1, 1, 1]), OPS.index(op) if op in OPS else 0,
                   float(self.text(node, "clamp", "0"))]
        elif kind == "BlendMap":
            ins = [5, None, self.operand(node, "blend_amount", [0.5]), self.operand(node, "color_A", [0, 0, 0]),
                   self.operand(node, "color_B", [1, 1, 1])]
        elif kind == "RampMap":
            if self.text(node, "ramp_type", "input") != "input":
                unmapped.add(f"{self.label}: RampMap type")
            pos = self.floats(node, "positions", [0, 1])
            cols = self.floats(node, "colors", [0, 0, 0, 1, 1, 1])
            interp = int(self.floats(node, "interpolations", [1])[0])
            if interp not in (0, 1):
                unmapped.add(f"{self.label}: RampMap interpolation {interp} as linear")
            ins = [6, None, self.operand(node, "input", [0]), len(pos), interp, *pos, *cols]
        elif kind == "DirectionalMap":
            if self.text(node, "polarity", "") != "parallel" or self.text(node, "clamping_behavior", "") != "absolute":
                unmapped.add(f"{self.label}: DirectionalMap mode")
            ins = [7, None]
        elif kind == "FloatToRgbMap":
            ins = [8, None, *(self.operand(node, c, [0]) for c in "RGB")]
        elif kind == "ConstantColorMap":
            ins = [0, None, *self.floats(node, "color_value", [0, 0, 0])]
        else:
            unmapped.add(f"{self.label}: map {kind}")
            ins = [0, None, 0.5, 0.5, 0.5]
        reg = len(self.regs)
        self.regs[node] = reg
        ins[1] = reg
        self.code += ins
        return reg

    def side(self, node):
        """A DwaBase's slots: values and the register bound to each, -1 for none."""
        values, binds = [], []
        for slot in SLOTS:
            a, src = self.attr(node, slot), self.source(node, slot)
            v = [float(x) for x in a[3].split()] if a else [-1e30]  # -1e30: NannaMaterial's default
            values += (v * 3)[:3]
            binds.append(self.map(src) if src else -1)
        for owner, name, kind, text in self.n.attrs:
            if owner == node and name not in SLOTS and name not in IGNORED:
                unmapped.add(f"{self.label}: {name}={text[:20]}")
            if owner == node and name == "presence" and text != "1.0":
                unmapped.add(f"{self.label}: presence {text[:20]}")
        for source, target, name in self.n.binds:
            if target == node and name not in SLOTS:
                unmapped.add(f"{self.label}: {name} bound")
        return values, binds


def nanna(material, label):
    """The Material line for what the translator makes of a material, and its glow."""
    output = material.node_tree.get_output_node("CYCLES") if material and material.node_tree else None
    if output is None or not output.inputs["Surface"].is_linked:
        return None, None
    network = native.translate(output.inputs["Surface"], tiled, None, None, None, True, None, None, "NANNA")
    if network.lost:
        unmapped.add(f"{label} lost {sorted(network.lost)}")
    prog = Program(network, label)
    root, kind = network.nodes[-1]
    while kind == "RaySwitchMaterial":  # the camera's material: what Nanna CPU shows first hand
        unmapped.add(f"{label}: RaySwitchMaterial, camera side only")
        root = prog.source(root, "camera_ray_material") or prog.source(root, "default_material")
        kind = prog.kinds[root]
    if kind == "DwaBaseMaterial":
        sides, mix = [prog.side(root)], -1
    elif kind == "DwaMixMaterial":
        a, b = prog.source(root, "material0"), prog.source(root, "material1")
        if prog.kinds[a] != "DwaBaseMaterial" or prog.kinds[b] != "DwaBaseMaterial":
            unmapped.add(f"{label}: nested mix")
            return None, network.glow
        sides = [prog.side(a), prog.side(b)]
        mix = prog.operand(root, "mix", [0.5])
    else:
        unmapped.add(f"{label}: root {kind}")
        return None, network.glow
    if len(prog.regs) > 16 or len(prog.tex) > 4 or len(prog.code) > 256:
        unmapped.add(f"{label}: program too large {len(prog.regs)} regs {len(prog.tex)} textures {len(prog.code)} floats")
        return None, network.glow
    line = 'Material "nanna"'
    for i, (values, binds) in enumerate(sides):
        line += f' "float side{i}" [ {" ".join(f"{v:.7g}" for v in values)} ] "integer bind{i}" [ {" ".join(map(str, binds))} ]'
    line += f' "integer sides" {len(sides)} "integer mix" {mix} "integer regs" {len(prog.regs)} "integer max_diffuse_depth" {DEPTHS[0]} "integer max_glossy_depth" {DEPTHS[1]}'
    if prog.code:
        line += f' "float code" [ {" ".join(f"{v:.7g}" for v in prog.code)} ]'
    for i, tex in enumerate(prog.tex):
        line += f' "texture tex{i}" "{tex}"'
    return line, network.glow


def surface(material):
    """pbrt lines for a Blender material. A glow is a two-sided area light over the surface, as MoonRay's MeshLight."""
    gray = 'Material "diffuse" "rgb reflectance" [ 0.5 0.5 0.5 ]'
    if material is None or not material.node_tree:
        return gray
    try:
        line, glow = nanna(material, material.name)
    except native.Untranslated as error:
        unmapped.add(f"{material.name}: untranslated {error}")
        line, glow = None, None
    if line is None:
        unmapped.add(f"{material.name}: gray")
        line = gray
    if glow:
        line = f'AreaLightSource "diffuse" "rgb L" {rgb(glow)} "bool twosided" true\n' + line
    return line


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


def world_image(lights_sun, n=2048):
    """The world as pbrt's equal-area square: Environment Texture (equirect, Cycles' mapping) times Strength, plus each sun as a disc."""
    import OpenImageIO as oiio
    d = equal_area_dirs(n)
    radiance = np.zeros((n, n, 3), np.float32)
    world = scene.world
    if world and world.use_nodes:
        out_node = world.node_tree.get_output_node("CYCLES")
        link = out_node.inputs["Surface"].links[0] if out_node and out_node.inputs["Surface"].is_linked else None
        bg = link.from_node if link else None
        if bg is not None and bg.type == "BACKGROUND":
            strength = bg.inputs["Strength"].default_value
            color = bg.inputs["Color"]
            env = color.links[0].from_node if color.is_linked else None
            if env is not None and env.type == "TEX_ENVIRONMENT" and env.projection == "EQUIRECTANGULAR" and not env.inputs["Vector"].is_linked:
                src = oiio.ImageBuf(bpy.path.abspath(env.image.filepath))
                img = src.get_pixels(oiio.FLOAT)[..., :3]
                h, w = img.shape[:2]
                uu = (0.5 - np.arctan2(d[..., 1], d[..., 0]) / (2 * np.pi)) % 1  # Cycles' direction_to_equirectangular
                vv = 0.5 + np.arcsin(np.clip(d[..., 2], -1, 1)) / np.pi
                fx, fy = uu * w - .5, (1 - vv) * h - .5  # bilinear, rows top down
                x0, y0 = np.floor(fx).astype(int), np.floor(fy).astype(int)
                tx, ty = (fx - x0)[..., None], (fy - y0)[..., None]
                px = lambda yy, xx: img[np.clip(yy, 0, h - 1), xx % w]
                radiance = ((px(y0, x0) * (1 - tx) + px(y0, x0 + 1) * tx) * (1 - ty) + (px(y0 + 1, x0) * (1 - tx) + px(y0 + 1, x0 + 1) * tx) * ty)
                radiance = (radiance * strength).astype(np.float32)
            elif not color.is_linked:
                radiance[:] = np.array(color.default_value[:3]) * strength
            else:
                unmapped.add(f"world colour from {env.type if env else '?'}")
        elif bg is not None:
            unmapped.add(f"world {bg.type}")
    for direction, irradiance, angle in lights_sun:  # a disc of angle across, its radiance the irradiance over its solid angle
        half = max(angle / 2, math.radians(0.25))  # at least a few pixels at this size
        inside = d @ np.array(direction) >= math.cos(half)
        solid = 2 * np.pi * (1 - math.cos(half))
        pixels = inside.sum()
        if pixels:  # the drawn disc carries exactly the sun's irradiance
            radiance[inside] += np.array(irradiance) / (pixels * 4 * np.pi / (n * n))
        print(f"EXPORT sun disc {pixels} pixels, solid angle {solid:.3e}")
    buf = oiio.ImageBuf(oiio.ImageSpec(n, n, 3, oiio.FLOAT))
    buf.set_pixels(oiio.ROI(), np.ascontiguousarray(radiance))
    buf.write(os.path.join(out, "world.exr"))
    return 'LightSource "infinite" "string filename" "world.exr"'


def light_lines():
    """Blender's lights in Cycles' units: a point or spot of P watts has intensity P/(4 pi), an area of P watts radiance P/(area pi)."""
    lines, suns = [], []
    for inst in depsgraph.object_instances:
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
                unmapped.add(f"light {obj.name}: radius {light.shadow_soft_size} as a point")
            intensity = color / (4 * math.pi)
            if light.type == "POINT":
                lines.append(f'AttributeBegin\nLightSource "point" "rgb I" {rgb(intensity)} "point3 from" [ {pos.x} {pos.y} {pos.z} ]\nAttributeEnd')
            else:
                cone = math.degrees(light.spot_size / 2)
                cos_half = math.cos(light.spot_size / 2)
                start = math.degrees(math.acos(min(1.0, cos_half + (1 - cos_half) * light.spot_blend)))  # Cycles' smoothstep
                to = pos + aim
                lines.append(f'AttributeBegin\nLightSource "spot" "rgb I" {rgb(intensity)} "point3 from" [ {pos.x} {pos.y} {pos.z} ] '
                             f'"point3 to" [ {to.x} {to.y} {to.z} ] "float coneangle" {cone} "float conedeltaangle" {cone - start}\nAttributeEnd')
        elif light.type == "AREA":
            sx = light.size
            sy = light.size if light.shape in ("SQUARE", "DISK") else light.size_y
            if light.shape in ("DISK", "ELLIPSE"):
                unmapped.add(f"light {obj.name}: {light.shape} as a rectangle")
            if light.spread < math.pi - 1e-3:
                unmapped.add(f"light {obj.name}: spread")
            area = sx * sy * m.to_scale().x * m.to_scale().y
            radiance = color / (area * math.pi)
            corners = [m @ mathutils.Vector((x * sx / 2, y * sy / 2, 0)) for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
            p = " ".join(f"{c.x} {c.y} {c.z}" for c in corners)
            n = " ".join(f"{aim.x} {aim.y} {aim.z}" for _ in corners)
            lines.append(f'AttributeBegin\nAreaLightSource "diffuse" "rgb L" {rgb(radiance)}\nMaterial "diffuse" "rgb reflectance" [ 0 0 0 ]\n'
                         f'Shape "trianglemesh" "integer indices" [ 0 2 1 0 3 2 ] "point3 P" [ {p} ] "normal N" [ {n} ] "bool nanna_hidden" true\nAttributeEnd')  # MoonRay's lights: unseen by the camera, block nothing
        print(f"EXPORT light {obj.name} {light.type} {light.energy}")
    lines.append(world_image(suns))
    return lines


groups = {}  # material name -> lists of world-space corners
materials = {}
for inst in depsgraph.object_instances:
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

camera = scene.camera
render = scene.render
height = round(render.resolution_y * width / render.resolution_x)
cam = camera.data
fov_x = 2 * math.atan(cam.sensor_width / 2 / cam.lens)
fov = fov_x if width <= height else 2 * math.atan(math.tan(fov_x / 2) * height / width)  # pbrt's fov is on the short side
m = camera.matrix_world
eye, target, up = m.translation, m @ mathutils.Vector((0, 0, -1)), m.col[1].xyz
lens_radius = cam.lens * 1e-3 / (2 * cam.dof.aperture_fstop) if cam.dof.use_dof else 0
if len(args) > 2:
    lens_radius = float(args[2])
scene_lines = [
    "# Exported from Blender by export_pbrt.py",
    "Scale -1 1 1",
    f"LookAt {eye.x:.6f} {eye.y:.6f} {eye.z:.6f}  {target.x:.6f} {target.y:.6f} {target.z:.6f}  {up.x:.6f} {up.y:.6f} {up.z:.6f}",
    f'Camera "perspective" "float fov" {math.degrees(fov):.6f} "float lensradius" {lens_radius:.6f} "float focaldistance" {cam.dof.focus_distance:.6f}',
    f'Film "rgb" "integer xresolution" {width} "integer yresolution" {height} "string filename" "out.exr"',
    'PixelFilter "gaussian" "float xradius" 1.5 "float yradius" 1.5',
    'Sampler "zsobol" "integer pixelsamples" 16',
    'Integrator "volpath" "integer maxdepth" 12',  # the add-on's Total, what Nanna CPU renders with
    "WorldBegin",
]
def pane_f0(material):
    """F0 of the glass if Nanna CPU makes this material a flat pane (native.Network.pane under lights.flat_share), else None.
    MoonRay's pane: a mirror lobe with presence Fresnel, and shadow and diffuse rays pass it, so the sun reaches the room through it."""
    import addon.lights as addon_lights
    output = material.node_tree.get_output_node("CYCLES") if material and material.node_tree else None
    if output is None or not output.inputs["Surface"].is_linked:
        return None
    node, i = output.inputs["Surface"].links[0].from_node, output.inputs["Surface"].links[0].from_node.inputs
    if node.type == "BSDF_GLASS":
        weight = 1.0
    elif node.type == "BSDF_PRINCIPLED" and not i["Metallic"].is_linked and i["Metallic"].default_value == 0 and not i["Alpha"].is_linked \
            and i["Alpha"].default_value == 1 and not i["Transmission Weight"].is_linked:
        weight = i["Transmission Weight"].default_value
    else:
        return None
    if not weight or i["IOR"].is_linked or i["Roughness"].is_linked or i["Roughness"].default_value > native.PANE_ROUGHNESS or i["Normal"].is_linked:
        return None
    if addon_lights.flat_share(material, depsgraph) <= native.PANE_SHARE:
        return None
    ior = i["IOR"].default_value
    return ((ior - 1) / (ior + 1)) ** 2


body = []
total = 0
for index, (name, parts) in enumerate(sorted(groups.items())):
    positions = np.concatenate([a for a, _, _ in parts])
    normals = np.concatenate([b for _, b, _ in parts])
    uvs = np.concatenate([c for _, _, c in parts])
    total += len(positions) // 3
    ply = f"m{index:02d}.ply"
    write_ply(os.path.join(out, ply), positions, normals, uvs)
    f0 = pane_f0(bpy.data.materials.get(name))
    if f0 is not None:  # Nanna CPU's flat pane: a mirror of presence Fresnel, shadow rays pass. Here a constant alpha of F0 on a mirror
        print(f"EXPORT pane {name}: alpha {f0:.4f}")
        body += ["AttributeBegin", f"# {name}: {len(positions) // 3} triangles, flat pane", 'Material "conductor" "rgb reflectance" [ 1 1 1 ] "float roughness" 0',
                 f'Shape "plymesh" "string filename" "{ply}" "float alpha" {f0:.6f}', "AttributeEnd"]
        continue
    body += ["AttributeBegin", f"# {name}: {len(positions) // 3} triangles", surface(materials[name]),
             f'Shape "plymesh" "string filename" "{ply}"', "AttributeEnd"]
if scene.world is not None and any(o.type == "LIGHT" for o in scene.objects) or "--lights" in args:
    body = light_lines() + body
scene_lines += texture_lines + body
with open(os.path.join(out, "scene.pbrt"), "w", encoding="utf-8") as f:
    f.write("\n".join(scene_lines) + "\n")
print(f"EXPORT {total} triangles, {len(groups)} materials, {len(textures)} textures, lens radius {lens_radius}, fov {math.degrees(fov):.3f}, {width}x{height}")
print("EXPORT UNMAPPED", sorted(unmapped))
