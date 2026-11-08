<!-- SPDX-License-Identifier: GPL-3.0-or-later | Copyright 2026 Kiet Ho -->
# Building Nanna Render

Windows x64 only. Every command runs in a plain `cmd.exe` window, after `call config.bat` from the repo root
(copy `config.example.bat` to `config.bat` and set its folders first). The scripts take every root from those variables or
from their own location, and stop if a root it needs is unset; nothing else needs editing. Each step starts from the repo root unless it says `cd`.

## Inputs

| Input | Source | Version | Goes to |
|---|---|---|---|
| Blender | blender.org | 5.1.2 exactly | anywhere; `NANNA_BLENDER`. Its `blender.shared\usd_ms.dll` and `python313.dll` are build inputs |
| MoonRay Windows package | https://github.com/mnraker/openmoonray/releases, folder `preview_opensourcedays2025_openmoonray_vfx2023_qt_usd` | MoonRay 17.14 (`lib\cmake\Moonray-17.14.0.0`), hdMoonray 6.16, zip SHA-256 `a0717efd39837bb386bd998363bbc69c2c9fccf363a248eed06ccff04b563ed6` | its `openmoonray` folder is `NANNA_MOONRAY_PKG`. Only read |
| hdMoonray | https://github.com/mnraker/hdMoonray | commit `a71790f61810dca9376bb3d0f5580c225937f5b7` | `%NANNA_HD%\hdMoonray`. No local changes: `edit_hdmoonray.py` makes a changed copy |
| OpenUSD | https://github.com/PixarAnimationStudios/OpenUSD | tag `v25.08` (`71e038c1`) | `%NANNA_HD%\OpenUSD`. Headers only |
| oneTBB headers | https://github.com/uxlfoundation/oneTBB | tag `v2022.3.0` | `%NANNA_HD%\probe\oneTBB`. The headers `gpu\delegate\build.bat` compiles `hdNannaGpu.dll` with (the TBB of Blender's `usd_ms.dll`), and step 1's ABI check |
| TBB 2020 headers | https://github.com/uxlfoundation/oneTBB | tag `v2020.3` (`eca91f16`) | `build\hdmoonray\tbb2020\include` |
| MoonRay source | https://github.com/dreamworksanimation/moonray | commit `eef67ae992b5037943a7716cca96ed443c36dcec` | `%NANNA_HD%\src\moonray`. Kernel: the energy compensation tables (`gen_tables.py`) and the ISPC stub list (`mkstubs.py`) |
| MoonRay source, Windows fork | https://github.com/mnraker/moonray | commit `e425507090a13fc5a0da9845b6cbd6ce953b8c96` | its `lib` folder as `%NANNA_MOONRAY_INC%\moonray` (a junction: `mklink /J "%NANNA_MOONRAY_INC%\moonray" <checkout>\lib`). Kernel tests: `#include <moonray/...>` headers |
| pbrt-v4 | https://github.com/mmp/pbrt-v4, cloned with `--recursive` | commit `b0bdf461ba5dd6504b70ebf0932acc0fe7aa744f` | `%NANNA_PBRT_WORK%\pbrt-v4` |
| CPython headers | https://github.com/python/cpython | tag `v3.13.9` | `build\hdmoonray\deps\python313\Include` |
| Boost headers | `boost_1_80_0.tar.bz2`, archives.boost.io | 1.80.0 | copy the archive's `boost` folder to `build\hdmoonray\deps\boost\include\boost` |
| log4cplus headers | https://github.com/log4cplus/log4cplus | tag `REL_2_1_1` | `build\hdmoonray\deps\log4cplus` |
| Lua headers | `lua-5.4.7.tar.gz`, lua.org/ftp | 5.4.7 | `build\hdmoonray\deps\lua\include` (`src\*.h` and `lua.hpp`) |
| MSVC | Visual Studio 2022 Build Tools | 14.44 (`cl` 19.44); Blender's USD and the package were linked with it | `NANNA_VCVARS` |
| CMake, Ninja | `pip install --target %NANNA_HD%\tools cmake ninja` | 4.4.4, 1.13.2 | `%NANNA_HD%\tools` |
| CUDA Toolkit | developer.nvidia.com | 12.4 | `NANNA_CUDA` |
| OptiX SDK | developer.nvidia.com | 7.6.0 | `NANNA_OPTIX` |

The versions are the ones the current build was made and checked with. Others are untested.

## Layout

```
<repo>\build\hdmoonray     scripts; also gets deps\, tbb2020\, include\, lib\, build\, isolated\, logs\ (ignored by git)
%NANNA_HD%\                hdMoonray\  OpenUSD\  probe\oneTBB\  src\moonray\  tools\
%NANNA_MOONRAY_PKG%\       bin\  include\  lib\  plugin\  rdl2dso\ ...  (the package's openmoonray folder)
%NANNA_PBRT_WORK%\         pbrt-v4\   nk\lobes.h  nk\lobe_tables.h   build\   logs\
```

The patched pbrt sources include `../../../nk/lobes.h` from `pbrt-v4\src\pbrt`, so `nk\` must sit beside `pbrt-v4\`.

## 1. hdMoonray for Blender

Fetch the headers listed above into `build\hdmoonray\deps` and `tbb2020`. For CPython, copy `Include` and `PC\pyconfig.h.in` as
`Include\pyconfig.h` (unchanged). Then make the export lists the import libraries are built from:

```bat
cd build\hdmoonray
python -I make_def.py "%BLENDER_DIR%\blender.shared\usd_ms.dll" usd_ms.def logs\usd_ms.json
python -I make_def.py "%NANNA_MOONRAY_PKG%\bin\tbb.dll" tbb.def logs\tbb.json
python -I make_def.py "%BLENDER_DIR%\python313.dll" deps\python313\python313.def logs\python313.json
```

(`config.bat` sets `BLENDER_DIR` to the folder of `NANNA_BLENDER`.) Then:

```bat
build_hd.bat
```

It regenerates USD's configured headers with Blender's settings (`gen_pxr_h.py`), makes the changed hdMoonray copy
(`refresh_hdmoonray.py`, which applies `edit_hdmoonray.py`), builds the import libraries, compiles `abi_check\abi_sizes.cc`
under oneTBB and TBB 2020 and compares the sizes, configures `cmake_root\CMakeLists.txt` with Ninja, builds
`hydramoonray` and `hd_moonray_debug`, and checks both DLLs with `verify_built.py` (must print `OK 0`). It stops at the first failure.
`rebuild.bat` repeats only the compile and the check. `build_hd.bat` passes `-DUSD_SOURCE="%NANNA_HD%\OpenUSD"` and
`-DPACKAGE_ROOT="%NANNA_MOONRAY_PKG%"` to CMake, and stops if either variable is unset. If `ABI size check` prints `DIFFERENT`
lines, read them before loading anything in Blender (Known limits).

### Diagnostics

Not part of the build; for when a DLL won't load or the build inputs change:

- `python -I check_imports.py logs\usd_ms.json logs\tbb.json "%NANNA_MOONRAY_PKG%\bin" <out json>` checks hdMoonray's USD and
  TBB imports in the package against the exports of Blender's `usd_ms.dll` and the package's `tbb.dll` (`make_def.py`'s json files),
  with the package's USD namespace swapped for Blender's.
- `python -I audit_imports.py <dll> ...` lists the USD imports of the built DLLs that name a class holding TBB types, and every
  USD class they import: the plugin's real USD surface, for judging the ABI check's `DIFFERENT` lines.
- `cmake -DSRC="%NANNA_HD%\OpenUSD" -DOUT=<file> -P gen_pxr_h_check.cmake` writes `pxr.h` with the same values as
  `gen_pxr_h.py`, through CMake's own `configure_file`, as a cross-check.

### The runtime folder

The runtime is the package's `bin` and `rdl2dso` with the DLLs that collide with Blender's renamed, plus the two built DLLs:

```bat
python -I patch_imports.py "%NANNA_MOONRAY_PKG%" isolated
deploy.bat
python -I preload_list.py isolated ..\..\addon\preload.txt
```

`patch_imports.py` stops if `isolated` exists. `deploy.bat` copies the built DLLs and both `plugInfo.json` files and compares
them byte for byte; run it again after every rebuild. `preload_list.py` is needed only when `isolated` changes.

## 2. Kernel tests (optional)

`gpu\kernel\run_all.bat` regenerates `lobe_tables.h` from the MoonRay source, builds the CPU tests against the package's
libraries, the CUDA check and the PTX, and runs the four checks into `gpu\kernel\out`. It needs step 1's `deps`, `tbb2020` and `lib`
(`NANNA_PREP`), the MoonRay headers through `NANNA_MOONRAY_INC`, and the package's `bin` on `PATH` to run.
`build_gpu.bat` alone needs only CUDA.

## 3. nanna_gpu.exe and the hdNannaGpu delegate for Device GPU (optional)

Device GPU is two parts: `nanna_gpu.exe`, a modified pbrt-v4 that stays running as a server process, and `hdNannaGpu.dll`, a
small Hydra render delegate inside Blender that turns Hydra's meshes and camera into pbrt scenes and sends them to that server.
A fault in the server fails a frame, never Blender.

```bat
cd /d <repo>
git clone --recursive https://github.com/mmp/pbrt-v4 "%NANNA_PBRT_WORK%\pbrt-v4"
git -C "%NANNA_PBRT_WORK%\pbrt-v4" checkout b0bdf461ba5dd6504b70ebf0932acc0fe7aa744f
git -C "%NANNA_PBRT_WORK%\pbrt-v4" submodule update --init --recursive
mkdir "%NANNA_PBRT_WORK%\nk"
copy gpu\kernel\lobes.h "%NANNA_PBRT_WORK%\nk\"
copy gpu\kernel\lobe_tables.h "%NANNA_PBRT_WORK%\nk\"
cd /d "%NANNA_PBRT_WORK%"
python -I <repo>\gpu\pbrt\patch_pbrt.py
python -I <repo>\gpu\pbrt\patch_server.py
python -I <repo>\gpu\pbrt\patch_memory.py
python -I <repo>\gpu\pbrt\patch_aux.py
<repo>\gpu\pbrt\build_server.bat
cd /d <repo>
gpu\delegate\build.bat
```

`patch_pbrt.py` changes a clean checkout once (it runs `patch_f12.py` at its end); `patch_server.py` adds the server mode
(`--nanna-serve`, protocol in its docstring), `patch_memory.py` frees a scene when the next one loads, `patch_aux.py` writes albedo and
normal beside an F12 picture for Denoise. Each stops on anything it doesn't find, so run them in this order on a clean checkout.
`build_server.bat` configures once, builds `pbrt_exe` into `%NANNA_PBRT_WORK%\build` and copies it to `nanna_gpu.exe` there.
`build.bat` is the same build without the copy (`build_nanna.sh` runs it under a timeout from Git Bash); on a checkout with only
`patch_pbrt.py` applied it gives the earlier one-shot `pbrt.exe`, which the add-on no longer ships.

`gpu\delegate\build.bat` compiles `gpu\delegate\src\nanna_gpu.cpp` into `gpu\delegate\build\hdNannaGpu.dll` against step 1's
`include`, `lib\usd_ms.lib` and `deps\python313` (`NANNA_PREP`), OpenUSD's headers and the oneTBB headers (`NANNA_HD`). It links only
Blender's `usd_ms.dll` and `python313.dll`: no CUDA, no pbrt and no TBB DLL go into Blender's process.

Two runtime folders come out of this step:
- `NANNA_GPU_RUNTIME`: `nanna_gpu.exe`, `VERSION.txt`, pbrt's `LICENSE.txt` as `pbrt_LICENSE.txt`, and the MSVC 14.44 runtime:
  `msvcp140.dll`, `msvcp140_codecvt_ids.dll`, `vcruntime140.dll`, `vcruntime140_1.dll`. The CUDA runtime is linked in; OptiX comes
  with the NVIDIA driver. The add-on accepts only the `nanna_gpu.exe` whose SHA-256 is `SERVER_SHA256` in `addon\gpu_hydra.py`, with
  a `VERSION.txt` whose first line is `SERVER_VERSION` there. A new build needs both updated.
- `NANNA_GPU_HYDRA`: `bin\hdNannaGpu.dll` and a copy of `gpu\delegate\plugin` as `plugin` (the two `plugInfo.json` files).

`gpu\pbrt\export_pbrt.py` writes a Blender scene as a pbrt scene the way the earlier one-shot route did, for testing outside the add-on:
`blender --background <file.blend> --python gpu\pbrt\export_pbrt.py -- <out folder> <width>`.

## 4. Package

```bat
cd /d <repo>
python -I build\hdmoonray\build_dist.py
```

Stages `addon`, `NANNA_RUNTIME` as `runtime` and, if set, `NANNA_GPU_RUNTIME` as `runtime\gpu` and `NANNA_GPU_HYDRA` as
`runtime\gpu_hydra`, builds and validates the zip with `blender --command extension` on an empty user folder, and checks the add-on
files in it. Output: `build\hdmoonray\dist\moonray_hydra-<version>-windows-x64.zip`.

### 4b. Trim

```bat
python -I release\apply_exclude.py build\hdmoonray\dist\moonray_hydra-<version>-windows-x64.zip
```

Leaves out the MoonRay package files the add-on never loads (`release\exclude.txt`: the Qt GUI, GLEW, MoonRay's own OIDN, the
command-line tools), writing `moonray_hydra-<version>-windows-x64-trimmed.zip` beside the input. The input is only read. It checks
that every pattern matched, every kept file is byte-identical and the Hydra plugin is still there, and validates the result.

### 4c. Licence texts

```bat
python -I release\add_third_party.py build\hdmoonray\dist\moonray_hydra-<version>-windows-x64-trimmed.zip build\hdmoonray\dist\nanna_render-<version>-windows-x64.zip
```

Adds `LICENSE` (the repo root's GPL-3.0 text), `release\THIRD_PARTY.md` and `release\third_party\` (every third-party licence
text) to the add-on folder of the zip, checks every entry byte for byte and validates it.

The release file is `nanna_render-<version>-windows-x64.zip`, the output of 4c. Publish only that one: the zips of steps 4 and
4b lack the licence texts.

## Running from the source folder

For development, the add-on can run without the zip: put the repo root on `sys.path`, `import addon; addon.register()`, and set
`HDMOONRAY_ROOT` to the runtime folder (`build\hdmoonray\isolated`). Device GPU looks only in the add-on's own `runtime\gpu` and
`runtime\gpu_hydra`, so for it copy the two folders of step 3 there. An installed add-on has them already.

## Known limits

- One Blender version: the plugin is built against Blender 5.1.2's `usd_ms.dll`. After a Blender update, rebuild and update
  `BLENDER_VERSION` and `USD_MS_SHA256` in `addon\runtime.py`.
- `NannaBxDF::MeshLights` in `nanna_bxdf.inc` is a fixed count of 2, not the scene's.
- Only Windows x64 is built or tested.
- Two TBBs. Blender's `usd_ms.dll` was built with oneTBB; the plugin compiles the same USD headers against TBB 2020, the package's.
  USD classes that hold TBB types then differ in size on the two sides: the ABI check finds 12 of 13 differ. The plugin reaches the
  two it uses, `HdChangeTracker` and `HdRenderIndex`, only through exported functions (`audit_imports.py`). Equal sizes
  wouldn't prove equal layout either. Nothing has failed so far, but that is evidence, not proof.
- Two thread pools. The package's `tbb.dll` and Blender's `tbb12.dll` keep their own workers; hdMoonray asks for all threads less
  one whatever Blender is doing, so the CPU can be oversubscribed.
- The package's `UsdGeometry` and `UsdInstanceGeometry` shaders need its USD 25.05 DLLs and don't load in Blender, so
  `preload_list.py` leaves them out. An RDLA scene that references USD geometry won't work; Hydra scenes don't need them.
- `moonrayShaderDiscovery` and `moonrayShaderParser` aren't built: they use `Ndr`, which `usd_ms.dll` doesn't export.
- No timeout for a frame that hangs inside MoonRay. A stop takes effect only once MoonRay's render prep ends: 0.5 s to 10 s on
  the test scenes, 38 s on a first run with an empty texture cache.
- In plain Python, outside Blender, MoonRay's global teardown ends in heap corruption (`0xC0000374`). Inside Blender, whose
  `tbbmalloc_proxy.dll` replaces the allocator, every run has exited cleanly, but the allocator may be hiding the fault.
