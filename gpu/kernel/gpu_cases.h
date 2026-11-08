// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Kiet Ho
// Binary test cases shared by test_lobes.cpp (writes) and gpu_lobes.cu (reads).
#pragma once
#include "lobes.h"

#define GPU_MAGIC 0x4E4B3031

struct GpuHeader { int magic, lobeSize, caseSize, configs, cases; };

struct GpuCase {
    int       config;
    int       isSample;
    nk::Slice slice;
    nk::Vec3  wi;            // eval input
    float     r1, r2;        // sample input
    nk::Col   refF;          // MoonRay
    float     refPdf;
    nk::Vec3  refWi;
    nk::Col   cpuF;          // lobes.h on the CPU
    float     cpuPdf;
    nk::Vec3  cpuWi;
};

struct GpuResult { nk::Col f; float pdf; nk::Vec3 wi; };

// One case, usable on host and device.
NK_FN GpuResult run_case(const nk::LobeTables &t, const nk::LayeredLobe &l, const GpuCase &c)
{
    GpuResult r;
    r.wi = nk::v3(0.0f, 0.0f, 0.0f);
    r.pdf = 0.0f;
    if (c.isSample) r.f = nk::layered_sample(t, l, c.slice, c.r1, c.r2, r.wi, r.pdf);
    else            r.f = nk::layered_eval(t, l, c.slice, c.wi, &r.pdf);
    return r;
}
