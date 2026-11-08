@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright 2026 Kiet Ho
rem Builds hydramoonray and hd_moonray_debug from a plain cmd.exe window, after "call config.bat" (BUILD.md).
rem Stops at the first failure. Regenerates what it can, so the only inputs are the clones, deps\ and the .def files.
rem Set BUILD_DIR to build somewhere other than build\ beside this script. Deployment is separate: run deploy.bat afterwards.
setlocal
set PREP=%~dp0
set PREP=%PREP:~0,-1%
rem NANNA_HD: the folder with OpenUSD\, hdMoonray\, tools\ and probe\oneTBB\. NANNA_MOONRAY_PKG: the package's openmoonray folder.
if "%NANNA_HD%"=="" echo NANNA_HD is not set: set it in config.bat, then run "call config.bat".& exit /b 2
if "%NANNA_MOONRAY_PKG%"=="" echo NANNA_MOONRAY_PKG is not set: set it in config.bat, then run "call config.bat".& exit /b 2
set "HD=%NANNA_HD%"
set CMAKE=%HD%\tools\cmake\data\bin\cmake.exe
set NINJA=%HD%\tools\bin\ninja.exe
set VSWHERE=C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe
if "%BUILD_DIR%"=="" set BUILD_DIR=%PREP%\build

rem NANNA_VCVARS: the vcvarsall.bat to use, as in rebuild.bat. Unset: the latest Visual Studio with the x64 tools, found by vswhere.
set "VCVARS=%NANNA_VCVARS%"
if not "%VCVARS%"=="" goto vcvars
if not exist "%VSWHERE%" goto nocompiler
set VSROOT=
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -products * -latest -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set VSROOT=%%i
if "%VSROOT%"=="" goto nocompiler
set "VCVARS=%VSROOT%\VC\Auxiliary\Build\vcvarsall.bat"
:vcvars
if not exist "%VCVARS%" goto nocompiler
call "%VCVARS%" x64 -vcvars_ver=14.44
if errorlevel 1 goto failed
where cl >nul 2>nul
if errorlevel 1 goto nocompiler

echo === generated headers and edited source
python -I "%PREP%\gen_pxr_h.py" "%HD%\OpenUSD" "%PREP%\include"
if errorlevel 1 goto failed
python -I "%PREP%\refresh_hdmoonray.py" "%HD%\hdMoonray" "%PREP%\hdMoonray_src"
if errorlevel 1 goto failed

echo === import libraries
if not exist "%PREP%\lib" mkdir "%PREP%\lib"
if not exist "%PREP%\deps\python313\libs" mkdir "%PREP%\deps\python313\libs"
lib /nologo /machine:x64 /def:"%PREP%\usd_ms.def" /out:"%PREP%\lib\usd_ms.lib"
if errorlevel 1 goto failed
lib /nologo /machine:x64 /def:"%PREP%\tbb.def" /out:"%PREP%\lib\tbb.lib"
if errorlevel 1 goto failed
lib /nologo /machine:x64 /def:"%PREP%\deps\python313\python313.def" /out:"%PREP%\deps\python313\libs\python313.lib"
if errorlevel 1 goto failed
set LIB=%PREP%\deps\python313\libs;%LIB%

echo === ABI size check: the same USD headers under oneTBB and TBB 2020
set ABIFLAGS=/nologo /c /std:c++17 /EHsc /Zc:__cplusplus /Zc:inline- /bigobj /DNOMINMAX /DWIN32_LEAN_AND_MEAN /D__TBB_NO_IMPLICIT_LINKAGE=1 /D__TBBMALLOC_NO_IMPLICIT_LINKAGE=1
set ABIINC=/I"%PREP%\include" /I"%HD%\OpenUSD" /I"%PREP%\deps\boost\include" /I"%PREP%\deps\python313\Include"
if not exist "%PREP%\abi_check\out" mkdir "%PREP%\abi_check\out"
cl %ABIFLAGS% %ABIINC% /I"%HD%\probe\oneTBB\include" /Fo"%PREP%\abi_check\out\onetbb.obj" "%PREP%\abi_check\abi_sizes.cc"
if errorlevel 1 goto failed
cl %ABIFLAGS% %ABIINC% /I"%PREP%\tbb2020\include" /Fo"%PREP%\abi_check\out\tbb2020.obj" "%PREP%\abi_check\abi_sizes.cc"
if errorlevel 1 goto failed
python -I "%PREP%\abi_check\abi_compare.py" "%PREP%\abi_check\out\onetbb.obj" "%PREP%\abi_check\out\tbb2020.obj" "%PREP%\abi_check\out\abi_asserts.h"
if errorlevel 2 goto failed
if errorlevel 1 echo Sizes differ: read the DIFFERENT lines above before loading anything in Blender (BUILD.md, Known limits).

echo === configure and build
"%CMAKE%" -S "%PREP%\cmake_root" -B "%BUILD_DIR%" -G Ninja -DCMAKE_MAKE_PROGRAM="%NINJA%" -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=cl -DCMAKE_CXX_COMPILER=cl -DBOOST_INCLUDE="%PREP%\deps\boost\include" -DLOG4CPLUS_INCLUDE="%PREP%\deps\log4cplus\include" -DLUA_INCLUDE="%PREP%\deps\lua\include" -DPYTHON_ROOT="%PREP%\deps\python313" -DUSD_SOURCE="%HD%\OpenUSD" -DPACKAGE_ROOT="%NANNA_MOONRAY_PKG%"
if errorlevel 1 goto failed
"%CMAKE%" --build "%BUILD_DIR%" --target hydramoonray hd_moonray_debug
if not "%errorlevel%"=="0" goto failed

echo === check the DLLs before anything loads them
python -I "%PREP%\verify_built.py" "%PREP%\logs\usd_ms.json" "%PREP%\logs\tbb.json" "%BUILD_DIR%\bin\hydramoonray.dll" "%BUILD_DIR%\bin\hd_moonray_debug.dll"
if errorlevel 1 goto failed
echo Built and verified. Next: deploy.bat.
exit /b 0

:nocompiler
echo No MSVC found. Install Visual Studio 2022 Build Tools with MSVC 14.44 (BUILD.md, Inputs), or set NANNA_VCVARS in config.bat.
exit /b 3

:failed
echo Build stopped at the step above.
exit /b 1
