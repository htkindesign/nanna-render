# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
# Builds only lib/hydramoonray and plugin/hd_moonray_debug against Blender's usd_ms.dll and the MoonRay package.
#
# This stands in for hdMoonray's root CMakeLists.txt, which requires Arras, Moonshine and the package's exported CMake
# targets. Those targets can't be used here: they hold paths from the machine that built the package
# (C:/Users/bober/...), and they carry a USD and TBB setup that doesn't match Blender's.
#
# Built with MSVC 14.44 and Ninja by build_hd.bat. Configure with -DPREP_DRY_RUN=ON needs no compiler, and checks everything up
# to generation.
#
# At configure time this includes hdMoonray's own cmake/ modules (HdMoonrayCompile*.cmake, HdMoonrayLinkOptions.cmake) from
# HDMOONRAY_SOURCE; they are hdMoonray's, Copyright DreamWorks Animation LLC, Apache-2.0, and are not part of this repo.

cmake_minimum_required(VERSION 3.23.1)

option(PREP_DRY_RUN "Configure without a compiler" OFF)
if(PREP_DRY_RUN)
    project(HdMoonray VERSION 6.16.0 LANGUAGES NONE)
else()
    project(HdMoonray VERSION 6.16.0 LANGUAGES C CXX)
    if(NOT MSVC_VERSION GREATER_EQUAL 1940)
        message(FATAL_ERROR "Expected MSVC 14.4x, got ${MSVC_VERSION}: usd_ms.dll and the package were linked by 14.44")
    endif()
endif()

set(PACKAGE_NAME hdMoonray)
get_filename_component(PREP "${CMAKE_CURRENT_SOURCE_DIR}/.." ABSOLUTE)
set(PACKAGE_ROOT "" CACHE PATH "The MoonRay package's openmoonray folder (NANNA_MOONRAY_PKG), read only")
set(USD_SOURCE "" CACHE PATH "OpenUSD v25.08 clone (NANNA_HD/OpenUSD)")
set(TBB_INCLUDE "${PREP}/tbb2020/include" CACHE PATH "TBB 2020.3 headers")
set(HDMOONRAY_SOURCE "${PREP}/hdMoonray_src" CACHE PATH "Edited copy of hdMoonray")
set(BOOST_INCLUDE "" CACHE PATH "Boost headers")
set(LOG4CPLUS_INCLUDE "" CACHE PATH "log4cplus 2.1.1 headers")
set(PYTHON_ROOT "" CACHE PATH "Python 3.13 install (Include, libs)")
set(LUA_INCLUDE "" CACHE PATH "Lua 5.4.7 headers: scene_rdl2/scene/rdl2/AsciiReader.h includes lua.hpp")

foreach(var BOOST_INCLUDE LOG4CPLUS_INCLUDE LUA_INCLUDE PYTHON_ROOT)
    if(NOT ${var} AND NOT PREP_DRY_RUN)
        message(FATAL_ERROR "Set -D${var}=...")
    endif()
endforeach()
foreach(var USD_SOURCE PACKAGE_ROOT)  # needed by the dry run too: the checks below read them
    if(NOT ${var})
        message(FATAL_ERROR "Set -D${var}=... (build_hd.bat passes it from config.bat)")
    endif()
endforeach()
foreach(path "${USD_SOURCE}/pxr/imaging/hd/renderDelegate.h" "${TBB_INCLUDE}/tbb/tbb_machine.h"
             "${PREP}/include/pxr/pxr.h" "${PREP}/include/pxr/base/work/impl.h"
             "${HDMOONRAY_SOURCE}/lib/hydramoonray/CMakeLists.txt" "${PACKAGE_ROOT}/lib/scene_rdl2.lib")
    if(NOT EXISTS "${path}")
        message(FATAL_ERROR "Missing ${path}")
    endif()
endforeach()

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# hdMoonray has no dllexport; the package's hydramoonray.dll has 1307 exports, so it was built this way.
set(CMAKE_WINDOWS_EXPORT_ALL_SYMBOLS ON)
set(BUILD_SHARED_LIBS ON)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# Definitions the package exports on its targets, in lib/cmake. __cdecl= comes from scene_rdl2.
set(GLOBAL_COMPILE_DEFINITIONS
    PLATFORM_WINDOWS __AVX__ UNICODE OPENVDB_USE_BLOSC __TBB_SUPPORTS_WORKERS_WAITING_IN_TERMINATE "__cdecl=")
# The package ships an import lib only for what we name below, so TBB mustn't auto-link anything else.
list(APPEND GLOBAL_COMPILE_DEFINITIONS __TBB_NO_IMPLICIT_LINKAGE=1 __TBBMALLOC_NO_IMPLICIT_LINKAGE=1)
# USD's cmake/defaults/msvcdefaults.cmake.
list(APPEND GLOBAL_COMPILE_DEFINITIONS _CRT_SECURE_NO_WARNINGS _SCL_SECURE_NO_WARNINGS WIN32_LEAN_AND_MEAN)
if(NOT PREP_DRY_RUN)
    add_compile_options(/EHsc /Zc:rvalueCast /Zc:strictStrings /Zc:__cplusplus /bigobj /W3 /arch:AVX2 /MP
        /wd4003 /wd4244 /wd4305 /wd4267 /wd4506 /wd4091 /wd4273 /wd4180 /wd4334)
    # /Zc:inline strips the arch_ctor symbols TF_REGISTRY_FUNCTION relies on, so the plugin would never register.
    add_compile_options(/Zc:inline-)
endif()

# Order matters: the generated headers come first, so nothing finds a pxr.h from elsewhere.
add_library(hdbuild_headers INTERFACE)
target_include_directories(hdbuild_headers SYSTEM INTERFACE
    "${PREP}/include" "${USD_SOURCE}" "${PACKAGE_ROOT}/include" "${TBB_INCLUDE}" "${BOOST_INCLUDE}" "${LOG4CPLUS_INCLUDE}" "${LUA_INCLUDE}")

# Import libraries generated from the parsed export tables.
add_library(usd_ms SHARED IMPORTED)
set_target_properties(usd_ms PROPERTIES IMPORTED_IMPLIB "${PREP}/lib/usd_ms.lib")
add_library(hd INTERFACE)
target_link_libraries(hd INTERFACE usd_ms hdbuild_headers)
add_library(usdImaging INTERFACE)
target_link_libraries(usdImaging INTERFACE usd_ms)

add_library(TBB::tbb SHARED IMPORTED)
set_target_properties(TBB::tbb PROPERTIES IMPORTED_IMPLIB "${PREP}/lib/tbb.lib")

add_library(Python::Module INTERFACE IMPORTED)
set_target_properties(Python::Module PROPERTIES
    INTERFACE_INCLUDE_DIRECTORIES "${PYTHON_ROOT}/Include"
    INTERFACE_LINK_LIBRARIES "${PYTHON_ROOT}/libs/python313.lib")

function(package_lib target file)
    add_library(${target} SHARED IMPORTED)
    set_target_properties(${target} PROPERTIES IMPORTED_IMPLIB "${PACKAGE_ROOT}/lib/${file}.lib")
    if(ARGN)
        target_link_libraries(${target} INTERFACE ${ARGN})
    endif()
endfunction()

# Links only what the package's own builds of these DLLs import. Linking a library that pulls in embree, OpenEXR, OIIO
# or OpenVDB directly would bring a colliding name into the import table.
add_library(package_ispc INTERFACE)
target_link_libraries(package_ispc INTERFACE "${PACKAGE_ROOT}/lib/fb_util_ispc.lib" "${PACKAGE_ROOT}/lib/math_ispc.lib")
add_library(SceneRdl2::common_except INTERFACE IMPORTED)
package_lib(SceneRdl2::render_logging render_logging)
package_lib(SceneRdl2::common_math common_math)
package_lib(SceneRdl2::common_fb_util common_fb_util package_ispc)
package_lib(SceneRdl2::scene_rdl2 scene_rdl2)
package_lib(Moonray::rendering_mcrt_common rendering_mcrt_common)
package_lib(Moonray::rendering_bvh_shading rendering_bvh_shading)
package_lib(Moonray::rendering_shading rendering_shading)
package_lib(Moonray::rendering_rndr rendering_rndr Moonray::rendering_shading Moonray::rendering_bvh_shading)

# hdMoonray's own option helpers, then the two targets.
list(APPEND CMAKE_MODULE_PATH "${HDMOONRAY_SOURCE}/cmake")
include(HdMoonrayCompileDefinitions)
include(HdMoonrayCompileFeatures)
include(HdMoonrayCompileOptions)
include(HdMoonrayLinkOptions)
function(SConscript_Stub)
endfunction()

add_subdirectory("${HDMOONRAY_SOURCE}/lib/hydramoonray" hydramoonray)
add_subdirectory("${HDMOONRAY_SOURCE}/plugin/hd_moonray_debug" hd_moonray_debug)

# Without this TF_REGISTRY_FUNCTION files the plugin under the literal text MFB_ALT_PACKAGE_NAME.
target_compile_definitions(hd_moonray_debug PRIVATE MFB_PACKAGE_NAME=hd_moonray_debug MFB_ALT_PACKAGE_NAME=hd_moonray_debug)

if(PREP_DRY_RUN)  # no compiler, so nothing can pick a linker language
    set_target_properties(hydramoonray hd_moonray_debug PROPERTIES LINKER_LANGUAGE CXX)
    message(STATUS "Dry run: USD ${USD_SOURCE}, TBB ${TBB_INCLUDE}, hdMoonray ${HDMOONRAY_SOURCE}")
endif()
