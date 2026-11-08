@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright 2026 Kiet Ho
rem The one place for the build's roots. Copy to config.bat (ignored by git), edit, and run "call config.bat" in the cmd window
rem before any build script. A script stops if a root it reads is unset; only the toolchain and packaging lines have defaults.
rem See BUILD.md for what goes in each folder.

rem Work folder for the hdMoonray build: OpenUSD\ (v25.08), hdMoonray\ (mnraker fork, a71790f), tools\ (cmake, ninja),
rem probe\oneTBB\ (v2022.3.0 headers), src\moonray\ (MoonRay source, eef67ae).
set "NANNA_HD=D:\nanna-work"

rem The openmoonray folder of the MoonRay Windows package.
set "NANNA_MOONRAY_PKG=D:\nanna-work\moonraker\openmoonray"

rem Where build\hdmoonray's outputs and third-party headers live, for the kernel tests (deps\, tbb2020\, lib\).
rem In a clone, build\hdmoonray itself.
set "NANNA_PREP=%~dp0build\hdmoonray"

rem Kernel tests: a folder whose moonray subfolder is a junction to the MoonRay source's lib folder.
set "NANNA_MOONRAY_INC=D:\nanna-work\moonray_inc"

rem Folder holding the pbrt-v4\ checkout, nk\ (lobes.h, lobe_tables.h) and build\.
set "NANNA_PBRT_WORK=D:\nanna-work\pbrt"

rem Toolchain. Defaults are the standard install folders.
set "NANNA_VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"
set "NANNA_CUDA=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4"
set "NANNA_OPTIX=C:\ProgramData\NVIDIA Corporation\OptiX SDK 7.6.0"

rem Packaging (build_dist.py) and Blender. NANNA_ADDON is required by build_dist.py.
set "NANNA_BLENDER=C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
for %%i in ("%NANNA_BLENDER%") do set "BLENDER_DIR=%%~dpi."
set "NANNA_ADDON=%~dp0addon"
rem NANNA_RUNTIME: default build\hdmoonray\isolated. NANNA_DIST: default build\hdmoonray\dist
rem NANNA_GPU_RUNTIME: nanna_gpu.exe's folder; NANNA_GPU_HYDRA: the delegate's runtime (bin\hdNannaGpu.dll, plugin\). Both unset = no Device GPU.
set "NANNA_GPU_RUNTIME="
set "NANNA_GPU_HYDRA="
rem Optional for build_server.bat: NANNA_PBRT_DEPS (imath-src\ and deflate-src\ instead of fetching them), NANNA_JOBS (default 8).

rem Running the add-on from its source folder, without installing the zip (development only; an installed add-on uses its runtime folder):
rem   HDMOONRAY_ROOT  the MoonRay runtime (build\hdmoonray\isolated)
rem   Device GPU: copy NANNA_GPU_RUNTIME and NANNA_GPU_HYDRA into addon\runtime\gpu and addon\runtime\gpu_hydra
rem export_pbrt.py finds the add-on by itself; NANNA_ADDON_PARENT overrides it (the folder that holds addon\).
