// Copyright 1998-2020 Matt Pharr, Wenzel Jakob and Greg Humphreys
// Copyright 2023-2024 DreamWorks Animation LLC
// Copyright 2026 Kiet Ho
// SPDX-License-Identifier: Apache-2.0
// Modified by Kiet Ho, 2026: code added to pbrt-v4 by patch_pbrt.py, built on pbrt-v4's classes and following
// MoonRay's integrator rules. See NOTICE.
// ---- end of licence header
// NANNA: NannaMaterial Method Definitions
std::string NannaMaterial::ToString() const { return "[ NannaMaterial ]"; }

// lobes.h's tables in memory the GPU can read, one copy for all materials
static nk::LobeTables NannaTables(Allocator alloc) {
    static nk::LobeTables tables;
    static bool done = false;
    if (done)
        return tables;
    const auto copy = [&](const float *from, size_t n) {
        float *to = alloc.allocate_object<float>(n);
        std::copy(from, from + n, to);
        return (const float *)to;
    };
    tables.beckmannE = copy(nk_tbl_beckmann_e, 1089);
    tables.ggxE = copy(nk_tbl_ggx_e, 1089);
    tables.beckmannOneMinusEavg = copy(nk_tbl_beckmann_one_minus_eavg, 33);
    tables.ggxOneMinusEavg = copy(nk_tbl_ggx_one_minus_eavg, 33);
    tables.transE = copy(nk_tbl_trans_e, 35937);
    tables.transEExit = copy(nk_tbl_trans_e_exit, 35937);
    tables.transOneMinusEavg = copy(nk_tbl_trans_one_minus_eavg, 1089);
    tables.transOneMinusEavgExit = copy(nk_tbl_trans_one_minus_eavg_exit, 1089);
    tables.velvetNorm = copy(nk_tbl_velvet_norm, 20);
    tables.velvetAlbedo = copy(nk_tbl_velvet_albedo, 2000);
    done = true;
    return tables;
}

NannaMaterial *NannaMaterial::Create(const TextureParameterDictionary &parameters,
                                     Image *normalMap, const FileLoc *loc, Allocator alloc) {
    Params p{};
    p.sides = parameters.GetOneInt("sides", 1);
    if (p.sides < 1 || p.sides > 2)
        ErrorExit(loc, "nanna: sides must be 1 or 2.");
    for (int s = 0; s < p.sides; ++s) {
        const std::string i = std::to_string(s);
        std::vector<Float> v = parameters.GetFloatArray("side" + i);
        std::vector<int> b = parameters.GetIntArray("bind" + i);
        if (v.size() != 3 * NSlots || b.size() != NSlots)
            ErrorExit(loc, "nanna: side%d needs %d values and bind%d %d.", s, 3 * NSlots, s, NSlots);
        for (int k = 0; k < NSlots; ++k) {
            for (int c = 0; c < 3; ++c)
                p.vals[s][k][c] = v[3 * k + c];
            p.binds[s][k] = b[k];
        }
    }
    p.mix = parameters.GetOneInt("mix", -1);
    p.regs = parameters.GetOneInt("regs", 0);
    std::vector<Float> code = parameters.GetFloatArray("code");
    if (code.size() > MaxCode || p.regs > MaxRegs)
        ErrorExit(loc, "nanna: at most %d code values and %d registers.", MaxCode, MaxRegs);
    p.codeLen = code.size();
    std::copy(code.begin(), code.end(), p.code);
    p.specularModel = parameters.GetOneInt("specular_model", nk::DIST_GGX);
    p.clearcoatModel = parameters.GetOneInt("clearcoat_model", nk::DIST_GGX);
    // The add-on's Diffuse and Glossy bounces
    p.maxDiffuseDepth = parameters.GetOneInt("max_diffuse_depth", 4);
    p.maxGlossyDepth = parameters.GetOneInt("max_glossy_depth", 8);
    SpectrumTexture tex[MaxTex];
    for (int i = 0; i < MaxTex; ++i)
        tex[i] = parameters.GetSpectrumTextureOrNull("tex" + std::to_string(i), SpectrumType::Unbounded, alloc);
    return alloc.new_object<NannaMaterial>(p, NannaTables(alloc), tex);
}
