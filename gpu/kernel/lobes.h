// Copyright 2023-2024 DreamWorks Animation LLC
// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Kiet Ho
// Modified by Kiet Ho, 2026: a port of MoonRay's BSDF lobes (moonray/lib/rendering/shading/bsdf) to one
// C++ source for CPU and CUDA. See NOTICE.
//
// Nanna lobe library: MoonRay's base lobes as one plain C++ source, for CPU and
// for CUDA/OptiX device code. No STL, no virtuals, no heap, no recursion.
//
// This is a modified derivative of MoonRay (Apache-2.0, DreamWorks Animation LLC).
// Every function names the MoonRay code it comes from under "Origin:". Paths are
// relative to moonray/lib/rendering/shading/. Changes from MoonRay, all of them:
//   - pointer lobes and virtual dispatch became a flat Lobe struct and a switch;
//   - nested Fresnel pointers became one flat Fresnel struct with a "wrap" mode;
//   - nested Under* lobes became a LayeredLobe array and loops, at most NK_MAX_LAYERS deep;
//   - IridescenceBsdfLobe became a block of fields on the lobe it wrapped;
//   - tables are reached through LobeTables pointers, not static arrays;
//   - rsqrt is 1/sqrt (MoonRay uses rcp + one Newton step, about 1e-6 away);
//   - table indices are clamped to the table size, see nk_clamp_index.
// MoonRay's quirks are kept on purpose, they decide the image. See "QUIRK" below.
// No code here comes from LuxCore or any other GPL source.
//
// Ported: Lambert, Oren-Nayar, FlatDiffuse, Cook-Torrance (Beckmann, GGX, Aniso), coupled and
// uncoupled energy compensation, TransmissionCookTorrance, mirror reflection and transmission,
// FabricVelvet, iridescence (hue mode), Under, UnderClearcoat, UnderClearcoatTransmission.
// LEFT, DwaBase can still ask for these (SPEC_closure.md, "What is left"): iridescence ramp mode,
// Fabric warp/weft, Toon, glitter flakes, hair, bssrdf. The sampling rule of test_mean.cpp
// belongs to the integrator, not to this file.
#pragma once

#include <math.h>

#if defined(__CUDACC__)
#  define NK_FN __host__ __device__ __forceinline__
#else
#  define NK_FN inline
#endif

namespace nk {

// scene_rdl2/lib/common/math/Constants.h
#define NK_PI 3.14159265358979323846f
#define NK_ONE_OVER_PI (1.0f / NK_PI)
#define NK_EPSILON 1e-6f /* sEpsilon = math::epsilon, 1e-6 in C++ and ISPC. Not float epsilon */
#define NK_ONE_MINUS_EPSILON (1.0f - NK_EPSILON)
#define NK_COMP 33

// ---------------------------------------------------------------- basic types
struct Vec3 { float x, y, z; };
struct Col  { float r, g, b; };

NK_FN Vec3  v3(float x, float y, float z) { Vec3 v; v.x = x; v.y = y; v.z = z; return v; }
NK_FN Vec3  operator+(Vec3 a, Vec3 b) { return v3(a.x + b.x, a.y + b.y, a.z + b.z); }
NK_FN Vec3  operator-(Vec3 a) { return v3(-a.x, -a.y, -a.z); }
NK_FN Vec3  operator*(float s, Vec3 a) { return v3(s * a.x, s * a.y, s * a.z); }
NK_FN float dot(Vec3 a, Vec3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
NK_FN Vec3  cross(Vec3 a, Vec3 b) { return v3(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x); }

NK_FN Col   col(float r, float g, float b) { Col c; c.r = r; c.g = g; c.b = b; return c; }
NK_FN Col   col(float v) { return col(v, v, v); }
NK_FN Col   operator+(Col a, Col b) { return col(a.r + b.r, a.g + b.g, a.b + b.b); }
NK_FN Col   operator-(Col a, Col b) { return col(a.r - b.r, a.g - b.g, a.b - b.b); }
NK_FN Col   operator*(Col a, Col b) { return col(a.r * b.r, a.g * b.g, a.b * b.b); }
NK_FN Col   operator/(Col a, Col b) { return col(a.r / b.r, a.g / b.g, a.b / b.b); }
NK_FN Col   operator*(float s, Col a) { return col(s * a.r, s * a.g, s * a.b); }

NK_FN float nk_min(float a, float b) { return a < b ? a : b; }
NK_FN float nk_max(float a, float b) { return a > b ? a : b; }
NK_FN float nk_clamp(float v, float lo, float hi) { return nk_min(nk_max(v, lo), hi); }
NK_FN float nk_clamp01(float v) { return nk_clamp(v, 0.0f, 1.0f); }
NK_FN float nk_lerp(float a, float b, float t) { return (b - a) * t + a; }       // Math.h lerp
NK_FN Col   nk_lerp(Col a, Col b, float t) { return t * (b - a) + a; }
NK_FN bool  nk_is_zero(float a) { return fabsf(a) <= NK_EPSILON; }        // Math.h isZero
NK_FN bool  nk_is_equal(float a, float b)                                 // Math.h isEqual
{ return fabsf(a - b) <= nk_max(fabsf(a), 1.0f) * NK_EPSILON; }
NK_FN bool  nk_is_black(Col c)                                            // Color.h isBlack
{ return nk_is_zero(c.r) && nk_is_zero(c.g) && nk_is_zero(c.b); }
#ifdef NK_RSQRT_HOOK   /* test only: MoonRay's SSE rsqrt, to tell rounding from real differences */
NK_FN float nk_rsqrt(float x) { return NK_RSQRT_HOOK(x); }
#else
NK_FN float nk_rsqrt(float x) { return 1.0f / sqrtf(x); }
#endif

// Origin: scene_rdl2/lib/common/math/Transcendental.cc, dw_acos (minimax, at most 2 ULP).
// Used by MoonRay on ISPC and on Linux. MoonRay's C++ on Windows calls the CRT acos instead.
NK_FN float dw_acos(float x)
{
#ifdef NK_ACOS_HOOK  /* test only, see nk_rsqrt */
    return NK_ACOS_HOOK(x);
#endif
    const float c0 = 1.5707963039207744f, c1 = -0.2145986966117427f, c2 = 0.08897731218991513f,
                c3 = -0.05016418844436275f, c4 = 0.030862751448600963f, c5 = -0.017045102007693682f,
                c6 = 0.006638618338665405f, c7 = -0.0012534570550072417f;
    const float c0t = 1.5707964f, c3t = -0.0501641f;
    if (x >= 0.0f) {
        const float p = c0 + x * (c1 + x * (c2 + x * (c3t + x * (c4 + x * (c5 + x * (c6 + x * c7))))));
        return p * sqrtf(1.0f - x);
    }
    const float p = c0t - x * (c1 - x * (c2 - x * (c3 - x * (c4 - x * (c5 - x * (c6 - x * c7))))));
    return 3.14159265f - p * sqrtf(1.0f + x);
}

// Origin: scene_rdl2/lib/common/math/MathUtil.h, bias.
NK_FN float nk_bias(float value, float b)
{
    if (b == 0.5f || value <= 0.0f || value >= 1.0f) return value;
    if (b <= 0.0f) return 0.0f;
    return powf(value, logf(b) * (1.0f / logf(0.5f)));
}

// Origin: scene_rdl2/lib/common/math/ReferenceFrame.h, ReferenceFrame(N)
// (Duff et al., "Building an Orthonormal Basis, Revisited").
struct Frame { Vec3 x, y, z; };
NK_FN Frame frame_from_n(Vec3 N)
{
    const float sign = copysignf(1.0f, N.z);
    const float a = -1.0f / (sign + N.z);
    const float b = N.x * N.y * a;
    Frame f;
    f.x = v3(1.0f + sign * N.x * N.x * a, sign * b, -sign * N.x);
    f.y = v3(b, sign + N.y * N.y * a, -N.y);
    f.z = N;
    return f;
}
NK_FN Vec3 frame_to_global(const Frame &f, Vec3 d) { return d.x * f.x + d.y * f.y + d.z * f.z; }

// ---------------------------------------------------------------- slice
// Origin: bsdf/BsdfSlice.h, ispc enum ShadowTerminatorFix.
enum ShadowFix { ST_OFF = 0, ST_CUSTOM, ST_SINE_COMPENSATION, ST_GGX, ST_COSINE_COMPENSATION };

struct Slice {
    Vec3 Ng;
    Vec3 wo;
    int  includeCosine;
    int  shadowFix;
};
NK_FN Slice make_slice(Vec3 Ng, Vec3 wo, bool includeCosine, int shadowFix = ST_OFF)
{ Slice s; s.Ng = Ng; s.wo = wo; s.includeCosine = includeCosine; s.shadowFix = shadowFix; return s; }

// Origin: bsdf/BsdfSlice.cc, BsdfSlice::computeShadowTerminatorFix.
NK_FN float shadow_terminator_fix(const Slice &s, Vec3 N, Vec3 wi)
{
    float G = 1.0f;
    switch (s.shadowFix) {
    case ST_CUSTOM: {
        // QUIRK: this is the C++ constant. The ISPC path uses 0.14944f here (BsdfSlice.ispc).
        const float cosX = nk_clamp01(dot(N, s.Ng));
        const float d = nk_lerp(0.14922f, 0.01999f, cosX);
        const float t = nk_clamp01(dot(s.Ng, wi) / d);
        G = t * t * (3.0f - 2.0f * t);
    } break;
    case ST_SINE_COMPENSATION: {
        const Vec3 c = cross(s.Ng, N);
        G = nk_clamp01(nk_min(1.0f, dot(s.Ng, wi) /
            (nk_max(NK_EPSILON, dot(N, wi)) * nk_max(NK_EPSILON, sqrtf(dot(c, c))))));
        G = -(G * G * G) + G * G + G;
    } break;
    case ST_GGX: {
        const float cosD = nk_min(fabsf(dot(s.Ng, N)), 1.0f);
        const float tan2D = (1.0f - cosD * cosD) / (cosD * cosD);
        const float alpha2 = nk_clamp01(0.125f * tan2D);
        const float cosI = nk_max(fabsf(dot(s.Ng, wi)), NK_EPSILON);
        const float tan2I = (1.0f - cosI * cosI) / (cosI * cosI);
        G = 2.0f / (1.0f + sqrtf(1.0f + alpha2 * tan2I));
    } break;
    case ST_COSINE_COMPENSATION:
        G = nk_clamp01(nk_min(1.0f, dot(s.Ng, wi) /
            (nk_max(NK_EPSILON, dot(N, wi)) * nk_max(NK_EPSILON, dot(s.Ng, N)))));
        G = -(G * G * G) + G * G + G;
        break;
    default:
        break;
    }
    return G;
}

// ---------------------------------------------------------------- direction helpers
// Origin: Util.h, sampleLocalHemisphereCosine.
NK_FN Vec3 sample_local_hemisphere_cosine(float r1, float r2)
{
    r1 = nk_clamp01(r1);
    const float cosTheta = sqrtf(r1);
    const float sinTheta = sqrtf(1.0f - r1);
    const float phi = r2 * (2.0f * NK_PI);
    return v3(sinTheta * cosf(phi), sinTheta * sinf(phi), cosTheta);
}
// Origin: Util.h, computeLocalSphericalDirection.
NK_FN Vec3 local_spherical_direction(float cosTheta, float sinTheta, float phi)
{ return v3(sinTheta * cosf(phi), sinTheta * sinf(phi), cosTheta); }

// Origin: Util.h, computeReflectionDirection.
NK_FN Vec3 reflect_dir(Vec3 N, Vec3 wo)
{ const float NdotWo = dot(N, wo); return -wo + 2.0f * NdotWo * N; }

// Origin: Util.h, computeRefractionDirection.
// wi points away from wo's side. Returns false on total internal reflection.
NK_FN bool refract_dir(Vec3 N, Vec3 wo, float neta, Vec3 &wi, float &NdotWo, float &NdotWi)
{
    NdotWo = dot(N, wo);
    const Vec3 Nf = NdotWo > 0.0f ? N : -N;
    NdotWo = fabsf(NdotWo);
    NdotWi = 1.0f - neta * neta * (1.0f - (NdotWo * NdotWo));
    if (NdotWi < 0.0f) return false;
    NdotWi = sqrtf(NdotWi);
    const float negMu = neta * NdotWo - NdotWi;
    wi = negMu * Nf + (-neta) * wo;
    return true;
}

// Origin: Util.h, computeNormalizedHalfVector.
NK_FN bool half_vector(Vec3 wo, Vec3 wi, Vec3 &H)
{
    H = wo + wi;
    const float l2 = dot(H, H);
    if (l2 > 0.001f * 0.001f) { H = nk_rsqrt(l2) * H; return true; }
    return false;
}

// ---------------------------------------------------------------- tables
// Pointers, so a host can fill them from lobe_tables.h and a GPU from a device buffer.
struct LobeTables {
    const float *beckmannE, *ggxE;                          // 33*33
    const float *beckmannOneMinusEavg, *ggxOneMinusEavg;    // 33
    const float *transE, *transEExit;                       // 33*33*33
    const float *transOneMinusEavg, *transOneMinusEavgExit; // 33*33
    const float *velvetNorm;                                // 20
    const float *velvetAlbedo;                              // 20*100
};

// Origin: bsdf/fabric/VelvetAlbedo.cc at, VelvetAlbedo.h getAlbedoTableIndices.
// QUIRK: frac is roughness - int(roughness), so roughness itself weighs the two rows.
// Change from MoonRay: same indices, written so nvcc can not fuse x * n - 1 into an FMA. Fused,
// r = 0.05 gives 1.5e-8 instead of 0, ceil lands one row up, and the GPU reads another albedo.
NK_FN float velvet_albedo_at(const LobeTables &t, float cosTheta, float roughness)
{
    const float r = nk_clamp01(roughness);
    const float rScaled = r * 20.0f;
    const int rLow  = (int)floorf(rScaled) - 1;
    const int rHigh = (int)ceilf(rScaled) - 1;
    const float c = nk_clamp01(cosTheta) * 100.0f;
    const int cosIndex = c >= 1.0f ? (int)c - 1 : (c < 2.98e-8f ? -1 : 0);   // (int)(c - 1), truncating
    const int last = 20 * 100 - 1;
    const int a = rLow * 100 + cosIndex, b = rHigh * 100 + cosIndex;
    const int iLow  = a < 0 ? 0 : (a > last ? last : a);
    const int iHigh = b < 0 ? 0 : (b > last ? last : b);
    const float frac = roughness - (float)(int)roughness;
    return (1.0f - frac) * t.velvetAlbedo[iLow] + frac * t.velvetAlbedo[iHigh];
}

// ---------------------------------------------------------------- Fresnel
enum FresnelType { FR_NONE = 0, FR_DIELECTRIC, FR_CONDUCTOR, FR_VELVET };
enum FresnelWrap { FR_DIRECT = 0, FR_ONE_MINUS, FR_ONE_MINUS_ROUGH };

// One flat struct for DielectricFresnel, ConductorFresnel, OneMinusVelvetFresnel and the OneMinus wrappers.
struct Fresnel {
    int   type;
    int   wrap;
    float weight;
    float etaI, etaT;        // dielectric
    Col   eta, k;            // conductor
    Col   oneMinusAt0;       // OneMinusRoughFresnel
    float interpolator;      // OneMinusRoughFresnel
    float velvetRoughness;   // OneMinusVelvetFresnel
};

NK_FN Fresnel fresnel_none()
{
    Fresnel f;
    f.type = FR_NONE; f.wrap = FR_DIRECT; f.weight = 1.0f; f.etaI = f.etaT = 1.0f;
    f.eta = f.k = f.oneMinusAt0 = col(0.0f); f.interpolator = 0.0f; f.velvetRoughness = 0.0f;
    return f;
}

// Origin: bsdf/Fresnel.h, DielectricFresnel::eval, evalPerp, evalParallel.
NK_FN Col fresnel_dielectric_eval(float hDotWi, float etaI, float etaT, float weight)
{
    hDotWi = nk_max(hDotWi, -1.0f);
    hDotWi = nk_min(hDotWi, 1.0f);
    const float eta = etaI / etaT;
    const float sinThetaTSqr = eta * eta * (1.0f - hDotWi * hDotWi);
    if (sinThetaTSqr >= 1.0f) return weight * col(1.0f);
    const float cosThetaT = sqrtf(1.0f - sinThetaTSqr);
    const float cosThetaI = fabsf(hDotWi);
    const float a = etaT * cosThetaI, b = etaI * cosThetaT;
    const float parallel = (a - b) / (a + b);
    const float c = etaI * cosThetaI, d = etaT * cosThetaT;
    const float perp = (c - d) / (c + d);
    const float fr = 0.5f * (parallel * parallel + perp * perp);
    return weight * col(fr);
}

// Origin: bsdf/Fresnel.h, ConductorFresnel::eval. QUIRK: no clamp on hDotWi.
NK_FN Col fresnel_conductor_eval(float hDotWi, Col eta, Col k, float weight)
{
    const float c2 = hDotWi * hDotWi;
    const Col factor = eta * eta + k * k;
    const Col tmp = c2 * factor;
    const Col etaC = hDotWi * eta;
    const Col parallel = (tmp - 2.0f * etaC + col(1.0f)) / (tmp + 2.0f * etaC + col(1.0f));
    const Col perp = (factor - 2.0f * etaC + col(c2)) / (factor + 2.0f * etaC + col(c2));
    return weight * (0.5f * (parallel + perp));
}

// Origin for FR_VELVET: bsdf/Fresnel.h, OneMinusVelvetFresnel::eval.
NK_FN Col fresnel_top_eval(const LobeTables &t, const Fresnel &f, float c)
{
    if (f.type == FR_DIELECTRIC) return fresnel_dielectric_eval(c, f.etaI, f.etaT, f.weight);
    if (f.type == FR_CONDUCTOR)  return fresnel_conductor_eval(c, f.eta, f.k, f.weight);
    if (f.type == FR_VELVET)     return col(1.0f - velvet_albedo_at(t, c, f.velvetRoughness) * f.weight);
    return col(1.0f);
}

// Origin: bsdf/Fresnel.h, OneMinusFresnel::eval, OneMinusRoughFresnel::eval.
NK_FN Col fresnel_eval(const LobeTables &t, const Fresnel &f, float c)
{
    if (f.wrap == FR_ONE_MINUS)
        return col(1.0f) - fresnel_top_eval(t, f, c);
    if (f.wrap == FR_ONE_MINUS_ROUGH)
        return nk_lerp(col(1.0f) - fresnel_top_eval(t, f, c), f.oneMinusAt0, f.interpolator);
    return fresnel_top_eval(t, f, c);
}

NK_FN Fresnel fresnel_dielectric(float etaI, float etaT, float weight = 1.0f)
{ Fresnel f = fresnel_none(); f.type = FR_DIELECTRIC; f.etaI = etaI; f.etaT = etaT; f.weight = weight; return f; }

NK_FN Fresnel fresnel_conductor(Col eta, Col k, float weight = 1.0f)
{ Fresnel f = fresnel_none(); f.type = FR_CONDUCTOR; f.eta = eta; f.k = k; f.weight = weight; return f; }

NK_FN Fresnel fresnel_one_minus(const Fresnel &top)
{ Fresnel f = top; f.wrap = FR_ONE_MINUS; return f; }

NK_FN Fresnel fresnel_velvet(float roughness, float weight)
{ Fresnel f = fresnel_none(); f.type = FR_VELVET; f.velvetRoughness = roughness; f.weight = weight; return f; }

// Origin: bsdf/Fresnel.h, OneMinusRoughFresnel constructor. top is dielectric or conductor, no table is read.
NK_FN Fresnel fresnel_one_minus_rough(const Fresnel &top, float specRoughness)
{
    Fresnel f = top;
    f.wrap = FR_ONE_MINUS_ROUGH;
    const LobeTables noTables = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
    f.oneMinusAt0 = col(1.0f) - fresnel_top_eval(noTables, top, 1.0f);
    const float r2 = specRoughness * specRoughness;
    const float tmp = 1.0f - r2;
    f.interpolator = nk_clamp01(1.0f - (tmp * tmp * tmp));
    return f;
}

// Origin: LobeAttenuator.h, SimpleAttenuator / ClearcoatAttenuator constructors.
NK_FN Fresnel fresnel_attenuator(const Fresnel &top, float roughness)
{ return nk_is_zero(roughness) ? fresnel_one_minus(top) : fresnel_one_minus_rough(top, roughness); }

// Origin: ispc/bsdf/Fresnel.isph, averageFresnelReflectance overloads.
NK_FN float average_fresnel(float neta) { return (neta - 1.0f) / (4.08567f + 1.00071f * neta); }
NK_FN float average_inv_fresnel(float neta)
{ return 0.997118f + 0.1014f * neta - 0.965241f * neta * neta - 0.130607f * neta * neta * neta; }
NK_FN void  average_fresnel(float neta, float &favg, float &favgInv)
{
    if (neta > 1.0f)      { favg = average_fresnel(neta); favgInv = average_inv_fresnel(1.0f / neta); }
    else if (neta < 1.0f) { favg = average_fresnel(1.0f / neta); favgInv = average_inv_fresnel(neta); }
    else                  { favg = favgInv = 0.0f; }
}
NK_FN Col average_fresnel(Col reflectivity, Col edgeTint)
{
    const Col r2 = reflectivity * reflectivity, e2 = edgeTint * edgeTint;
    return col(0.087237f) + 0.0230685f * edgeTint - 0.0864902f * e2 + 0.0774594f * (e2 * edgeTint) +
           0.782654f * reflectivity - 0.136432f * r2 + 0.278708f * (r2 * reflectivity) +
           0.19744f * (edgeTint * reflectivity) + 0.0360605f * (e2 * reflectivity) -
           0.2586f * (edgeTint * r2);
}

// ---------------------------------------------------------------- albedo table lookups
// Change from MoonRay: its index() can run one row past the table at roughness 1,
// where the weight is 0 and the value unused. A GPU buffer must not be read there.
NK_FN int nk_clamp_index(int i, int size) { return i < size ? i : size - 1; }

enum Distribution { DIST_BECKMANN = 0, DIST_GGX = 1 };   // ispc MicrofacetDistribution

// Origin: bsdf/cook_torrance/energy_compensation/ReflectionAlbedo.h, E, rIndex, index.
// QUIRK: cosTheta uses ceil, not interpolation, so E steps across cells.
NK_FN float reflection_E(const LobeTables &t, int type, float cosTheta, float roughness)
{
    const float i = roughness * (NK_COMP - 1);
    const float indexI = floorf(i);
    const float w = i - indexI;
    const int lIndex = (int)indexI;
    const int top = NK_COMP * NK_COMP - 1;
    const int hIndex = lIndex + 1 < top ? lIndex + 1 : top;
    const float if1 = (float)(lIndex * NK_COMP) + cosTheta * (NK_COMP - 1);
    const float if2 = (float)(hIndex * NK_COMP) + cosTheta * (NK_COMP - 1);
    const int iLow  = nk_clamp_index((int)ceilf(if1), NK_COMP * NK_COMP);
    const int iHigh = nk_clamp_index((int)ceilf(if2), NK_COMP * NK_COMP);
    const float *e = type == DIST_BECKMANN ? t.beckmannE : t.ggxE;
    return nk_lerp(e[iLow], e[iHigh], w);
}

// Origin: ReflectionAlbedo.h, oneMinusAvg.
NK_FN float reflection_one_minus_avg(const LobeTables &t, int type, float roughness)
{
    const float i = roughness * (NK_COMP - 1);
    const float indexI = floorf(i);
    const float w = i - indexI;
    const int iLow = (int)indexI;
    const int iHigh = iLow + 1 < NK_COMP - 1 ? iLow + 1 : NK_COMP - 1;
    const float *e = type == DIST_BECKMANN ? t.beckmannOneMinusEavg : t.ggxOneMinusEavg;
    return nk_lerp(e[iLow], e[iHigh], w);
}

// Origin: energy_compensation/TransmissionAlbedo.h, netaRange.
NK_FN float trans_neta_range(float eta) { return (eta - 1.05f) / (3.05f - 1.05f); }

// Origin: TransmissionAlbedo.h, nIndex + rIndex.
NK_FN int trans_r_index(float eta, float roughness, float &w)
{
    const float nZeroToOne = nk_clamp01(trans_neta_range(eta));
    const int n = (int)(nZeroToOne * (NK_COMP - 1));
    const float i = (float)(n * NK_COMP) + roughness * (NK_COMP - 1);
    const float indexI = floorf(i);
    w = i - indexI;
    return (int)indexI;
}

// Origin: TransmissionAlbedo.h, E + index.
NK_FN float transmission_E(const LobeTables &t, float eta, float cosTheta, float roughness)
{
    const bool exitSide = eta < 1.0f;
    float w;
    const int lIndex = trans_r_index(exitSide ? 1.0f / eta : eta, roughness, w);
    const int top = NK_COMP * NK_COMP - 1;
    const int hIndex = lIndex + 1 < top ? lIndex + 1 : top;
    const float if1 = (float)(lIndex * NK_COMP) + cosTheta * (NK_COMP - 1);
    const float if2 = (float)(hIndex * NK_COMP) + cosTheta * (NK_COMP - 1);
    const int size = NK_COMP * NK_COMP * NK_COMP;
    const int iLow  = nk_clamp_index((int)ceilf(if1), size);
    const int iHigh = nk_clamp_index((int)ceilf(if2), size);
    const float *tab = exitSide ? t.transEExit : t.transE;
    return nk_lerp(tab[iLow], tab[iHigh], w);
}

// Origin: TransmissionAlbedo.h, oneMinusAvg.
NK_FN float transmission_one_minus_avg(const LobeTables &t, float eta, float roughness)
{
    const bool exitSide = eta < 1.0f;
    float w;
    const int iLow = trans_r_index(exitSide ? 1.0f / eta : eta, roughness, w);
    const int top = NK_COMP * NK_COMP - 1;
    const int iHigh = iLow + 1 < top ? iLow + 1 : top;
    const float *tab = exitSide ? t.transOneMinusEavgExit : t.transOneMinusEavg;
    return nk_lerp(tab[iLow], tab[iHigh], w);
}

// ---------------------------------------------------------------- lobes
enum LobeKind { LOBE_LAMBERT = 0, LOBE_OREN_NAYAR, LOBE_FLAT_DIFFUSE, LOBE_BECKMANN, LOBE_GGX,
                LOBE_ANISO, LOBE_BTDF, LOBE_MIRROR_R, LOBE_MIRROR_T, LOBE_VELVET };

// Origin: bsdf/BsdfIridescence.h, hue mode. Ramp mode is not ported.
// scale is the scale MoonRay sets on the IridescenceBsdfLobe wrapper. The builder sets no Fresnel there.
struct IridescenceParams {
    float strength;          // 0: lobe has no wrapper
    Vec3  N;
    Col   primary, secondary;
    int   flipHue;
    float thickness, exponent, at0, at90;
    Col   scale;
};

struct Lobe {
    int     kind;
    int     reflection;      // diffuse lobes: REFLECTION or TRANSMISSION
    Frame   frame;
    Col     scale;
    Fresnel fresnel;
    // diffuse
    Col     albedo;
    float   a, b;            // Oren-Nayar
    float   terminatorShift, flatness, flatnessFalloff;   // flat diffuse
    // Cook-Torrance
    float   inputRoughness, roughness, invRoughness;
    Col     favg, favgInv;
    float   etaI, etaT;
    int     coupled;         // mCoupledWithTransmission
    // Aniso: roughness above is U
    float   inputRoughnessV, roughnessV;
    // BTDF, mirror transmission
    Col     tint;
    int     dispersion;      // mAllowDispersion
    float   etaR, etaG, etaB;
    // velvet: color is albedo, roughness is mRoughness
    float   specularExponent, normalizationFactor;
    IridescenceParams iri;
};

// BsdfLobe::Type GLOSSY. This decides the clearcoat Jacobian.
NK_FN bool lobe_is_glossy(const Lobe &l)
{ return l.kind == LOBE_BECKMANN || l.kind == LOBE_GGX || l.kind == LOBE_ANISO || l.kind == LOBE_BTDF || l.kind == LOBE_VELVET; }
NK_FN bool lobe_is_mirror(const Lobe &l) { return l.kind == LOBE_MIRROR_R || l.kind == LOBE_MIRROR_T; }
NK_FN bool lobe_is_reflection(const Lobe &l)
{ return l.kind != LOBE_BTDF && l.kind != LOBE_MIRROR_T && l.reflection != 0; }

NK_FN Lobe lobe_base(int kind, Vec3 N)
{
    Lobe l;
    l.kind = kind; l.reflection = 1; l.frame = frame_from_n(N); l.scale = col(1.0f);
    l.fresnel = fresnel_none(); l.albedo = col(0.0f); l.a = l.b = 0.0f;
    l.terminatorShift = l.flatness = l.flatnessFalloff = 0.0f;
    l.inputRoughness = l.roughness = l.invRoughness = 0.0f;
    l.favg = l.favgInv = col(0.0f); l.etaI = 1.0f; l.etaT = 1.5f; l.coupled = 0;
    l.inputRoughnessV = l.roughnessV = 0.0f; l.tint = col(1.0f); l.dispersion = 0;
    l.etaR = l.etaG = l.etaB = 1.0f; l.specularExponent = l.normalizationFactor = 0.0f;
    l.iri.strength = 0.0f; l.iri.N = N; l.iri.primary = l.iri.secondary = col(0.0f); l.iri.flipHue = 0;
    l.iri.thickness = l.iri.exponent = l.iri.at0 = l.iri.at90 = 0.0f; l.iri.scale = col(1.0f);
    return l;
}

// Origin: bsdf/BsdfLambert.h, LambertBsdfLobe constructor.
NK_FN Lobe lobe_lambert(Vec3 N, Col albedo, bool reflection = true)
{ Lobe l = lobe_base(LOBE_LAMBERT, N); l.albedo = albedo; l.reflection = reflection; return l; }

// Origin: bsdf/BsdfOrenNayar.h, OrenNayarBsdfLobe constructor.
NK_FN Lobe lobe_oren_nayar(Vec3 N, Col albedo, float roughness, bool reflection = true)
{
    Lobe l = lobe_base(LOBE_OREN_NAYAR, N);
    l.albedo = albedo; l.reflection = reflection;
    const float s0 = (roughness * 90.0f) * (NK_PI / 180.0f);
    const float s = s0 * s0;
    l.a = 1.0f - (0.5f * (s / (s + 0.33f)));
    l.b = 0.45f * (s / (s + 0.09f));
    return l;
}

// Origin: bsdf/npr/BsdfFlatDiffuse.h, FlatDiffuseBsdfLobe constructor. This, not Lambert or
// plain Oren-Nayar, is DwaBase's diffuse reflection (DwaBaseLayerable.ispc, addFlatDiffuseBRDF).
NK_FN Lobe lobe_flat_diffuse(Vec3 N, Col albedo, float roughness, float terminatorShift, float flatness,
                             float flatnessFalloff, bool reflection = true)
{
    Lobe l = lobe_oren_nayar(N, albedo, roughness, reflection);
    l.kind = LOBE_FLAT_DIFFUSE;
    l.terminatorShift = terminatorShift; l.flatness = flatness; l.flatnessFalloff = flatnessFalloff;
    return l;
}

// Origin: bsdf/cook_torrance/BsdfCookTorrance.cc, CookTorranceBsdfLobe constructor.
NK_FN Lobe lobe_cook_torrance(int kind, Vec3 N, float roughness, Col favg, Col favgInv,
                              float etaI, float etaT, bool coupledWithTransmission)
{
    Lobe l = lobe_base(kind, N);
    l.inputRoughness = roughness;
    l.favg = favg; l.favgInv = favgInv; l.etaI = etaI; l.etaT = etaT;
    l.coupled = coupledWithTransmission;
    l.roughness = roughness * roughness;
    if (l.roughness < 0.001f) l.roughness = 0.001f;
    l.invRoughness = 1.0f / l.roughness;
    return l;
}

// Origin: Util.h, computeSpectralIOR, shared by BTDF and mirror transmission.
NK_FN void lobe_set_dispersion(Lobe &l, float etaT, float abbeNumber)
{
    if (nk_is_zero(abbeNumber)) { l.dispersion = 0; return; }
    l.etaG = etaT;
    l.etaB = (l.etaG - 1.0f) / (2.0f * abbeNumber) + l.etaG;
    l.etaR = 2.0f * l.etaG - l.etaB;
    l.dispersion = 1;
}

// Origin: bsdf/cook_torrance/BsdfTransmissionCookTorrance.cc, constructor.
// Always Beckmann: BsdfBuilder takes a distribution for BTDFs and drops it. favg comes from
// average_fresnel(etaT / etaI, favg, favgInv).
NK_FN Lobe lobe_btdf(Vec3 N, float roughness, float etaI, float etaT, Col tint, float favg, float favgInv,
                     float abbeNumber = 0.0f)
{
    Lobe l = lobe_cook_torrance(LOBE_BTDF, N, roughness, col(favg), col(favgInv), etaI, etaT, false);
    l.reflection = 0; l.tint = tint;
    lobe_set_dispersion(l, etaT, abbeNumber);
    return l;
}

// Origin: bsdf/BsdfMirror.h, MirrorReflectionBsdfLobe constructor.
NK_FN Lobe lobe_mirror_reflection(Vec3 N) { return lobe_base(LOBE_MIRROR_R, N); }

// Origin: bsdf/BsdfMirror.h, MirrorTransmissionBsdfLobe constructor.
NK_FN Lobe lobe_mirror_transmission(Vec3 N, float etaI, float etaT, Col tint, float abbeNumber = 0.0f)
{
    Lobe l = lobe_base(LOBE_MIRROR_T, N);
    l.reflection = 0; l.etaI = etaI; l.etaT = etaT; l.tint = tint;
    lobe_set_dispersion(l, etaT, abbeNumber);
    return l;
}

// Origin: scene_rdl2/lib/common/math/ReferenceFrame.h, ReferenceFrame(N, T).
NK_FN Frame frame_from_n_t(Vec3 N, Vec3 T)
{
    Frame f;
    const Vec3 y = cross(N, T);
    f.z = N;
    f.y = nk_rsqrt(dot(y, y)) * y;
    f.x = cross(f.y, f.z);
    return f;
}

// Origin: BsdfCookTorrance.cc, AnisoCookTorranceBsdfLobe constructor.
// Beckmann only, no energy compensation. MoonRay has no anisotropic GGX: BsdfBuilder's
// MicrofacetAnisotropic* always land here, whatever the distribution on the component.
NK_FN Lobe lobe_aniso(Vec3 N, Vec3 anisoDirection, float roughnessU, float roughnessV)
{
    Lobe l = lobe_cook_torrance(LOBE_ANISO, N, roughnessU, col(0.0f), col(0.0f), 1.0f, 1.5f, false);
    l.inputRoughnessV = roughnessV;
    l.roughnessV = roughnessV * roughnessV;
    if (l.roughnessV < 0.001f) l.roughnessV = 0.001f;
    l.frame = frame_from_n_t(N, anisoDirection);
    return l;
}

// Origin: bsdf/fabric/BsdfFabricVelvet.cc, FabricVelvetBsdfLobe constructor.
NK_FN Lobe lobe_velvet(const LobeTables &t, Vec3 N, float roughness, Col color)
{
    Lobe l = lobe_base(LOBE_VELVET, N);
    l.albedo = color;
    l.roughness = nk_clamp(roughness, 0.05f, 1.0f);
    l.specularExponent = 1.0f / l.roughness;
    const float scaled = l.roughness * 20.0f;       // floor(x) - 1, see velvet_albedo_at
    const int index = (int)floorf(scaled) - 1;
    l.normalizationFactor = 1.0f / t.velvetNorm[index];
    return l;
}

NK_FN Col lobe_scale_fresnel(const LobeTables &t, const Lobe &l, float c) { return l.scale * fresnel_eval(t, l.fresnel, c); }

// Origin: bsdf/cook_torrance/BsdfCookTorrance.h, widenRoughness.
NK_FN float widen_roughness(float roughness, float cosNO)
{ return (1.2f - 0.2f * sqrtf(cosNO)) * roughness; }

// Origin: energy_compensation/CookTorranceEnergyCompensation.cc, weight, and
// TransmissionCookTorranceEnergyCompensation.cc, weightR;
// BsdfCookTorrance.cc, energyCompensationWeight.
NK_FN float ec_weight(const Lobe &l)
{
    if (nk_is_black(l.favg)) return 0.0f;
    if (l.coupled) {
        const float w = l.etaI < l.etaT ? l.inputRoughness - 0.5f : l.inputRoughness - 0.3f;
        return nk_max(0.0f, w);
    }
    return nk_max(0.0f, l.inputRoughness - 0.5f);
}

// Origin: TransmissionCookTorranceEnergyCompensation.cc, computeFactor.
NK_FN float ec_compute_factor(const LobeTables &t, float favg, float favgInv,
                              float etaI, float etaT, float roughness)
{
    const float neta = etaI < etaT ? etaT / etaI : etaI / etaT;
    const float divisor = transmission_one_minus_avg(t, neta, roughness);
    if (nk_is_zero(divisor)) return 1.0f;
    float y = transmission_one_minus_avg(t, 1.0f / neta, roughness);
    y = y / divisor;
    y *= neta * neta;
    const float tt = y * (1.0f - favgInv) / (1.0f - favg);
    return nk_max(0.0f, tt / (1.0f + tt));
}

// Origin: CookTorranceEnergyCompensation.cc, eval; TransmissionCookTorranceEnergyCompensation.cc,
// evalR; BsdfCookTorrance.cc, evalEnergyCompensation.
NK_FN Col ec_eval(const LobeTables &t, const Lobe &l, int type, float cosNO, float cosNI, bool includeCos)
{
    if (nk_is_black(l.favg)) return col(0.0f);
    const float r = l.inputRoughness;
    if (l.coupled) {
        const float neta = l.etaT / l.etaI;
        float compensation = (1.0f - transmission_E(t, neta, cosNO, r)) *
                             (1.0f - transmission_E(t, neta, cosNI, r));
        const float divisor = transmission_one_minus_avg(t, neta, r);
        const float favg = l.favg.r, favgInv = l.favgInv.r;
        const float factor = ec_compute_factor(t, favg, favgInv, l.etaI, l.etaT, r);
        const float ratio = neta > 1.0f ? 1.0f - (1.0f - favg) * factor
                                        : 1.0f - (1.0f - favgInv) * (1.0f - factor);
        compensation = nk_is_zero(divisor) ? 0.0f : compensation / divisor;
        compensation *= NK_ONE_OVER_PI;
        const float f = ratio * compensation * (includeCos ? cosNI : 1.0f);
        return col(nk_max(0.0f, f));
    }
    float compensation = (1.0f - reflection_E(t, type, cosNO, r)) *
                         (1.0f - reflection_E(t, type, cosNI, r));
    const float divisor = reflection_one_minus_avg(t, type, r);
    compensation = nk_is_zero(divisor) ? 0.0f : compensation / divisor;
    const float eavg = 1.0f - reflection_one_minus_avg(t, type, r);
    const Col multiplier = (eavg * (l.favg * l.favg)) / (col(1.0f) - (1.0f - eavg) * l.favg);
    const Col comp = (compensation * NK_ONE_OVER_PI) * multiplier;
    return (includeCos ? cosNI : 1.0f) * comp;
}

// Origin: TransmissionCookTorranceEnergyCompensation.cc, evalT. Added to every BTDF, no weight.
NK_FN float ec_eval_t(const LobeTables &t, float cosNO, float cosNI, float roughness, float etaI, float etaT,
                      float favg, float favgInv, bool includeCos)
{
    const float neta = etaT / etaI;
    const float divisor = transmission_one_minus_avg(t, 1.0f / neta, roughness);
    const float factor = ec_compute_factor(t, favg, favgInv, etaI, etaT, roughness);
    float n, ratio;
    if (etaI < etaT) { n = 1.0f; ratio = (1.0f - favg) * factor; }
    else             { n = etaT / etaI; ratio = (1.0f - favgInv) * (1.0f - factor); }
    float compensation = (1.0f - transmission_E(t, neta, cosNO, roughness)) *
                         (1.0f - transmission_E(t, 1.0f / neta, cosNI, roughness));
    compensation = nk_is_zero(divisor) ? 0.0f : compensation / divisor;
    compensation *= NK_ONE_OVER_PI;
    return nk_max(0.0f, ratio * compensation * n * n * (includeCos ? cosNI : 1.0f));
}

// Origin: CookTorranceEnergyCompensation.cc, pdf; TransmissionCookTorranceEnergyCompensation.cc,
// pdfR; BsdfCookTorrance.cc, energyCompensationPDF.
NK_FN float ec_pdf(const LobeTables &t, const Lobe &l, int type, float cosNI)
{
    if (nk_is_black(l.favg)) return 0.0f;
    const float r = l.inputRoughness;
    if (l.coupled) {
        const float neta = l.etaT / l.etaI;
        const float norm = NK_PI * transmission_one_minus_avg(t, neta, r);
        const float result = cosNI * (1.0f - transmission_E(t, neta, cosNI, r));
        return nk_max(0.0f, result / norm);
    }
    const float norm = NK_PI * reflection_one_minus_avg(t, type, r);
    if (nk_is_zero(norm)) return 0.0f;
    return nk_max(0.0f, (cosNI * (1.0f - reflection_E(t, type, cosNI, r))) / norm);
}

// Origin: CookTorranceEnergyCompensation.cc, sample; TransmissionCookTorranceEnergyCompensation.cc,
// sampleR; BsdfCookTorrance.cc, sampleEnergyCompensation.
NK_FN Vec3 ec_sample(const Lobe &l, int type, float r1, float r2)
{
    const float r = l.inputRoughness;
    if (l.coupled) {
        float cosThetaI;
        if (l.etaI < l.etaT) {
            const float n = trans_neta_range(l.etaT / l.etaI);
            cosThetaI = powf(r1, (0.133f * n + 3.02f) * r * r + (-0.66f * n - 5.12f) * r + 3.009f);
        } else {
            const float n = trans_neta_range(l.etaI / l.etaT);
            cosThetaI = powf(r1, (0.38f * n + 2.4f) * r * r + (-0.68f * n - 4.34f) * r + 2.57f);
        }
        cosThetaI = nk_clamp(cosThetaI, 0.0f, 0.99f);
        float sinThetaI = nk_clamp01(1.0f - cosThetaI * cosThetaI);
        sinThetaI = sinThetaI > 0.0f ? sqrtf(sinThetaI) : 0.0f;
        return frame_to_global(l.frame, local_spherical_direction(cosThetaI, sinThetaI, 2.0f * NK_PI * r2));
    }
    const float power = type == DIST_BECKMANN ? 2.767f * r * r - 5.4f * r + 2.99412f
                                              : 1.1933f * r * r - 2.0969f * r + 1.3698f;
    const float cosThetaI = powf(r1, power);
    const float sinThetaI = sqrtf(1.0f - cosThetaI * cosThetaI);
    return frame_to_global(l.frame, local_spherical_direction(cosThetaI, sinThetaI, 2.0f * NK_PI * r2));
}

// Origin: bsdf/BsdfLambert.h, LambertBsdfLobe::eval.
NK_FN Col lambert_eval(const LobeTables &t, const Lobe &l, const Slice &s, Vec3 wi, float *pdf)
{
    const Vec3 N = l.frame.z;
    const float cosThetaWi = nk_max(dot(N, wi), 0.0f);
    if (pdf) *pdf = cosThetaWi * NK_ONE_OVER_PI;
    float cosThetaWo = 1.0f;
    if (l.fresnel.type != FR_NONE) {
        const Vec3 Nr = l.reflection ? N : -N;
        cosThetaWo = nk_max(dot(Nr, s.wo), 0.0f);
    }
    const float Gs = l.reflection ? shadow_terminator_fix(s, N, wi) : 1.0f;
    return (Gs * NK_ONE_OVER_PI * (s.includeCosine ? cosThetaWi : 1.0f)) *
           (l.albedo * lobe_scale_fresnel(t, l, cosThetaWo));
}

// Origin: bsdf/BsdfOrenNayar.h, getOrenNayarIntensity and OrenNayarBsdfLobe::eval.
// QUIRK: the "pdf" is the Oren-Nayar intensity / pi, while sample() draws a plain
// cosine, so it is not the density of sample(). The mean of f/pdf differs from the
// integral of f: on this lobe a change of sampling strategy can change the image.
NK_FN Col oren_nayar_eval(const LobeTables &t, const Lobe &l, const Slice &s, Vec3 wi, float *pdf)
{
    const Vec3 N = l.frame.z;
    const float nl = nk_max(dot(N, wi), 0.0f);
    const float nv = nk_max(dot(N, s.wo), 0.0f);
    const float ss = dot(wi, s.wo) - nl * nv;
    float tt = 1.0f;
    if (ss > 0.0f) tt = nk_max(nl, nv) + NK_EPSILON;
    const float cosThetaWi = nl * (l.a + l.b * ss / tt);
    if (pdf) *pdf = cosThetaWi * NK_ONE_OVER_PI;
    float cosThetaWo = 1.0f;
    if (l.fresnel.type != FR_NONE) {
        const Vec3 Nr = l.reflection ? N : -N;
        cosThetaWo = nk_max(dot(Nr, s.wo), 0.0f);
    }
    const float Gs = l.reflection ? shadow_terminator_fix(s, N, wi) : 1.0f;
    return (NK_ONE_OVER_PI * Gs * (s.includeCosine ? cosThetaWi : 1.0f)) *
           (l.albedo * lobe_scale_fresnel(t, l, cosThetaWo));
}

// Origin: bsdf/npr/BsdfFlatDiffuse.h, getOrenNayarFalloffIntensity and FlatDiffuseBsdfLobe::eval.
// Same "pdf" QUIRK as Oren-Nayar. Sample and albedo are the Oren-Nayar ones.
NK_FN Col flat_diffuse_eval(const LobeTables &t, const Lobe &l, const Slice &s, Vec3 wi, float *pdf)
{
    const float halfPi = NK_PI * 0.5f;
    const Vec3 N = l.frame.z;
    const float shift = l.terminatorShift + 1.0f;
    const float theta = nk_min(halfPi, shift * dw_acos(nk_clamp(dot(N, wi), -1.0f, 1.0f)));
    const float fa = 1.0f - l.flatnessFalloff;
    const float fb = 1.0f + fa * fa * fa * fa * 100.0f;
    const float fc = halfPi / fb;
    const float fd = fc - halfPi;
    const float ft = (theta > halfPi - fc) ? cosf((theta + fd) * fb) : 1.0f;
    const float flatness = l.flatness * ft;
    const Vec3 lerped = flatness * (wi + (-1.0f) * N) + N;
    const Vec3 flatN = nk_rsqrt(dot(lerped, lerped)) * lerped;

    const float nl = nk_max(0.0f, cosf(nk_min(halfPi, dw_acos(nk_clamp(dot(flatN, wi), -1.0f, 1.0f)) * shift)));
    const float nv = nk_max(dot(flatN, s.wo), 0.0f);
    const float ss = dot(wi, s.wo) - nl * nv;
    float tt = 1.0f;
    if (ss > 0.0f) tt = nk_max(nl, nv) + NK_EPSILON;
    const float cosThetaWi = nl * (l.a + l.b * ss / tt);
    if (pdf) *pdf = cosThetaWi * NK_ONE_OVER_PI;

    float cosThetaWo = 1.0f;
    if (l.fresnel.type != FR_NONE) {
        const Vec3 Nr = l.reflection ? flatN : -flatN;
        cosThetaWo = nk_max(dot(Nr, s.wo), 0.0f);
    }
    const float Gs = l.reflection ? shadow_terminator_fix(s, flatN, wi) : 1.0f;
    const float norm = nk_lerp(1.0f, 0.75f, nk_bias(l.flatness, 0.75f));
    return (norm * NK_ONE_OVER_PI * Gs * (s.includeCosine ? cosThetaWi : 1.0f)) *
           (l.albedo * lobe_scale_fresnel(t, l, cosThetaWo));
}

// Origin: bsdf/cook_torrance/BsdfCookTorrance.cc, CookTorranceBsdfLobe::eval.
NK_FN Col beckmann_eval(const LobeTables &t, const Lobe &l, const Slice &s, Vec3 wi, float *pdf)
{
    if (pdf) *pdf = 0.0f;
    const Vec3 N = l.frame.z;
    float cosNO = dot(N, s.wo);
    if (cosNO <= NK_EPSILON) return col(0.0f);
    cosNO = nk_min(cosNO, NK_ONE_MINUS_EPSILON);
    float cosNI = dot(N, wi);
    if (cosNI <= NK_EPSILON) return col(0.0f);
    cosNI = nk_min(cosNI, NK_ONE_MINUS_EPSILON);
    Vec3 m;
    if (!half_vector(s.wo, wi, m)) return col(0.0f);
    const float cosMI = dot(m, wi);
    if (cosMI <= NK_EPSILON) return col(0.0f);
    const float cosThetaM = dot(m, N);
    if (cosThetaM <= NK_EPSILON) return col(0.0f);

    const float alpha2 = l.roughness * l.roughness;
    const float cosThetaM2 = cosThetaM * cosThetaM;
    const float cosThetaM4 = cosThetaM2 * cosThetaM2;
    const float minusTanThetaM2 = (cosThetaM2 - 1.0f) / cosThetaM2;
    const float D4 = expf(minusTanThetaM2 / alpha2) / (4.0f * NK_PI * alpha2 * cosThetaM4);

    const float ao = cosNO * l.invRoughness * nk_rsqrt(1.0f - cosNO * cosNO);
    const float ai = cosNI * l.invRoughness * nk_rsqrt(1.0f - cosNI * cosNI);
    const float G1o = (ao < 1.6f) ? (3.535f * ao + 2.181f * ao * ao) / (1.0f + 2.276f * ao + 2.577f * ao * ao) : 1.0f;
    const float G1i = (ai < 1.6f) ? (3.535f * ai + 2.181f * ai * ai) / (1.0f + 2.276f * ai + 2.577f * ai * ai) : 1.0f;
    const float G = G1o * G1i;

    Col f = (G * D4 / cosNO * (s.includeCosine ? 1.0f : 1.0f / cosNI)) * lobe_scale_fresnel(t, l, cosMI);
    const float w2 = ec_weight(l);
    if (w2 > 0.0f) f = f + ec_eval(t, l, DIST_BECKMANN, cosNO, cosNI, s.includeCosine);

    if (pdf) {
        const float alpha = widen_roughness(l.roughness, cosNO);
        const float a2 = alpha * alpha;
        const float D4p = expf(minusTanThetaM2 / a2) / (4.0f * NK_PI * a2 * cosThetaM4);
        const float p2 = ec_pdf(t, l, DIST_BECKMANN, cosNI);
        const float w1 = 1.0f - w2;
        const float p1 = D4p * cosThetaM / cosMI;
        *pdf = w1 * p1 + w2 * p2;
    }
    return shadow_terminator_fix(s, N, wi) * f;
}

// Origin: BsdfCookTorrance.cc, GGXCookTorranceBsdfLobe::eval (both overloads).
NK_FN Col ggx_eval(const LobeTables &t, const Lobe &l, const Slice &s, Vec3 wi, float *pdf)
{
    const Vec3 N = l.frame.z;
    const float cosNO = dot(N, s.wo);
    const float cosNI = dot(N, wi);
    const float Gs = shadow_terminator_fix(s, N, wi);
    if (pdf) *pdf = 0.0f;
    if (cosNO <= 0.0f || cosNI <= 0.0f) return col(0.0f);
    Vec3 m;
    if (!half_vector(s.wo, wi, m)) return col(0.0f);
    const float cosMI = dot(m, wi);
    if (cosMI <= 0.0f) return col(0.0f);
    const float cosThetaM = dot(m, N);
    if (cosThetaM <= 0.0f) return col(0.0f);

    const float alpha2 = l.roughness * l.roughness;
    const float cosThetaM2 = cosThetaM * cosThetaM;
    const float piCosThetaM4 = NK_PI * cosThetaM2 * cosThetaM2;
    const float tanThetaM2 = (1.0f - cosThetaM2) / cosThetaM2;
    const float tmp = alpha2 + tanThetaM2;
    const float D4 = 0.25f * alpha2 / (piCosThetaM4 * tmp * tmp);

    const float invCosNO = 1.0f / cosNO;
    const float invCosNI = 1.0f / cosNI;
    const float G1o = 2.0f / (1.0f + sqrtf(1.0f + alpha2 * (1.0f - cosNO * cosNO) * (invCosNO * invCosNO)));
    const float G1i = 2.0f / (1.0f + sqrtf(1.0f + alpha2 * (1.0f - cosNI * cosNI) * (invCosNI * invCosNI)));
    const float G = G1o * G1i;

    Col f = (G * D4 * invCosNO * (s.includeCosine ? 1.0f : invCosNI)) * lobe_scale_fresnel(t, l, cosMI);
    const float w2 = ec_weight(l);
    if (w2 > 0.0f) f = f + ec_eval(t, l, DIST_GGX, cosNO, cosNI, s.includeCosine);

    if (pdf) {
        const float alpha = widen_roughness(l.roughness, cosNO);
        const float a2 = alpha * alpha;
        const float tmp2 = a2 + tanThetaM2;
        const float D4p = 0.25f * a2 / (piCosThetaM4 * tmp2 * tmp2);
        const float p2 = ec_pdf(t, l, DIST_GGX, cosNI);
        const float w1 = 1.0f - w2;
        const float p1 = D4p * cosThetaM / cosMI;
        *pdf = w1 * p1 + w2 * p2;
    }
    return Gs * f;
}

// Origin: BsdfCookTorrance.cc, AnisoCookTorranceBsdfLobe::eval.
NK_FN Col aniso_eval(const LobeTables &t, const Lobe &l, const Slice &s, Vec3 wi, float *pdf)
{
    if (pdf) *pdf = 0.0f;
    const Vec3 N = l.frame.z;
    const float cosNO = dot(N, s.wo);
    if (cosNO <= 0.0f) return col(0.0f);
    const float cosNI = dot(N, wi);
    if (cosNI <= 0.0f) return col(0.0f);
    Vec3 m;
    if (!half_vector(s.wo, wi, m)) return col(0.0f);
    const float cosMI = dot(m, wi);
    if (cosMI <= 0.0f) return col(0.0f);
    const float cosThetaM = dot(m, N);
    if (cosThetaM <= 0.0f) return col(0.0f);

    const float dotMX = dot(m, l.frame.x);
    const float dotMY = dot(m, l.frame.y);
    const float cosThetaMX = dotMX / l.roughness;
    const float cosThetaMY = dotMY / l.roughnessV;
    const float cosThetaM2 = cosThetaM * cosThetaM;
    const float D4 = 0.25f * expf(-(cosThetaMX * cosThetaMX + cosThetaMY * cosThetaMY) / cosThetaM2)
                   / (NK_PI * l.roughness * l.roughnessV * cosThetaM2 * cosThetaM2);

    const float tmpXO = dot(l.frame.x, s.wo) * l.roughness;
    const float tmpYO = dot(l.frame.y, s.wo) * l.roughnessV;
    const float ao = cosNO / sqrtf(tmpXO * tmpXO + tmpYO * tmpYO);
    const float tmpXI = dot(l.frame.x, wi) * l.roughness;
    const float tmpYI = dot(l.frame.y, wi) * l.roughnessV;
    const float ai = cosNI / sqrtf(tmpXI * tmpXI + tmpYI * tmpYI);
    const float G1o = (ao < 1.6f) ? (3.535f * ao + 2.181f * ao * ao) / (1.0f + 2.276f * ao + 2.577f * ao * ao) : 1.0f;
    const float G1i = (ai < 1.6f) ? (3.535f * ai + 2.181f * ai * ai) / (1.0f + 2.276f * ai + 2.577f * ai * ai) : 1.0f;
    const float G = G1o * G1i;

    const Col f = (G * D4 / cosNO * (s.includeCosine ? 1.0f : 1.0f / cosNI)) * lobe_scale_fresnel(t, l, cosMI);

    if (pdf) {
        const float alphaU = widen_roughness(l.roughness, cosNO);
        const float alphaV = widen_roughness(l.roughnessV, cosNO);
        const float pMX = dotMX / alphaU, pMY = dotMY / alphaV;
        const float D4p = 0.25f * expf(-(pMX * pMX + pMY * pMY) / cosThetaM2)
                        / (NK_PI * alphaU * alphaV * cosThetaM2 * cosThetaM2);
        *pdf = D4p * cosThetaM / cosMI;
    }
    return shadow_terminator_fix(s, N, wi) * f;
}

// Origin: BsdfCookTorrance.cc, AnisoCookTorranceBsdfLobe::sample, up to the half vector.
NK_FN Vec3 aniso_sample_m(const Lobe &l, float cosNO, float r1, float r2)
{
    const float alphaU = widen_roughness(l.roughness, cosNO);
    const float alphaV = widen_roughness(l.roughnessV, cosNO);
    const float invU2 = 1.0f / (alphaU * alphaU);
    const float invV2 = 1.0f / (alphaV * alphaV);
    const float phi = 2.0f * NK_PI * r2;
    const float cosOrigPhi = alphaU * cosf(phi);
    const float sinOrigPhi = alphaV * sinf(phi);
    const float cosSqrdPhi = 1.0f / (sinOrigPhi * sinOrigPhi / (cosOrigPhi * cosOrigPhi) + 1.0f);
    const float sinSqrdPhi = 1.0f - cosSqrdPhi;
    const float sinPhiM = sinOrigPhi < 0.0f ? -sqrtf(sinSqrdPhi) : sqrtf(sinSqrdPhi);
    const float cosPhiM = cosOrigPhi < 0.0f ? -sqrtf(cosSqrdPhi) : sqrtf(cosSqrdPhi);
    const float denominator = cosSqrdPhi * invU2 + sinSqrdPhi * invV2;
    const float cosThetaMSqrd = denominator / (denominator - logf(nk_max(r1, NK_EPSILON)));
    const float cosThetaM = sqrtf(cosThetaMSqrd);
    const float sinThetaM = sqrtf(1.0f - cosThetaMSqrd);
    return frame_to_global(l.frame, v3(sinThetaM * cosPhiM, sinThetaM * sinPhiM, cosThetaM));
}

// Origin: BsdfTransmissionCookTorrance.cc, eval(slice, wi, etaT, pdf).
// etaT is the sampled one under dispersion. The energy compensation keeps mEtaT and is not in the pdf.
NK_FN Col btdf_eval(const LobeTables &t, const Lobe &l, const Slice &s, Vec3 wi, float etaT, float *pdf)
{
    if (pdf) *pdf = 0.0f;
    const Vec3 N = l.frame.z;
    float cosNO = dot(N, s.wo);
    float cosNI = dot(N, wi);
    if (cosNO * cosNI > 0.0f) return col(0.0f);
    cosNO = cosNO > 0.0f ? nk_min(cosNO, NK_ONE_MINUS_EPSILON) : nk_max(cosNO, -NK_ONE_MINUS_EPSILON);
    cosNI = cosNI > 0.0f ? nk_min(cosNI, NK_ONE_MINUS_EPSILON) : nk_max(cosNI, -NK_ONE_MINUS_EPSILON);
    const float absCosNO = fabsf(cosNO);
    if (absCosNO <= NK_EPSILON) return col(0.0f);
    const float absCosNI = fabsf(cosNI);
    if (absCosNI <= NK_EPSILON) return col(0.0f);

    // Util.h computeNormalizedRefractionHalfVector
    Vec3 m = -(l.etaI * s.wo + etaT * wi);
    const float lengthSqr = dot(m, m);
    if (!(lengthSqr > 0.001f * 0.001f)) return col(0.0f);
    m = nk_rsqrt(lengthSqr) * m;
    if (l.etaI > etaT) m = -m;

    const float cosThetaM = dot(m, N);
    if (cosThetaM <= NK_EPSILON) return col(0.0f);
    const float cosMI = dot(m, wi);
    const float cosMO = dot(m, s.wo);
    if (cosNO * cosMO <= NK_EPSILON || cosNI * cosMI <= NK_EPSILON) return col(0.0f);
    const float absCosMI = -cosMI;
    if (absCosMI <= NK_EPSILON) return col(0.0f);
    const float absCosMO = cosMO;
    if (absCosMO <= NK_EPSILON) return col(0.0f);

    const float alpha2 = l.roughness * l.roughness;
    const float cosThetaM2 = cosThetaM * cosThetaM;
    const float cosThetaM4 = cosThetaM2 * cosThetaM2;
    const float minusTanThetaM2 = (cosThetaM2 - 1.0f) / cosThetaM2;
    const float D = expf(minusTanThetaM2 / alpha2) / (NK_PI * alpha2 * cosThetaM4);

    const float ao = absCosNO * l.invRoughness * nk_rsqrt(1.0f - cosNO * cosNO);
    const float ai = absCosNI * l.invRoughness * nk_rsqrt(1.0f - cosNI * cosNI);
    const float G1o = (ao < 1.6f) ? (3.535f * ao + 2.181f * ao * ao) / (1.0f + 2.276f * ao + 2.577f * ao * ao) : 1.0f;
    const float G1i = (ai < 1.6f) ? (3.535f * ai + 2.181f * ai * ai) / (1.0f + 2.276f * ai + 2.577f * ai * ai) : 1.0f;
    const float G = G1o * G1i;

    float denom = l.etaI * cosMO + etaT * cosMI;
    denom = 1.0f / (denom * denom);
    const float etaTSqr = etaT * etaT;
    Col f = (absCosMO * absCosMI * etaTSqr * denom * G * D / absCosNO * (s.includeCosine ? 1.0f : 1.0f / absCosNI)) *
            lobe_scale_fresnel(t, l, absCosMO);
    f = f + col(ec_eval_t(t, absCosNO, absCosNI, l.inputRoughness, l.etaI, l.etaT, l.favg.r, l.favgInv.r, s.includeCosine));

    if (pdf) {
        const float alpha = widen_roughness(l.roughness, absCosNO);
        const float a2 = alpha * alpha;
        const float Dp = expf(minusTanThetaM2 / a2) / (NK_PI * a2 * cosThetaM4);
        *pdf = Dp * cosThetaM * etaTSqr * absCosMI * denom;
    }
    return f * l.tint;
}

// Origin: Util.h, sampleSpectralIOR. Renormalizes r, picks one wavelength.
NK_FN void sample_spectral_ior(float &r, const Lobe &l, float &eta, float &pdf, Col &dispersionColor)
{
    if (r < 0.35f)     { r /= 0.35f;              dispersionColor = col(1.0f, 0.0f, 0.0f); eta = l.etaR; pdf = 0.35f; }
    else if (r < 0.7f) { r = (r - 0.35f) / 0.35f; dispersionColor = col(0.0f, 1.0f, 0.0f); eta = l.etaG; pdf = 0.35f; }
    else               { r = (r - 0.7f) / 0.3f;   dispersionColor = col(0.0f, 0.0f, 1.0f); eta = l.etaB; pdf = 0.3f; }
}

// Origin: BsdfTransmissionCookTorrance.cc, sample.
NK_FN Col btdf_sample(const LobeTables &t, const Lobe &l, const Slice &s, float r1, float r2, Vec3 &wi, float &pdf)
{
    const float absCosNO = fabsf(dot(l.frame.z, s.wo));
    Col dispersionColor = col(1.0f);
    float etaT = l.etaT, spectralPdf = 1.0f;
    pdf = 0.0f;
    if (l.dispersion) sample_spectral_ior(r1, l, etaT, spectralPdf, dispersionColor);
    const float neta = l.etaI / etaT;

    const float alpha = widen_roughness(l.roughness, absCosNO);
    const float alpha2 = alpha * alpha;
    const float tanThetaMSqr = -alpha2 * logf(1.0f - r1);
    const float tanThetaM = sqrtf(tanThetaMSqr);
    const float cosThetaM = nk_rsqrt(1.0f + tanThetaMSqr);
    const float sinThetaM = cosThetaM * tanThetaM;
    const Vec3 m = frame_to_global(l.frame, local_spherical_direction(cosThetaM, sinThetaM, 2.0f * NK_PI * r2));

    float cosMO, cosMI;
    if (!refract_dir(m, s.wo, neta, wi, cosMO, cosMI)) { wi = v3(0.0f, 0.0f, 0.0f); pdf = 0.0f; return col(0.0f); }
    const Col result = btdf_eval(t, l, s, wi, etaT, &pdf);
    pdf *= spectralPdf;
    return dispersionColor * result;
}

// Origin: bsdf/fabric/BsdfFabricVelvet.cc, eval. The pdf is the true cosine density.
NK_FN Col velvet_eval(const LobeTables &t, const Lobe &l, const Slice &s, Vec3 wi, float *pdf)
{
    if (pdf) *pdf = 0.0f;
    const Vec3 N = l.frame.z;
    const float cosThetaWi = dot(N, wi);
    if (cosThetaWi <= NK_EPSILON) return col(0.0f);
    const float cosThetaWo = dot(N, s.wo);
    if (cosThetaWo <= NK_EPSILON) return col(0.0f);
    Vec3 H;
    if (!half_vector(s.wo, wi, H)) return col(0.0f);
    const float cosNDotH = nk_clamp(dot(N, H), -0.99f, 0.99f);
    const float sinNDotH = sqrtf(1.0f - cosNDotH * cosNDotH);
    // QUIRK: the extra cosThetaWi is MOONSHINE-921, it softens the terminator and breaks reciprocity.
    const float velvetFactor = l.normalizationFactor * powf(sinNDotH, l.specularExponent) * cosThetaWi;
    if (pdf) *pdf = cosThetaWi * NK_ONE_OVER_PI;
    return (velvetFactor * (s.includeCosine ? cosThetaWi : 1.0f)) * (lobe_scale_fresnel(t, l, cosThetaWo) * l.albedo);
}

NK_FN Col lobe_eval_child(const LobeTables &t, const Lobe &l, const Slice &s, Vec3 wi, float *pdf)
{
    switch (l.kind) {
    case LOBE_LAMBERT:    return lambert_eval(t, l, s, wi, pdf);
    case LOBE_OREN_NAYAR: return oren_nayar_eval(t, l, s, wi, pdf);
    case LOBE_FLAT_DIFFUSE: return flat_diffuse_eval(t, l, s, wi, pdf);
    case LOBE_BECKMANN:   return beckmann_eval(t, l, s, wi, pdf);
    case LOBE_ANISO:      return aniso_eval(t, l, s, wi, pdf);
    case LOBE_BTDF:       return btdf_eval(t, l, s, wi, l.etaT, pdf);
    case LOBE_VELVET:     return velvet_eval(t, l, s, wi, pdf);
    case LOBE_MIRROR_R:
    case LOBE_MIRROR_T:   if (pdf) *pdf = 0.0f; return col(0.0f);    // BsdfMirror.h: delta, never evaluated
    default:              return ggx_eval(t, l, s, wi, pdf);
    }
}

// Origin: BsdfCookTorrance.cc, CookTorranceBsdfLobe::sample + computeBeckmannMicrofacet,
// GGXCookTorranceBsdfLobe::sample (both overloads); BsdfLambert.h, BsdfOrenNayar.h,
// BsdfFabricVelvet.cc, BsdfMirror.h sample.
NK_FN Col lobe_sample_child(const LobeTables &t, const Lobe &l, const Slice &s, float r1, float r2,
                            Vec3 &wi, float &pdf)
{
    const Vec3 N = l.frame.z;
    if (l.kind == LOBE_MIRROR_R) {
        const float cosTheta = fabsf(dot(N, s.wo));
        wi = reflect_dir(N, s.wo);
        pdf = 1.0f;
        return (s.includeCosine ? 1.0f : 1.0f / cosTheta) * lobe_scale_fresnel(t, l, cosTheta);
    }
    if (l.kind == LOBE_MIRROR_T) {
        float neta = l.etaI / l.etaT, spectralEta = l.etaT;
        Col dispersionColor = col(1.0f);
        pdf = 1.0f;
        if (l.dispersion) { sample_spectral_ior(r1, l, spectralEta, pdf, dispersionColor); neta = l.etaI / spectralEta; }
        float cosThetaWo, cosThetaWi;
        // QUIRK: MoonRay leaves wi undefined here, pdf 0 is what callers test.
        if (!refract_dir(N, s.wo, neta, wi, cosThetaWo, cosThetaWi)) { wi = v3(0.0f, 0.0f, 0.0f); pdf = 0.0f; return col(0.0f); }
        return (s.includeCosine ? 1.0f : 1.0f / cosThetaWi) * (l.tint * lobe_scale_fresnel(t, l, cosThetaWo) * dispersionColor);
    }
    if (l.kind == LOBE_BTDF) return btdf_sample(t, l, s, r1, r2, wi, pdf);
    if (l.kind == LOBE_LAMBERT || l.kind == LOBE_OREN_NAYAR || l.kind == LOBE_FLAT_DIFFUSE || l.kind == LOBE_VELVET) {
        wi = frame_to_global(l.frame, sample_local_hemisphere_cosine(r1, r2));
        return lobe_eval_child(t, l, s, wi, &pdf);
    }
    const float cosNO = dot(N, s.wo);
    if (cosNO <= 0.0f) { wi = v3(0.0f, 0.0f, 0.0f); pdf = 0.0f; return col(0.0f); }
    if (l.kind == LOBE_ANISO) {
        wi = reflect_dir(aniso_sample_m(l, cosNO, r1, r2), s.wo);
        return lobe_eval_child(t, l, s, wi, &pdf);
    }

    const int type = l.kind == LOBE_BECKMANN ? DIST_BECKMANN : DIST_GGX;
    const float w2 = ec_weight(l);
    if (r1 < w2) {
        wi = ec_sample(l, type, r1 / w2, r2);
    } else {
        const float w1 = 1.0f - w2;
        r1 = (r1 - w2) / w1;
        const float alpha = widen_roughness(l.roughness, cosNO);
        const float alpha2 = alpha * alpha;
        float cosThetaM, sinThetaM;
        if (l.kind == LOBE_BECKMANN) {
            const float tanThetaMSqr = -alpha2 * logf(1.0f - r1);
            const float tanThetaM = sqrtf(tanThetaMSqr);
            cosThetaM = nk_rsqrt(1.0f + tanThetaMSqr);
            sinThetaM = cosThetaM * tanThetaM;
        } else {
            const float tanThetaM2 = alpha2 * r1 / (1.0f - r1);
            cosThetaM = 1.0f / sqrtf(1.0f + tanThetaM2);
            sinThetaM = cosThetaM * sqrtf(tanThetaM2);
        }
        const Vec3 m = frame_to_global(l.frame, local_spherical_direction(cosThetaM, sinThetaM, 2.0f * NK_PI * r2));
        wi = reflect_dir(m, s.wo);
    }
    return lobe_eval_child(t, l, s, wi, &pdf);
}

// Origin: scene_rdl2/lib/common/math/ColorSpace.cc, rgbToHue.
NK_FN float rgb_to_hue(Col c)
{
    const float maxC = nk_max(c.r, nk_max(c.g, c.b));
    const float chroma = maxC - nk_min(c.r, nk_min(c.g, c.b));
    if (nk_is_zero(chroma)) return 0.0f;
    float h;
    if (nk_is_equal(maxC, c.r))      h = (c.g - c.b) / chroma;
    else if (nk_is_equal(maxC, c.g)) h = 2.0f + (c.b - c.r) / chroma;
    else                             h = 4.0f + (c.r - c.g) / chroma;
    h = fmodf(h * 60.0f, 360.0f);
    if (h < 0.0f) h += 360.0f;
    return h / 360.0f;
}

// Origin: ColorSpace.cc hsvToRgb with s = v = 1, ColorSpace.h hueToRgb.
NK_FN Col hue_to_rgb(float h)
{
    h = fmodf(h, 1.0f);
    if (h < 0.0f) h += 1.0f;
    float hue = h * 360.0f;
    if (nk_is_equal(hue, 360.0f)) hue = 0.0f;
    hue /= 60.0f;
    const int sector = (int)hue;
    const float f = hue - (float)sector;
    const float q = 1.0f - f;
    const float u = 1.0f - (1.0f - f);
    switch (sector) {
    case 0:  return col(1.0f, u, 0.0f);
    case 1:  return col(q, 1.0f, 0.0f);
    case 2:  return col(0.0f, 1.0f, u);
    case 3:  return col(0.0f, q, 1.0f);
    case 4:  return col(u, 0.0f, 1.0f);
    case 5:  return col(1.0f, 0.0f, q);
    default: return col(0.0f);
    }
}

// Origin: BsdfIridescence.h, fitInRange and computeIridescenceColor.
NK_FN float fit_in_range(float value, float oldMin, float oldMax, float newMin, float newMax)
{
    const float range = oldMax - oldMin;
    if (nk_is_zero(range)) return newMin;
    return ((newMax - newMin) * (value - oldMin) / range) + newMin;
}

NK_FN Col iridescence_color(const IridescenceParams &p, float nDotV)
{
    const float index = fmodf(powf(fabsf(1.0f - nDotV), p.exponent) * p.thickness, 1.0f);
    const bool same = nk_is_equal(p.primary.r, p.secondary.r) && nk_is_equal(p.primary.g, p.secondary.g) &&
                      nk_is_equal(p.primary.b, p.secondary.b);
    if (same) {
        if (p.flipHue) return p.primary;
        const float huePrimary = rgb_to_hue(p.primary);
        return hue_to_rgb(fit_in_range(index, 0.0f, 1.0f, huePrimary, huePrimary + 1.0f));
    }
    float huePrimary = rgb_to_hue(p.primary), hueSecondary = rgb_to_hue(p.secondary);
    if (hueSecondary < huePrimary && !p.flipHue) hueSecondary += 1.0f;
    else if (huePrimary < hueSecondary && p.flipHue) huePrimary += 1.0f;
    const float lerpBackDistance = 1.0f / (fabsf(hueSecondary - huePrimary) / 0.166667f + 1.0f);
    const float spectrumDistance = 1.0f - lerpBackDistance;
    if (index < spectrumDistance) return hue_to_rgb(fit_in_range(index, 0.0f, spectrumDistance, huePrimary, hueSecondary));
    return nk_lerp(hue_to_rgb(hueSecondary), hue_to_rgb(huePrimary), (index - spectrumDistance) / lerpBackDistance);
}

// Origin: BsdfIridescence.h, tintWithIridescence, then its computeScaleAndFresnel.
NK_FN Col iridescence_tint(const IridescenceParams &p, float cosThetaHV, Col lobeColor)
{
    if (nk_is_black(lobeColor)) return col(0.0f);
    const float att = nk_clamp01(nk_lerp(p.at90, p.at0, cosThetaHV));
    return p.scale * (lobeColor * nk_lerp(col(1.0f), iridescence_color(p, cosThetaHV), att * p.strength));
}

// Origin: BsdfIridescence.h, computeAverageOfHalfVectorCosines.
NK_FN bool average_half_cos(Vec3 wo, Vec3 wi, float &avgCos)
{
    Vec3 m;
    if (!half_vector(wo, wi, m)) return false;
    avgCos = (dot(m, wo) + dot(m, wi)) * 0.5f;
    return true;
}

// Origin: BsdfIridescence.h, IridescenceBsdfLobe::eval around the child lobe.
NK_FN Col lobe_eval(const LobeTables &t, const Lobe &l, const Slice &s, Vec3 wi, float *pdf)
{
    if (nk_is_zero(l.iri.strength)) return lobe_eval_child(t, l, s, wi, pdf);
    if (lobe_is_mirror(l)) { if (pdf) *pdf = 0.0f; return col(0.0f); }
    const Col f = lobe_eval_child(t, l, s, wi, pdf);
    float avgCos;
    if (!average_half_cos(s.wo, wi, avgCos)) return col(0.0f);
    return iridescence_tint(l.iri, avgCos, f);
}

NK_FN float lobe_pdf(const LobeTables &t, const Lobe &l, const Slice &s, Vec3 wi)
{ float p = 0.0f; lobe_eval(t, l, s, wi, &p); return p; }

// Origin: BsdfIridescence.h, IridescenceBsdfLobe::sample around the child lobe.
NK_FN Col lobe_sample(const LobeTables &t, const Lobe &l, const Slice &s, float r1, float r2,
                      Vec3 &wi, float &pdf)
{
    const Col f = lobe_sample_child(t, l, s, r1, r2, wi, pdf);
    if (nk_is_zero(l.iri.strength)) return f;
    if (lobe_is_mirror(l)) return iridescence_tint(l.iri, dot(l.iri.N, s.wo), f);
    float avgCos;
    if (!average_half_cos(s.wo, wi, avgCos)) return col(0.0f);
    return iridescence_tint(l.iri, avgCos, f);
}

// Origin: BsdfLambert.h, BsdfOrenNayar.h, BsdfCookTorrance.h, BsdfMirror.h, BsdfFabricVelvet.cc, albedo.
// QUIRK: for Cook-Torrance this is only Fresnel at cosThetaWo ("TODO: Improve this approximation!").
// The BTDF drops its energy compensation here; mirrors use |cos|; the iridescence wrapper forwards to its child.
NK_FN Col lobe_albedo(const LobeTables &t, const Lobe &l, const Slice &s)
{
    const float signedCos = dot(l.frame.z, s.wo);
    const float cosThetaWo = nk_max(signedCos, 0.0f);
    switch (l.kind) {
    case LOBE_BECKMANN: case LOBE_GGX: case LOBE_ANISO: return lobe_scale_fresnel(t, l, cosThetaWo);
    case LOBE_BTDF:     return l.tint * lobe_scale_fresnel(t, l, cosThetaWo);
    case LOBE_MIRROR_R: return lobe_scale_fresnel(t, l, fabsf(signedCos));
    case LOBE_MIRROR_T: return l.tint * lobe_scale_fresnel(t, l, fabsf(signedCos));
    case LOBE_VELVET:   return velvet_albedo_at(t, cosThetaWo, l.roughness) * (lobe_scale_fresnel(t, l, cosThetaWo) * l.albedo);
    default:            return lobe_scale_fresnel(t, l, cosThetaWo) * l.albedo;
    }
}

// ---------------------------------------------------------------- layering
// MoonRay wraps a lobe in UnderBsdfLobe / UnderClearcoatBsdfLobe, one pointer per
// attenuator above it. Here layers[0] is the outermost wrapper, layers[count-1] sits
// directly on the base lobe. All layer effects multiply, so loops replace recursion.
enum UnderKind { UNDER_PLAIN = 0, UNDER_CLEARCOAT, UNDER_CLEARCOAT_TRANSMISSION };
#define NK_MAX_LAYERS 4   /* MoonRay allows 16 attenuators. DwaBase without toon needs 3: fuzz, outer specular, specular */

struct UnderLayer {
    int     kind;
    Vec3    N;
    float   thickness;
    Col     attenuationColor;
    float   attenuationWeight;
    Fresnel fresnel;          // the OneMinus* Fresnel set on the wrapper; FR_NONE if absent
    Fresnel exitingFresnel;   // clearcoat kinds: DielectricFresnel(etaT, etaI)
    float   neta;             // clearcoat kinds: etaI / etaT
    int     passThroughTIR;   // UNDER_CLEARCOAT: mPassThroughTIRWhenSampling, true in production
};

struct LayeredLobe {
    Lobe       base;
    int        count;
    int        overflow;      // set when layered_push dropped a layer: the lobe is brighter than MoonRay's
    UnderLayer layers[NK_MAX_LAYERS];
};

NK_FN LayeredLobe layered(const Lobe &base) { LayeredLobe s; s.base = base; s.count = 0; s.overflow = 0; return s; }

// Adds a wrapper under the ones already there: push outermost first, the order of
// BsdfBuilder's active attenuators. False, and overflow set, when the stack is full.
NK_FN bool layered_push(LayeredLobe &l, const UnderLayer &u)
{
    if (l.count < 0 || l.count >= NK_MAX_LAYERS) { l.overflow = 1; return false; }
    l.layers[l.count++] = u;
    return true;
}

// The only way eval and sample read count: a bad count from a buffer cannot walk off layers[].
NK_FN int layered_count(const LayeredLobe &l)
{ return l.count < 0 ? 0 : (l.count > NK_MAX_LAYERS ? NK_MAX_LAYERS : l.count); }

// Origin: bsdf/under/BsdfUnder.h, UnderBsdfLobe constructor.
NK_FN UnderLayer under_plain(Vec3 N, const Fresnel &oneMinus, float thickness, Col attenuationColor,
                             float attenuationWeight)
{
    UnderLayer u;
    u.kind = UNDER_PLAIN; u.N = N; u.thickness = thickness;
    u.attenuationColor = attenuationColor; u.attenuationWeight = attenuationWeight;
    u.fresnel = oneMinus; u.exitingFresnel = fresnel_none(); u.neta = 1.0f; u.passThroughTIR = 1;
    return u;
}

// Origin: bsdf/under/BsdfUnderClearcoat.h, UnderClearcoatBsdfLobe constructor.
NK_FN UnderLayer under_clearcoat(Vec3 N, const Fresnel &oneMinus, float etaI, float etaT, float thickness,
                                 Col attenuationColor, float attenuationWeight, bool passThroughTIR = true)
{
    UnderLayer u = under_plain(N, oneMinus, thickness, attenuationColor, attenuationWeight);
    u.kind = UNDER_CLEARCOAT;
    u.exitingFresnel = fresnel_dielectric(etaT, etaI);
    u.neta = etaI / etaT;
    u.passThroughTIR = passThroughTIR;
    return u;
}

// Origin: bsdf/under/BsdfUnderClearcoatTransmission.h, constructor.
NK_FN UnderLayer under_clearcoat_transmission(Vec3 N, const Fresnel &oneMinus, float etaI, float etaT, float thickness,
                                              Col attenuationColor, float attenuationWeight)
{
    UnderLayer u = under_clearcoat(N, oneMinus, etaI, etaT, thickness, attenuationColor, attenuationWeight, true);
    u.kind = UNDER_CLEARCOAT_TRANSMISSION;
    return u;
}

// Origin: LobeAttenuator.h, ClearcoatAttenuator::operator(BsdfLobe*): which wrapper is used.
NK_FN UnderLayer clearcoat_attenuator(Vec3 N, const Fresnel &topFresnel, float roughness, float etaI, float etaT,
                                      bool refracts, float thickness, Col attenuationColor, float weight,
                                      bool reflectionLobe = true)
{
    const Fresnel f = fresnel_attenuator(topFresnel, roughness);
    if (refracts && !nk_is_equal(etaI, etaT)) {
        if (reflectionLobe) return under_clearcoat(N, f, etaI, etaT, thickness, attenuationColor, weight);
        return under_clearcoat_transmission(N, f, etaI, etaT, thickness, attenuationColor, weight);
    }
    return under_plain(N, f, thickness, attenuationColor, weight);
}

// Origin: LobeAttenuator.h, VelvetAttenuator constructor. MoonRay's default wrap is a plain UnderBsdfLobe.
NK_FN UnderLayer velvet_attenuator(Vec3 N, float roughness, Col color, bool useAbsorbingFibers, float weight)
{
    if (!useAbsorbingFibers) weight *= nk_max(color.r, nk_max(color.g, color.b));
    return under_plain(N, fresnel_velvet(roughness, weight), 0.0f, col(1.0f), 1.0f);
}

// Origin: bsdf/under/BsdfUnder.h, UnderBsdfLobe::computeTransmission.
NK_FN Col under_transmission(const UnderLayer &u, float cos1, float cos2)
{
    if (nk_is_zero(u.thickness)) return col(1.0f);
    float rcp1 = 0.0f, rcp2 = 0.0f;
    if (!nk_is_zero(cos1)) rcp1 = 1.0f / fabsf(cos1);
    if (!nk_is_zero(cos2)) rcp2 = 1.0f / fabsf(cos2);
    const float d = u.thickness * (rcp1 + rcp2);
    const Col T = col(powf(u.attenuationColor.r, d), powf(u.attenuationColor.g, d), powf(u.attenuationColor.b, d));
    return nk_lerp(col(1.0f), T, u.attenuationWeight);
}

NK_FN float under_cos_wo(const UnderLayer &u, Vec3 wo)
{ return u.fresnel.type == FR_NONE ? 1.0f : nk_max(dot(u.N, wo), 0.0f); }

// Origin: BsdfUnder.h, UnderBsdfLobe::albedo; BsdfUnderClearcoat.h forwards to it.
// Albedo does not bend: the same wo is used all the way down.
NK_FN Col layered_albedo_from(const LobeTables &t, const LayeredLobe &l, int first, const Slice &s)
{
    Col a = lobe_albedo(t, l.base, s);
    const int count = layered_count(l);
    for (int i = first; i < count; ++i)
        a = fresnel_eval(t, l.layers[i].fresnel, under_cos_wo(l.layers[i], s.wo)) * a;
    return a;
}

// The clearcoat inter-reflection and Jacobian shared by eval and sample.
// Origin: bsdf/under/BsdfUnderClearcoat.cc, the common tail of eval() and sample().
NK_FN Col clearcoat_factor(const LobeTables &t, const LayeredLobe &l, int i, const Slice &under, float cosThetaWo,
                           float cosThetaWoPrime, float cosThetaWi, float cosThetaWiPrime, float &jacobian)
{
    const UnderLayer &u = l.layers[i];
    const Col ct = under_transmission(u, cosThetaWoPrime, cosThetaWiPrime);
    const Col Fr = fresnel_eval(t, u.exitingFresnel, cosThetaWoPrime);
    const Col Ft = col(1.0f) - Fr;
    const Col albedo = layered_albedo_from(t, l, i + 1, under);
    const Col d = col(1.0f) - ct * Fr * albedo;
    const Col denom = col(nk_max(d.r, 0.01f), nk_max(d.g, 0.01f), nk_max(d.b, 0.01f));
    jacobian = fabsf(cosThetaWi) / nk_max(fabsf(cosThetaWiPrime), 0.01f);
    if (lobe_is_glossy(l.base)) jacobian *= u.neta * u.neta;
    return jacobian * ((fresnel_eval(t, u.fresnel, cosThetaWo) * ct * Ft) / denom);
}

// Origin: BsdfUnder.h UnderBsdfLobe::eval, BsdfUnderClearcoat.cc UnderClearcoatBsdfLobe::eval,
// BsdfUnderClearcoatTransmission.cc eval: wo bends, wi does not, and no Jacobian.
NK_FN Col layered_eval(const LobeTables &t, const LayeredLobe &l, const Slice &slice, Vec3 wiIn, float *pdf)
{
    if (pdf) *pdf = 0.0f;
    Slice s = slice;
    Vec3 wi = wiIn;
    Col product = col(1.0f);
    float pdfProduct = 1.0f;
    const int count = layered_count(l);
    for (int i = 0; i < count; ++i) {
        const UnderLayer &u = l.layers[i];
        if (u.kind == UNDER_PLAIN) {
            const float cosWo = under_cos_wo(u, s.wo);
            const float cosWi = nk_max(dot(u.N, wi), 0.0f);
            product = product * fresnel_eval(t, u.fresnel, cosWo) * under_transmission(u, cosWo, cosWi);
            continue;
        }
        if (u.kind == UNDER_CLEARCOAT_TRANSMISSION) {
            Vec3 woPrime;
            float cosThetaWo, cosThetaWoPrime;
            const float signedCosWo = dot(u.N, s.wo);
            if (!refract_dir(u.N, s.wo, u.neta, woPrime, cosThetaWo, cosThetaWoPrime)) return col(0.0f);
            s.wo = -woPrime;
            product = product * fresnel_eval(t, u.fresnel, signedCosWo) * under_transmission(u, cosThetaWoPrime, 0.0f);
            continue;
        }
        Vec3 woPrime, wiPrime;
        float cosThetaWo, cosThetaWoPrime, cosThetaWi, cosThetaWiPrime;
        if (!refract_dir(u.N, s.wo, u.neta, woPrime, cosThetaWo, cosThetaWoPrime)) return col(0.0f);
        if (cosThetaWo <= NK_EPSILON) return col(0.0f);
        s.wo = -woPrime;
        if (!refract_dir(u.N, wi, u.neta, wiPrime, cosThetaWi, cosThetaWiPrime)) return col(0.0f);
        if (cosThetaWi <= NK_EPSILON) return col(0.0f);
        wi = -wiPrime;
        float jacobian;
        product = product * clearcoat_factor(t, l, i, s, cosThetaWo, cosThetaWoPrime, cosThetaWi, cosThetaWiPrime, jacobian);
        pdfProduct *= jacobian;
    }
    float p = 0.0f;
    const Col f = lobe_eval(t, l.base, s, wi, &p);
    if (pdf) *pdf = p * pdfProduct;
    return product * f;
}

NK_FN float layered_pdf(const LobeTables &t, const LayeredLobe &l, const Slice &s, Vec3 wi)
{ float p = 0.0f; layered_eval(t, l, s, wi, &p); return p; }

// Origin: BsdfUnder.h UnderBsdfLobe::sample, BsdfUnderClearcoat.cc UnderClearcoatBsdfLobe::sample,
// BsdfUnderClearcoatTransmission.cc sample.
// QUIRK: when the sampled ray hits TIR on the way out, passThroughTIR keeps wi unbent,
// and eval() of that wi can be black or different. sample() and eval() do not agree there.
NK_FN Col layered_sample(const LobeTables &t, const LayeredLobe &l, const Slice &slice, float r1, float r2,
                         Vec3 &wiOut, float &pdf)
{
    pdf = 0.0f;
    Slice s = slice;
    float cosWo[NK_MAX_LAYERS], cosWoPrime[NK_MAX_LAYERS];
    Slice underSlice[NK_MAX_LAYERS];
    const int count = layered_count(l);
    for (int i = 0; i < count; ++i) {
        const UnderLayer &u = l.layers[i];
        if (u.kind == UNDER_PLAIN) { cosWo[i] = under_cos_wo(u, s.wo); cosWoPrime[i] = 0.0f; continue; }
        Vec3 woPrime;
        if (!refract_dir(u.N, s.wo, u.neta, woPrime, cosWo[i], cosWoPrime[i])) return col(0.0f);
        s.wo = -woPrime;
        underSlice[i] = s;
    }
    Vec3 wi;
    float p = 0.0f;
    Col result = lobe_sample(t, l.base, s, r1, r2, wi, p);
    for (int i = count - 1; i >= 0; --i) {
        const UnderLayer &u = l.layers[i];
        if (u.kind == UNDER_PLAIN) {
            const float cosWi = nk_max(dot(u.N, wi), 0.0f);
            result = fresnel_eval(t, u.fresnel, cosWo[i]) * under_transmission(u, cosWo[i], cosWi) * result;
            continue;
        }
        if (u.kind == UNDER_CLEARCOAT_TRANSMISSION) {
            result = result * fresnel_eval(t, u.fresnel, cosWo[i]) * under_transmission(u, cosWoPrime[i], 0.0f);
            continue;
        }
        if (nk_is_zero(p)) { pdf = 0.0f; return col(0.0f); }
        Vec3 wiNext;
        float cosThetaWi, cosThetaWiPrime;
        if (!refract_dir(u.N, wi, 1.0f / u.neta, wiNext, cosThetaWiPrime, cosThetaWi)) {
            if (!u.passThroughTIR) { pdf = 0.0f; return col(0.0f); }
            wiNext = wi;
            cosThetaWi = cosThetaWiPrime;
        } else {
            wiNext = -wiNext;
        }
        float jacobian;
        result = result * clearcoat_factor(t, l, i, underSlice[i], cosWo[i], cosWoPrime[i], cosThetaWi, cosThetaWiPrime, jacobian);
        p *= jacobian;
        wi = wiNext;
    }
    wiOut = wi;
    pdf = p;
    return result;
}

} // namespace nk
