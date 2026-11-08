# Copyright 1998-2020 Matt Pharr, Wenzel Jakob and Greg Humphreys
# Copyright 2026 Kiet Ho
# SPDX-License-Identifier: Apache-2.0
# Modified by Kiet Ho, 2026: holds excerpts of pbrt-v4 (github.com/mmp/pbrt-v4, b0bdf46) and the changed code. See NOTICE.
"""Denoise aux for the server, on top of patch_server.py + patch_memory.py. Run from the folder that holds pbrt-v4.

A scene with Film "gbuffer" (the delegate writes one for an F12 with Denoise on) gives an image with more channels than the PFM the
delegate reads can hold. The server now writes R, G, B to OUTFILE as before and albedo + shading normal (Albedo.R/G/B, Ns.X/Y/Z, Ns in
camera space) beside it as <OUTFILE without extension>_aux.exr. An rgb film (every viewport frame) writes exactly what it wrote before.
"""
R = "pbrt-v4/src/pbrt/"


def sub(f, old, new):
    p = R + f
    t = open(p, encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in t else "\n"
    old, new = old.replace("\n", nl), new.replace("\n", nl)
    assert t.count(old) == 1, (f, old, t.count(old))
    open(p, "w", encoding="utf-8", newline="").write(t.replace(old, new))


sub("wavefront/wavefront.cpp", """            Image image = integrator->film.GetImage(&metadata);
            image.Write(name, metadata);
        };""", """            Image image = integrator->film.GetImage(&metadata);
            // NANNA: patch_aux.py. A gbuffer film: albedo and shading normal beside the picture, the picture itself RGB only.
            const std::string auxChannels[] = {"Albedo.R", "Albedo.G", "Albedo.B", "Ns.X", "Ns.Y", "Ns.Z"};
            const std::string rgbChannels[] = {"R", "G", "B"};
            ImageChannelDesc aux = image.NChannels() > 3 ? image.GetChannelDesc(auxChannels) : ImageChannelDesc();
            if (aux) {
                size_t dot = name.rfind('.');
                image.SelectChannels(aux).Write(name.substr(0, dot) + "_aux.exr", metadata);
                image = image.SelectChannels(image.GetChannelDesc(rgbChannels));
            }
            image.Write(name, metadata);
        };""")
# GBufferFilm::Create takes only an .exr filename. The server writes OUTFILE itself (the film never writes its own file), so the
# film is given the same name with .exr; an rgb film doesn't look at it.
sub("wavefront/wavefront.cpp", """            Options->imageFile = f[4];
""", """            Options->imageFile = f[4].substr(0, f[4].rfind('.')) + ".exr";  // NANNA: patch_aux.py, see there
""")
print("patched aux")
