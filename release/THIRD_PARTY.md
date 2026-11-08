# Third-party software in Nanna Render

Nanna Render itself (the add-on's Python code and `runtime/gpu_hydra`, the hdNannaGpu Hydra delegate `hdNannaGpu.dll`) is
Copyright 2026 Kiet Ho, licensed under the GNU General Public License, version 3 or later: the full text is `LICENSE` beside this file.
Its complete source, including the source of `hdNannaGpu.dll` and the build scripts for the binaries below, is at
https://github.com/htkindesign/nanna-render, tag `v0.10.0`.

The add-on ships binaries built from the projects below. Each project's licence text is in `third_party/<folder>/`.
File names are as shipped under `runtime/`: `bin/` is the MoonRay runtime, `gpu/` the GPU server, `gpu_hydra/` the GPU Hydra delegate.
Some MoonRay DLLs are renamed copies (last letter before `.dll` replaced by `m`, e.g. `OpenEXm.dll` = OpenEXR) so they do not clash with Blender's own libraries.

| Project | Version | Licence | Files | Texts | Source of the text |
|---|---|---|---|---|---|
| OpenMoonRay (MoonRay, scene_rdl2, mcrt_denoise, shader DSOs) | prebuilt package 'preview_opensourcedays2025_openmoonray_vfx2023_qt_usd' (OpenMoonRay 1.x source tree, package.py 3.6) | Apache-2.0 | bin/common_*, deepfile, geometry_*, map_*, material_*, rendering_*, render_cache, render_logging, render_util, scene_rdl2, statistics, texturing_sampler; rdl2dso/*.so; plugin/pxr/plugInfo.json | `third_party/moonray/` | `LICENSE.txt`: OpenMoonRay source tree LICENSE; `THIRD-PARTY.md`: OpenMoonRay source tree THIRD-PARTY.md |
| hdMoonray (MoonRay's Hydra render delegate), modified | Windows fork https://github.com/mnraker/hdMoonray, commit a71790f (a71790f61810dca9376bb3d0f5580c225937f5b7), changed by the repo's build/hdmoonray/edit_hdmoonray.py and rebuilt against OpenUSD v25.08 headers for Blender 5.1.2's USD | Apache-2.0 | bin/hydramoonray.dll, bin/hd_moonray_debug.dll, plugin/pxr/hd_moonray_debug/plugInfo.json | `third_party/moonray/` | `LICENSE.txt`: OpenMoonRay source tree LICENSE (the same Apache-2.0 text; hdMoonray is part of OpenMoonRay) |
| OpenUSD (headers compiled into hdMoonray and hdNannaGpu; USD DLLs themselves come from Blender, not shipped) | v25.08 (the version of Blender 5.1.2's USD; hdMoonray and hdNannaGpu are built against its headers). The MoonRay package's own geometry_usd.dll was built against 25.02 | TOST-1.0 (modified Apache-2.0) | inline/template code in hydramoonray, geometry_usd, hd_moonray_debug, hdNannaGpu | `third_party/openusd/` | `LICENSE.txt`: OpenUSD v25.08 LICENSE.txt; `NOTICE.txt`: OpenUSD v25.08 NOTICE.txt |
| OpenSSL | 1.1.1w | OpenSSL License + original SSLeay License (dual) | libcrypto-1_1.dll (libssl-1_1.dll is not shipped) | `third_party/openssl/` | `LICENSE.txt`: OpenSSL 1.1.1 LICENSE text as reproduced in the CPython 3.9 Windows installer LICENSE.txt (same OpenSSL 1.1.1 licence) |
| OpenImageIO | 2.5.18.0 | Apache-2.0 | OpenImageIm.dll (OpenImageIO), OpenImageIO_Utim.dll (OpenImageIO_Util) | `third_party/openimageio/` | `LICENSE.md`, `THIRD-PARTY.md`: OpenImageIO v2.5.18.0 LICENSE.md and THIRD-PARTY.md (bundled fmt, pugixml, tinyformat, kissfft, ...); `LICENSE.txt`: Blender 5.1 license/spdx/Apache-2.0.txt (the standard licence text); `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| OpenEXR | 3.x in MoonRay bin (strings show 3.3.3); 3.3.1 static in nanna_gpu.exe | BSD-3-Clause | Iem.dll (Iex), IlmThream.dll (IlmThread), OpenEXm.dll, OpenEXRCorm.dll; static in gpu/nanna_gpu.exe | `third_party/openexr/` | `LICENSE.md`: pbrt-v4 b0bdf46 src/ext/openexr/LICENSE.md (OpenEXR 3.3.1) |
| Imath | 3.1 (Imath-3_1.dll); 3.1.12 static in nanna_gpu.exe | BSD-3-Clause | Imath-3_1.dll; static in gpu/nanna_gpu.exe | `third_party/imath/` | `LICENSE.md`: Imath 3.1.12 source used by the pbrt build (LICENSE.md) |
| Intel Embree | 4.4.0 | Apache-2.0 | embreem.dll (embree4) | `third_party/embree/` | `LICENSE.txt`: Blender 5.1 license/spdx/Apache-2.0.txt (the standard licence text); `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| Intel TBB | 2020.3 | Apache-2.0 | tbb.dll, tbbmallom.dll (tbbmalloc) | `third_party/tbb/` | `LICENSE.txt`: Blender 5.1 license/spdx/Apache-2.0.txt (the standard licence text); `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| OpenVDB (+ NanoVDB headers in pbrt) | MoonRay bin: version not recorded in the package; pbrt ext: 2021-11 snapshot | MPL-2.0 | openvdm.dll (openvdb); NanoVDB headers compiled into gpu/nanna_gpu.exe | `third_party/openvdb/` | `LICENSE.txt`: pbrt-v4 b0bdf46 src/ext/openvdb/LICENSE; `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| c-blosc | 1.21.1 | BSD-3-Clause | blosc.dll | `third_party/blosc/` | `LICENSE.txt`: Blender 5.1 license/spdx/BSD-3-Clause.txt (the standard licence text); `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| Boost | 1.80.0 | BSL-1.0 | boost_thread.dll | `third_party/boost/` | `LICENSE_1_0.txt`: boost_1_80_0.tar.bz2 LICENSE_1_0.txt |
| log4cplus | 2.1.1 | BSD-2-Clause / Apache-2.0 (per file) | log4cplus.dll | `third_party/log4cplus/` | `LICENSE.txt`: log4cplus 2.1.1 source LICENSE |
| Lua | 5.4.7 | MIT | statically linked into scene_rdl2.dll and render_util.dll | `third_party/lua/` | `LICENSE.txt`: lua-5.4.7.tar.gz src/lua.h licence block |
| OpenColorIO | 2.2.1 | BSD-3-Clause | OpenColorIO_2_2.dll | `third_party/opencolorio/` | `THIRD-PARTY.md`: OpenColorIO v2.2.1 THIRD-PARTY.md (bundled dependencies); `LICENSE.txt`: Blender 5.1 license/spdx/BSD-3-Clause.txt (the standard licence text); `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| pystring | version not recorded in the package | BSD-3-Clause | pystring.dll | `third_party/pystring/` | `LICENSE.txt`: Blender 5.1 license/spdx/BSD-3-Clause.txt (the standard licence text); `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| zlib | 1.2.13 (zlib.dll); 1.3.1.1-motley static in nanna_gpu.exe | Zlib | zlib.dll; static in gpu/nanna_gpu.exe | `third_party/zlib/` | `LICENSE.txt`: pbrt-v4 b0bdf46 src/ext/zlib/LICENSE |
| libpng | 1.6.37 | libpng-2.0 | libpng16.dll | `third_party/libpng/` | `LICENSE.txt`: Blender 5.1 license/spdx/libpng-2.0.txt (same licence text family; Blender ships 1.6.50) |
| libjpeg-turbo | 3.1.0 | IJG + BSD-3-Clause + Zlib | jpeg62.dll | `third_party/libjpeg-turbo/` | `LICENSE.md`, `README.ijg`: libjpeg-turbo 3.1.0 LICENSE.md and the IJG licence (README.ijg); `BSD-3-Clause.txt`: Blender 5.1 license/spdx/BSD-3-Clause.txt (the standard licence text); `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| LibTIFF | 4.7.0 | libtiff | tiff.dll | `third_party/libtiff/` | `LICENSE.txt`: Blender 5.1 license/spdx/libtiff.txt (Blender ships the same 4.7.0); `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| Zstandard | 1.5.0 | BSD-3-Clause (dual GPL-2.0) | zstd.dll | `third_party/zstd/` | `LICENSE.txt`: zstd project LICENSE as shipped by Git for Windows (mingw64/share/licenses/zstd) |
| Expat | 2.5.0 | MIT | libexpat.dll | `third_party/expat/` | `COPYING.txt`: Expat project COPYING as shipped by Git for Windows (mingw64/share/licenses/expat) |
| OpenJPEG | 2.4.0 | BSD-2-Clause | openjp2.dll | `third_party/openjpeg/` | `LICENSE.txt`: Blender 5.1 license/spdx/BSD-2-Clause.txt (the standard licence text); `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| LibRaw | 0.21.3 | LGPL-2.1 or CDDL-1.0 (recipient's choice) | libraw.dll | `third_party/libraw/` | `COPYRIGHT`, `LICENSE.CDDL`: LibRaw 0.21.3 COPYRIGHT and LICENSE.CDDL; `LGPL-2.1.txt`: Blender 5.1 license/spdx/LGPL-2.1-or-later.txt (the standard licence text); `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| Ptex | pbrt ext submodule 054047d | BSD-3-Clause | Ptex.dll; static in gpu/nanna_gpu.exe | `third_party/ptex/` | `LICENSE.txt`: pbrt-v4 b0bdf46 src/ext/ptex/LICENSE |
| yaml-cpp | version not recorded in the package | MIT | yaml-cpp.dll | `third_party/yaml-cpp/` | `LICENSE`: yaml-cpp LICENSE (master branch); `LICENSE.txt`: Blender 5.1 license/spdx/MIT.txt (the standard licence text); `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| JsonCpp | version not recorded in the package | MIT (or Public Domain where recognised) | jsoncpp.dll (also referenced by render_util.dll) | `third_party/jsoncpp/` | `LICENSE`: JsonCpp LICENSE (master branch); `LICENSE.txt`: Blender 5.1 license/spdx/MIT.txt (the standard licence text); `COPYRIGHT.txt`: the project's copyright line, from Blender 5.1 license.md |
| pbrt-v4 (patched, Nanna GPU server) | b0bdf46 + Nanna patches | Apache-2.0 | gpu/nanna_gpu.exe: nanna_gpu.exe is a modified pbrt-v4 (patch_pbrt.py, patch_f12.py, patch_server.py, patch_memory.py, patch_aux.py) | `third_party/pbrt-v4/` | `LICENSE.txt`: pbrt-v4 b0bdf46 LICENSE.txt; `THIRD_PARTY.md`: pbrt-v4 b0bdf46 THIRD_PARTY.md; `rawtoaces_LICENSE`: rawtoaces LICENSE (AMPAS; camera sensor data in pbrt-v4) |
| Nanna GPU Hydra delegate (hdNannaGpu, Nanna Render's own code) | 0.10.0 | GPL-3.0-or-later, as the add-on (Copyright 2026 Kiet Ho) | gpu_hydra/bin/hdNannaGpu.dll, gpu_hydra/plugin/plugInfo.json, gpu_hydra/plugin/hdNannaGpu/plugInfo.json. Links Blender's own usd_ms.dll and python313.dll (not shipped); inline code from the OpenUSD and oneTBB headers (rows above and below) | `LICENSE` (beside this file) | Source: https://github.com/htkindesign/nanna-render, tag `v0.10.0`, folder `gpu/delegate` |
| oneTBB (headers compiled into hdNannaGpu; the TBB DLL itself is Blender's, not shipped) | 2022.3 (the version Blender 5.1.2's usd_ms.dll was built with) | Apache-2.0 | inline/template code in gpu_hydra/bin/hdNannaGpu.dll | `third_party/onetbb/` | `LICENSE.txt`: oneTBB 2022.3 source LICENSE.txt |
| double-conversion (pbrt ext) | 101e1ba | BSD-3-Clause | static in gpu/nanna_gpu.exe | `third_party/double-conversion/` | `LICENSE.txt`: pbrt-v4 src/ext/double-conversion/LICENSE |
| filesystem (wjakob, pbrt ext) | c5f9de3 | BSD-3-Clause | header code in gpu/nanna_gpu.exe | `third_party/filesystem/` | `LICENSE.txt`: pbrt-v4 src/ext/filesystem/LICENSE |
| GLFW (pbrt ext) | 3.4.0 (7b6aead) | Zlib | static in gpu/nanna_gpu.exe | `third_party/glfw/` | `LICENSE.md`: pbrt-v4 src/ext/glfw/LICENSE.md |
| glad-generated OpenGL loader + KHR/khrplatform.h (pbrt ext) | glad 0.1.35 | generated code: Public Domain/CC0 (glad); khrplatform.h: MIT-Khronos | static in gpu/nanna_gpu.exe | `third_party/glad/` | `khrplatform_LICENSE.txt`: pbrt-v4 src/ext/glad/include/KHR/khrplatform.h header |
| libdeflate | 1.18 | MIT | static in gpu/nanna_gpu.exe | `third_party/libdeflate/` | `COPYING.txt`: libdeflate 1.18 source used by the pbrt build (COPYING) |
| LodePNG (pbrt ext) | 8c6a9e3 | Zlib | static in gpu/nanna_gpu.exe | `third_party/lodepng/` | `LICENSE.txt`: pbrt-v4 src/ext/lodepng/LICENSE |
| utf8proc (pbrt ext) | 2.x (d7bf128) | MIT + Unicode data licence | static in gpu/nanna_gpu.exe | `third_party/utf8proc/` | `LICENSE.md`: pbrt-v4 src/ext/utf8proc/LICENSE.md |
| stb (pbrt ext) | af1a5bc | MIT or Public Domain | header code in gpu/nanna_gpu.exe | `third_party/stb/` | `LICENSE.txt`: pbrt-v4 src/ext/stb/LICENSE |
| QOI (pbrt ext) | 028c75f | MIT | header code in gpu/nanna_gpu.exe | `third_party/qoi/` | `LICENSE.txt`: pbrt-v4 src/ext/qoi/qoi.h header |
| RPly (pbrt ext) | 1.1.x | MIT | static in gpu/nanna_gpu.exe | `third_party/rply/` | `LICENSE.txt`: pbrt-v4 src/ext/rply/rply.h licence block |
| Hosek-Wilkie sky model (pbrt ext) | 1.4a | BSD-3-Clause | static in gpu/nanna_gpu.exe | `third_party/skymodel/` | `LICENSE.txt`: pbrt-v4 src/ext/skymodel/ArHosekSkyModel.h header |
| NVIDIA FLIP (pbrt ext) | 2020 | BSD-3-Clause | static in gpu/nanna_gpu.exe | `third_party/flip/` | `LICENSE.txt`: pbrt-v4 src/ext/flip/flip.h header |
| NVIDIA CUDA runtime (cudart_static) | 12.4 | NVIDIA CUDA Toolkit EULA | static in gpu/nanna_gpu.exe | `third_party/cuda/` | `EULA.txt`: CUDA 12.4 toolkit EULA.txt |
| NVIDIA OptiX SDK headers | 7.6.0 | NVIDIA OptiX SDK EULA | OptiX API headers compiled into gpu/nanna_gpu.exe (runtime nvoptix.dll comes from the user's driver) | `third_party/optix/` | `OptiX_EndUserLicense.pdf`: OptiX SDK 7.6.0 doc/OptiX_EndUserLicense.pdf; `OptiX_ThirdParty_Licenses.txt`: OptiX SDK 7.6.0 doc/OptiX_ThirdParty_Licenses.txt |
| Microsoft Visual C++ runtime | VS 2022 (v143) | Microsoft Visual Studio licence, Distributable Code terms | gpu/msvcp140.dll, gpu/msvcp140_codecvt_ids.dll, gpu/vcruntime140.dll, gpu/vcruntime140_1.dll | `third_party/msvc-runtime/` | `Redist.txt`: VS 2022 Build Tools Licenses/1033/Redist.txt; `VS2022_Redistribution.txt`: https://aka.ms/vs/17/redist.txt as plain text (fetched 2026-11-08). The licence terms themselves are link only: Microsoft publishes them as a web page, https://go.microsoft.com/fwlink/?LinkId=2179911 (the link the Build Tools installer shows), not as a text file |
| CPython (headers compiled into hdNannaGpu; python313.dll itself is Blender's, not shipped) | 3.13.9 | PSF-2.0 (Python-2.0) | inline code in gpu_hydra/bin/hdNannaGpu.dll | `third_party/python/` | `LICENSE.txt`: Blender 5.1 license/spdx/Python-2.0.txt (PSF License Version 2, the licence of the Python 3.13.9 that Blender 5.1 ships) |

## Notices required by licence terms

- **OpenSSL:** This product includes software developed by the OpenSSL Project for use in the OpenSSL Toolkit (http://www.openssl.org/).
  This product includes cryptographic software written by Eric Young (eay@cryptsoft.com).
- **libjpeg-turbo (IJG licence):** This software is based in part on the work of the Independent JPEG Group.
- **OpenVDB / NanoVDB (MPL-2.0):** `runtime/bin/openvdm.dll` is an unmodified OpenVDB library from the MoonRay package (renamed from
  openvdb.dll), and `runtime/gpu/nanna_gpu.exe` contains unmodified NanoVDB headers. Source: https://github.com/AcademySoftwareFoundation/openvdb;
  the exact copy built into the GPU server: https://github.com/mmp/pbrt-v4/tree/b0bdf461ba5dd6504b70ebf0932acc0fe7aa744f/src/ext/openvdb
- **LibRaw (LGPL-2.1):** `runtime/bin/libraw.dll` is an unmodified, dynamically loaded LibRaw 0.21.3. You may replace it with your own build.
  Source: https://github.com/LibRaw/LibRaw/tree/0.21.3
- **NVIDIA CUDA runtime and OptiX:** `runtime/gpu/nanna_gpu.exe` contains the CUDA 12.4 runtime and code compiled from OptiX 7.6 SDK headers,
  distributed as part of this application under NVIDIA's terms (`third_party/cuda/`, `third_party/optix/`). The OptiX runtime itself comes
  with the user's NVIDIA driver.
- **Microsoft Visual C++ runtime:** `runtime/gpu/msvcp140*.dll` and `vcruntime140*.dll` are unmodified redistributable files under the Visual
  Studio Distributable Code terms (`third_party/msvc-runtime/`; licence terms: https://go.microsoft.com/fwlink/?LinkId=2179911).
