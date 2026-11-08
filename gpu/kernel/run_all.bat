@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright 2026 Kiet Ho
rem Rebuilds everything, each build in its own setlocal because env.bat grows PATH, and runs the four checks. Results land in out\.
rem Paths: NANNA_HD (its src\moonray is the MoonRay source checkout) and NANNA_MOONRAY_PKG, see env.bat.
set "K=%~dp0"
set "K=%K:~0,-1%"
if "%NANNA_HD%"=="" echo NANNA_HD is not set: set it in config.bat, then run "call config.bat".& exit /b 2
if "%NANNA_MOONRAY_PKG%"=="" echo NANNA_MOONRAY_PKG is not set: set it in config.bat, then run "call config.bat".& exit /b 2
cd /d "%K%"
set "PATH=%NANNA_MOONRAY_PKG%\bin;%PATH%"
python gen_tables.py "%NANNA_HD%\src\moonray\lib\rendering\shading\bsdf\cook_torrance\energy_compensation" lobe_tables.h || exit /b 1
python mkstubs.py || exit /b 1
setlocal
call "%K%\build_test.bat" "" "" || exit /b 1
endlocal
setlocal
call "%K%\build_test.bat" _sse /DNK_TEST_SSE_RSQRT || exit /b 1
endlocal
setlocal
call "%K%\build_mean.bat" || exit /b 1
endlocal
setlocal
call "%K%\build_gpu.bat" || exit /b 1
endlocal
cd /d "%K%"
out\test_lobes.exe > out\test_lobes.txt 2>&1 || exit /b 1
out\test_lobes_sse.exe > out\test_lobes_sse.txt 2>&1 || exit /b 1
out\test_mean.exe > out\test_mean.txt 2>&1 || exit /b 1
out\gpu_lobes.exe > out\gpu_lobes.txt 2>&1 || exit /b 1
echo done
