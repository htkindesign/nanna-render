// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Kiet Ho
// Which mean does each estimator give on one lobe? Numeric, on the CPU, no scene.
// Runs the estimators of pbr/integrator/PathIntegratorUtil.cc on MoonRay's own lobes and on
// lobes.h, under a constant light and a small light. See SPEC_closure.md for the rule it proves.
//
// MoonRay's direct light for lobe k and light l (drawBsdfSamples + drawLightSamples):
//   bsdf sample:  f/r * Li * w(ni r, nl q) / ni    r = reported pdf, q = light pdf without selection
//   light sample: f   * Li / q * w(nl q, ni r) / nl
// and the indirect ray carries f/r / ni, no weight. r is what eval() or sample() returns,
// which on some lobes is not the density of sample(). Expected value of the two together:
//   mean = integral of f Li ( w_l + (p/r) w_b ) dw        p = true density of sample()
#include <moonray/rendering/shading/bsdf/BsdfLambert.h>
#include <moonray/rendering/shading/bsdf/BsdfOrenNayar.h>
#include <moonray/rendering/shading/bsdf/cook_torrance/BsdfCookTorrance.h>
#include <moonray/rendering/shading/bsdf/under/BsdfUnder.h>
#include <moonray/rendering/shading/bsdf/under/BsdfUnderClearcoat.h>
#include <moonray/rendering/shading/bsdf/npr/BsdfFlatDiffuse.h>
#include <moonray/rendering/shading/bsdf/fabric/BsdfFabric.h>
#include <moonray/rendering/shading/bsdf/Fresnel.h>
#include <moonray/rendering/shading/BsdfComponent.h>
#include <moonray/rendering/shading/LobeAttenuator.h>
#include <scene_rdl2/render/util/Arena.h>

#include "lobes.h"
#include "lobe_tables.h"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <functional>
#include <string>
#include <thread>
#include <vector>

namespace ms = moonray::shading;
namespace sm = scene_rdl2::math;
using sm::Vec3f;
using sm::Color;
using nk::Vec3;

static nk::LobeTables gT = {nk_tbl_beckmann_e, nk_tbl_ggx_e, nk_tbl_beckmann_one_minus_eavg,
                            nk_tbl_ggx_one_minus_eavg, nk_tbl_trans_e, nk_tbl_trans_e_exit,
                            nk_tbl_trans_one_minus_eavg, nk_tbl_trans_one_minus_eavg_exit,
                            nk_tbl_velvet_norm, nk_tbl_velvet_albedo};
static Vec3f toM(Vec3 v) { return Vec3f(v.x, v.y, v.z); }
static Vec3 toN(const Vec3f &v) { return nk::v3(v.x, v.y, v.z); }

static const Vec3 kN = {0.f, 0.f, 1.f};

// One lobe, either side of the comparison. Green channel, white albedo, so a float is enough.
struct LobeIf {
    bool reflection = true;
    virtual ~LobeIf() {}
    virtual float eval(Vec3 wo, Vec3 wi, float &pdf) const = 0;
    virtual float sample(Vec3 wo, float r1, float r2, Vec3 &wi, float &pdf) const = 0;
};
struct NkLobe : LobeIf {
    nk::LayeredLobe l;
    float eval(Vec3 wo, Vec3 wi, float &pdf) const override
    { pdf = 0.f; return nk::layered_eval(gT, l, nk::make_slice(kN, wo, true), wi, &pdf).g; }
    float sample(Vec3 wo, float r1, float r2, Vec3 &wi, float &pdf) const override
    { pdf = 0.f; return nk::layered_sample(gT, l, nk::make_slice(kN, wo, true), r1, r2, wi, pdf).g; }
};
struct MrLobe : LobeIf {
    ms::BsdfLobe *m;
    float eval(Vec3 wo, Vec3 wi, float &pdf) const override
    {
        const ms::BsdfSlice s(toM(kN), toM(wo), true, true, (ms::ShadowTerminatorFix)0);
        pdf = 0.f;
        return m->eval(s, toM(wi), &pdf).g;
    }
    float sample(Vec3 wo, float r1, float r2, Vec3 &wi, float &pdf) const override
    {
        const ms::BsdfSlice s(toM(kN), toM(wo), true, true, (ms::ShadowTerminatorFix)0);
        Vec3f w(0.f);
        pdf = 0.f;
        const float f = m->sample(s, r1, r2, w, pdf).g;
        wi = toN(w);
        return f;
    }
};

struct Light {
    std::string name;
    bool constant;
    Vec3 axis;
    nk::Frame fr;
    float cosMax, L, q;
    float Li(Vec3 wi) const { return constant || nk::dot(wi, axis) >= cosMax ? L : 0.f; }
    Vec3 sample(float u, float v) const
    {
        const float phi = 2.f * NK_PI * v;
        if (constant) {
            const float z = 1.f - 2.f * u, s = sqrtf(nk::nk_max(0.f, 1.f - z * z));
            return nk::v3(s * cosf(phi), s * sinf(phi), z);
        }
        const float z = 1.f - u * (1.f - cosMax), s = sqrtf(nk::nk_max(0.f, 1.f - z * z));
        return nk::frame_to_global(fr, nk::v3(s * cosf(phi), s * sinf(phi), z));
    }
};
static Light constantLight()
{ return {"constant", true, kN, nk::frame_from_n(kN), -1.f, 1.f, 1.f / (4.f * NK_PI)}; }
// Radiance 1/solid angle: unit irradiance at the axis, so the mean reads as f at that direction.
static Light smallLight(const std::string &name, Vec3 axis, float degrees, float scale = 1.f)
{
    const float cosMax = cosf(degrees * NK_PI / 180.f);
    const float omega = 2.f * NK_PI * (1.f - cosMax);
    return {name, false, axis, nk::frame_from_n(axis), cosMax, scale / omega, 1.f / omega};
}

static float ph(float a, float b) { const float aa = a * a, bb = b * b; return aa + bb > 0.f ? aa / (aa + bb) : 0.f; }
static bool sideOk(const LobeIf &l, Vec3 wo, Vec3 wi) { return (nk::dot(kN, wo) * nk::dot(kN, wi) > 0.f) == l.reflection; }

struct Rng { unsigned s; float operator()() { s = s * 1664525u + 1013904223u; return (s >> 8) * (1.0f / 16777216.0f); } };

struct Ratio { const char *name; float ni, nl; };
static const Ratio kRatios[] = {{"1:1", 1.f, 1.f}, {"nl4", 1.f, 4.f}, {"ni4", 4.f, 1.f}};
enum { R_COUNT = 3 };

struct Means { double bsdf, light, mix[R_COUNT], se; };

// The four estimators on one lobe. Stratified, replicas give the standard error of the 1:1 column.
static Means estimate(const LobeIf &l, Vec3 wo, const Light &lt, int grid, int replicas)
{
    Means out{};
    double sum = 0, sum2 = 0;
    for (int rep = 0; rep < replicas; ++rep) {
        Rng rng{2024u + rep * 7919u};
        double b = 0, ll = 0, mb[R_COUNT] = {}, ml[R_COUNT] = {};
        for (int i = 0; i < grid; ++i) for (int j = 0; j < grid; ++j) {
            const float r1 = (i + rng()) / grid, r2 = (j + rng()) / grid;
            Vec3 wi; float r = 0.f;
            const float f = l.sample(wo, r1, r2, wi, r);
            if (r != 0.f && f != 0.f && sideOk(l, wo, wi)) {     // BsdfSampler::sample validity
                const float Li = lt.Li(wi);
                if (Li > 0.f) {
                    const double t = (double)f / r * Li;
                    b += t;
                    for (int k = 0; k < R_COUNT; ++k) mb[k] += t * ph(kRatios[k].ni * r, kRatios[k].nl * lt.q);
                }
            }
            const float u = (i + rng()) / grid, v = (j + rng()) / grid;
            const Vec3 wl = lt.sample(u, v);
            if (sideOk(l, wo, wl)) {
                float rl = 0.f;
                const float fl = l.eval(wo, wl, rl);
                if (rl != 0.f && fl != 0.f) {                    // isSampleInvalid in integrateLightSetSample
                    const double t = (double)fl * lt.Li(wl) / lt.q;
                    ll += t;
                    for (int k = 0; k < R_COUNT; ++k) ml[k] += t * ph(kRatios[k].nl * lt.q, kRatios[k].ni * rl);
                }
            }
        }
        const double n = double(grid) * grid;
        out.bsdf += b / n; out.light += ll / n;
        for (int k = 0; k < R_COUNT; ++k) out.mix[k] += (mb[k] + ml[k]) / n;
        const double v = (mb[0] + ml[0]) / n;
        sum += v; sum2 += v * v;
    }
    out.bsdf /= replicas; out.light /= replicas;
    for (int k = 0; k < R_COUNT; ++k) out.mix[k] /= replicas;
    const double mean = sum / replicas;
    out.se = replicas > 1 ? sqrt(std::max(0.0, sum2 / replicas - mean * mean) / (replicas - 1)) : 0.0;
    return out;
}

// ---------------------------------------------------------------- two lobes on one hit
struct Stack { const LobeIf *a, *b; float Pa; };

// Same lobes, MoonRay's split against what a one-light-one-lobe kernel can do.
//   moonray   every lobe sampled, every lobe weighted alone                (the target)
//   held      pick one lobe with P, weight it alone, then divide by P      (the rule)
//   combined  pick one lobe with P, then f = sum f, pdf = sum P r          (Cycles, pbrt)
// clampAt > 0 applies smartClamp at depth >= 1. heldBad clamps after the division by P.
struct StackMeans { double moonray, held, combined, heldBad, indM, indHeld, indComb; };

static StackMeans estimateStack(const Stack &st, Vec3 wo, const Light &lt, int grid, float clampAt)
{
    StackMeans o{};
    const auto C = [&](double x) { return clampAt > 0.f && x > clampAt ? (double)clampAt : x; };
    const LobeIf *ls[2] = {st.a, st.b};
    const float P[2] = {st.Pa, 1.f - st.Pa};
    Rng rng{99u};
    double mo = 0, he = 0, co = 0, bad = 0, iM = 0, iH = 0, iC = 0;
    for (int i = 0; i < grid; ++i) for (int j = 0; j < grid; ++j) {
        const float r1 = (i + rng()) / grid, r2 = (j + rng()) / grid;
        // moonray: one bsdf sample in each lobe, one light sample through each lobe
        for (int k = 0; k < 2; ++k) {
            Vec3 wi; float r = 0.f;
            const float f = ls[k]->sample(wo, r1, r2, wi, r);
            if (r != 0.f && f != 0.f && sideOk(*ls[k], wo, wi)) {
                const float Li = lt.Li(wi);
                iM += (double)f / r;
                if (Li > 0.f) mo += C((double)f / r * Li * ph(r, lt.q));
            }
        }
        const float u = (i + rng()) / grid, v = (j + rng()) / grid;
        const Vec3 wl = lt.sample(u, v);
        float rl[2] = {0.f, 0.f}, fl[2] = {0.f, 0.f};
        for (int k = 0; k < 2; ++k) {
            if (!sideOk(*ls[k], wo, wl)) continue;
            fl[k] = ls[k]->eval(wo, wl, rl[k]);
            if (rl[k] != 0.f && fl[k] != 0.f) mo += C((double)fl[k] * lt.Li(wl) / lt.q * ph(lt.q, rl[k]));
        }
        // one lobe picked for the bsdf ray, one lobe picked for the shadow ray
        const float pick = rng(), pickL = rng();
        const int k = pick < P[0] ? 0 : 1, kl = pickL < P[0] ? 0 : 1;
        Vec3 wi; float r = 0.f;
        const float f = ls[k]->sample(wo, r1, r2, wi, r);
        if (r != 0.f && f != 0.f && sideOk(*ls[k], wo, wi)) {
            const float Li = lt.Li(wi);
            iH += (double)f / r / P[k];
            if (Li > 0.f) {
                const double t = (double)f / r * Li * ph(r, lt.q);
                he += C(t) / P[k];
                bad += C(t / P[k]);
            }
            float F = 0.f, R = 0.f;
            for (int n = 0; n < 2; ++n) {
                float rn = 0.f; float fn = 0.f;
                if (sideOk(*ls[n], wo, wi)) fn = ls[n]->eval(wo, wi, rn);
                F += fn; R += P[n] * rn;
            }
            if (R > 0.f) {
                iC += (double)F / R;
                if (Li > 0.f) co += C((double)F / R * Li * ph(R, lt.q));
            }
        }
        if (rl[kl] != 0.f && fl[kl] != 0.f) {
            const double t = (double)fl[kl] * lt.Li(wl) / lt.q * ph(lt.q, rl[kl]);
            he += C(t) / P[kl];
            bad += C(t / P[kl]);
        }
        const float Fl = fl[0] + fl[1], Rl = P[0] * rl[0] + P[1] * rl[1];
        if (Rl > 0.f) co += C((double)Fl * lt.Li(wl) / lt.q * ph(lt.q, Rl));
    }
    const double n = double(grid) * grid;
    o.moonray = mo / n; o.held = he / n; o.combined = co / n; o.heldBad = bad / n;
    o.indM = iM / n; o.indHeld = iH / n; o.indComb = iC / n;
    return o;
}

// ---------------------------------------------------------------- lobes under test
static scene_rdl2::alloc::Arena *gArena;

struct Pair { std::string name; NkLobe n; MrLobe m; };
static void setRefl(Pair &p, bool r) { p.n.reflection = r; p.m.reflection = r; }

static Pair *mk(const std::string &name, ms::BsdfLobe *m, const nk::LayeredLobe &n, bool refl = true)
{ Pair *p = new Pair; p->name = name; p->m.m = m; p->n.l = n; setRefl(*p, refl); return p; }

// A clearcoat, ior 1.5, over a lobe: the test_lobes furnace setup.
static Pair *coat(const std::string &name, ms::BsdfLobe *m, nk::Lobe n)
{
    auto *top = new ms::DielectricFresnel(1.f, 1.5f);
    auto *om = new ms::OneMinusRoughFresnel(top, 0.1f);
    auto *w = new ms::UnderClearcoatBsdfLobe(m, gArena, toM(kN), 1.f, 1.5f, 0.f, sm::sWhite, 1.f, true);
    w->setFresnel(om);
    nk::LayeredLobe l = nk::layered(n);
    nk::layered_push(l, nk::under_clearcoat(kN, nk::fresnel_one_minus_rough(nk::fresnel_dielectric(1.f, 1.5f), 0.1f), 1.f, 1.5f, 0.f, nk::col(1.f), 1.f, true));
    return mk(name, w, l);
}

static std::vector<Pair *> buildLobes()
{
    std::vector<Pair *> v;
    const Color W = sm::sWhite; const nk::Col w = nk::col(1.f);
    const Vec3f mN = toM(kN);
    v.push_back(mk("lambert", new ms::LambertBsdfLobe(mN, W, true), nk::layered(nk::lobe_lambert(kN, w))));
    v.push_back(mk("flat r0", new ms::FlatDiffuseBsdfLobe(mN, W, 0.f, 0.f, 0.f, 0.f, true), nk::layered(nk::lobe_flat_diffuse(kN, w, 0.f, 0.f, 0.f, 0.f))));
    v.push_back(mk("flat r.4", new ms::FlatDiffuseBsdfLobe(mN, W, 0.4f, 0.f, 0.f, 0.f, true), nk::layered(nk::lobe_flat_diffuse(kN, w, 0.4f, 0.f, 0.f, 0.f))));
    v.push_back(mk("oren r.8", new ms::OrenNayarBsdfLobe(mN, W, 0.8f, true), nk::layered(nk::lobe_oren_nayar(kN, w, 0.8f))));
    v.push_back(mk("ggx r.3", new ms::GGXCookTorranceBsdfLobe(mN, 0.3f, sm::sBlack, sm::sBlack, 1.f, 1.5f, false),
                   nk::layered(nk::lobe_cook_torrance(nk::LOBE_GGX, kN, 0.3f, nk::col(0.f), nk::col(0.f), 1.f, 1.5f, false))));
    v.push_back(mk("ggx r.7 ec", new ms::GGXCookTorranceBsdfLobe(mN, 0.7f, W, sm::sBlack, 1.f, 1.5f, false),
                   nk::layered(nk::lobe_cook_torrance(nk::LOBE_GGX, kN, 0.7f, w, nk::col(0.f), 1.f, 1.5f, false))));
    v.push_back(mk("velvet r.5", new ms::FabricVelvetBsdfLobe(mN, 0.5f, W), nk::layered(nk::lobe_velvet(gT, kN, 0.5f, w))));
    {
        float favg, favgInv;
        nk::average_fresnel(1.5f, favg, favgInv);
        auto *m = new ms::TransmissionCookTorranceBsdfLobe(mN, 0.5f, 1.f, 1.5f, W, favg, favgInv);
        v.push_back(mk("btdf r.5", m, nk::layered(nk::lobe_btdf(kN, 0.5f, 1.f, 1.5f, w, favg, favgInv)), false));
    }
    v.push_back(coat("coat/lambert", new ms::LambertBsdfLobe(mN, W, true), nk::lobe_lambert(kN, w)));
    v.push_back(coat("coat/flat r.4", new ms::FlatDiffuseBsdfLobe(mN, W, 0.4f, 0.f, 0.f, 0.f, true), nk::lobe_flat_diffuse(kN, w, 0.4f, 0.f, 0.f, 0.f)));
    v.push_back(coat("coat/ggx.5 ec", new ms::GGXCookTorranceBsdfLobe(mN, 0.5f, W, sm::sBlack, 1.f, 1.5f, false),
                     nk::lobe_cook_torrance(nk::LOBE_GGX, kN, 0.5f, w, nk::col(0.f), 1.f, 1.5f, false)));
    return v;
}

static Vec3 dirAt(float thetaDeg, float phi)
{ const float t = thetaDeg * NK_PI / 180.f; return nk::v3(sinf(t) * cosf(phi), sinf(t) * sinf(phi), cosf(t)); }

static void runParallel(int n, const std::function<void(int)> &fn)
{
    std::atomic<int> next{0};
    std::vector<std::thread> th;
    for (unsigned t = 0; t < std::thread::hardware_concurrency(); ++t) th.emplace_back([&] {
        for (int i = next++; i < n; i = next++) fn(i);
    });
    for (auto &x : th) x.join();
}

int main()
{
    auto *pool = scene_rdl2::util::alignedMallocCtor<scene_rdl2::alloc::ArenaBlockPool>(CACHE_LINE_SIZE);
    scene_rdl2::alloc::Arena arena;
    arena.init(pool);
    gArena = &arena;

    std::vector<Pair *> lobes = buildLobes();
    struct Row { Pair *p; float woT; int light; Means mr, nk; };
    std::vector<Row> rows;
    for (Pair *p : lobes) for (float woT : {45.f, 75.f}) for (int li = 0; li < 3; ++li) rows.push_back({p, woT, li});

    runParallel((int)rows.size(), [&](int i) {
        Row &r = rows[i];
        const float phi = 0.4f;
        const Vec3 wo = dirAt(r.woT, phi);
        const bool T = !r.p->n.reflection;
        Light lt = constantLight();
        // "spec": around the mirror direction, "retro": 20 degrees off wo, towards the normal.
        // A transmission lobe gets the lights on the far side.
        const Vec3 specDir = dirAt(r.woT, phi + NK_PI), retroDir = dirAt(r.woT - 20.f, phi);
        const Vec3 mirrored = nk::v3(specDir.x * (T ? -1.f : 1.f), specDir.y * (T ? -1.f : 1.f), specDir.z * (T ? -1.f : 1.f));
        const Vec3 retro = T ? nk::v3(-retroDir.x, -retroDir.y, -retroDir.z) : retroDir;
        if (r.light == 1) lt = smallLight("small spec", mirrored, 3.f);
        if (r.light == 2) lt = smallLight("small retro", retro, 3.f);
        const int grid = r.light == 0 ? 500 : 1100;
        r.mr = estimate(r.p->m, wo, lt, grid, 8);
        r.nk = estimate(r.p->n, wo, lt, grid, 8);
    });

    printf("One lobe, white, no shadowing. Ratio columns are nl:ni in the MIS weight: w(ni r, nl q).\n");
    printf("bsdf only: the mean of f/r under sample(), which is also what an indirect ray carries.\n");
    printf("light only: the true integral of f Li. 1:1 is MoonRay at its defaults and any one-light-one-lobe MIS on a single lobe.\n");
    printf("se is the standard error of the 1:1 column, from 8 replicas.\n\n");
    printf("%-14s %4s %-12s %8s %8s %8s %8s %8s %8s %8s %8s\n", "lobe", "woT", "light", "bsdf", "light", "1:1", "4:1", "1:4", "se", "kernel1:1", "1:1-kern");
    for (const Row &r : rows) {
        const char *ln[] = {"constant", "small spec", "small retro"};
        printf("%-14s %4.0f %-12s %8.4f %8.4f %8.4f %8.4f %8.4f %8.4f %8.4f %8.4f\n", r.p->name.c_str(), r.woT, ln[r.light],
               r.mr.bsdf, r.mr.light, r.mr.mix[0], r.mr.mix[1], r.mr.mix[2], r.mr.se, r.nk.mix[0], r.mr.mix[0] - r.nk.mix[0]);
    }

    // ---- two lobes on one hit
    Pair *flat = nullptr, *ggx = nullptr, *cg = nullptr, *lam = nullptr;
    for (Pair *p : lobes) {
        if (p->name == "flat r.4") flat = p;
        if (p->name == "ggx r.3") ggx = p;
        if (p->name == "coat/ggx.5 ec") cg = p;
        if (p->name == "lambert") lam = p;
    }
    struct SRow { const char *name; Stack st; float woT; int light; float clamp; float scale; StackMeans m; };
    std::vector<SRow> srows;
    const Stack s1{&ggx->m, &flat->m, 0.5f}, s2{&ggx->m, &lam->m, 0.5f}, s3{&cg->m, &flat->m, 0.3f};
    for (float woT : {45.f}) for (int li = 0; li < 3; ++li) {
        srows.push_back({"ggx.3+flat.4", s1, woT, li, 0.f, 1.f});
        srows.push_back({"ggx.3+lambert", s2, woT, li, 0.f, 1.f});
        srows.push_back({"coat/ggx+flat.4", s3, woT, li, 0.f, 1.f});
    }
    // sample_clamping_value 10 with bsdf_samples 2: PathIntegrator.cc:1150 gives 2.5 per sample at depth >= 1
    srows.push_back({"ggx.3+flat.4 clamp", s1, 45.f, 1, 2.5f, 40.f});
    srows.push_back({"ggx.3+flat.4 clamp", s1, 45.f, 2, 2.5f, 40.f});
    srows.push_back({"ggx.3+flat.4 clamp", s1, 45.f, 0, 2.5f, 40.f});
    runParallel((int)srows.size(), [&](int i) {
        SRow &r = srows[i];
        const float phi = 0.4f;
        const Vec3 wo = dirAt(r.woT, phi);
        Light lt = constantLight();
        if (r.light == 1) lt = smallLight("", dirAt(r.woT, phi + NK_PI), 3.f, r.scale);
        if (r.light == 2) lt = smallLight("", dirAt(r.woT - 20.f, phi), 3.f, r.scale);
        if (r.light == 0) lt.L = r.scale;
        r.m = estimateStack(r.st, wo, lt, r.light == 0 ? 2000 : 3000, r.clamp);
    });
    printf("\nTwo lobes on one hit, MoonRay's lobes. moonray = every lobe, each weighted alone (the target).\n");
    printf("held = pick one lobe with P, weight it alone with MoonRay's w, divide by P. combined = one-lobe MIS of Cycles and pbrt.\n");
    printf("clamp rows: per sample clamp 2.5, light 40 times brighter. held clamps before the division by P, badclamp after it.\n\n");
    printf("%-20s %-12s %9s %9s %9s %9s | %-9s %9s %9s %9s\n", "stack", "light", "moonray", "held", "combined", "badclamp", "indirect:", "moonray", "held", "combined");
    for (const SRow &r : srows) {
        const char *ln[] = {"constant", "small spec", "small retro"};
        printf("%-20s %-12s %9.4f %9.4f %9.4f %9.4f | %-9s %9.4f %9.4f %9.4f\n", r.name, ln[r.light], r.m.moonray, r.m.held, r.m.combined,
               r.m.heldBad, "", r.m.indM, r.m.indHeld, r.m.indComb);
    }
    fflush(stdout);
    _exit(0);   // MoonRay objects are leaked on purpose
}
