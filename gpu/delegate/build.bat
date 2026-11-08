@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright 2026 Kiet Ho
rem Builds hdNannaGpu.dll, the Device GPU Hydra delegate (src\nanna_gpu.cpp), against Blender 5.1.2's usd_ms.dll, into build\ beside
rem this script. Inputs are BUILD.md step 1's: NANNA_PREP\include (generated pxr.h, work\impl.h, arch\attributes.h), NANNA_PREP\lib\usd_ms.lib,
rem NANNA_PREP\deps\python313 (Include, libs); NANNA_HD\OpenUSD (v25.08 headers) and NANNA_HD\probe\oneTBB\include (the oneTBB usd_ms.dll
rem was built with). All read only. The runtime folder is bin\hdNannaGpu.dll beside plugin\ (this folder's plugin\, plugInfo.json files).
setlocal
if "%NANNA_HD%"=="" echo NANNA_HD is not set: set it in config.bat, then run "call config.bat".& exit /b 2
if "%NANNA_PREP%"=="" echo NANNA_PREP is not set: set it in config.bat, then run "call config.bat".& exit /b 2
if "%NANNA_VCVARS%"=="" set "NANNA_VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"
call "%NANNA_VCVARS%" x64 -vcvars_ver=14.44 >nul || exit /b 1
set "D=%~dp0"
if not exist "%D%build" mkdir "%D%build"
cd /d "%D%build"
cl /nologo /c /std:c++17 /O2 /MD /EHsc /Zc:inline- /Zc:__cplusplus /bigobj /W3 /wd4244 /wd4267 /wd4305 /wd4003 ^
  /DNOMINMAX /DWIN32_LEAN_AND_MEAN /D_CRT_SECURE_NO_WARNINGS /D__TBB_NO_IMPLICIT_LINKAGE=1 /D__TBBMALLOC_NO_IMPLICIT_LINKAGE=1 ^
  /I"%NANNA_PREP%\include" /I"%NANNA_HD%\OpenUSD" /I"%NANNA_HD%\probe\oneTBB\include" /I"%NANNA_PREP%\deps\python313\Include" ^
  "%D%src\nanna_gpu.cpp" /Fonanna_gpu.obj || exit /b 2
link /nologo /DLL /OUT:hdNannaGpu.dll nanna_gpu.obj /LIBPATH:"%NANNA_PREP%\lib" /LIBPATH:"%NANNA_PREP%\deps\python313\libs" usd_ms.lib || exit /b 3
echo BUILD OK
