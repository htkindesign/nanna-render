@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright 2026 Kiet Ho
rem Configures and builds pbrt.exe from the patched pbrt-v4 checkout. -j 8 leaves room for other work on the machine.
rem Roots (config.example.bat at the top of the repo): NANNA_PBRT_WORK holds pbrt-v4\ (the checkout patch_pbrt.py changed) and gets build\;
rem NANNA_HD\tools has cmake and ninja; NANNA_OPTIX, NANNA_CUDA, NANNA_VCVARS. The roots must be set; the toolchain has the standard install folders as defaults.
setlocal
if "%NANNA_HD%"=="" echo NANNA_HD is not set: set it in config.bat, then run "call config.bat".& exit /b 2
if "%NANNA_PBRT_WORK%"=="" echo NANNA_PBRT_WORK is not set: set it in config.bat, then run "call config.bat".& exit /b 2
if "%NANNA_OPTIX%"=="" set "NANNA_OPTIX=C:\ProgramData\NVIDIA Corporation\OptiX SDK 7.6.0"
if "%NANNA_CUDA%"=="" set "NANNA_CUDA=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4"
if "%NANNA_VCVARS%"=="" set "NANNA_VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"
set "HD=%NANNA_HD%"
set "SPIKE=%NANNA_PBRT_WORK%"
set "OPTIX=%NANNA_OPTIX%"
set "CUDA_PATH=%NANNA_CUDA%"
call "%NANNA_VCVARS%" x64 -vcvars_ver=14.44 >nul
set PATH=%CUDA_PATH%\bin;%PATH%
if not exist "%SPIKE%\build\build.ninja" "%HD%\tools\cmake\data\bin\cmake.exe" -S "%SPIKE%\pbrt-v4" -B "%SPIKE%\build" -G Ninja -DCMAKE_MAKE_PROGRAM="%HD%\tools\bin\ninja.exe" -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=cl -DCMAKE_CXX_COMPILER=cl -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DPBRT_OPTIX_PATH="%OPTIX%" -DPBRT_OPTIX7_PATH="%OPTIX%" -DPBRT_BUILD_NATIVE_EXECUTABLE=OFF
if errorlevel 1 exit /b 1
if "%1"=="configure" exit /b 0
"%HD%\tools\cmake\data\bin\cmake.exe" --build "%SPIKE%\build" --target pbrt_exe -j 8
