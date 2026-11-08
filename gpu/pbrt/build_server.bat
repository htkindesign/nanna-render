@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright 2026 Kiet Ho
rem Builds the Device GPU server nanna_gpu.exe, a modified pbrt-v4, from a pbrt-v4 checkout changed by patch_pbrt.py (which runs
rem patch_f12.py), then patch_server.py, patch_memory.py and patch_aux.py, in that order (each run from the folder holding pbrt-v4\).
rem Same configure as build.bat; pbrt's build output pbrt.exe is copied to nanna_gpu.exe, the name the add-on ships and checks.
rem Roots (config.example.bat at the top of the repo): NANNA_PBRT_WORK holds pbrt-v4\ and gets build\; NANNA_HD\tools has cmake and
rem ninja; NANNA_OPTIX, NANNA_CUDA, NANNA_VCVARS (standard install folders by default). Optional: NANNA_PBRT_DEPS, a folder with
rem imath-src\ and deflate-src\ used instead of fetching them; NANNA_JOBS, parallel jobs (default 8).
setlocal
if "%NANNA_HD%"=="" echo NANNA_HD is not set: set it in config.bat, then run "call config.bat".& exit /b 2
if "%NANNA_PBRT_WORK%"=="" echo NANNA_PBRT_WORK is not set: set it in config.bat, then run "call config.bat".& exit /b 2
if "%NANNA_OPTIX%"=="" set "NANNA_OPTIX=C:\ProgramData\NVIDIA Corporation\OptiX SDK 7.6.0"
if "%NANNA_CUDA%"=="" set "NANNA_CUDA=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4"
if "%NANNA_VCVARS%"=="" set "NANNA_VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"
if "%NANNA_JOBS%"=="" set "NANNA_JOBS=8"
set "HD=%NANNA_HD%"
set "LP=%NANNA_PBRT_WORK%"
set "OPTIX=%NANNA_OPTIX%"
set "CUDA_PATH=%NANNA_CUDA%"
set "DEPS="
if not "%NANNA_PBRT_DEPS%"=="" set DEPS=-DFETCHCONTENT_FULLY_DISCONNECTED=ON "-DFETCHCONTENT_SOURCE_DIR_IMATH=%NANNA_PBRT_DEPS%\imath-src" "-DFETCHCONTENT_SOURCE_DIR_DEFLATE=%NANNA_PBRT_DEPS%\deflate-src"
call "%NANNA_VCVARS%" x64 -vcvars_ver=14.44 >nul
set PATH=%CUDA_PATH%\bin;%PATH%
if not exist "%LP%\build\build.ninja" "%HD%\tools\cmake\data\bin\cmake.exe" -S "%LP%\pbrt-v4" -B "%LP%\build" -G Ninja -DCMAKE_MAKE_PROGRAM="%HD%\tools\bin\ninja.exe" -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=cl -DCMAKE_CXX_COMPILER=cl -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DPBRT_OPTIX_PATH="%OPTIX%" -DPBRT_OPTIX7_PATH="%OPTIX%" -DPBRT_BUILD_NATIVE_EXECUTABLE=OFF -DCMAKE_DISABLE_FIND_PACKAGE_Git=ON %DEPS%
if errorlevel 1 exit /b 1
"%HD%\tools\cmake\data\bin\cmake.exe" --build "%LP%\build" --target pbrt_exe -j %NANNA_JOBS%
if errorlevel 1 exit /b 1
copy /y "%LP%\build\pbrt.exe" "%LP%\build\nanna_gpu.exe" >nul
