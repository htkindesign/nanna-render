# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""MoonRay's own shaders for a Blender material, written into the texture manifest because Hydra can't carry them.

Blender's Hydra export reduces a material to one UsdPreviewSurface network. This walks the material's node tree and writes the network
MoonRay should use instead, keyed by the name Blender's export gives the material. nativeNetwork() in Material.cc swaps it in. Lines:
    native<TAB>M_<pointer><TAB>node<TAB>id<TAB>class        the last node is the material
    native<TAB>M_<pointer><TAB>attr<TAB>id<TAB>name<TAB>type<TAB>value    type: f float, b bool, s string, c "r g b", v "x y"
    native<TAB>M_<pointer><TAB>bind<TAB>from id<TAB>to id<TAB>attribute
    type also F "x y ...", C "r g b r g b ...", I "n n ...": a list
Translated shaders: Principled, Glass, Diffuse, Glossy, Translucent, Transparent, Emission, Mix Shader, Add Shader of a shader and an Emission or of
a Diffuse and a Glossy. Maps: Image Texture with Mapping, Math, Mix,
Vector Math, Invert, Gamma, Brightness/Contrast, Hue/Saturation/Value, Separate and Combine Color, Color Ramp, RGB to BW, Fresnel, Layer Weight,
Noise and Voronoi (MoonRay's own pattern, so named in the warning), Normal Map, Bump. A link from anything else falls back to the socket's value
and is named in the warning. A shader that isn't translated leaves the material on the Hydra network.

Bump: MoonRay has no height-to-normal map. A height made of images is worked out in Python, height_field() below, and arrives as a normal .tx,
with the mesh's own UV scale put in at each shaded point, baked_normal(). A height made of noise depends on the object and not on UV, so noise_bump()
does what Cycles does, with maps: the noise at the point and at two points beside it.
"""
import math

import numpy as np


# OpMap holds all of these channel by channel. Others in Blender's Math node have no OpMap operation.
MATH_OPERATIONS = {"ADD": "add", "SUBTRACT": "subtract", "MULTIPLY": "multiply", "DIVIDE": "divide", "POWER": "power",
                   "MINIMUM": "minimum", "MAXIMUM": "maximum", "ABSOLUTE": "abs", "SINE": "sine", "COSINE": "cosine",
                   "FLOOR": "floor", "CEIL": "ceil", "FRACT": "fraction", "ROUND": "round", "LESS_THAN": "less_than",
                   "GREATER_THAN": "greater_than", "MODULO": "modulo", "ARCCOSINE": "acos"}
# Cycles' Mix is a lerp from A to this operation on A and B, at Fac. Overlay chooses its branch on OpMap's op2 and Blender's on A.
MIX_OPERATIONS = {"MULTIPLY": ("multiply", False), "ADD": ("add", False), "SUBTRACT": ("subtract", False), "DIVIDE": ("divide", False),
                  "DARKEN": ("minimum", False), "LIGHTEN": ("maximum", False), "SCREEN": ("screen", False), "OVERLAY": ("overlay", True)}


SHEEN_SCALE, SHEEN_POWER = 1.0, 6  # fuzz is a far stronger lobe than Cycles' sheen and lifts the whole face, so it goes as (1 - cosine) ** power
EDGE_SLOPE, EDGE_FROM = 1.8, 0.2  # see edge_tint()
COSINES = (0.0, 0.02, 0.06, 0.12, 0.2, 0.3, 0.45, 0.6, 0.8, 1.0)  # RampMap asks for fewer than 20 points but holds at the tenth. Closest where the curves bend
WHITE, BLACK = (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)
PANE_SHARE = 0.6  # of a material's area in its six main face directions, to count as flat panes. On a test interior: panes 0.82 to 1, a decanter 0.07, wine 0.29
PANE_ROUGHNESS = 0.1  # above it a pane blurs what's behind it, and only a real refraction lobe does that
CLEAR_SHADOW = 0.5  # luminance of the Transparent under a Light Path. Above it the glass casts no shadow on the rest, see Network.clear_shadow. Below it, presence
GLOW_LIMIT = 0.01  # luminance. Above it, a constant emission is a MeshLight, see lights.py. Below it, it stays on the surface, where it barely lights anything
BUMP_STEP = 0.1  # of the period of a noise's finest octave: how far apart noise_bump() reads it
BUMP_TILT = (0.31, 0.53, 0.79)  # any direction: a tangent for noise_bump() is at right angles to it and to the normal. dPds is 0 on a mesh with no UV
BUMP_MAX_STRETCH = 16.0  # most that baked_normal() scales a slope, for the UV seams and poles where dPds goes to almost 0
CONDUCTOR_IOR = 4.0  # a Principled non-metal at this IOR or above becomes a conductor, see high_ior(). 0 is off
CONDUCTOR_EDGE = 1.3  # its edge colour, against the colour at normal incidence. Fitted on chrome and Chrome_Tray_Mtl, standard scene rim 0.85 to 0.96 and 1.02. 1.0 leaves the tray at 0.85
CONDUCTOR_DARK = 0.1  # above this luminance Base Color's diffuse matters, and it stays a dielectric
VORONOI_GAIN = 3.2  # NoiseWorleyMap's distance against Cycles'. Fitted on the lab's p_voronoi
RAMP_MODES = {"CONSTANT": 0, "LINEAR": 1, "EASE": 4, "CARDINAL": 5, "B_SPLINE": 6}  # RampMap's interpolations
RAY_ATTRS = {"camera": "camera_ray_material", "glossy": "indirect_glossy_ray_material", "mirror": "indirect_mirror_ray_material",
             "diffuse": "indirect_diffuse_ray_material"}
LIGHT_PATH_RAYS = {"Is Camera Ray": {"camera"}, "Is Shadow Ray": {"shadow"}, "Is Diffuse Ray": {"diffuse"},
                   "Is Glossy Ray": {"glossy", "mirror"}}


class Untranslated(Exception):
    """The root of the material, or a shader under it, has no native translation."""


def fresnel(cosine, eta):
    """Cycles' fresnel_dielectric_cos."""
    g = eta * eta - 1 + cosine * cosine
    if g <= 0:
        return 1.0
    g = math.sqrt(g)
    a = (g - cosine) / (g + cosine)
    b = (cosine * (g + cosine) - 1) / (cosine * (g - cosine) + 1)
    return 0.5 * a * a * (1 + b * b)


def luminance(color):
    return 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]


def plain(value):
    """A socket's default value as a float or a colour tuple."""
    return float(value) if isinstance(value, (int, float)) else tuple(float(item) for item in value[:3])


class Network:
    def __init__(self, tiled, bake=None, flat_share=None, caustics=True, extent=None, pixel=None, look="CYCLES"):
        # The Look setting. NANNA turns off what was fitted to imitate Cycles, and nothing else: 1 clearcoat_bending back on, 2 sheen as plain fuzz, no SHEEN_SCALE and no
        # SHEEN_POWER cosine curve, 3 no EDGE_SLOPE fit in edge_tint(), MoonRay's conductor with its own edge, 4 specular untouched, so Specular IOR Level and Specular Tint
        # are not read. Mix, Fac, Bump, Alpha, glass passing light and units are translation, not fits, and stay in both looks.
        self.nanna = look == "NANNA"
        self.tiled = tiled  # image -> path of its tiled copy
        self.bake = bake  # (Bump node, distance, strength) -> (path, scale): the normal .tx. None for a count, with no pixels read
        self.clear_shadow = False  # True: Light Path's Transparent on see-through glass. The delegate takes it out of the shadow rays of other geometry, Material names
        # go to it in blender:clearShadowMaterials, GeometryMixin::assign() in edit_hdmoonray.py
        self.flat_share = flat_share  # called for the share of this material's area in its six main directions, see lights.flat_share
        self.extent = extent  # called for the size of the texture space of what this material covers, the world length of Generated's 0 to 1
        self.pixel = pixel  # called for the world size of a pixel on what this material covers
        self.caustics = caustics  # Cycles' Refractive Caustics on this scene, which decides what a diffuse ray makes of glass
        self.nodes, self.attrs, self.binds, self.lost = [], [], [], set()
        self.done = {}  # (node, socket name) -> (node id, is colour)
        self.normals = {}  # Bump or Normal Map node -> (normal map id, dial)
        self.shares = {}  # Translucent node -> its constant share in the Mix Shader above it
        self.alpha_done = set()  # Principled nodes whose Alpha a Mix Shader above has taken over
        self.gain = 1.0  # what the colours of a Diffuse and a Glossy are multiplied by: 2 under an Add Shader, whose two sides become a mix at 0.5
        self.beside_alpha = {}  # Translucent node -> the Alpha of the shader mixed with it
        self.tint = None  # what a Volume Absorption leaves of each channel per surface crossed
        self.glow = None  # the radiance of the MeshLight that takes this material's emission, if one does

    def add(self, class_name):
        node = f"{class_name}_{len(self.nodes)}"  # the class is in the id: the same id in a later render must not change class
        self.nodes.append((node, class_name))
        return node

    def set(self, node, name, value):
        """A number, a bool, a string, or a colour or a 2-vector as a tuple."""
        if isinstance(value, bool):
            kind, text = "b", str(int(value))
        elif isinstance(value, str):
            kind, text = "s", value
        elif isinstance(value, tuple):
            kind, text = "c" if len(value) == 3 else "v", " ".join(repr(float(item)) for item in value)
        else:
            kind, text = "f", repr(float(value))
        self.attrs.append((node, name, kind, text))

    def set_list(self, node, name, kind, values):
        self.attrs.append((node, name, kind, " ".join(repr(item) for item in values)))

    def bind(self, source, node, name):
        self.binds.append((source, node, name))

    def put(self, node, name, item):
        """A term: a map's id, or a literal."""
        if isinstance(item, str):
            self.bind(item, node, name)
        else:
            self.set(node, name, item)

    def term(self, socket, want_color=True):
        """A socket as a map's id or a literal: a colour for want_color, otherwise a float, as in wire()."""
        value = self.constant(socket)
        map_output = None
        if value is None:
            map_output = self.output(*self.source(socket))
            if map_output is None:
                value = plain(socket.default_value)  # output() has named it in lost
        if value is not None:
            if want_color:
                return (value, value, value) if isinstance(value, float) else value
            return luminance(value) if isinstance(value, tuple) else value
        node, is_color = map_output
        return self.gray(node) if is_color and not want_color else node

    def op(self, operation, a, b=WHITE, clamp=False):
        node = self.add("OpMap")
        self.set(node, "operation", operation)
        self.set(node, "clamp", clamp)
        self.put(node, "op1", (a, a, a) if isinstance(a, float) else a)
        self.put(node, "op2", (b, b, b) if isinstance(b, float) else b)
        return node

    def blend(self, factor, a, b):
        """The map or literal going from a to b at a float term."""
        node = self.add("BlendMap")
        self.put(node, "blend_amount", factor)
        self.put(node, "color_A", a)
        self.put(node, "color_B", b)
        return node

    def source(self, socket):
        """The (node, output) feeding an input, past reroutes, or None."""
        links = socket.links
        while links and links[0].from_node.type == "REROUTE":
            links = links[0].from_node.inputs[0].links
        return (links[0].from_node, links[0].from_socket) if links else None

    def constant(self, socket):
        """The value at an input as a float or a colour tuple, or None if a map must feed it."""
        feeder = self.source(socket)
        if feeder is None:
            return plain(socket.default_value)
        if feeder[0].type in {"VALUE", "RGB"}:
            return plain(feeder[1].default_value)
        return None

    def output(self, node, socket):
        """The map for a node's output, as (id, is colour), or None if there's no translation."""
        key = (node.as_pointer(), socket.identifier)
        if key not in self.done:
            maker = {"TEX_IMAGE": self.image, "MATH": self.math, "MIX": self.mix, "INVERT": self.invert, "GAMMA": self.gamma,
                     "BRIGHTCONTRAST": self.bright_contrast, "HUE_SAT": self.hue_saturation, "SEPARATE_COLOR": self.separate,
                     "COMBINE_COLOR": self.combine, "VALTORGB": self.ramp, "RGBTOBW": self.bw, "FRESNEL": self.fresnel,
                     "LAYER_WEIGHT": self.layer_weight, "VECT_MATH": self.vector_math, "TEX_NOISE": self.noise,
                     "TEX_VORONOI": self.voronoi}.get(node.type)
            self.done[key] = maker(node, socket) if maker else None
            if self.done[key] is None:
                self.lost.add(node.bl_label)
        return self.done[key]

    def wire(self, target, name, socket, want_color):
        """Set an attribute from a socket, as a literal or a binding. MoonRay reads a bound float attribute from red only, so a
        colour going to a float passes through luminance, as Cycles does."""
        value = self.constant(socket)
        map_output = None
        if value is None:
            map_output = self.output(*self.source(socket))
            if map_output is None:
                value = plain(socket.default_value)  # output() has named it in lost
        if value is not None:
            if want_color and isinstance(value, float):
                value = (value, value, value)
            elif not want_color and isinstance(value, tuple):
                value = luminance(value)
            self.set(target, name, value)
            return
        node, is_color = map_output
        self.bind(self.gray(node) if is_color and not want_color else node, target, name)

    def gained(self, target, name, socket):
        """A colour attribute, times self.gain. DwaBase clamps these at 1, so a side of an Add Shader over 0.5 comes out dimmer."""
        if self.gain == 1.0:
            self.wire(target, name, socket, True)
            return
        value = self.constant(socket)
        if value is None:
            self.bind(self.op("multiply", self.term(socket), self.gain), target, name)
            return
        value = (value, value, value) if isinstance(value, float) else value
        if max(value) * self.gain > 1:
            self.lost.add("Add Shader of colours over 0.5")
        self.set(target, name, tuple(item * self.gain for item in value))

    def gray(self, node):
        luma = self.add("RgbToFloatMap")
        self.set(luma, "mode", "luminance")
        self.bind(node, luma, "input")
        return luma

    def image(self, node, socket):
        image = node.image.original if node.image else None
        if image not in self.tiled or node.projection != "FLAT":
            return None
        map_id = self.add("ImageMap")
        self.place(map_id, node.inputs["Vector"])
        self.set(map_id, "texture", self.tiled[image])
        data = image.is_float or image.colorspace_settings.is_data or image.colorspace_settings.name != "sRGB"
        self.set(map_id, "gamma", "off" if data else "on")
        self.set(map_id, "wrap_around", node.extension == "REPEAT")
        if socket.identifier == "Alpha":
            self.set(map_id, "alpha_only", True)
        return map_id, socket.identifier != "Alpha"

    def uv_mapping(self, socket):
        """(scale, offset, degrees) of the UV feeding a Vector input, in the order ImageMap applies them. Anything else is named and
        sampled at plain UV. ImageMap rotates, then scales, then offsets. A Mapping scales first, so both at once needs a uniform scale."""
        vector = self.source(socket)
        if vector and vector[0].type == "MAPPING":
            mapping = vector[0]
            inputs = mapping.inputs
            inner = self.source(inputs["Vector"])
            rotation, location, scale = (self.constant(inputs[name]) for name in ("Rotation", "Location", "Scale"))
            plain_uv = inner is None or inner[0].type == "UVMAP" or (inner[0].type == "TEX_COORD" and inner[1].identifier == "UV")
            if mapping.vector_type == "POINT" and plain_uv and None not in (rotation, location, scale) and not (rotation[0] or rotation[1]):
                if rotation[2] and abs(scale[0] - scale[1]) > 1e-6:
                    self.lost.add("Mapping with rotation and uneven scale")
                return (scale[0], scale[1]), (location[0], location[1]), math.degrees(rotation[2])
            self.lost.add(mapping.bl_label)
        elif vector and not (vector[0].type == "UVMAP" or (vector[0].type == "TEX_COORD" and vector[1].identifier == "UV")):
            self.lost.add(vector[0].bl_label)
        return (1.0, 1.0), (0.0, 0.0), 0.0

    def place(self, map_id, socket):
        scale, offset, degrees = self.uv_mapping(socket)
        if scale != (1.0, 1.0) or offset != (0.0, 0.0) or degrees:
            self.set(map_id, "scale", scale)
            self.set(map_id, "offset", offset)
            self.set(map_id, "rotation_angle", -degrees)  # ImageMap turns the other way
            self.set(map_id, "rotation_center", (0.0, 0.0))

    def math(self, node, socket):
        a, b = self.term(node.inputs[0]), self.term(node.inputs[1])
        clamp = bool(node.use_clamp)
        name = node.operation
        if name in MATH_OPERATIONS:
            return self.op(MATH_OPERATIONS[name], a, b, clamp), False
        if name == "MULTIPLY_ADD":
            return self.op("add", self.op("multiply", a, b), self.term(node.inputs[2]), clamp), False
        if name == "SQRT":
            return self.op("power", a, 0.5, clamp), False
        if name == "INVERSE_SQRT":
            return self.op("power", a, -0.5, clamp), False
        if name == "EXPONENT":
            return self.op("power", math.e, a, clamp), False
        if name == "RADIANS":
            return self.op("multiply", a, math.pi / 180, clamp), False
        if name == "DEGREES":
            return self.op("multiply", a, 180 / math.pi, clamp), False
        if name == "TANGENT":
            return self.op("divide", self.op("sine", a), self.op("cosine", a), clamp), False
        self.lost.add(f"Math {name}")
        return None

    def vector_math(self, node, socket):
        a, b = self.term(node.inputs[0]), self.term(node.inputs[1])
        name = node.operation
        if name == "DOT_PRODUCT":
            return self.op("dot", a, b), False
        if name == "SCALE":
            return self.op("multiply", a, self.term(node.inputs["Scale"])), True
        if name in {"ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "MINIMUM", "MAXIMUM"}:
            return self.op(name.lower(), a, b), True
        self.lost.add(f"Vector Math {name}")
        return None

    def invert(self, node, socket):
        color = self.term(node.inputs["Color"])
        inverted = self.op("subtract", WHITE, color)
        factor = self.term(node.inputs["Fac"], False)
        return (inverted if factor == 1.0 else self.blend(factor, color, inverted)), True

    def gamma(self, node, socket):
        return self.op("power", self.term(node.inputs["Color"]), self.term(node.inputs["Gamma"])), True

    def bright_contrast(self, node, socket):
        bright, contrast = self.constant(node.inputs["Bright"]), self.constant(node.inputs["Contrast"])
        if bright is None or contrast is None:
            self.lost.add("linked Brightness/Contrast")
            bright, contrast = plain(node.inputs["Bright"].default_value), plain(node.inputs["Contrast"].default_value)
        scaled = self.op("multiply", self.term(node.inputs["Color"]), 1 + contrast)
        return self.op("maximum", self.op("add", scaled, bright - contrast / 2), 0.0), True

    def hue_saturation(self, node, socket):
        i = node.inputs
        color = self.term(i["Color"])
        hsv = self.add("ColorCorrectHsvMap")
        self.put(hsv, "input", color)
        hue = self.constant(i["Hue"])
        if hue is None:
            self.lost.add("linked Hue")
        else:
            self.set(hsv, "hue_shift", (hue - 0.5) * 360)
        self.wire(hsv, "saturation_factor", i["Saturation"], False)
        self.wire(hsv, "value_factor", i["Value"], False)
        factor = self.term(i["Fac"], False)
        return (hsv if factor == 1.0 else self.blend(factor, color, hsv)), True

    def bw(self, node, socket):
        return self.term(node.inputs[0], False), False

    def separate(self, node, socket):
        if node.mode != "RGB":
            self.lost.add(f"Separate Color {node.mode}")
            return None
        channel = self.add("RgbToFloatMap")
        self.set(channel, "mode", {"Red": "r", "Green": "g", "Blue": "b"}[socket.identifier])
        self.put(channel, "input", self.term(node.inputs[0]))
        return channel, False

    def combine(self, node, socket):
        if node.mode != "RGB":
            self.lost.add(f"Combine Color {node.mode}")
            return None
        color = self.add("FloatToRgbMap")
        for name, item in zip("RGB", node.inputs):
            self.wire(color, name, item, False)
        return color, True

    def ramp(self, node, socket):
        ramp = node.color_ramp
        if ramp.color_mode != "RGB" or socket.identifier == "Alpha":
            self.lost.add("Color Ramp in HSV or HSL, or its Alpha")
            return None
        stops = [(item.position, tuple(item.color[:3])) for item in ramp.elements]
        stops = [(0.0, stops[0][1])] * (stops[0][0] > 0) + stops + [(1.0, stops[-1][1])] * (stops[-1][0] < 1)
        map_id = self.add("RampMap")
        self.set(map_id, "ramp_type", "input")
        self.set_list(map_id, "positions", "F", [item[0] for item in stops])
        self.set_list(map_id, "colors", "C", [value for item in stops for value in item[1]])
        self.set_list(map_id, "interpolations", "I", [RAMP_MODES[ramp.interpolation]] * len(stops))
        self.wire(map_id, "input", node.inputs["Fac"], False)
        return map_id, True

    def curve(self, values):
        """A float map of the angle to the camera: RampMap holds a table of it against DirectionalMap's cosine, at COSINES."""
        facing = self.add("DirectionalMap")
        self.set(facing, "polarity", "parallel")
        self.set(facing, "clamping_behavior", "absolute")
        positions = list(COSINES)
        map_id = self.add("RampMap")
        self.set(map_id, "ramp_type", "input")
        self.set_list(map_id, "positions", "F", positions)
        self.set_list(map_id, "colors", "C", [value for item in values for value in (item, item, item)])
        self.set_list(map_id, "interpolations", "I", [1] * len(values))
        self.bind(facing, map_id, "input")
        return map_id

    def fresnel(self, node, socket):
        ior = self.constant(node.inputs["IOR"])
        if ior is None:
            self.lost.add("linked Fresnel IOR")
            ior = plain(node.inputs["IOR"].default_value)
        if node.inputs["Normal"].is_linked:
            self.lost.add("Fresnel Normal")
        return self.curve([fresnel(item, max(ior, 1e-5)) for item in COSINES]), False

    def layer_weight(self, node, socket):
        blend = self.constant(node.inputs["Blend"])
        if blend is None:
            self.lost.add("linked Layer Weight Blend")
            blend = plain(node.inputs["Blend"].default_value)
        if node.inputs["Normal"].is_linked:
            self.lost.add("Layer Weight Normal")
        if socket.identifier == "Fresnel":
            eta = 1 / max(1 - blend, 1e-5)
            return self.curve([fresnel(item, eta) for item in COSINES]), False
        power = 2 * blend if blend < 0.5 else 0.5 / max(1 - blend, 1e-5)
        return self.curve([1 - item ** power for item in COSINES]), False

    def space_of(self, socket):
        """(frequency factor, is UV) for Noise and Voronoi, from what feeds their Vector. Nothing linked is Generated, which goes 0 to 1 across the mesh."""
        vector = self.source(socket)
        factor = 1.0
        if vector and vector[0].type == "MAPPING":
            scale = self.constant(vector[0].inputs["Scale"])
            location = self.constant(vector[0].inputs["Location"])
            rotation = self.constant(vector[0].inputs["Rotation"])
            if vector[0].vector_type != "POINT" or scale is None or len(set(scale)) > 1 or location != BLACK or rotation != BLACK:
                self.lost.add("Mapping before a procedural")
            factor = (scale or WHITE)[0]
            vector = self.source(vector[0].inputs["Vector"])
        uv = bool(vector) and (vector[0].type == "UVMAP" or (vector[0].type == "TEX_COORD" and vector[1].identifier == "UV"))
        if vector and not uv and not (vector[0].type == "TEX_COORD" and vector[1].identifier in {"Object", "Generated"}):
            self.lost.add(vector[0].bl_label)
        if self.extent and (vector is None or (vector[0].type == "TEX_COORD" and vector[1].identifier == "Generated")):
            factor /= max(self.extent(), 1e-6)
        return factor, uv

    def texture_space(self, map_id, socket):
        """Space and frequency factor for Noise and Voronoi."""
        factor, uv = self.space_of(socket)
        self.set(map_id, "space", "texture" if uv else "object")
        return factor

    def noise(self, node, socket, coordinates=None):
        """coordinates: a map to read the noise at, instead of the shaded point."""
        i = node.inputs
        if node.noise_dimensions != "3D" or node.noise_type != "FBM":
            self.lost.add(f"Noise Texture {node.noise_dimensions} {node.noise_type} {socket.identifier}")
            return None
        values = [self.constant(i[name]) for name in ("Scale", "Detail", "Roughness", "Lacunarity", "Distortion")]
        if None in values:
            self.lost.add("linked Noise Texture input")
            return None
        scale, detail, roughness, lacunarity, distortion = values
        map_id = self.add("NoiseMap_v2")
        self.set(map_id, "frequency_multiplier", scale * self.texture_space(map_id, i["Vector"]))
        self.set(map_id, "max_level", detail + 1)
        self.set(map_id, "persistence", roughness)
        self.set(map_id, "lacunarity", lacunarity)
        self.set(map_id, "distortion", distortion)
        self.set(map_id, "noise_type", "perlin classic")
        if coordinates:
            self.set(map_id, "space", "input texture coordinates")
            self.bind(coordinates, map_id, "input_texture_coordinates")
        self.lost.add("Noise Texture pattern")
        if socket.identifier == "Color":  # NoiseMap's own three channels, as Cycles' Fac and two offset copies
            self.set(map_id, "color", True)
            return map_id, True
        return map_id, False

    def voronoi(self, node, socket):
        i = node.inputs
        if node.voronoi_dimensions != "3D" or node.feature != "F1" or node.distance != "EUCLIDEAN" or socket.identifier not in {"Distance", "Color"}:
            self.lost.add(f"Voronoi Texture {node.feature} {node.distance} {socket.identifier}")
            return None
        scale, randomness = self.constant(i["Scale"]), self.constant(i["Randomness"])
        if scale is None or randomness is None:
            self.lost.add("linked Voronoi Texture input")
            return None
        map_id = self.add("NoiseWorleyMap_v2")
        self.set(map_id, "frequency", scale * self.texture_space(map_id, i["Vector"]))
        self.set(map_id, "jitter", randomness)
        self.lost.add("Voronoi Texture pattern")
        if socket.identifier == "Color":  # NoiseWorleyMap's own random colour per cell
            self.set(map_id, "output_mode", "cell id")
            return map_id, True
        return self.op("multiply", map_id, VORONOI_GAIN), False

    def mix(self, node, socket):
        if node.data_type not in {"RGBA", "FLOAT"}:
            return None
        color = node.data_type == "RGBA"
        inputs = {item.identifier: item for item in node.inputs}
        a, b = (self.term(inputs[f"{name}_{'Color' if color else 'Float'}"]) for name in "AB")
        result = b if not color or node.blend_type == "MIX" else self.mix_operation(node.blend_type, a, b)
        blend = self.blend(self.term(inputs["Factor_Float"], False), a, result)
        if color and node.clamp_result:
            clamp = self.add("ClampMap")
            self.bind(blend, clamp, "input")
            return clamp, color
        return blend, color

    def mix_operation(self, kind, a, b):
        """Blender's Mix at Factor 1. Burn and Dodge put the Factor inside the division, so between 0 and 1 they're near, not equal."""
        if kind in MIX_OPERATIONS:
            operation, swap = MIX_OPERATIONS[kind]
            return self.op(operation, b, a) if swap else self.op(operation, a, b)
        if kind == "DIFFERENCE":
            return self.op("abs", self.op("subtract", a, b))
        if kind == "EXCLUSION":
            both = self.op("multiply", self.op("multiply", a, b), 2.0)
            return self.op("maximum", self.op("subtract", self.op("add", a, b), both), 0.0)
        if kind == "LINEAR_LIGHT":
            return self.op("subtract", self.op("add", a, self.op("multiply", b, 2.0)), 1.0)
        if kind == "DODGE":
            return self.op("divide", a, self.op("subtract", WHITE, b), True)
        if kind == "BURN":
            return self.op("subtract", WHITE, self.op("divide", self.op("subtract", WHITE, a), b), True)
        if kind == "SOFT_LIGHT":
            first = self.op("multiply", self.op("multiply", self.op("subtract", WHITE, a), a), b)
            return self.op("add", first, self.op("multiply", a, self.op("screen", a, b)))
        if kind in {"HUE", "SATURATION", "VALUE", "COLOR"}:
            taken = {"HUE": "r", "SATURATION": "g", "VALUE": "b", "COLOR": "rg"}[kind]
            first, second = self.add("RgbToHsvMap"), self.add("RgbToHsvMap")
            self.put(first, "input", a)
            self.put(second, "input", b)
            out = self.add("FloatToRgbMap")
            for channel, name in zip("rgb", "RGB"):
                pick = self.add("RgbToFloatMap")
                self.set(pick, "mode", channel)
                self.bind(second if channel in taken else first, pick, "input")
                self.bind(pick, out, name)
            back = self.add("HsvToRgbMap")
            self.bind(out, back, "input")
            return back
        self.lost.add(f"Mix {kind}")
        return b

    def times(self, target, name, sockets, scale):
        """A float attribute that's the product of sockets, colours counting as their luminance, and a number."""
        constant, maps = scale, []
        for socket in sockets:
            value = self.constant(socket)
            feeder = None if value is not None else self.output(*self.source(socket))
            if value is None and feeder is None:
                value = plain(socket.default_value)
            if value is not None:
                constant *= value if isinstance(value, float) else luminance(value)
            else:
                maps.append(self.gray(feeder[0]) if feeder[1] else feeder[0])
        if not maps:
            self.set(target, name, constant)
            return
        source = maps[0]
        for following in maps[1:]:
            op = self.add("OpMap")
            self.set(op, "operation", "multiply")
            self.bind(source, op, "op1")
            self.bind(following, op, "op2")
            source = op
        if constant != 1.0:
            op = self.add("OpMap")
            self.set(op, "operation", "multiply")
            self.bind(source, op, "op1")
            self.set(op, "op2", (constant, constant, constant))
            source = op
        self.bind(source, target, name)

    def edge_tint(self, color, tint=WHITE):
        """metallic_edge_color for a Principled metal, times a low power of Specular Tint, which Cycles uses on a metal only as its colour at 82 degrees: at
        a colour of 0.8 a tint of 0.9 fits an edge of 0.96 from 45 to 78 degrees. Cycles' metal is Schlick from Base Color to white. MoonRay's is a real conductor made
        from colour and edge tint, and a white edge makes a dark channel reflect twice what Schlick does at 60 degrees (0.26 against 0.13
        at a colour of 0.1). This line is the fit of the edge tint against Schlick, per channel."""
        if isinstance(color, tuple):
            fit = tuple(min(max(EDGE_SLOPE * item - EDGE_FROM, 0.0), 1.0) for item in color)
            if isinstance(tint, tuple):
                return tuple(item * shade ** 0.25 for item, shade in zip(fit, tint))
        else:
            fit = self.op("subtract", self.op("multiply", color, EDGE_SLOPE), EDGE_FROM, True)
        return fit if tint == WHITE else self.op("multiply", fit, self.op("power", tint, 0.25))

    def anisotropy(self, material, socket, roughness):
        """Cycles: roughness squared over and times sqrt(1 - 0.9 a), the long axis along the tangent. DwaBase's a shortens the tangent's
        axis if positive, the other if negative, and roughness goes in unsquared."""
        amount = self.constant(socket)
        if amount is None:
            self.lost.add("linked Anisotropic")
            amount = plain(socket.default_value)
        if not amount:
            self.put(material, "roughness", roughness)
            return
        aspect = math.sqrt(1 - 0.9 * min(amount, 1.0))
        self.set(material, "anisotropy", aspect - 1)
        if isinstance(roughness, str):
            self.bind(self.op("multiply", roughness, 1 / math.sqrt(aspect)), material, "roughness")
        else:
            self.set(material, "roughness", min(roughness / math.sqrt(aspect), 1.0))

    def presence(self, material, alpha, extra, thin=True):
        """presence from an Alpha term, times what Mix Shaders with a Transparent above this shader leave of it.
        thin: a see-through closed mesh shows its far side from within. DwaBase takes that as a ray leaving a medium and uses the Fresnel of the inside,
        all of it past about 40 degrees, and that was the wide flare on every Alpha row: 1.65 to 1.01 on the lab's Alpha. Cycles has no such thing here."""
        if extra is None or extra == 1.0:
            self.put(material, "presence", alpha)
        elif alpha == 1.0:
            self.put(material, "presence", extra)
        else:
            self.bind(self.op("multiply", alpha, extra), material, "presence")
        if thin and not (alpha == 1.0 and extra in (None, 1.0)):
            self.set(material, "thin_geometry", True)

    def normal(self, material, socket):
        feeder = self.source(socket)
        if feeder is None:
            return
        node = feeder[0]
        key = node.as_pointer()
        if key not in self.normals:
            self.normals[key] = {"NORMAL_MAP": self.normal_map, "BUMP": self.bump}.get(node.type, self.no_normal)(node)
        normal, dial = self.normals[key]
        if normal:
            self.bind(normal, material, "input_normal")
            if dial != 1.0:
                self.set(material, "input_normal_dial", dial)

    def no_normal(self, node):
        self.lost.add(node.bl_label)
        return None, 1.0

    def normal_map(self, node):
        strength = self.constant(node.inputs["Strength"])
        if strength is None or strength > 1 or node.space != "TANGENT":
            self.lost.add("Normal Map strength over 1, linked, or not tangent")
        dial = min(strength, 1.0) if strength is not None else 1.0
        color = self.source(node.inputs["Color"])
        if color is None:
            return None, 1.0
        image = color[0].image.original if color[0].type == "TEX_IMAGE" and color[0].image else None
        if image in self.tiled and color[1].identifier == "Color" and (image.colorspace_settings.is_data or image.is_float):
            map_id = self.add("ImageNormalMap")
            self.set(map_id, "tangent_space_normal_texture", self.tiled[image])
            self.set(map_id, "wrap_around", color[0].extension == "REPEAT")
            self.place(map_id, color[0].inputs["Vector"])
            return map_id, dial
        rendered = self.add("TransformNormalMap")  # a Separate and Combine for a flipped green, and any other graph
        self.put(rendered, "input_normal", self.term(node.inputs["Color"]))
        map_id = self.add("RgbToNormalMap")
        self.bind(rendered, map_id, "input")
        return map_id, dial

    def noise_height(self, socket):
        """(Noise Texture node, height it spans) if a height is a Noise Texture, alone or between two constants by a Mix."""
        feeder = self.source(socket)
        if feeder and feeder[0].type == "TEX_NOISE":
            return feeder[0], 1.0
        if feeder and feeder[0].type == "MIX" and feeder[0].blend_type == "MIX":
            inputs = {item.identifier: item for item in feeder[0].inputs}
            color = feeder[0].data_type == "RGBA"
            factor = self.source(inputs["Factor_Float"])
            a, b = (self.constant(inputs[f"{name}_{'Color' if color else 'Float'}"]) for name in "AB")
            if factor and factor[0].type == "TEX_NOISE" and a is not None and b is not None:
                a, b = (luminance(item) if isinstance(item, tuple) else item for item in (a, b))
                return factor[0], abs(b - a)
        return None

    def attribute(self, kind):
        """What the renderer knows of the shaded point."""
        map_id = self.add("AttributeMap")
        self.set(map_id, "map_type", kind)
        return map_id

    def noise_bump(self, noise, span, distance, strength, filter_width):
        """Cycles' Bump: N minus distance times the height's slope along the surface, then strength towards N. The slope is the noise at two points beside
        the shaded one, in render space, which on Blender's meshes is world space. Three noise reads and about 20 maps at every shaded point.
        The points are Filter Width of a pixel apart, as in Cycles: a noise much finer than a pixel gives a much weaker slope than its own frequency says."""
        i = noise.inputs
        values = [self.constant(i[name]) for name in ("Scale", "Detail", "Lacunarity")]
        if None in values:
            self.lost.add("linked Noise Texture input")
            return None, 1.0
        scale, detail, lacunarity = values
        factor, uv = self.space_of(i["Vector"])
        step = max(BUMP_STEP / max(abs(scale * factor) * lacunarity ** detail, 1e-6), filter_width * self.pixel() if self.pixel else 0.0)
        normal = self.attribute("shading normal")
        if uv:
            at = self.attribute("texture st")
            along, across = self.op("normalize op1", self.attribute("dpds")), self.op("normalize op1", self.attribute("dpdt"))
            sideways = self.op("divide", (step, 0.0, 0.0), self.op("length", self.attribute("dpds")))
            upwards = self.op("divide", (0.0, step, 0.0), self.op("length", self.attribute("dpdt")))
            points = at, self.op("add", at, sideways), self.op("add", at, upwards)
        else:
            along = self.op("normalize op1", self.op("cross", normal, BUMP_TILT))
            across = self.op("cross", normal, along)
            here = self.attribute("position")
            points = [here, self.op("add", here, self.op("multiply", along, step)), self.op("add", here, self.op("multiply", across, step))]
            for index, point in enumerate(points):
                local = self.add("TransformSpaceMap")
                self.set(local, "input_type", "point")
                self.set(local, "from_space", "render")
                self.set(local, "to_space", "object")
                self.bind(point, local, "input")
                points[index] = local
        heights = [self.noise(noise, noise.outputs[0], point) for point in points]
        if None in heights:
            return None, 1.0
        slope = distance * span / step
        first = self.op("multiply", along, self.op("multiply", self.op("subtract", heights[1][0], heights[0][0]), slope))
        second = self.op("multiply", across, self.op("multiply", self.op("subtract", heights[2][0], heights[0][0]), slope))
        bent = self.op("normalize op1", self.op("subtract", normal, self.op("add", first, second)))
        if strength < 1.0:
            bent = self.op("normalize op1", self.blend(strength, normal, bent))
        map_id = self.add("RgbToNormalMap")
        self.bind(bent, map_id, "input")
        return map_id, 1.0

    def baked_normal(self, path, scale, offset, degrees, world):
        """A normal .tx from a Bump, made for one world size of a UV unit. Its slopes are put right for this point of this mesh by that size over dPds, so a
        material on meshes of any UV density, and a mesh with uneven UV, get the height Cycles gets."""
        texture = self.add("ImageMap")
        self.set(texture, "texture", path)
        self.set(texture, "gamma", "off")
        if scale != (1.0, 1.0) or offset != (0.0, 0.0) or degrees:
            self.set(texture, "scale", scale)
            self.set(texture, "offset", offset)
            self.set(texture, "rotation_angle", -degrees)
            self.set(texture, "rotation_center", (0.0, 0.0))
        decoded = self.op("subtract", self.op("multiply", texture, 2.0), 1.0)
        channels = []
        for mode in "rgb":
            channel = self.add("RgbToFloatMap")
            self.set(channel, "mode", mode)
            self.bind(decoded, channel, "input")
            channels.append(channel)
        tangent = self.add("FloatToRgbMap")
        for name, channel, kind in zip("RG", channels, ("dpds", "dpdt")):
            stretch = self.op("minimum", self.op("divide", world, self.op("length", self.attribute(kind))), BUMP_MAX_STRETCH)
            self.bind(self.op("multiply", self.op("divide", channel, channels[2]), stretch), tangent, name)
        self.set(tangent, "B", 1.0)
        rendered = self.add("TransformNormalMap")
        self.set(rendered, "decode_input_normal", False)
        self.bind(tangent, rendered, "input_normal")
        map_id = self.add("RgbToNormalMap")
        self.bind(rendered, map_id, "input")
        return map_id

    def bump(self, node):
        strength, distance = self.constant(node.inputs["Strength"]), self.constant(node.inputs["Distance"])
        if self.source(node.inputs["Normal"]):
            self.lost.add("Normal into Bump")
        if strength is None or distance is None:
            self.lost.add("linked Bump Strength or Distance")
            return None, 1.0
        if not strength or not distance:
            return None, 1.0
        distance *= -1 if node.invert else 1
        procedural = self.noise_height(node.inputs["Height"])
        if procedural:
            return self.noise_bump(*procedural, distance, strength, self.constant(node.inputs["Filter Width"]) or 0.1)
        if self.bake:
            baked = self.bake(self, node, distance, strength)
            return (None, 1.0) if baked is None else (self.baked_normal(*baked), 1.0)
        height_field(self, node.inputs["Height"], (1, 1), None)  # only names what it can't work out
        return self.add("ImageNormalMap"), 1.0

    def high_ior(self, i):
        """The colour at normal incidence if this Principled is really a mirror with a dielectric's IOR: nothing under the specular that matters."""
        ior, level, metal = self.constant(i["IOR"]), self.constant(i["Specular IOR Level"]), self.constant(i["Metallic"])
        color = self.constant(i["Base Color"])
        if not CONDUCTOR_IOR or not isinstance(ior, float) or ior < CONDUCTOR_IOR or not isinstance(level, float) or metal != 0.0:
            return None
        if i["Transmission Weight"].is_linked or i["Transmission Weight"].default_value or color is None:
            return None
        if luminance(color) > CONDUCTOR_DARK:
            return None
        normal = min(((ior - 1) / (ior + 1)) ** 2 * 2 * level, 1.0)
        return normal + (1 - normal) * luminance(color)  # the diffuse under it, which a metal hasn't got

    def principled(self, node, partner=None, presence=None):
        """partner is the Base Color of the dielectric this metal is mixed with, if it is. DwaMix blends parameters: it lerps
        albedo and metallic, then weights albedo by 1 - metallic, and a metal has no albedo, so the dielectric's diffuse would go as
        (1 - Fac) squared. Giving the metal the dielectric's albedo, and a bound metallic so that its albedo is read, makes it 1 - Fac."""
        i = node.inputs
        material = self.add("DwaBaseMaterial")
        base = i["Base Color"]
        self.wire(material, "albedo", partner or base, True)
        mirror = self.high_ior(i)
        if mirror is None:
            self.wire(material, "metallic_color", base, True)
        if mirror is not None:
            self.set(material, "metallic_color", (mirror, mirror, mirror))
            self.set(material, "metallic_edge_color", (min(mirror * CONDUCTOR_EDGE, 1.0),) * 3)
        elif self.constant(i["Metallic"]) != 0.0 and not self.nanna:  # Look item 3: Nanna leaves DwaBase's edge colour alone
            self.put(material, "metallic_edge_color", self.edge_tint(self.term(base), self.term(i["Specular Tint"])))
        if partner:
            one = self.add("ConstantColorMap")
            self.set(one, "color_value", (1.0, 1.0, 1.0))
            self.bind(one, material, "metallic")
        elif mirror is not None:
            self.set(material, "metallic", 1.0)
        else:
            self.wire(material, "metallic", i["Metallic"], False)
        self.anisotropy(material, i["Anisotropic"], self.term(i["Roughness"], False))
        self.wire(material, "diffuse_roughness", i["Diffuse Roughness"], False)
        solid = i["Transmission Weight"].is_linked or i["Transmission Weight"].default_value > 0
        self.presence(material, 1.0 if node.as_pointer() in self.alpha_done else self.term(i["Alpha"], False), presence, not solid)
        if self.constant(i["IOR"]) is not None:
            self.set(material, "refractive_index", self.constant(i["IOR"]))
        else:
            self.lost.add("linked IOR")
        # Specular IOR Level scales F0, 0.5 being neutral. DwaBase's specular has no tint, so a tint scales it by its luminance.
        level, ior = self.constant(i["Specular IOR Level"]), self.constant(i["IOR"])
        scaling, scale = [i["Specular IOR Level"], i["Specular Tint"]], 2.0
        transmits = bool(i["Transmission Weight"].default_value)
        if not self.nanna and isinstance(level, float) and isinstance(ior, float) and (level != 0.5 if transmits else level > 0.5):  # Look item 4
            # Cycles makes the changed F0 a new IOR, and glass refracts by that one as well. Otherwise DwaBase clamps specular at 1, so above neutral it's a higher IOR
            root = math.sqrt(((ior - 1) / (ior + 1)) ** 2 * 2 * level)
            self.set(material, "refractive_index", (1 + root) / (1 - root))
            scaling, scale = [i["Specular Tint"]], 1.0
        # On a metal specular weighs the whole lobe, and Cycles' metal has neither: Level isn't used and Tint went to the edge above.
        metal = 1.0 if partner or mirror is not None else self.constant(i["Metallic"])
        if self.nanna:  # Look item 4: DwaBase's specular stays at its own default
            pass
        elif metal == 0.0:
            self.times(material, "specular", scaling, scale)
        elif metal != 1.0:  # part metal, or a map of it: the dielectric's value where it isn't metal
            dielectric = scale
            for item in scaling:
                term = self.term(item, False)
                dielectric = term * dielectric if isinstance(term, float) and isinstance(dielectric, float) else self.op("multiply", dielectric, term)
            if isinstance(dielectric, float):
                dielectric = (dielectric, dielectric, dielectric)
            self.bind(self.blend(self.term(i["Metallic"], False), dielectric, WHITE), material, "specular")
        if i["Anisotropic Rotation"].is_linked or i["Anisotropic Rotation"].default_value:
            self.lost.add("Anisotropic Rotation")

        transmission = i["Transmission Weight"]
        if transmission.is_linked or transmission.default_value > 0:
            self.wire(material, "transmission", transmission, False)
            color = self.constant(base)
            if color is None:
                self.wire(material, "transmission_color", base, True)
            else:  # as in refractiveNetwork(): MoonRay tints at each surface a ray crosses
                tint = self.tint or WHITE
                self.set(material, "transmission_color", tuple(math.sqrt(max(item, 0.0)) * shade for item, shade in zip(color, tint)))
            self.set(material, "casts_caustics", True)  # otherwise the sun doesn't come through a window

        coat = i["Coat Weight"]
        if coat.is_linked or coat.default_value > 0:
            self.set(material, "show_clearcoat", True)
            self.set(material, "clearcoat_bending", self.nanna)  # Look item 1. It darkens what's under the coat, Cycles doesn't bend
            self.wire(material, "clearcoat", coat, False)
            self.wire(material, "clearcoat_roughness", i["Coat Roughness"], False)
            if self.constant(i["Coat IOR"]) is not None:
                self.set(material, "clearcoat_refractive_index", self.constant(i["Coat IOR"]))
            if i["Coat Tint"].is_linked or tuple(i["Coat Tint"].default_value[:3]) != (1.0, 1.0, 1.0):
                self.set(material, "clearcoat_thickness", 1.0)
                self.wire(material, "clearcoat_attenuation_color", i["Coat Tint"], True)

        sheen = i["Sheen Weight"]
        if sheen.is_linked or sheen.default_value > 0:
            self.set(material, "show_fuzz", True)
            if self.nanna:  # Look item 2
                self.wire(material, "fuzz", sheen, False)
            else:
                edge = self.curve([(1 - item) ** SHEEN_POWER for item in COSINES])
                self.bind(self.op("multiply", edge, self.product(self.term(sheen, False), SHEEN_SCALE)), material, "fuzz")
            self.wire(material, "fuzz_roughness", i["Sheen Roughness"], False)
            self.wire(material, "fuzz_albedo", i["Sheen Tint"], True)

        weight = i["Subsurface Weight"]
        if weight.is_linked or weight.default_value > 0:
            radius = i["Subsurface Radius"].default_value
            amount = self.constant(weight)
            self.set(material, "scattering_color", self.constant(base) or (1.0, 1.0, 1.0))
            self.set(material, "scattering_radius", i["Subsurface Scale"].default_value * sum(radius) / 3 * (amount or 1.0))

        emission, strength = i["Emission Color"], i["Emission Strength"]
        factor = self.constant(strength)
        if (factor is None or factor > 0) and not self.glow:
            self.set(material, "show_emission", True)
            color = self.constant(emission)
            if color is not None and factor is not None:
                self.set(material, "emission", tuple(item * factor for item in color))
            elif factor == 1.0:
                self.wire(material, "emission", emission, True)
            else:
                op = self.add("OpMap")
                self.set(op, "operation", "multiply")
                self.wire(op, "op1", emission, True)
                self.wire(op, "op2", strength, True)
                self.bind(op, material, "emission")
        self.normal(material, i["Normal"])
        return material

    def glass(self, node, partner=None, presence=None, beside_metal=False):
        """As a Principled with full transmission, with Glass's sockets."""
        i = node.inputs
        material = self.add("DwaBaseMaterial")
        self.set(material, "albedo", BLACK)
        self.wire(material, "roughness", i["Roughness"], False)
        if self.constant(i["IOR"]) is not None:
            self.set(material, "refractive_index", self.constant(i["IOR"]))
        else:
            self.lost.add("linked IOR")
        self.set(material, "transmission", 1.0)
        color = self.constant(i["Color"])
        if color is None:
            self.wire(material, "transmission_color", i["Color"], True)
        else:  # as in principled()
            tint = self.tint or WHITE
            self.set(material, "transmission_color", tuple(math.sqrt(max(item, 0.0)) * shade for item, shade in zip(color, tint)))
        self.set(material, "casts_caustics", True)
        if presence is not None:
            self.presence(material, 1.0, presence, False)
        self.normal(material, i["Normal"])
        return material

    def diffuse(self, node, partner=None, presence=None, beside_metal=False):
        """beside_metal: the metal's Roughness socket. DwaMix lerps specular as well, and a metal's weight is specular times metallic. A
        specular of 1 here keeps it at Fac, an IOR of 1 means it reflects nothing itself, and because both now have a specular, DwaMix lerps their
        roughness as well, so this one takes the metal's."""
        material = self.add("DwaBaseMaterial")
        self.gained(material, "albedo", node.inputs["Color"])
        self.wire(material, "diffuse_roughness", node.inputs["Roughness"], False)
        if beside_metal:
            self.set(material, "refractive_index", 1.0)
            self.wire(material, "roughness", beside_metal, False)
        else:
            self.set(material, "specular", 0.0)
        if presence is not None:
            self.presence(material, 1.0, presence)
        self.normal(material, node.inputs["Normal"])
        return material

    def glossy(self, node, partner=None, presence=None, beside_metal=False):
        """A Glossy reflects its colour at every angle: a metal with that colour at its edge as well."""
        i = node.inputs
        material = self.add("DwaBaseMaterial")
        self.gained(material, "albedo", partner or i["Color"])
        self.gained(material, "metallic_color", i["Color"])
        self.set(material, "metallic_edge_color", BLACK)  # flattest a conductor gets
        one = self.add("ConstantColorMap")  # bound so that albedo is read, see principled()
        self.set(one, "color_value", WHITE)
        self.bind(one, material, "metallic")
        self.wire(material, "roughness", i["Roughness"], False)
        self.set(material, "refractive_index", 1.0)
        if i["Anisotropy"].is_linked or i["Anisotropy"].default_value:
            self.lost.add("Glossy Anisotropy")
        if presence is not None:
            self.presence(material, 1.0, presence)
        self.normal(material, i["Normal"])
        return material

    def translucent(self, node, partner=None, presence=None, beside_metal=False):
        """partner: the shader it's mixed with. DwaMix leaves that shader's albedo A times 1 - Fac * colour where Cycles has 1 - Fac, so
        this albedo is A (1 - Fac) colour / (1 - Fac * colour), per channel, which gives it back. A Fac that's a map counts as 0.5."""
        material = self.add("DwaBaseMaterial")
        if partner:
            share = self.shares.get(node.as_pointer(), 0.5)
            color = self.term(node.inputs["Color"])
            kept = self.op("multiply", self.term(partner), self.op("multiply", color, 1 - share))
            if node.as_pointer() in self.beside_alpha:  # where the partner is cut away, none of its albedo
                kept = self.op("multiply", kept, self.beside_alpha[node.as_pointer()])
            self.bind(self.op("divide", kept, self.op("subtract", WHITE, self.op("multiply", color, share))), material, "albedo")
        else:
            self.set(material, "albedo", BLACK)
        self.set(material, "specular", 0.0)
        self.set(material, "diffuse_transmission", 1.0)
        self.wire(material, "diffuse_transmission_color", node.inputs["Color"], True)
        if presence is not None:
            self.presence(material, 1.0, presence)
        self.normal(material, node.inputs["Normal"])
        return material

    def transparent(self, node, partner=None, presence=None, beside_metal=False):
        material = self.add("DwaBaseMaterial")
        self.set(material, "albedo", BLACK)
        self.set(material, "specular", 0.0)
        color = self.constant(node.inputs["Color"])
        self.presence(material, 1 - luminance(color) if color else 0.0, presence)
        return material

    def emission(self, node, partner=None, presence=None, beside_metal=False):
        material = self.add("DwaBaseMaterial")
        self.set(material, "albedo", BLACK)
        self.set(material, "specular", 0.0)
        color, strength = self.constant(node.inputs["Color"]), self.constant(node.inputs["Strength"])
        if not self.glow:
            self.set(material, "show_emission", True)
            if color is not None and strength is not None:
                self.set(material, "emission", tuple(item * strength for item in color))
            else:
                self.bind(self.op("multiply", self.term(node.inputs["Color"]), self.term(node.inputs["Strength"])), material, "emission")
        if presence is not None:
            self.presence(material, 1.0, presence)
        return material

    def add_shader(self, node, partner, presence, beside_metal):
        """Emission adds to what's beside it on the one DwaBase. A Diffuse and a Glossy: DwaMix at 0.5, with their colours doubled."""
        inputs = node.inputs[0], node.inputs[1]
        kinds = [self.kind(item) for item in inputs]
        if "EMISSION" in kinds and kinds[0] != kinds[1]:
            glow = self.source(inputs[kinds.index("EMISSION")])[0]
            material = self.shader(inputs[1 - kinds.index("EMISSION")], partner, presence, beside_metal)
            if not material.startswith("DwaBaseMaterial"):
                raise Untranslated("Add Shader of a mix and an Emission")
            if not self.glow:
                self.set(material, "show_emission", True)
                self.bind(self.op("multiply", self.term(glow.inputs["Color"]), self.term(glow.inputs["Strength"])), material, "emission")
            return material
        if sorted(kinds) != ["BSDF_DIFFUSE", "BSDF_GLOSSY"] or self.gain != 1.0:
            raise Untranslated(node.bl_label)
        glossy, diffuse = (inputs[0], inputs[1]) if kinds[0] == "BSDF_GLOSSY" else (inputs[1], inputs[0])
        self.gain = 2.0
        first = self.shader(diffuse, None, presence, self.metal_roughness(glossy))
        second = self.shader(glossy, self.dielectric_base(diffuse), presence)
        self.gain = 1.0
        mix = self.add("DwaMixMaterial")
        self.bind(first, mix, "material0")
        self.bind(second, mix, "material1")
        self.set(mix, "mix", 0.5)
        return mix

    def radiance(self, socket):
        """The emission under a shader input as an RGB tuple, or None if a map drives any of it. A Mix Shader's map Fac is only fine with
        nothing emitting beneath it."""
        feeder = self.source(socket)
        node = feeder[0] if feeder else None
        if node is None or node.type not in {"EMISSION", "BSDF_PRINCIPLED", "MIX_SHADER", "ADD_SHADER"}:
            return BLACK
        if node.type == "ADD_SHADER":
            first, second = self.radiance(node.inputs[0]), self.radiance(node.inputs[1])
            return None if first is None or second is None else tuple(a + b for a, b in zip(first, second))
        if node.type == "MIX_SHADER":
            first, second = self.radiance(node.inputs[1]), self.radiance(node.inputs[2])
            factor = self.constant(node.inputs[0])
            if first is None or second is None:
                return None
            if factor is None:
                return None if max(first + second) > 0 else BLACK
            return tuple(a * (1 - factor) + b * factor for a, b in zip(first, second))
        color, strength = ((node.inputs["Color"], node.inputs["Strength"]) if node.type == "EMISSION" else
                           (node.inputs["Emission Color"], node.inputs["Emission Strength"]))
        color, strength = self.constant(color), self.constant(strength)
        if strength == 0.0:
            return BLACK
        if color is None or strength is None:
            return None
        return tuple(item * strength for item in color)

    def is_metal(self, socket):
        node = _shader_at(self, socket)
        return bool(node) and (node.type == "BSDF_GLOSSY" or (node.type == "BSDF_PRINCIPLED" and self.constant(node.inputs["Metallic"]) == 1.0))

    def product(self, a, b):
        return a * b if isinstance(a, float) and isinstance(b, float) else self.op("multiply", a, b)

    def mix_alpha(self, socket):
        """The Alpha term of the Principled at a Mix Shader's input, 1.0 if it's opaque or anything else."""
        node = _shader_at(self, socket)
        if node is None or node.type != "BSDF_PRINCIPLED" or self.constant(node.inputs["Alpha"]) == 1.0:
            return 1.0
        self.alpha_done.add(node.as_pointer())
        return self.term(node.inputs["Alpha"], False)

    def metal_roughness(self, socket):
        """The Roughness socket of the metal at this shader input, or None if it isn't one."""
        return _shader_at(self, socket).inputs["Roughness"] if self.is_metal(socket) else None

    def dielectric_base(self, socket):
        """The colour socket under this shader input, if it's a Diffuse, or a Principled that isn't metal."""
        node = _shader_at(self, socket)
        if node and node.type == "BSDF_DIFFUSE":
            return node.inputs["Color"]
        return node.inputs["Base Color"] if node and node.type == "BSDF_PRINCIPLED" and self.constant(node.inputs["Metallic"]) == 0.0 else None

    def ray_mask(self, socket):
        """The rays a Fac of Light Path outputs is 1 on, or None if it isn't only that."""
        feeder = self.source(socket)
        if feeder is None:
            return None
        node = feeder[0]
        if node.type == "LIGHT_PATH":
            return LIGHT_PATH_RAYS.get(feeder[1].name)
        if node.type == "MATH" and node.operation in {"MAXIMUM", "ADD"}:
            masks = [self.ray_mask(item) for item in node.inputs[:2]]
            return None if None in masks else masks[0] | masks[1]
        return None

    def kind(self, socket):
        feeder = self.source(socket)
        return feeder[0].type if feeder else None

    def pane(self, node):
        """A flat pane for the rays that see it: a mirror lobe with presence as Fresnel, so what isn't reflected goes straight on. A transmission lobe
        would refract, but it tests lights at each surface, past the presence of the shadow side, and sky through a pane comes out twice (test room
        1.97, this 1.00). The two faces of a pane bend a ray back anyway. None if it isn't a clear, sharp glass of a known IOR."""
        i = node.inputs
        if node.type == "BSDF_GLASS":
            color, weight = self.constant(i["Color"]), 1.0
        elif node.type == "BSDF_PRINCIPLED":
            color, weight = self.constant(i["Base Color"]), self.constant(i["Transmission Weight"])
            if self.constant(i["Metallic"]) != 0.0 or self.constant(i["Alpha"]) != 1.0:
                return None
        else:
            return None
        ior, roughness = self.constant(i["IOR"]), self.constant(i["Roughness"])
        if color is None or None in (weight, ior, roughness) or not weight or roughness > PANE_ROUGHNESS or i["Normal"].is_linked:
            return None
        through = math.sqrt(max(luminance(color), 0.0)) * weight  # at each face, as in principled()
        material = self.add("DwaBaseMaterial")
        self.set(material, "albedo", BLACK)
        self.set(material, "metallic", 1.0)
        self.set(material, "metallic_color", WHITE)
        self.set(material, "metallic_edge_color", WHITE)
        self.set(material, "roughness", roughness)
        self.bind(self.curve([1 - (1 - fresnel(item, ior)) * through for item in COSINES]), material, "presence")
        return material

    def shadow_trick(self, solid, clear, rays):
        """Mix Shader with Light Path, Transparent for shadow rays. RaySwitch doesn't switch for a shadow ray: it reads its default material's presence,
        and that's the Transparent. A ray that goes on by presence adds no light, so the rays Light Path sends to the Transparent take it as well, and
        bounce from outside comes in. Only what the camera, mirrors and glossy rays see can be glass: a pane if it's flat, otherwise the
        glass itself, which refracts. A BSDF sample at its entry face tests the sky through the object with default_material's presence, and the path carries on
        to find the same sky at the exit, 1.7 times the light in a white world with presence 0.3. So curved glass stops its own shadow rays, default_material
        is the solid glass, and clear_shadow takes it out of other geometry's shadow rays, which cross it unseen."""
        if self.kind(clear) != "BSDF_TRANSPARENT":
            self.lost.add("Light Path on shadow rays with something other than Transparent")
            return self.shader(solid, top=True)
        passing = self.shader(clear)
        if self.tint:  # the sun also crosses the medium to what's behind it, and presence has no colour, so its passage goes in the tint as well
            self.tint = tuple(item ** 2 for item in self.tint)
        node = self.source(solid)[0]
        glass = node.type == "BSDF_GLASS" or (node.type == "BSDF_PRINCIPLED" and self.constant(node.inputs["Transmission Weight"]) != 0.0)
        seen = self.pane(node) if self.flat_share and self.flat_share() > PANE_SHARE else None
        color = self.constant(self.source(clear)[0].inputs["Color"])
        self.clear_shadow = bool(glass and color and luminance(color) > CLEAR_SHADOW)
        shadow_side = passing
        if seen is None:
            seen = self.shader(solid, top=True)
            if self.clear_shadow:
                shadow_side = seen
            if "diffuse" in rays and seen.startswith("DwaBaseMaterial"):
                self.set(seen, "casts_caustics", False)
        switch = self.add("RaySwitchMaterial")
        self.bind(shadow_side, switch, "default_material")
        for ray, name in RAY_ATTRS.items():
            self.bind(passing if ray in rays else seen, switch, name)
        if "diffuse" not in rays and glass:
            if self.caustics:  # a diffuse ray goes through glass, and finds the sky and sun the shadow rays already found. Test room: Cycles 2.2, here 1.1
                self.lost.add("Is Shadow Ray alone with Refractive Caustics on, where Cycles adds the sun and sky twice")
                bounce = self.shader(solid, top=True)
            else:  # Cycles stops a diffuse ray at glass
                bounce = self.add("DwaBaseMaterial")
                self.set(bounce, "albedo", BLACK)
                self.set(bounce, "specular", 0.0)
            self.bind(bounce, switch, RAY_ATTRS["diffuse"])
        return switch

    def shader(self, socket, partner=None, presence=None, beside_metal=False, top=False):
        feeder = self.source(socket)
        if feeder is None:
            raise Untranslated("an empty shader input")
        node = feeder[0]
        leaf = {"BSDF_PRINCIPLED": lambda *args: self.principled(node, args[0], args[1]), "BSDF_DIFFUSE": lambda *args: self.diffuse(node, *args),
                "BSDF_GLASS": lambda *args: self.glass(node, *args),
                "BSDF_GLOSSY": lambda *args: self.glossy(node, *args), "BSDF_TRANSLUCENT": lambda *args: self.translucent(node, *args),
                "BSDF_TRANSPARENT": lambda *args: self.transparent(node, *args), "EMISSION": lambda *args: self.emission(node, *args)}.get(node.type)
        if leaf:
            if top and self.nanna and (node.type == "BSDF_GLASS" or (node.type == "BSDF_PRINCIPLED" and self.constant(node.inputs["Transmission Weight"]))) \
                    and self.flat_share and self.flat_share() > PANE_SHARE:
                pane = self.pane(node)  # Nanna: sun passes a clear flat pane directly, as on the GPU. As glass it stops shadow rays and the sun only comes in as caustics
                if pane is not None:
                    return pane
            return leaf(partner, presence, beside_metal)
        if node.type == "ADD_SHADER":
            return self.add_shader(node, partner, presence, beside_metal)
        if node.type != "MIX_SHADER":
            raise Untranslated(node.bl_label)
        inputs = node.inputs[1], node.inputs[2]
        rays = self.ray_mask(node.inputs[0])
        if rays:  # Fac is 0 or 1 by ray
            if not top:
                raise Untranslated("Light Path under a Mix Shader")
            if "shadow" in rays:
                return self.shadow_trick(*(inputs[::-1] if "camera" in rays else inputs), rays)
            first, second = self.shader(inputs[0]), self.shader(inputs[1])
            switch = self.add("RaySwitchMaterial")
            self.bind(second if "shadow" in rays else first, switch, "default_material")
            for ray, name in RAY_ATTRS.items():
                self.bind(second if ray in rays else first, switch, name)
            return switch
        kinds = [(feeder_[0].type if feeder_ else None) for feeder_ in (self.source(inputs[0]), self.source(inputs[1]))]
        if "BSDF_TRANSPARENT" in kinds:  # presence is the only way MoonRay has of letting a ray through unchanged
            keep = inputs[1] if kinds[0] == "BSDF_TRANSPARENT" else inputs[0]
            if kinds[0] == kinds[1]:
                keep = inputs[0]
            clear = self.source(inputs[0] if keep == inputs[1] else inputs[1])[0]
            color = self.constant(clear.inputs["Color"])
            if color is None or luminance(color) < 0.9:
                self.lost.add("tinted Transparent")
            factor = self.term(node.inputs[0], False)
            weight = factor if keep == inputs[1] else (self.op("subtract", 1.0, factor) if isinstance(factor, str) else 1 - factor)
            if presence is not None:
                weight = self.op("multiply", weight, presence)
            return self.shader(keep, partner, weight)
        needs_albedo = [self.is_metal(item) or kind == "BSDF_TRANSLUCENT" for item, kind in zip(inputs, kinds)]
        factor = self.constant(node.inputs[0])
        if factor is not None:
            for item, share in zip(inputs, (1 - factor, factor)):
                if self.source(item)[0].type == "BSDF_TRANSLUCENT":
                    self.shares[self.source(item)[0].as_pointer()] = share
        # Cycles' Alpha takes from one side only: A (1 - Fac) of the first, Fac of the second, and the rest passes. In DwaMix, presence blends but the lobes
        # don't follow it, so a cut-away side would still shine: Fac becomes the second side's share of what's there, and both sides get what's there
        alphas = self.mix_alpha(inputs[0]), self.mix_alpha(inputs[1])
        amount = None
        if alphas != (1.0, 1.0):
            fac = self.term(node.inputs[0], False)
            sides = (self.product(alphas[0], self.op("subtract", 1.0, fac) if isinstance(fac, str) else 1 - fac), self.product(alphas[1], fac))
            there = self.op("add", *sides)
            amount = self.op("divide", sides[1], self.op("maximum", there, 1e-4))
            presence = there if presence is None else self.product(there, presence)
            for item, other in ((inputs[0], alphas[1]), (inputs[1], alphas[0])):
                if other != 1.0:
                    self.beside_alpha[self.source(item)[0].as_pointer()] = other
        first = self.shader(inputs[0], self.dielectric_base(inputs[1]) if needs_albedo[0] else None, presence, self.metal_roughness(inputs[1]))
        second = self.shader(inputs[1], self.dielectric_base(inputs[0]) if needs_albedo[1] else None, presence, self.metal_roughness(inputs[0]))
        if "BSDF_TRANSLUCENT" in kinds:  # translucent()'s albedo counts on the colour taking from the reflection channel by channel. DwaBase's default is grey
            for side in (first, second):
                if side.startswith("DwaBaseMaterial"):
                    self.set(side, "diffuse_transmission_blending_behavior", "RGB")
        mix = self.add("DwaMixMaterial")
        self.bind(first, mix, "material0")
        self.bind(second, mix, "material1")
        if amount:
            self.bind(amount, mix, "mix")
        else:
            self.wire(mix, "mix", node.inputs[0], False)
        return mix


def _shader_at(network, socket):
    feeder = network.source(socket)
    return feeder[0] if feeder and feeder[0].type.startswith("BSDF_") else None


def srgb_to_linear(values):
    return np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4)


class _Field:
    """Evaluates a height's graph at UV positions, as arrays of shape (rows, columns, 3), with no render. read(image) gives an image's
    linear pixels, bottom row first. With read None it only walks the graph, so that the warnings name what's lost."""

    def __init__(self, network, u, v, read):
        self.network, self.u, self.v, self.read = network, u, v, read

    def value(self, socket):
        feeder = self.network.source(socket)
        if feeder is None:
            return self.flat(plain(socket.default_value))
        node, output = feeder
        if node.type in {"VALUE", "RGB"}:
            return self.flat(plain(output.default_value))
        maker = getattr(self, "node_" + node.type.lower(), None)
        if maker is None:
            self.network.lost.add(node.bl_label + " in a Bump height")
            return self.flat(plain(socket.default_value))
        return maker(node, output)

    def flat(self, value):
        return np.broadcast_to(np.array(value if isinstance(value, tuple) else (value,) * 3, dtype=np.float32), self.u.shape + (3,))

    def node_tex_image(self, node, output):
        image = node.image.original if node.image else None
        scale, offset, degrees = self.network.uv_mapping(node.inputs["Vector"])
        if image is None or self.read is None:
            return self.flat(0.5)
        pixels = self.read(image)
        rows, columns = pixels.shape[:2]
        angle = math.radians(degrees)
        cosine, sine = math.cos(angle), math.sin(angle)
        u = (cosine * self.u - sine * self.v) * scale[0] + offset[0]
        v = (sine * self.u + cosine * self.v) * scale[1] + offset[1]
        x = (u % 1.0) * columns - 0.5
        y = (v % 1.0) * rows - 0.5
        x0, y0 = np.floor(x).astype(np.int32), np.floor(y).astype(np.int32)
        fx, fy = (x - x0)[..., None], (y - y0)[..., None]
        x1, y1, x0, y0 = (x0 + 1) % columns, (y0 + 1) % rows, x0 % columns, y0 % rows
        channel = 3 if output.identifier == "Alpha" and pixels.shape[2] > 3 else None
        data = pixels[..., [channel] * 3] if channel else pixels[..., :3]
        return (data[y0, x0] * (1 - fx) + data[y0, x1] * fx) * (1 - fy) + (data[y1, x0] * (1 - fx) + data[y1, x1] * fx) * fy

    def node_mix(self, node, output):
        inputs = {item.identifier: item for item in node.inputs}
        color = node.data_type == "RGBA"
        a, b = (self.value(inputs[f"{name}_{'Color' if color else 'Float'}"]) for name in "AB")
        factor = self.value(inputs["Factor_Float"])[..., :1]
        kind = node.blend_type if color else "MIX"
        result = {"MIX": lambda: b, "MULTIPLY": lambda: a * b, "ADD": lambda: a + b, "SUBTRACT": lambda: a - b, "DARKEN": lambda: np.minimum(a, b),
                  "LIGHTEN": lambda: np.maximum(a, b), "SCREEN": lambda: 1 - (1 - a) * (1 - b),
                  "OVERLAY": lambda: np.where(a < 0.5, 2 * a * b, 1 - 2 * (1 - a) * (1 - b))}.get(kind)
        if result is None:
            self.network.lost.add(f"Mix {kind} in a Bump height")
            return b
        return a * (1 - factor) + result() * factor

    def node_math(self, node, output):
        a, b = self.value(node.inputs[0]), self.value(node.inputs[1])
        result = {"ADD": lambda: a + b, "SUBTRACT": lambda: a - b, "MULTIPLY": lambda: a * b, "DIVIDE": lambda: a / np.where(b == 0, 1, b),
                  "POWER": lambda: np.maximum(a, 0) ** b, "MINIMUM": lambda: np.minimum(a, b), "MAXIMUM": lambda: np.maximum(a, b)}.get(node.operation)
        if result is None:
            self.network.lost.add(f"Math {node.operation} in a Bump height")
            return a
        return np.clip(result(), 0, 1) if node.use_clamp else result()

    def node_brightcontrast(self, node, output):
        bright, contrast = plain(node.inputs["Bright"].default_value), plain(node.inputs["Contrast"].default_value)
        return np.maximum(self.value(node.inputs["Color"]) * (1 + contrast) + bright - contrast / 2, 0)

    def node_gamma(self, node, output):
        return np.maximum(self.value(node.inputs["Color"]), 0) ** self.value(node.inputs["Gamma"])

    def node_invert(self, node, output):
        color, factor = self.value(node.inputs["Color"]), self.value(node.inputs["Fac"])
        return color * (1 - factor) + (1 - color) * factor

    def node_hue_sat(self, node, output):  # a height is close to the same with its hue turned
        return self.value(node.inputs["Color"]) * plain(node.inputs["Value"].default_value)

    def node_vect_math(self, node, output):
        if node.operation != "DOT_PRODUCT":
            self.network.lost.add(f"Vector Math {node.operation} in a Bump height")
        total = (self.value(node.inputs[0]) * self.value(node.inputs[1])).sum(axis=-1, keepdims=True)
        return np.broadcast_to(total, self.u.shape + (3,))

    def node_separate_color(self, node, output):
        channel = {"Red": 0, "Green": 1, "Blue": 2}[output.identifier]
        return np.repeat(self.value(node.inputs[0])[..., channel:channel + 1], 3, axis=-1)

    def node_rgbtobw(self, node, output):
        data = self.value(node.inputs[0]) @ np.array((0.2126, 0.7152, 0.0722), dtype=np.float32)
        return np.repeat(data[..., None], 3, axis=-1)


def height_field(network, socket, shape, read):
    """Luminance of a height at the texels of a shape, with texels' centres over UV 0 to 1. See _Field."""
    rows, columns = shape
    u, v = np.meshgrid((np.arange(columns, dtype=np.float32) + 0.5) / columns, (np.arange(rows, dtype=np.float32) + 0.5) / rows)
    return _Field(network, u, v, read).value(socket) @ np.array((0.2126, 0.7152, 0.0722), dtype=np.float32)


def bump_normals(height, texel, distance, strength):
    """Tangent-space normals, as Cycles' Bump: N minus distance times the height's slope, then strength towards N. height: bottom row first.
    texel: world size of a texel, along U and along V. The result is encoded 0 to 1, in 16 bits."""
    slope_u = (np.roll(height, -1, axis=1) - np.roll(height, 1, axis=1)) / (2 * texel[0])
    slope_v = (np.roll(height, -1, axis=0) - np.roll(height, 1, axis=0)) / (2 * texel[1])
    normal = np.stack((-distance * slope_u, -distance * slope_v, np.ones_like(height)), axis=-1)
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)
    normal *= strength
    normal[..., 2] += 1 - strength
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)
    return np.rint((normal * 0.5 + 0.5) * 65535).astype(np.uint16)


def absorption_tint(network, socket, distance):
    """A Volume Absorption as a tint, or None. MoonRay's DwaBase has no medium and tints per surface a ray crosses, so this is Beer's law over the
    mesh's mean chord (lights.mean_chord), for half a passage: right for a mesh's mean colour, not for how it deepens with thickness."""
    feeder = network.source(socket)
    if feeder is None or feeder[0].type != "VOLUME_ABSORPTION" or not distance:
        return None
    color, density = network.constant(feeder[0].inputs["Color"]), network.constant(feeder[0].inputs["Density"])
    if color is None or density is None:
        return None
    return tuple(math.exp(-density * (1 - min(item, 1.0)) * distance / 2) for item in color)


def translate(surface_socket, tiled, bake=None, chord=None, flat_share=None, caustics=True, extent=None, pixel=None, look="CYCLES"):
    """The Network for a Surface input. Raises Untranslated. chord: called for a mesh's mean chord, if a Volume Absorption needs it.
    flat_share, caustics, extent, pixel, look: see Network."""
    network = Network(tiled, bake, flat_share, caustics, extent, pixel, look)
    if chord and surface_socket.node.inputs["Volume"].is_linked:
        network.tint = absorption_tint(network, surface_socket.node.inputs["Volume"], chord())
    glow = network.radiance(surface_socket)
    if glow and luminance(glow) > GLOW_LIMIT:
        network.glow = glow
    root = network.shader(surface_socket, top=True)
    for name in ("Volume", "Displacement"):
        if surface_socket.node.inputs[name].is_linked:
            network.lost.add(f"{name} output")
    network.nodes.sort(key=lambda item: item[0] == root)  # stable: Material.cc takes the last node as the root
    return network


def lines(name, network):
    """Manifest lines for a material."""
    out = [f"native\t{name}\tnode\t{node}\t{class_name}\n" for node, class_name in network.nodes]
    out += [f"native\t{name}\tattr\t{node}\t{attr}\t{kind}\t{text}\n" for node, attr, kind, text in network.attrs]
    out += [f"native\t{name}\tbind\t{source}\t{node}\t{attr}\n" for source, node, attr in network.binds]
    return out
