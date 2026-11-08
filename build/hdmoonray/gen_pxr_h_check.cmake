# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
# Same values as gen_pxr_h.py, through CMake's own configure_file, as a cross-check.
# cmake -DSRC=<OpenUSD dir> -DOUT=<output file> -P gen_pxr_h_check.cmake
set(PXR_MAJOR_VERSION 0)
set(PXR_MINOR_VERSION 25)
set(PXR_PATCH_VERSION 8)
set(PXR_VERSION 2508)
set(PXR_USE_NAMESPACES 1)
set(PXR_EXTERNAL_NAMESPACE pxr)
set(PXR_INTERNAL_NAMESPACE pxrBlender_v25_08)
set(PXR_PYTHON_SUPPORT_ENABLED 1)
set(PXR_PREFER_SAFETY_OVER_SPEED 1)
configure_file(${SRC}/pxr/pxr.h.in ${OUT} @ONLY)
