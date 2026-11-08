// Copyright 1998-2020 Matt Pharr, Wenzel Jakob and Greg Humphreys
// Copyright 2023-2024 DreamWorks Animation LLC
// Copyright 2026 Kiet Ho
// SPDX-License-Identifier: Apache-2.0
// Modified by Kiet Ho, 2026: code added to pbrt-v4 by patch_pbrt.py, built on pbrt-v4's classes and following
// MoonRay's integrator rules. See NOTICE.
// ---- end of licence header
// NANNA: NannaMaterial Definition. The add-on's network for any material: one DwaBase, or a DwaMix
// of two, each parameter a constant or a register of the instruction list. The list is the map
// graph (ImageMap, ImageNormalMap, RgbToFloatMap, OpMap, BlendMap, RampMap, DirectionalMap,
// FloatToRgbMap, constants), run once per hit, in the order export_pbrt.py wrote it. GetBxDF is
// DWABASE_createLobes through BsdfBuilder, for the parts of DwaBase a Blender material reaches.
class NannaMaterial {
  public:
    using BxDF = NannaBxDF;
    using BSSRDF = void;
    // Parameter slots, the order of SLOTS in export_pbrt.py
    enum Slot { S_ALBEDO, S_METALLIC_COLOR, S_EDGE, S_TRANS_COLOR, S_CC_COLOR, S_SPECULAR,
                S_ROUGHNESS, S_DIFF_ROUGH, S_IOR, S_METALLIC, S_TRANSMISSION, S_CLEARCOAT,
                S_CC_ROUGH, S_CC_IOR, S_CC_THICK, S_NORMAL, S_CC_BENDING, NSlots };
    static constexpr int MaxCode = 256, MaxRegs = 16, MaxTex = 4;

    struct Params {
        Float vals[2][NSlots][3];
        int binds[2][NSlots];
        int sides, mix, regs, codeLen;
        Float code[MaxCode];
        int specularModel, clearcoatModel, maxDiffuseDepth, maxGlossyDepth;
    };

    static const char *Name() { return "NannaMaterial"; }

    PBRT_CPU_GPU
    FloatTexture GetDisplacement() const { return FloatTexture(); }
    PBRT_CPU_GPU
    const Image *GetNormalMap() const { return nullptr; }

    static NannaMaterial *Create(const TextureParameterDictionary &parameters, Image *normalMap,
                                 const FileLoc *loc, Allocator alloc);

    template <typename TextureEvaluator>
    PBRT_CPU_GPU void GetBSSRDF(TextureEvaluator texEval, MaterialEvalContext ctx,
                                SampledWavelengths &lambda, void *) const {}

    PBRT_CPU_GPU static constexpr bool HasSubsurfaceScattering() { return false; }

    std::string ToString() const;

    NannaMaterial(const Params &params, const nk::LobeTables &tables, const SpectrumTexture *t)
        : p(params), tables(tables) {
        for (int i = 0; i < MaxTex; ++i)
            tex[i] = t[i];
    }

    template <typename TextureEvaluator>
    PBRT_CPU_GPU bool CanEvaluateTextures(TextureEvaluator texEval) const {
        return texEval.CanEvaluate({}, {tex[0], tex[1], tex[2], tex[3]});
    }

    template <typename TextureEvaluator>
    PBRT_CPU_GPU NannaBxDF GetBxDF(TextureEvaluator texEval, MaterialEvalContext ctx,
                                   SampledWavelengths &lambda) const {
        const Frame frame = Frame::FromXZ(Normalize(ctx.dpdus), Vector3f(ctx.ns));
        const bool back = Dot(ctx.wo, Vector3f(ctx.n)) < 0;
        const Vector3f woR = frame.ToLocal(ctx.wo), NgR = frame.ToLocal(Vector3f(ctx.n));
        const Vector3f NgL = back ? Vector3f(NgR.x, NgR.y, -NgR.z) : NgR;
        const nk::Vec3 wo = nk::v3(woR.x, woR.y, back ? -woR.z : woR.z);
        const nk::Vec3 Ng = nk::v3(NgL.x, NgL.y, NgL.z);

        // The map graph
        nk::Col reg[MaxRegs];
        Run(texEval, ctx, lambda, std::abs(woR.z), reg);

        // DwaMix: Blending.cc lerps the parameters of its two sides by the mix
        const Float t = p.sides == 2 && p.mix >= 0 ? nk::nk_clamp(reg[p.mix].r, 0.f, 1.f) : 0.f;
        const auto get = [&](int s, int i) {
            nk::Col a = Value(0, s, reg);
            if (p.sides == 2)
                a = nk::nk_lerp(a, Value(1, s, reg), t);
            return a;
        };
        const auto getf = [&](int s) { return get(s, 0).r; };

        // input_normal: ImageNormalMap's tangent-space normal. Frame x is dpdu, so the frame is
        // its tangent space. A back face sees it from below, as MoonRay flips N there
        nk::Vec3 N = nk::v3(0, 0, 1);
        const int nb = p.binds[0][S_NORMAL] >= 0 ? p.binds[0][S_NORMAL] : (p.sides == 2 ? p.binds[1][S_NORMAL] : -1);
        if (nb >= 0) {
            const nk::Col c = reg[nb];
            const float len = sqrtf(c.r * c.r + c.g * c.g + c.b * c.b);
            if (len > 0 && c.b > 0)
                N = back ? nk::v3(-c.r / len, -c.g / len, c.b / len) : nk::v3(c.r / len, c.g / len, c.b / len);
        }
        const nk::Vec3 Na = AdaptNormal(N, wo, Ng);

        const nk::Col albedo = get(S_ALBEDO, 0), metallicColor = get(S_METALLIC_COLOR, 0),
                      edgeColor = get(S_EDGE, 0), transColor = get(S_TRANS_COLOR, 0),
                      ccColor = get(S_CC_COLOR, 0);
        const Float specular = getf(S_SPECULAR), roughness = nk::nk_clamp(getf(S_ROUGHNESS), 0.f, 1.f),
                    diffuseRoughness = getf(S_DIFF_ROUGH), ior = getf(S_IOR),
                    metallic = nk::nk_clamp(getf(S_METALLIC), 0.f, 1.f),
                    transmission = nk::nk_clamp(getf(S_TRANSMISSION), 0.f, 1.f),
                    clearcoat = getf(S_CLEARCOAT), ccRoughness = getf(S_CC_ROUGH),
                    ccIor = getf(S_CC_IOR), ccThickness = getf(S_CC_THICK);
        const bool ccBending = getf(S_CC_BENDING) > .5f;

        NannaBxDF bxdf(tables, NgL, back, p.maxDiffuseDepth, p.maxGlossyDepth);
        nk::UnderLayer layers[NK_MAX_LAYERS];
        int layerCount = 0;
        Float transmittance = 1;   // mCurrentTransmittance
        Float mediumIor = 1;       // mCurrentMediumIor

        const auto under = [&](nk::LayeredLobe l) {
            for (int i = 0; i < layerCount; ++i)
                nk::layered_push(l, layers[i]);
            return l;
        };

        // DWABASE_addOuterSpecularLobes
        if (clearcoat > NK_EPSILON) {
            const float favg = nk::average_fresnel(ccIor / mediumIor);
            nk::Lobe coat = nk::lobe_cook_torrance(Kind(p.clearcoatModel), Na, ccRoughness, nk::col(favg),
                                                   nk::col(0.f), mediumIor, ccIor, false);
            coat.fresnel = nk::fresnel_dielectric(mediumIor, ccIor, clearcoat);
            bxdf.Add(nk::layered(coat));
            layers[layerCount++] = nk::clearcoat_attenuator(
                Na, coat.fresnel, ccRoughness, mediumIor, ccIor, ccBending, ccThickness, ccColor, clearcoat);
            mediumIor = ccIor;
        }

        const bool smooth = roughness < 1e-6f;  // roughnessClamped < sEpsilon: MoonRay's mirrors
        if (specular > NK_EPSILON || transmission > NK_EPSILON) {
            // Metallic: "conductors don't transmit the energy", so its weight leaves the rest
            const Float metallicWeight = specular * metallic;
            if (metallicWeight >= NK_EPSILON) {
                nk::Col eta, k;
                ComplexIor(metallicColor, edgeColor, eta, k);
                nk::Lobe l = smooth ? nk::lobe_mirror_reflection(Na)
                                    : nk::lobe_cook_torrance(Kind(p.specularModel), Na, roughness,
                                                             nk::average_fresnel(metallicColor, edgeColor),
                                                             nk::col(0.f), 1.f, 1.5f, false);
                l.fresnel = nk::fresnel_conductor(eta, k, metallicWeight);
                bxdf.Add(under(nk::layered(l)));
                transmittance *= 1 - metallicWeight;
            }
            if (metallicWeight >= 1)
                return bxdf;

            // Coupled dielectric reflection and transmission. ShaderIor: a back face is the way out
            const Float etaI = back ? ior : mediumIor, etaT = back ? mediumIor : ior;
            const Float reflWeight = specular, transWeight = transmission;
            nk::Fresnel fresnel = nk::fresnel_none();
            bool haveFresnel = false;
            if (reflWeight > 0 && std::abs(etaI - etaT) > 1e-5f) {
                nk::Lobe l;
                if (smooth) {
                    l = nk::lobe_mirror_reflection(Na);
                } else {
                    float favg, favgInv;
                    nk::average_fresnel(etaT / etaI, favg, favgInv);
                    l = nk::lobe_cook_torrance(Kind(p.specularModel), Na, roughness, nk::col(favg), nk::col(favgInv),
                                               etaI, etaT, transWeight > 0);
                }
                l.fresnel = fresnel = nk::fresnel_dielectric(etaI, etaT, reflWeight);
                haveFresnel = true;
                l.scale = nk::col(transmittance);
                bxdf.Add(under(nk::layered(l)));
            }
            if (transWeight > 0) {
                nk::Lobe l;
                if (smooth) {
                    l = nk::lobe_mirror_transmission(N, etaI, etaT, transColor);
                } else {
                    float favg, favgInv;
                    nk::average_fresnel(etaT / etaI, favg, favgInv);
                    l = nk::lobe_btdf(N, roughness, etaI, etaT, transColor, favg, favgInv);
                }
                if (haveFresnel)
                    l.fresnel = nk::fresnel_one_minus(fresnel);
                l.scale = nk::col(transmittance * transWeight);
                bxdf.Add(under(nk::layered(l)));
            }
            if (haveFresnel && transWeight < 1)
                layers[layerCount++] = nk::under_plain(Na, nk::fresnel_attenuator(fresnel, smooth ? 0.f : roughness), 0, nk::col(1.f), 0);
            transmittance *= nk::nk_max(0.f, 1 - transWeight);  // mWeightAccum
        }

        // FlatDiffuseBRDF, under what's above it
        if (!nk::nk_is_black(albedo) && transmittance > 0) {
            nk::Lobe l = nk::lobe_flat_diffuse(N, albedo, diffuseRoughness, 0, 0, 0);
            l.scale = nk::col(transmittance);
            bxdf.Add(under(nk::layered(l)));
        }
        return bxdf;
    }

  private:
    // A slot's value on a side: its register (a float reads red), its constant, or DwaBase's default
    PBRT_CPU_GPU
    nk::Col Value(int side, int s, const nk::Col *reg) const {
        const int b = p.binds[side][s];
        if (b >= 0)
            return reg[b];
        const Float *v = p.vals[side][s];
        if (v[0] > -1e29f)
            return nk::col(v[0], v[1], v[2]);
        switch (s) {
        case S_ALBEDO: case S_METALLIC_COLOR: case S_EDGE: case S_TRANS_COLOR: case S_CC_COLOR:
            return nk::col(1.f);
        case S_SPECULAR: return nk::col(1.f);
        case S_ROUGHNESS: return nk::col(.5f);
        case S_IOR: case S_CC_IOR: return nk::col(1.5f);
        case S_CC_BENDING: return nk::col(1.f);
        default: return nk::col(0.f);
        }
    }

    // The instruction list. Each op writes one register
    template <typename TextureEvaluator>
    PBRT_CPU_GPU void Run(TextureEvaluator texEval, const MaterialEvalContext &ctx,
                          SampledWavelengths &lambda, Float facing, nk::Col *reg) const {
        int pc = 0;
        while (pc < p.codeLen) {
            const Float *c = p.code + pc;
            const int op = int(c[0]), dst = int(c[1]);
            nk::Col r = nk::col(0.f);
            switch (op) {
            case 0:  // constant
                r = nk::col(c[2], c[3], c[4]);
                pc += 5;
                break;
            case 1:    // ImageMap
            case 2: {  // ImageNormalMap: 2c - 1 in tangent space
                // ImageMap's placement: rotate about the centre, then scale, then offset
                const Float a = Radians(c[7]), cs = std::cos(a), sn = std::sin(a);
                TextureEvalContext tc = ctx;
                const Float u = ctx.uv[0] - c[8], v = ctx.uv[1] - c[9];
                tc.uv = Point2f((cs * u - sn * v + c[8]) * c[3] + c[5], (sn * u + cs * v + c[9]) * c[4] + c[6]);
                tc.dudx = (cs * ctx.dudx - sn * ctx.dvdx) * c[3];
                tc.dvdx = (sn * ctx.dudx + cs * ctx.dvdx) * c[4];
                tc.dudy = (cs * ctx.dudy - sn * ctx.dvdy) * c[3];
                tc.dvdy = (sn * ctx.dudy + cs * ctx.dvdy) * c[4];
                const SampledSpectrum s = texEval(tex[int(c[2])], tc, lambda);
                r = nk::col(s[0], s[1], s[2]);
                if (op == 2)
                    r = nk::col(2 * r.r - 1, 2 * r.g - 1, 2 * r.b - 1);
                pc += 10;
                break;
            }
            case 3: {  // RgbToFloatMap
                const nk::Col a = reg[int(c[2])];
                const int mode = int(c[3]);
                const Float f = mode == 1 ? a.r : mode == 2 ? a.g : mode == 3 ? a.b
                                : .2126f * a.r + .7152f * a.g + .0722f * a.b;
                r = nk::col(f);
                pc += 4;
                break;
            }
            case 4: {  // OpMap, per channel
                const nk::Col a = reg[int(c[2])], b = reg[int(c[3])];
                const int o = int(c[4]);
                if (o == 18) {
                    r = nk::col(a.r * b.r + a.g * b.g + a.b * b.b);
                } else {
                    r = nk::col(Op(o, a.r, b.r), Op(o, a.g, b.g), Op(o, a.b, b.b));
                }
                if (c[5] > .5f)
                    r = nk::col(nk::nk_clamp(r.r, 0.f, 1.f), nk::nk_clamp(r.g, 0.f, 1.f), nk::nk_clamp(r.b, 0.f, 1.f));
                pc += 6;
                break;
            }
            case 5:  // BlendMap
                r = nk::nk_lerp(reg[int(c[3])], reg[int(c[4])], reg[int(c[2])].r);
                pc += 5;
                break;
            case 6: {  // RampMap, constant or linear
                const Float x = reg[int(c[2])].r;
                const int n = int(c[3]), interp = int(c[4]);
                const Float *pos = c + 5, *col = c + 5 + n;
                int i = 0;
                while (i < n && pos[i] < x)
                    ++i;
                if (i == 0)
                    r = nk::col(col[0], col[1], col[2]);
                else if (i == n)
                    r = nk::col(col[3 * n - 3], col[3 * n - 2], col[3 * n - 1]);
                else {
                    const Float f = interp == 0 ? 0.f : (x - pos[i - 1]) / nk::nk_max(pos[i] - pos[i - 1], 1e-9f);
                    r = nk::nk_lerp(nk::col(col[3 * i - 3], col[3 * i - 2], col[3 * i - 1]),
                                    nk::col(col[3 * i], col[3 * i + 1], col[3 * i + 2]), f);
                }
                pc += 5 + 4 * n;
                break;
            }
            case 7:  // DirectionalMap, "parallel", "absolute": |N . wo|
                r = nk::col(facing);
                pc += 2;
                break;
            case 8:  // FloatToRgbMap
                r = nk::col(reg[int(c[2])].r, reg[int(c[3])].r, reg[int(c[4])].r);
                pc += 5;
                break;
            default:
                pc = p.codeLen;
            }
            if (dst >= 0 && dst < MaxRegs)
                reg[dst] = r;
        }
    }

    // OpMap's operations, the order of OPS in export_pbrt.py
    PBRT_CPU_GPU
    static Float Op(int o, Float a, Float b) {
        switch (o) {
        case 0: return a + b;
        case 1: return a - b;
        case 2: return a * b;
        case 3: return b != 0 ? a / b : 0;
        case 4: return std::min(a, b);
        case 5: return std::max(a, b);
        case 6: return a > 0 ? std::pow(a, b) : 0;
        case 7: return std::abs(a);
        case 8: return std::sin(a);
        case 9: return std::cos(a);
        case 10: return std::floor(a);
        case 11: return std::ceil(a);
        case 12: return a - std::floor(a);
        case 13: return std::floor(a + .5f);
        case 14: return a < b ? 1 : 0;
        case 15: return a > b ? 1 : 0;
        case 16: return b != 0 ? std::fmod(a, b) : 0;
        case 17: return std::acos(nk::nk_clamp(a, -1.f, 1.f));
        case 19: return 1 - (1 - a) * (1 - b);
        case 20: return b < .5f ? 2 * a * b : 1 - 2 * (1 - a) * (1 - b);
        default: return a;
        }
    }

    // lobes.h's kind for ispc's MicrofacetDistribution
    PBRT_CPU_GPU
    static int Kind(int distribution) { return distribution == nk::DIST_GGX ? nk::LOBE_GGX : nk::LOBE_BECKMANN; }

    // Intersection::adaptNormal
    PBRT_CPU_GPU
    static nk::Vec3 AdaptNormal(nk::Vec3 Ns, nk::Vec3 wo, nk::Vec3 Ng) {
        const nk::Vec3 r = nk::reflect_dir(Ns, wo);
        const float rDotNg = nk::dot(r, Ng), eps = 0.001f;
        if (rDotNg >= eps)
            return Ns;
        nk::Vec3 rBent = r + (eps - rDotNg) * Ng;
        rBent = (1.f / sqrtf(nk::dot(rBent, rBent))) * rBent;
        const nk::Vec3 h = wo + rBent;
        return (1.f / sqrtf(nk::dot(h, h))) * h;
    }

    // Ior.h, createComplexIorFromColor
    PBRT_CPU_GPU
    static void ComplexIor(nk::Col color, nk::Col edge, nk::Col &eta, nk::Col &k) {
        const float a[3] = {nk::nk_min(color.r, 0.999f), nk::nk_min(color.g, 0.999f), nk::nk_min(color.b, 0.999f)};
        const float g[3] = {nk::nk_min(edge.r, 0.999f), nk::nk_min(edge.g, 0.999f), nk::nk_min(edge.b, 0.999f)};
        float e[3], o[3];
        for (int i = 0; i < 3; ++i) {
            const float s = sqrtf(a[i]);
            e[i] = g[i] * (1 - a[i]) / (1 + a[i]) + (1 - g[i]) * (1 + s) / (1 - s);
            const float x = e[i] + 1, y = e[i] - 1;
            o[i] = sqrtf(nk::nk_max(0.f, a[i] * x * x - y * y) / (1 - a[i]));
        }
        eta = nk::col(e[0], e[1], e[2]);
        k = nk::col(o[0], o[1], o[2]);
    }

    Params p;
    nk::LobeTables tables;
    SpectrumTexture tex[MaxTex];
};
