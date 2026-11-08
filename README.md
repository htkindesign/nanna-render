<!-- SPDX-License-Identifier: GPL-3.0-or-later | Copyright 2026 Kiet Ho -->
# Nanna Render

A render engine for Blender 5.1 built on [OpenMoonRay](https://github.com/dreamworksanimation/openmoonray) (DreamWorks Animation) through Hydra, with a GPU path built on [pbrt-v4](https://github.com/mmp/pbrt-v4).

Status: early development, Windows x64 only. The add-on's id is still `moonray_hydra`.

## What is here

| Folder | Contents |
|---|---|
| `addon/` | The Blender add-on: render engine (Hydra, hdMoonray), Render panels, material translator from Blender nodes to MoonRay `DwaBase`, textures, lights, denoising, viewport, GPU render |
| `build/hdmoonray/` | Scripts that rebuild hdMoonray so it loads inside Blender's own USD on Windows, and package the add-on |
| `gpu/kernel/` | A port of MoonRay's `DwaBase` BSDF lobes (`lobes.h`) and its energy compensation tables (`lobe_tables.h`, generated from the MoonRay source) to one C++ source shared by CPU and CUDA, with tests against MoonRay. `SPEC_closure.md` describes the sampling rules |
| `gpu/pbrt/` | Patches that add the MoonRay lobes and Blender export to pbrt-v4 to build the GPU renderer `pbrt.exe` |
| `config.example.bat` | Every root the build scripts read, in one file |

No source code of OpenMoonRay, hdMoonray, OpenUSD or pbrt-v4 is included, only the excerpts the patch scripts change and the port
of MoonRay's lobes and energy tables in `gpu/kernel` (Apache-2.0, see Licence). The build does
not start from the upstream OpenMoonRay releases: it uses the prebuilt MoonRay Windows package from
[mnraker/openmoonray releases](https://github.com/mnraker/openmoonray/releases), the Windows fork
[mnraker/hdMoonray](https://github.com/mnraker/hdMoonray), and fixed commits of OpenUSD and pbrt-v4. `BUILD.md` lists every input.

## Requirements

To use it:
- Windows 10 or 11, x64, and Blender 5.1.2 exactly (the add-on checks Blender's `usd_ms.dll`)
- Device GPU: an NVIDIA RTX card and a current driver

To build it:
- Visual Studio 2022 Build Tools with MSVC 14.44 and a Windows 10/11 SDK
- Python 3.11 or later to run the scripts, Git, and Git Bash for the `.sh` helpers
- CMake and Ninja (`pip install --target <work>\tools cmake ninja`)
- For the GPU parts: CUDA Toolkit 12.4 and OptiX SDK 7.6.0

## Build

`BUILD.md` is the full guide. In short, from a cmd window, after `call config.bat`:

1. Fetch the inputs into the work folder (`BUILD.md`, Inputs).
2. BUILD.md step 1 (`make_def.py`, `build_hd.bat`, `patch_imports.py`, `deploy.bat`, `preload_list.py`): the Hydra plugin for Blender's USD and the runtime folder.
3. `gpu\pbrt\patch_pbrt.py` on a pbrt-v4 checkout, then `gpu\pbrt\build.bat`: `pbrt.exe` for Device GPU (optional).
4. `python -I build\hdmoonray\build_dist.py`: the installable zip.

## Install

Prebuilt add-on packages will be published under Releases. To install a zip: Blender > Edit > Preferences > Get Extensions > Install from Disk.
`addon/CHANGES_TO_BLENDER.txt` says what it changes in Blender and how to remove it.

## Licence

GPL-3.0-or-later, see `LICENSE`. Files that contain or adapt OpenMoonRay, hdMoonray or pbrt-v4 code, and the kernel tests, are
Apache-2.0 (`LICENSES/Apache-2.0.txt`); those holding upstream code keep the upstream copyright. `NOTICE` lists them and every
upstream project.
