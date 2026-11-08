# Copyright 1998-2020 Matt Pharr, Wenzel Jakob and Greg Humphreys
# Copyright 2026 Kiet Ho
# SPDX-License-Identifier: Apache-2.0
# Modified by Kiet Ho, 2026: holds excerpts of pbrt-v4 (github.com/mmp/pbrt-v4, b0bdf46) and the changed code. See NOTICE.
"""F12 changes on top of patch_pbrt.py (which runs this at its end). Alone, it patches a tree patch_pbrt.py already did. Run from the folder that holds pbrt-v4.

- GBufferFilm in Nanna's RGB mode: sensor RGB is output RGB, and albedo is the channels as they are, as RGBFilm.
- --nanna-wait: pbrt starts CUDA and OptiX's needs, prints NANNA_READY, then waits for one line on stdin:
  spp TAB seed TAB outfile TAB scenefile. It renders that scene, prints NANNA_DONE once the image is written, and exits.
  End of stdin (Blender gone) makes it exit at once. The add-on keeps one such process warm, so F12 skips the start-up.
"""
R = "pbrt-v4/src/pbrt/"


def sub(f, old, new, count=1):
    p = R + f
    t = open(p, encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in t else "\n"
    old, new = old.replace("\n", nl), new.replace("\n", nl)
    assert t.count(old) >= 1, (f, old)
    t = t.replace(old, new) if count == 0 else t.replace(old, new, count)
    open(p, "w", encoding="utf-8", newline="").write(t)


sub("film.cpp", """    filmPixelMemory += pixelBounds.Area() * sizeof(Pixel);
    outputRGBFromSensorRGB = colorSpace->RGBFromXYZ * sensor->XYZFromSensorRGB;
}

PBRT_CPU_GPU void GBufferFilm::AddSplat(""", """    filmPixelMemory += pixelBounds.Area() * sizeof(Pixel);
    outputRGBFromSensorRGB = SquareMatrix<3>();  // NANNA: sensor RGB is already output RGB
}

PBRT_CPU_GPU void GBufferFilm::AddSplat(""")
sub("film.cpp", """        SampledSpectrum albedo =
            visibleSurface->albedo * colorSpace->illuminant.Sample(lambda);
        RGB albedoRGB = albedo.ToRGB(lambda, *colorSpace);
""", """        // NANNA: a channel is a colour, as the sensor's ToSensorRGB
        RGB albedoRGB(visibleSurface->albedo[0], visibleSurface->albedo[1], visibleSurface->albedo[2]);
""")

sub("cmd/pbrt.cpp", "#include <string>\n", "#include <iostream>\n#include <string>\n")
sub("cmd/pbrt.cpp", "    bool format = false, toPly = false;\n", "    bool format = false, toPly = false;\n    bool nannaWait = false;  // NANNA\n")
sub("cmd/pbrt.cpp", """            ParseArg(&iter, args.end(), "upgrade", &options.upgrade, onError)) {""",
    """            ParseArg(&iter, args.end(), "upgrade", &options.upgrade, onError) ||
            ParseArg(&iter, args.end(), "nanna-wait", &nannaWait, onError)) {  // NANNA""")
sub("cmd/pbrt.cpp", """    } else {
        // Parse provided scene description files
        BasicScene scene;""", """    } else {
        if (nannaWait) {  // NANNA: warm and waiting, see patch_f12.py
            printf("NANNA_READY\\n");
            fflush(stdout);
            std::string line;
            if (!std::getline(std::cin, line))
                return 0;
            std::vector<std::string> f;
            for (size_t start = 0, tab;; start = tab + 1) {
                tab = line.find('\\t', start);
                f.push_back(line.substr(start, tab == std::string::npos ? std::string::npos : tab - start));
                if (tab == std::string::npos)
                    break;
            }
            if (f.size() != 4)
                ErrorExit("--nanna-wait: expected spp, seed, outfile, scene: \\"%s\\"", line);
            Options->pixelSamples = atoi(f[0].c_str());
            Options->seed = atoi(f[1].c_str());
            Options->imageFile = f[2];
#ifdef PBRT_BUILD_GPU_RENDERER
            if (Options->useGPU)
                CopyOptionsToGPU();
#endif
            filenames = {f[3]};
        }
        // Parse provided scene description files
        BasicScene scene;""")
sub("cmd/pbrt.cpp", """            RenderCPU(scene);
""", """            RenderCPU(scene);
        if (nannaWait) {  // NANNA
            printf("\\nNANNA_DONE\\n");
            fflush(stdout);
        }
""")
print("patched f12")
