@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright 2026 Kiet Ho
rem Builds and links test_lobes.exe against the package's MoonRay libraries.
rem Flags follow the MoonRay XPU build. NANNA_PREP is only read, for third-party headers. Paths: env.bat.
if "%NANNA_MOONRAY_PKG%"=="" echo NANNA_MOONRAY_PKG is not set: set it in config.bat, then run "call config.bat".& exit /b 2
if "%NANNA_PREP%"=="" echo NANNA_PREP is not set: set it in config.bat, then run "call config.bat".& exit /b 2
if "%NANNA_MOONRAY_INC%"=="" echo NANNA_MOONRAY_INC is not set: set it in config.bat, then run "call config.bat".& exit /b 2
call "%~dp0env.bat"
set "PKG=%NANNA_MOONRAY_PKG%"
set "PREP=%NANNA_PREP%"
cd /d "%~dp0"
if not exist out mkdir out
cl /nologo /std:c++17 /O2 /EHsc /MD /Zc:__cplusplus /bigobj /arch:AVX2 /W1 ^
   /wd4293 /wd4083 /wd4616 /wd4003 /wd4244 /wd4305 /wd4267 ^
   /DNOMINMAX /DPLATFORM_WINDOWS /D_USE_MATH_DEFINES /DTBB_SUPPRESS_DEPRECATED_MESSAGES=1 /DBOOST_ALL_NO_LIB /D__AVX__ /D_CRT_SECURE_NO_WARNINGS ^
   /D__TBB_NO_IMPLICIT_LINKAGE=1 /D__TBBMALLOC_NO_IMPLICIT_LINKAGE=1 ^
   /I. /Iout\stubs /I"%PKG%\include" /I"%NANNA_MOONRAY_INC%" /Ishim ^
   /I"%PREP%\tbb2020\include" /I"%PREP%\deps\boost\include" /I"%PREP%\deps\log4cplus\include" /I"%PREP%\deps\lua\include" ^
   /Foout\ /Feout\test_lobes%~1.exe %~2 test_lobes.cpp ^
   /link /LIBPATH:"%PKG%\lib" /LIBPATH:"%PREP%\lib" rendering_shading.lib scene_rdl2.lib common_math.lib ^
   render_util.lib common_platform.lib render_logging.lib
exit /b %errorlevel%
