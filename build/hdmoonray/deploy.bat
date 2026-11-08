@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright 2026 Kiet Ho
rem After build_hd.bat (BUILD.md, The runtime folder): copy the built DLLs and plugInfo into the isolated copy, then compare them. Honours BUILD_DIR like build_hd.bat.
setlocal
set PREP=%~dp0
set PREP=%PREP:~0,-1%
if "%BUILD_DIR%"=="" set BUILD_DIR=%PREP%\build
rem NANNA_MOONRAY_PKG: the openmoonray folder of the MoonRay Windows package (config.example.bat).
if "%NANNA_MOONRAY_PKG%"=="" echo NANNA_MOONRAY_PKG is not set: set it in config.bat, then run "call config.bat".& exit /b 2
copy /Y "%BUILD_DIR%\bin\hydramoonray.dll" "%PREP%\isolated\bin\" >nul || exit /b 1
copy /Y "%BUILD_DIR%\bin\hd_moonray_debug.dll" "%PREP%\isolated\bin\" >nul || exit /b 1
if not exist "%PREP%\isolated\plugin\pxr\hd_moonray_debug" mkdir "%PREP%\isolated\plugin\pxr\hd_moonray_debug"
copy /Y "%BUILD_DIR%\hd_moonray_debug\plugInfo.json" "%PREP%\isolated\plugin\pxr\hd_moonray_debug\" >nul || exit /b 1
copy /Y "%NANNA_MOONRAY_PKG%\plugin\pxr\plugInfo.json" "%PREP%\isolated\plugin\pxr\" >nul || exit /b 1
fc /b "%BUILD_DIR%\bin\hydramoonray.dll" "%PREP%\isolated\bin\hydramoonray.dll" >nul || exit /b 1
fc /b "%BUILD_DIR%\bin\hd_moonray_debug.dll" "%PREP%\isolated\bin\hd_moonray_debug.dll" >nul || exit /b 1
echo Deployed.
