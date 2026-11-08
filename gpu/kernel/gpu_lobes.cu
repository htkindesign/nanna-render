// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Kiet Ho
// Tiny CUDA check: runs lobes.h on the GPU over out\cases.bin and compares to the
// same code on the CPU and to MoonRay. Tables travel in device buffers, as a real
// kernel would hold them. Short by design, one batch of a few hundred thousand cases.
#include <cuda_runtime.h>
#include <stdio.h>
#include <stdlib.h>
#include <vector>
#include <algorithm>
#include <chrono>

#include "lobes.h"
#include "gpu_cases.h"
#include "lobe_tables.h"

#define CK(x) do { cudaError_t e_ = (x); if (e_ != cudaSuccess) { printf("CUDA error %s line %d\n", cudaGetErrorString(e_), __LINE__); exit(2); } } while (0)

__global__ void runCases(nk::LobeTables t, const nk::LayeredLobe *configs, const GpuCase *cases, GpuResult *out, int n)
{
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) out[i] = run_case(t, configs[cases[i].config], cases[i]);
}

template <typename T> static const T *upload(const T *src, size_t n, std::vector<void *> &keep)
{
    T *d = nullptr;
    CK(cudaMalloc(&d, n * sizeof(T)));
    CK(cudaMemcpy(d, src, n * sizeof(T), cudaMemcpyHostToDevice));
    keep.push_back(d);
    return d;
}

struct S {
    double sum = 0, maxRel = 0, maxAbs = 0; long long n = 0, over = 0;
    void add(double a, double b) {
        if (a != a && b != b) return;
        const double e = fabs(a - b);
        double r = e / std::max(std::max(fabs(a), fabs(b)), 1e-5);
        if (r != r) r = 1e30;
        sum += r; ++n; maxRel = std::max(maxRel, r); maxAbs = std::max(maxAbs, e);
        if (r > 1e-3) ++over;
    }
    void line(const char *name) const
    { printf("%-26s %10lld %10.2e %10.2e %10.2e %8lld\n", name, n, n ? sum / n : 0.0, maxRel, maxAbs, over); }
};

int main()
{
    FILE *f = fopen("out\\cases.bin", "rb");
    if (!f) { printf("no out\\cases.bin, run test_lobes first\n"); return 1; }
    GpuHeader h;
    fread(&h, sizeof h, 1, f);
    if (h.magic != GPU_MAGIC || h.lobeSize != sizeof(nk::LayeredLobe) || h.caseSize != sizeof(GpuCase)) {
        printf("struct layout differs from the CPU build: lobe %d/%d case %d/%d\n",
               h.lobeSize, (int)sizeof(nk::LayeredLobe), h.caseSize, (int)sizeof(GpuCase));
        return 1;
    }
    std::vector<nk::LayeredLobe> cfg(h.configs);
    std::vector<GpuCase> cases(h.cases);
    fread(cfg.data(), sizeof(nk::LayeredLobe), cfg.size(), f);
    fread(cases.data(), sizeof(GpuCase), cases.size(), f);
    fclose(f);

    std::vector<void *> keep;
    nk::LobeTables t;
    t.beckmannE = upload(nk_tbl_beckmann_e, 33 * 33, keep);
    t.ggxE = upload(nk_tbl_ggx_e, 33 * 33, keep);
    t.beckmannOneMinusEavg = upload(nk_tbl_beckmann_one_minus_eavg, 33, keep);
    t.ggxOneMinusEavg = upload(nk_tbl_ggx_one_minus_eavg, 33, keep);
    t.transE = upload(nk_tbl_trans_e, 33 * 33 * 33, keep);
    t.transEExit = upload(nk_tbl_trans_e_exit, 33 * 33 * 33, keep);
    t.transOneMinusEavg = upload(nk_tbl_trans_one_minus_eavg, 33 * 33, keep);
    t.transOneMinusEavgExit = upload(nk_tbl_trans_one_minus_eavg_exit, 33 * 33, keep);
    t.velvetNorm = upload(nk_tbl_velvet_norm, 20, keep);
    t.velvetAlbedo = upload(nk_tbl_velvet_albedo, 2000, keep);

    const nk::LayeredLobe *dCfg = upload(cfg.data(), cfg.size(), keep);
    const GpuCase *dCases = upload(cases.data(), cases.size(), keep);
    GpuResult *dOut = nullptr;
    CK(cudaMalloc(&dOut, cases.size() * sizeof(GpuResult)));

    const int block = 128;
    const auto t0 = std::chrono::high_resolution_clock::now();
    runCases<<<(h.cases + block - 1) / block, block>>>(t, dCfg, dCases, dOut, h.cases);
    CK(cudaGetLastError());
    CK(cudaDeviceSynchronize());
    const auto t1 = std::chrono::high_resolution_clock::now();
    const double ms = std::chrono::duration<double, std::milli>(t1 - t0).count();

    std::vector<GpuResult> out(cases.size());
    CK(cudaMemcpy(out.data(), dOut, out.size() * sizeof(GpuResult), cudaMemcpyDeviceToHost));

    cudaDeviceProp prop;
    CK(cudaGetDeviceProperties(&prop, 0));
    printf("%s: %d cases in %.2f ms (%.1f M/s). LayeredLobe %d bytes\n", prop.name, h.cases, ms, h.cases / ms / 1000.0, h.lobeSize);

    S cpuF, cpuPdf, cpuWi, mrF, mrPdf, mrWi;
    for (size_t i = 0; i < cases.size(); ++i) {
        const GpuCase &c = cases[i];
        const GpuResult &r = out[i];
        const float a[3] = {r.f.r, r.f.g, r.f.b}, b[3] = {c.cpuF.r, c.cpuF.g, c.cpuF.b}, d[3] = {c.refF.r, c.refF.g, c.refF.b};
        for (int k = 0; k < 3; ++k) { cpuF.add(a[k], b[k]); mrF.add(a[k], d[k]); }
        cpuPdf.add(r.pdf, c.cpuPdf); mrPdf.add(r.pdf, c.refPdf);
        if (c.isSample && r.pdf > 0.f) {
            if (c.cpuPdf > 0.f) cpuWi.maxAbs = std::max(cpuWi.maxAbs, (double)nk::dot(nk::v3(r.wi.x - c.cpuWi.x, r.wi.y - c.cpuWi.y, r.wi.z - c.cpuWi.z), nk::v3(r.wi.x - c.cpuWi.x, r.wi.y - c.cpuWi.y, r.wi.z - c.cpuWi.z)));
            if (c.refPdf > 0.f) mrWi.maxAbs = std::max(mrWi.maxAbs, (double)nk::dot(nk::v3(r.wi.x - c.refWi.x, r.wi.y - c.refWi.y, r.wi.z - c.refWi.z), nk::v3(r.wi.x - c.refWi.x, r.wi.y - c.refWi.y, r.wi.z - c.refWi.z)));
        }
    }
    printf("%-26s %10s %10s %10s %10s %8s\n", "gpu against", "n", "mean rel", "max rel", "max abs", ">1e-3");
    cpuF.line("cpu lobes.h: f"); cpuPdf.line("cpu lobes.h: pdf");
    mrF.line("moonray: f"); mrPdf.line("moonray: pdf");
    printf("max wi distance: gpu-cpu %.2e, gpu-moonray %.2e\n", sqrt(cpuWi.maxAbs), sqrt(mrWi.maxAbs));

    // the same, only cases on lobes with roughness >= 0.1 (no near-delta rounding)
    S rf, rp, rw;
    for (size_t i = 0; i < cases.size(); ++i) {
        const nk::Lobe &b = cfg[cases[i].config].base;
        if (nk::lobe_is_glossy(b) && b.inputRoughness < 0.1f) continue;
        const GpuCase &c = cases[i];
        const GpuResult &r = out[i];
        rf.add(r.f.g, c.refF.g); rp.add(r.pdf, c.refPdf);
        if (c.isSample && r.pdf > 0.f && c.refPdf > 0.f) rw.add(r.f.g / r.pdf, c.refF.g / c.refPdf);
    }
    rf.line("moonray r>=0.1: f"); rp.line("moonray r>=0.1: pdf"); rw.line("moonray r>=0.1: f/pdf");

    // by lobe kind, so a problem in one lobe can not hide in the totals
    const char *kinds[] = {"lambert", "oren_nayar", "flat", "beckmann", "ggx", "aniso", "btdf", "mirror_r", "mirror_t", "velvet"};
    S kf[10], kw[10], kc[10];
    double worst[10] = {}; int worstAt[10] = {};
    for (size_t i = 0; i < cases.size(); ++i) {
        const nk::Lobe &b = cfg[cases[i].config].base;
        const bool sharp = nk::lobe_is_glossy(b) && b.kind != nk::LOBE_VELVET && b.inputRoughness < 0.1f;
        const GpuCase &c = cases[i];
        const GpuResult &r = out[i];
        if (!sharp) {
            const float a[3] = {r.f.r, r.f.g, r.f.b}, d[3] = {c.refF.r, c.refF.g, c.refF.b}, e[3] = {c.cpuF.r, c.cpuF.g, c.cpuF.b};
            for (int k = 0; k < 3; ++k) {
                kf[b.kind].add(a[k], d[k]); kc[b.kind].add(a[k], e[k]);
                const double rel = fabs(a[k] - e[k]) / std::max(std::max(fabs(a[k]), fabs(e[k])), 1e-5f);
                if (rel > worst[b.kind]) { worst[b.kind] = rel; worstAt[b.kind] = (int)i; }
            }
            if (c.isSample && r.pdf > 0.f && c.refPdf > 0.f) kw[b.kind].add(r.f.g / r.pdf, c.refF.g / c.refPdf);
        }
    }
    printf("\nby lobe, near-delta glossy left out: f and f/pdf against moonray, f against cpu lobes.h\n");
    for (int k = 0; k < 10; ++k) {
        char n[64];
        snprintf(n, sizeof n, "%s: f", kinds[k]); kf[k].line(n);
        snprintf(n, sizeof n, "%s: f/pdf", kinds[k]); kw[k].line(n);
        snprintf(n, sizeof n, "%s: f gpu-cpu", kinds[k]); kc[k].line(n);
        if (worst[k] > 1e-2) {
            const GpuCase &c = cases[worstAt[k]];
            const nk::LayeredLobe &L = cfg[c.config];
            printf("   worst: config %d sample %d layers %d iri %.2f r %.3f disp %d: gpu %g %g %g cpu %g %g %g moonray %g %g %g\n", c.config, c.isSample, L.count, L.base.iri.strength,
                   L.base.inputRoughness, L.base.dispersion, out[worstAt[k]].f.r, out[worstAt[k]].f.g, out[worstAt[k]].f.b, c.cpuF.r, c.cpuF.g, c.cpuF.b, c.refF.r, c.refF.g, c.refF.b);
            if (L.count > 0) printf("   layer 0: fresnel type %d, weight %g, velvet roughness %g, cosWo * 100 = %.7f\n", L.layers[0].fresnel.type, L.layers[0].fresnel.weight,
                                    L.layers[0].fresnel.velvetRoughness, nk::dot(L.layers[0].N, c.slice.wo) * 100.0f);
        }
    }

    for (void *p : keep) cudaFree(p);
    cudaFree(dOut);
    return 0;
}
