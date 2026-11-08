// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Kiet Ho
// Compile-only proof for the OptiX route: lobes.h goes through the PTX build of
// MoonRay XPU build (its PTX flags). Device entry point with no STL and no host calls.
#include "lobes.h"
#include "gpu_cases.h"

__global__ void evalOne(nk::LobeTables t, const nk::LayeredLobe *l, const GpuCase *c, GpuResult *out)
{
    *out = run_case(t, *l, *c);
}
