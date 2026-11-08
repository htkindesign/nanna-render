@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright 2026 Kiet Ho
rem Paths and toolchain for the kernel builds: MSVC x64, the cmake and ninja of NANNA_HD\tools, and CUDA.
rem Every root is a variable; config.example.bat at the top of the repo lists them. The scripts that read a root stop if it is unset;
rem the toolchain has the standard install folders as defaults.
if "%NANNA_VCVARS%"=="" set "NANNA_VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"
if "%NANNA_CUDA%"=="" set "NANNA_CUDA=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4"
call "%NANNA_VCVARS%" x64 >nul
set "PYTHONPATH=%NANNA_HD%\tools"
set "PATH=%NANNA_HD%\tools\cmake\data\bin;%NANNA_HD%\tools\bin;%NANNA_CUDA%\bin;%PATH%"
