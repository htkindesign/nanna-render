@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright 2026 Kiet Ho
rem Rebuilds the two targets only, then runs verify_built. Skips import libs and the ABI check that build_hd.bat runs.
setlocal
set PREP=%~dp0
set PREP=%PREP:~0,-1%
rem NANNA_HD: the folder with OpenUSD\, hdMoonray\, tools\ and probe\oneTBB\ (config.example.bat).
if "%NANNA_HD%"=="" echo NANNA_HD is not set: set it in config.bat, then run "call config.bat".& exit /b 2
set "HD=%NANNA_HD%"
if "%NANNA_VCVARS%"=="" set "NANNA_VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"
call "%NANNA_VCVARS%" x64 -vcvars_ver=14.44
set LIB=%PREP%\deps\python313\libs;%LIB%
"%HD%\tools\cmake\data\bin\cmake.exe" --build "%PREP%\build" --target hydramoonray hd_moonray_debug
if not "%errorlevel%"=="0" exit /b 1
python -I "%PREP%\verify_built.py" "%PREP%\logs\usd_ms.json" "%PREP%\logs\tbb.json" "%PREP%\build\bin\hydramoonray.dll" "%PREP%\build\bin\hd_moonray_debug.dll"
