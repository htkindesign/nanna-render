@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright 2026 Kiet Ho
rem Builds the CUDA check and the OptiX-route PTX, from the same lobes.h. Paths: env.bat.
call "%~dp0env.bat"
cd /d "%~dp0"
if not exist out mkdir out
nvcc -m64 -O2 -std=c++17 --gpu-architecture sm_89 -I. -D_USE_MATH_DEFINES -o out\gpu_lobes.exe gpu_lobes.cu
if errorlevel 1 exit /b 1
rem PTX compile only, with the flags MoonRay's XPU build uses for its PTX
nvcc --ptx -m64 --gpu-architecture sm_61 -std=c++11 --use_fast_math -D_USE_MATH_DEFINES -I. -o out\lobes_optix.ptx gpu_lobes_ptx.cu
exit /b %errorlevel%
