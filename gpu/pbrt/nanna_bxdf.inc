// Copyright 1998-2020 Matt Pharr, Wenzel Jakob and Greg Humphreys
// Copyright 2023-2024 DreamWorks Animation LLC
// Copyright 2026 Kiet Ho
// SPDX-License-Identifier: Apache-2.0
// Modified by Kiet Ho, 2026: code added to pbrt-v4 by patch_pbrt.py, built on pbrt-v4's classes and following
// MoonRay's integrator rules. See NOTICE.
// ---- end of licence header
// NANNA: NannaBxDF Definition. A fixed array of MoonRay lobes. Built in local space, N = (0,0,1).
// lobes.h stays as it is. Everything here is how MoonRay's integrator treats those lobes.
class NannaBxDF {
  public:
    static constexpr int MaxLobes = 4;
    // The add-on's defaults, what Nanna CPU renders with
    static constexpr Float ClampValue = 10, RRThreshold = 0.0375f;
    static constexpr int ClampDepth = 1;  // the depths are the material's: maxDiffuseDepth, maxGlossyDepth
    // pbrt's lights are triangles, MoonRay's a mesh light. Two: a fixed count, a known limit
    static constexpr Float MeshLights = 2;

    struct LobeSample {
        Vector3f wi;
        SampledSpectrum fCos;
        Float pdf, q;
        bool diffuse, mirror, transmission;
    };

    NannaBxDF() = default;
    PBRT_CPU_GPU
    NannaBxDF(const nk::LobeTables &tables, Vector3f Ng, bool back, int maxDiffuseDepth = 4,
              int maxGlossyDepth = 8)
        : maxDiffuseDepth(maxDiffuseDepth), maxGlossyDepth(maxGlossyDepth), tables(tables), Ng(Ng),
          back(back), count(0) {}
    int maxDiffuseDepth, maxGlossyDepth;

    PBRT_CPU_GPU
    void Add(const nk::LayeredLobe &lobe) {
        if (count < MaxLobes)
            lobes[count++] = lobe;
    }

    PBRT_CPU_GPU
    static SampledSpectrum SmartClamp(SampledSpectrum c, Float value) {  // pbr/core/Util.h
        Float m = c.MaxComponentValue();
        return (value > 0 && m > value) ? c * (value / m) : c;
    }

    // BsdfSampler: every lobe gets one sample, whatever its albedo. One ray can't do that, so
    // one lobe is chosen uniformly. The mean of the choice is then the sum MoonRay takes.
    PBRT_CPU_GPU
    bool SampleLobe(Vector3f wo, Float uc, Point2f u, LobeSample *s) const {
        if (count == 0)
            return false;
        const int k = int(uc * count) < count ? int(uc * count) : count - 1;
        nk::Vec3 wi;
        float pdf = 0;
        const nk::Col f = nk::layered_sample(tables, lobes[k], MakeSlice(wo), u[0], u[1], wi, pdf);
        if (!(pdf > 0) || nk::nk_is_black(f) || !(wi.x == wi.x && wi.y == wi.y && wi.z == wi.z) ||
            !(f.r + f.g + f.b < Infinity) || !Matches(k, wo, Unflip(wi)))
            return false;
        s->wi = Unflip(wi);
        s->fCos = ToSpectrum(f);
        s->pdf = pdf;
        s->q = Float(1) / count;
        s->mirror = nk::lobe_is_mirror(lobes[k].base);
        s->diffuse = !s->mirror && !nk::lobe_is_glossy(lobes[k].base);
        s->transmission = !nk::lobe_is_reflection(lobes[k].base);
        return true;
    }

    // integrateLightSetSample: each lobe has its own power heuristic and its own clamp.
    // betaLi is throughput * Li / lightPDF.
    PBRT_CPU_GPU
    SampledSpectrum DirectLight(Vector3f wo, Vector3f wi, SampledSpectrum betaLi, Float lightPDF,
                                Float clamp) const {
        SampledSpectrum t(0.f);
        const nk::Slice slice = MakeSlice(wo);
        const nk::Vec3 w = Flip(wi);
        for (int k = 0; k < count; ++k) {
            if (!Matches(k, wo, wi))
                continue;
            float pdf = 0;
            const nk::Col f = nk::layered_eval(tables, lobes[k], slice, w, &pdf);
            if (!(pdf > 0) || nk::nk_is_black(f))
                continue;
            const Float mis = Sqr(lightPDF) / (Sqr(lightPDF) + Sqr(pdf));
            t += SmartClamp(betaLi * ToSpectrum(f) * mis, clamp);
        }
        return t;
    }

    PBRT_CPU_GPU
    SampledSpectrum f(Vector3f wo, Vector3f wi, TransportMode mode) const {
        SampledSpectrum sum(0.f);
        if (wi.z == 0)
            return sum;
        const nk::Slice slice = MakeSlice(wo);
        for (int k = 0; k < count; ++k)
            if (Matches(k, wo, wi))
                sum += ToSpectrum(nk::layered_eval(tables, lobes[k], slice, Flip(wi), nullptr));
        return sum / AbsCosTheta(wi);
    }

    PBRT_CPU_GPU
    pstd::optional<BSDFSample> Sample_f(
        Vector3f wo, Float uc, Point2f u, TransportMode mode,
        BxDFReflTransFlags sampleFlags = BxDFReflTransFlags::All) const {
        LobeSample s;
        if (!SampleLobe(wo, uc, u, &s) || s.wi.z == 0)
            return {};
        const BxDFFlags kind = s.mirror ? BxDFFlags::Specular : s.diffuse ? BxDFFlags::Diffuse : BxDFFlags::Glossy;
        return BSDFSample(s.fCos / AbsCosTheta(s.wi), s.wi, s.pdf * s.q,
                          kind | (s.transmission ? BxDFFlags::Transmission : BxDFFlags::Reflection));
    }

    PBRT_CPU_GPU
    Float PDF(Vector3f wo, Vector3f wi, TransportMode mode,
              BxDFReflTransFlags sampleFlags = BxDFReflTransFlags::All) const {
        if (count == 0)
            return 0;
        const nk::Slice slice = MakeSlice(wo);
        Float sum = 0;
        for (int k = 0; k < count; ++k)
            if (Matches(k, wo, wi) && !nk::lobe_is_mirror(lobes[k].base))
                sum += nk::layered_pdf(tables, lobes[k], slice, Flip(wi));
        return sum / count;
    }

    PBRT_CPU_GPU
    static constexpr const char *Name() { return "NannaBxDF"; }

    std::string ToString() const;

    PBRT_CPU_GPU
    void Regularize() {}

    PBRT_CPU_GPU
    BxDFFlags Flags() const {
        return count ? (BxDFFlags::DiffuseReflection | BxDFFlags::GlossyReflection |
                        BxDFFlags::GlossyTransmission | BxDFFlags::SpecularReflection |
                        BxDFFlags::SpecularTransmission)
                     : BxDFFlags::Unset;
    }

  private:
    // A back face is shaded from its own side, as MoonRay flips N and Ng there
    PBRT_CPU_GPU
    nk::Vec3 Flip(Vector3f v) const { return nk::v3(v.x, v.y, back ? -v.z : v.z); }
    PBRT_CPU_GPU
    Vector3f Unflip(nk::Vec3 v) const { return Vector3f(v.x, v.y, back ? -v.z : v.z); }
    PBRT_CPU_GPU
    nk::Slice MakeSlice(Vector3f wo) const {
        return nk::make_slice(nk::v3(Ng.x, Ng.y, Ng.z), Flip(wo), true, nk::ST_OFF);
    }
    // BsdfSlice::getSurfaceFlags: a lobe answers only on its own side of Ng
    PBRT_CPU_GPU
    bool Matches(int k, Vector3f wo, Vector3f wi) const {
        return AboveNg(wo, wi) == nk::lobe_is_reflection(lobes[k].base);
    }
    PBRT_CPU_GPU
    bool AboveNg(Vector3f wo, Vector3f wi) const {
        const nk::Vec3 n = nk::v3(Ng.x, Ng.y, Ng.z);
        return nk::dot(n, Flip(wo)) * nk::dot(n, Flip(wi)) > 0;
    }
    PBRT_CPU_GPU
    static SampledSpectrum ToSpectrum(nk::Col c) {
        SampledSpectrum s;
        s[0] = c.r;
        s[1] = c.g;
        s[2] = c.b;
        return s;
    }

    nk::LobeTables tables;
    Vector3f Ng;
    bool back;
    int count;
    nk::LayeredLobe lobes[MaxLobes];
};
