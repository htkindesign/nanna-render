// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Kiet Ho
// Numeric check of lobes.h against MoonRay's own C++ lobes, linked from the package's
// rendering_shading.dll. Same inputs go to both, the outputs are compared.
// Also runs a white furnace, and writes out\cases.bin for the GPU check.
#include <moonray/rendering/shading/bsdf/BsdfLambert.h>
#include <moonray/rendering/shading/bsdf/BsdfOrenNayar.h>
#include <moonray/rendering/shading/bsdf/cook_torrance/BsdfCookTorrance.h>
#include <moonray/rendering/shading/bsdf/under/BsdfUnder.h>
#include <moonray/rendering/shading/bsdf/under/BsdfUnderClearcoat.h>
#include <moonray/rendering/shading/bsdf/npr/BsdfFlatDiffuse.h>
#include <moonray/rendering/shading/bsdf/BsdfMirror.h>
#include <moonray/rendering/shading/bsdf/BsdfIridescence.h>
#include <moonray/rendering/shading/bsdf/fabric/BsdfFabric.h>
#include <moonray/rendering/shading/bsdf/fabric/VelvetAlbedo.h>
#include <moonray/rendering/shading/bsdf/Fresnel.h>
#include <moonray/rendering/shading/BsdfComponent.h>
#include <moonray/rendering/shading/LobeAttenuator.h>
#include <moonray/rendering/shading/bsdf/cook_torrance/energy_compensation/ReflectionAlbedo.h>
#include <moonray/rendering/shading/bsdf/cook_torrance/energy_compensation/TransmissionAlbedo.h>
#include <scene_rdl2/render/util/Arena.h>

#ifdef NK_TEST_SSE_RSQRT
#define NK_RSQRT_HOOK scene_rdl2::math::rsqrt
#define NK_ACOS_HOOK scene_rdl2::math::acos
#endif
#include "lobes.h"
#include "lobe_tables.h"
#include "gpu_cases.h"

#include <algorithm>
#include <cstdio>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace ms = moonray::shading;
namespace sm = scene_rdl2::math;
using sm::Vec3f;
using sm::Color;

static nk::LobeTables gT = {nk_tbl_beckmann_e, nk_tbl_ggx_e, nk_tbl_beckmann_one_minus_eavg,
                            nk_tbl_ggx_one_minus_eavg, nk_tbl_trans_e, nk_tbl_trans_e_exit,
                            nk_tbl_trans_one_minus_eavg, nk_tbl_trans_one_minus_eavg_exit,
                            nk_tbl_velvet_norm, nk_tbl_velvet_albedo};

static Vec3f toM(nk::Vec3 v) { return Vec3f(v.x, v.y, v.z); }
static Color toM(nk::Col c) { return Color(c.r, c.g, c.b); }
static nk::Vec3 toN(const Vec3f &v) { return nk::v3(v.x, v.y, v.z); }
static nk::Col toN(const Color &c) { return nk::col(c.r, c.g, c.b); }

// ------------------------------------------------------------------ test objects
struct Fr { ms::Fresnel *mr; nk::Fresnel n; };
static Fr frNone() { return {nullptr, nk::fresnel_none()}; }
static Fr frDiel(float etaI, float etaT, float w = 1.f)
{ auto *f = new ms::DielectricFresnel(etaI, etaT); f->setWeight(w); return {f, nk::fresnel_dielectric(etaI, etaT, w)}; }
static Fr frCond(nk::Col eta, nk::Col k, float w = 1.f)
{ auto *f = new ms::ConductorFresnel(toM(eta), toM(k)); f->setWeight(w); return {f, nk::fresnel_conductor(eta, k, w)}; }
static Fr frOneMinus(Fr t) { return {new ms::OneMinusFresnel(t.mr), nk::fresnel_one_minus(t.n)}; }
static Fr frOneMinusRough(Fr t, float r) { return {new ms::OneMinusRoughFresnel(t.mr, r), nk::fresnel_one_minus_rough(t.n, r)}; }

struct Config {
    std::string family, name;
    ms::BsdfLobe *mr;
    nk::LayeredLobe n;
    Vec3f Ng;
    int shadowFix;
};

static ms::BsdfLobe *makeCT(int kind, Vec3f N, float r, Color favg, Color favgInv, float etaI, float etaT, bool coupled)
{
    if (kind == nk::LOBE_GGX) return new ms::GGXCookTorranceBsdfLobe(N, r, favg, favgInv, etaI, etaT, coupled);
    return new ms::CookTorranceBsdfLobe(N, r, favg, favgInv, etaI, etaT, coupled);
}

static scene_rdl2::alloc::Arena *gArena;
static std::vector<Config> gConfigs;

static void add(const std::string &fam, const std::string &name, ms::BsdfLobe *mr, const nk::LayeredLobe &n,
                Vec3f Ng, int fix = 0)
{ gConfigs.push_back({fam, name, mr, n, Ng, fix}); }

static void setF(ms::BsdfLobe *m, nk::Lobe &n, Fr f, nk::Col scale = nk::col(1.f))
{ m->setFresnel(f.mr); n.fresnel = f.n; m->setScale(toM(scale)); n.scale = scale; }

static const float kRough[] = {0.0f, 0.02f, 0.1f, 0.2f, 1.f / 3.f, 0.5f, 0.6f, 0.75f, 0.9f, 1.0f};
static const nk::Col kGoldEta = {0.143f, 0.375f, 1.442f}, kGoldK = {3.983f, 2.386f, 1.603f};
static const nk::Col kCopperEta = {0.2f, 0.92f, 1.1f}, kCopperK = {3.9f, 2.45f, 2.14f};
static const nk::Col kIronEta = {2.91f, 2.95f, 2.58f}, kIronK = {3.08f, 2.93f, 2.77f};

static void buildConfigs()
{
    const nk::Vec3 Ns[] = {nk::v3(0, 0, 1), nk::v3(0.48f, -0.6f, 0.64f)};
    for (nk::Vec3 N : Ns) {
        Vec3f mN = toM(N);
        const std::string sN = N.z == 1.f ? "Nz" : "Ntilt";
        // ---- Lambert, Oren-Nayar
        Fr covers[] = {frNone(), frOneMinus(frDiel(1, 1.5f)), frOneMinusRough(frDiel(1, 1.5f), 0.4f)};
        const char *coverName[] = {"none", "1-F", "1-roughF"};
        nk::Col alb = nk::col(0.8f, 0.5f, 0.2f);
        for (int c = 0; c < 3; ++c) {
            for (int refl = 0; refl < 2; ++refl) {
                auto *m = new ms::LambertBsdfLobe(mN, toM(alb), refl != 0);
                nk::Lobe n = nk::lobe_lambert(N, alb, refl != 0);
                setF(m, n, covers[c], nk::col(0.9f, 0.8f, 0.7f));
                add("lambert", sN + " " + coverName[c] + (refl ? " R" : " T"), m, nk::layered(n), mN);
                for (float r : {0.0f, 0.1f, 0.3f, 0.5f, 0.8f, 1.0f}) {
                    auto *mo = new ms::OrenNayarBsdfLobe(mN, toM(alb), r, refl != 0);
                    nk::Lobe no = nk::lobe_oren_nayar(N, alb, r, refl != 0);
                    setF(mo, no, covers[c], nk::col(0.9f, 0.8f, 0.7f));
                    add("oren_nayar", sN + " " + coverName[c] + (refl ? " R r=" : " T r=") + std::to_string(r), mo, nk::layered(no), mN);
                }
            }
        }
        // ---- flat diffuse, DwaBase's own diffuse reflection
        for (int c = 0; c < 3; ++c) for (int refl = 0; refl < 2; ++refl)
        for (float r : {0.0f, 0.3f, 0.8f}) for (float sh : {0.0f, 0.4f, 1.0f}) for (float fl : {0.0f, 0.5f, 1.0f}) for (float ff : {0.0f, 0.5f, 1.0f}) {
            auto *m = new ms::FlatDiffuseBsdfLobe(mN, toM(alb), r, sh, fl, ff, refl != 0);
            nk::Lobe n = nk::lobe_flat_diffuse(N, alb, r, sh, fl, ff, refl != 0);
            setF(m, n, covers[c], nk::col(0.9f, 0.8f, 0.7f));
            add(sh == 0.f && fl == 0.f ? "flat_default" : "flat_npr", sN + " " + coverName[c] + (refl ? " R" : " T") + " r=" + std::to_string(r) + " sh=" + std::to_string(sh) +
                " fl=" + std::to_string(fl) + " ff=" + std::to_string(ff), m, nk::layered(n), mN);
        }
        // ---- Cook-Torrance
        struct FS { const char *name; Fr f; };
        std::vector<FS> specs = {{"noF", frNone()}, {"ior1.1", frDiel(1, 1.1f)}, {"ior1.5", frDiel(1, 1.5f)},
                                 {"ior2.5", frDiel(1, 2.5f, 0.7f)}, {"gold", frCond(kGoldEta, kGoldK)},
                                 {"copper", frCond(kCopperEta, kCopperK)}, {"iron", frCond(kIronEta, kIronK, 0.8f)}};
        for (int kind : {nk::LOBE_GGX, nk::LOBE_BECKMANN}) {
            std::string fam = kind == nk::LOBE_GGX ? "ggx" : "beckmann";
            for (float r : kRough) for (auto &s : specs) {
                for (int ec = 0; ec < 3; ++ec) {
                    nk::Col favg = ec == 0 ? nk::col(0.f) : ec == 1 ? nk::col(1.f) : nk::col(0.55f, 0.6f, 0.1f);
                    auto *m = makeCT(kind, mN, r, toM(favg), sm::sBlack, 1.f, 1.5f, false);
                    nk::Lobe n = nk::lobe_cook_torrance(kind, N, r, favg, nk::col(0.f), 1.f, 1.5f, false);
                    setF(m, n, s.f);
                    add((ec ? fam + "_ec" : fam) + (r < 0.1f ? "_sharp" : ""), sN + " " + s.name + " r=" + std::to_string(r) + " ec=" + std::to_string(ec), m, nk::layered(n), mN);
                }
                // reflection coupled with transmission, entering and exiting
                for (int side = 0; side < 2; ++side) {
                    const float etaI = side ? 1.5f : 1.f, etaT = side ? 1.f : 1.5f;
                    float favg, favgInv;
                    nk::average_fresnel(etaT / etaI, favg, favgInv);
                    auto *m = makeCT(kind, mN, r, Color(favg), Color(favgInv), etaI, etaT, true);
                    nk::Lobe n = nk::lobe_cook_torrance(kind, N, r, nk::col(favg), nk::col(favgInv), etaI, etaT, true);
                    setF(m, n, s.f);
                    add(fam + "_coupled" + (r < 0.1f ? "_sharp" : ""), sN + " " + s.name + " r=" + std::to_string(r) + " side=" + std::to_string(side), m, nk::layered(n), mN);
                }
            }
        }
        // ---- clearcoat over one base lobe
        struct Base { const char *name; int kind; float r; Fr f; bool ec; };
        Base bases[] = {{"lambert", nk::LOBE_LAMBERT, 0, frNone(), false},
                        {"oren.5", nk::LOBE_OREN_NAYAR, 0.5f, frNone(), false},
                        {"ggx.2 gold", nk::LOBE_GGX, 0.2f, frCond(kGoldEta, kGoldK), true},
                        {"ggx.7 gold", nk::LOBE_GGX, 0.7f, frCond(kGoldEta, kGoldK), true},
                        {"beck.4 ior1.5", nk::LOBE_BECKMANN, 0.4f, frDiel(1, 1.5f), false}};
        for (Base &b : bases) for (float ccIor : {1.3f, 1.5f, 1.8f}) for (float ccR : {0.f, 0.1f, 0.5f})
        for (float thick : {0.f, 0.1f, 1.f}) for (int refr = 0; refr < 2; ++refr) for (int tir = 0; tir < 2; ++tir) {
            if (!refr && !tir) continue;
            nk::Col favg = b.ec ? nk::col(1.f) : nk::col(0.f);
            ms::BsdfLobe *m; nk::Lobe n;
            if (b.kind == nk::LOBE_LAMBERT) { m = new ms::LambertBsdfLobe(mN, toM(alb), true); n = nk::lobe_lambert(N, alb); }
            else if (b.kind == nk::LOBE_OREN_NAYAR) { m = new ms::OrenNayarBsdfLobe(mN, toM(alb), b.r, true); n = nk::lobe_oren_nayar(N, alb, b.r); }
            else { m = makeCT(b.kind, mN, b.r, toM(favg), sm::sBlack, 1.f, 1.5f, false);
                   n = nk::lobe_cook_torrance(b.kind, N, b.r, favg, nk::col(0.f), 1.f, 1.5f, false); }
            setF(m, n, b.f);
            Fr top = frDiel(1.f, ccIor, 0.9f);
            Fr om = ccR == 0.f ? frOneMinus(top) : frOneMinusRough(top, ccR);
            const nk::Col att = nk::col(0.6f, 0.8f, 0.9f);
            nk::LayeredLobe l = nk::layered(n);
            l.count = 1;
            ms::BsdfLobe *w;
            if (refr) {
                w = new ms::UnderClearcoatBsdfLobe(m, gArena, mN, 1.f, ccIor, thick, toM(att), 0.9f, tir != 0);
                l.layers[0] = nk::under_clearcoat(N, om.n, 1.f, ccIor, thick, att, 0.9f, tir != 0);
            } else {
                w = new ms::UnderBsdfLobe(m, mN, thick, toM(att), 0.9f);
                l.layers[0] = nk::under_plain(N, om.n, thick, att, 0.9f);
            }
            w->setFresnel(om.mr);
            std::string nm = sN + " " + b.name + " cc=" + std::to_string(ccIor) + " r=" + std::to_string(ccR) +
                             " t=" + std::to_string(thick) + " refr=" + std::to_string(refr) + " tir=" + std::to_string(tir);
            add(refr ? (tir ? "clearcoat" : "clearcoat_noTIR") : "under_plain", nm, w, l, mN);
        }
        // ---- the real DwaBase order: clearcoat, then dielectric spec, then diffuse.
        // lobe list: spec is under the coat; diffuse is under spec and coat.
        for (float ccIor : {1.3f, 1.5f}) for (float specR : {0.2f, 0.5f}) for (int ty = 0; ty < 2; ++ty) for (float diffR : {0.0f, 0.4f}) {
            auto *lam = new ms::FlatDiffuseBsdfLobe(mN, toM(alb), diffR, 0.f, 0.f, 0.f, true);
            nk::Lobe nl = nk::lobe_flat_diffuse(N, alb, diffR, 0.f, 0.f, 0.f);
            Fr spec = frDiel(ccIor, 1.5f);              // spec is lit from inside the coat
            Fr coat = frDiel(1.f, ccIor);
            Fr omSpec = frOneMinusRough(spec, specR);
            Fr omCoat = frOneMinusRough(coat, 0.1f);
            const nk::Col att = nk::col(0.6f, 0.8f, 0.9f);
            ms::BsdfLobe *inner = new ms::UnderBsdfLobe(lam, mN);
            inner->setFresnel(omSpec.mr);
            ms::BsdfLobe *outer;
            nk::LayeredLobe l = nk::layered(nl);
            l.count = 2;
            l.layers[1] = nk::under_plain(N, omSpec.n, 0.f, nk::col(1.f), 1.f);
            if (ty == 0) {
                outer = new ms::UnderClearcoatBsdfLobe(inner, gArena, mN, 1.f, ccIor, 0.2f, toM(att), 1.f, true);
                l.layers[0] = nk::under_clearcoat(N, omCoat.n, 1.f, ccIor, 0.2f, att, 1.f, true);
            } else {
                outer = new ms::UnderBsdfLobe(inner, mN, 0.2f, toM(att), 1.f);
                l.layers[0] = nk::under_plain(N, omCoat.n, 0.2f, att, 1.f);
            }
            outer->setFresnel(omCoat.mr);
            add("stack_2layer", sN + " cc=" + std::to_string(ccIor) + " specR=" + std::to_string(specR) + " ty=" + std::to_string(ty) + " dr=" + std::to_string(diffR), outer, l, mN);
        }
        // ---- MoonRay's own ClearcoatAttenuator object, as BsdfBuilder uses it
        for (float ccR : {0.f, 0.3f}) for (int refr = 0; refr < 2; ++refr) for (float etaT : {1.f, 1.5f}) {
            Fr top = frDiel(1.f, etaT, 0.8f);
            const nk::Col att = nk::col(0.6f, 0.8f, 0.9f);
            ms::ClearcoatAttenuator a(gArena, mN, ccR, 1.f, etaT, refr != 0, 0.2f, toM(att), 0.8f, top.mr);
            auto *m = new ms::FlatDiffuseBsdfLobe(mN, toM(alb), 0.f, 0.f, 0.f, 0.f, true);
            ms::BsdfLobe *w = a(gArena, m);
            nk::LayeredLobe l = nk::layered(nk::lobe_flat_diffuse(N, alb, 0.f, 0.f, 0.f, 0.f));
            l.count = 1;
            l.layers[0] = nk::clearcoat_attenuator(N, top.n, ccR, 1.f, etaT, refr != 0, 0.2f, att, 0.8f);
            add("attenuator", sN + " ccR=" + std::to_string(ccR) + " refr=" + std::to_string(refr) + " etaT=" + std::to_string(etaT), w, l, mN);
        }
    }
    // ---- the lobes added for the full DwaBase list
    for (nk::Vec3 N : Ns) {
        Vec3f mN = toM(N);
        const std::string sN = N.z == 1.f ? "Nz" : "Ntilt";
        const nk::Col alb = nk::col(0.8f, 0.5f, 0.2f);
        const nk::Col tint = nk::col(0.9f, 0.75f, 0.6f), scale = nk::col(0.9f, 0.8f, 0.7f), att = nk::col(0.6f, 0.8f, 0.9f);
        Fr none = frNone();
        struct Eta { float i, t; };
        const Eta etas[] = {{1.f, 1.5f}, {1.5f, 1.f}, {1.f, 1.33f}, {1.33f, 1.f}, {1.f, 2.2f}};
        // ---- microfacet BTDF. r=0 stays in, BsdfBuilder only swaps to a mirror at relative ior 1
        for (float r : kRough) for (const Eta &e : etas) for (float abbe : {0.f, 40.f}) for (int c = 0; c < 3; ++c) {
            float favg, favgInv;
            nk::average_fresnel(e.t / e.i, favg, favgInv);
            Fr top = frDiel(e.i, e.t);
            Fr f = c == 0 ? none : c == 1 ? frOneMinus(top) : frOneMinusRough(top, 0.4f);
            auto *m = new ms::TransmissionCookTorranceBsdfLobe(mN, r, e.i, e.t, toM(tint), favg, favgInv, abbe);
            nk::Lobe n = nk::lobe_btdf(N, r, e.i, e.t, tint, favg, favgInv, abbe);
            setF(m, n, f, scale);
            add(std::string("btdf") + (abbe != 0.f ? "_disp" : "") + (r < 0.1f ? "_sharp" : ""),
                sN + " r=" + std::to_string(r) + " eta=" + std::to_string(e.i) + "/" + std::to_string(e.t) + " c=" + std::to_string(c), m, nk::layered(n), mN);
        }
        // ---- mirrors
        for (int c = 0; c < 3; ++c) {
            Fr f = c == 0 ? none : c == 1 ? frDiel(1, 1.5f, 0.8f) : frCond(kGoldEta, kGoldK);
            auto *m = new ms::MirrorReflectionBsdfLobe(mN);
            nk::Lobe n = nk::lobe_mirror_reflection(N);
            setF(m, n, f, scale);
            add("mirror_r", sN + " f=" + std::to_string(c), m, nk::layered(n), mN);
        }
        for (const Eta &e : etas) for (float abbe : {0.f, 40.f}) for (int c = 0; c < 2; ++c) {
            Fr top = frDiel(e.i, e.t);
            Fr f = c == 0 ? none : frOneMinus(top);
            auto *m = new ms::MirrorTransmissionBsdfLobe(mN, e.i, e.t, toM(tint), abbe);
            nk::Lobe n = nk::lobe_mirror_transmission(N, e.i, e.t, tint, abbe);
            setF(m, n, f, scale);
            add(abbe != 0.f ? "mirror_t_disp" : "mirror_t", sN + " eta=" + std::to_string(e.i) + "/" + std::to_string(e.t) + " c=" + std::to_string(c), m, nk::layered(n), mN);
        }
        // ---- anisotropic Cook-Torrance. Beckmann: MoonRay has no anisotropic GGX
        const nk::Frame tf = nk::frame_from_n(N);
        const nk::Vec3 T = tf.x + 0.5f * tf.y;
        const float tl = 1.f / sqrtf(nk::dot(T, T));
        const nk::Vec3 Tn = tl * T;
        for (float ru : {0.0f, 0.1f, 0.3f, 0.6f, 1.0f}) for (float rv : {0.05f, 0.3f, 0.8f}) for (int c = 0; c < 3; ++c) {
            Fr f = c == 0 ? none : c == 1 ? frDiel(1, 1.5f) : frCond(kCopperEta, kCopperK);
            auto *m = new ms::AnisoCookTorranceBsdfLobe(mN, toM(Tn), ru, rv);
            nk::Lobe n = nk::lobe_aniso(N, Tn, ru, rv);
            setF(m, n, f);
            add(ru < 0.1f ? "aniso_sharp" : "aniso", sN + " ru=" + std::to_string(ru) + " rv=" + std::to_string(rv) + " c=" + std::to_string(c), m, nk::layered(n), mN);
        }
        // ---- velvet, and the attenuator it puts over everything below it
        for (float r : {0.05f, 0.1f, 0.3f, 0.5f, 0.77f, 1.0f}) for (int c = 0; c < 2; ++c) {
            Fr f = c == 0 ? none : frOneMinus(frDiel(1, 1.5f));
            const nk::Col vc = nk::col(0.7f, 0.4f, 0.3f);
            auto *m = new ms::FabricVelvetBsdfLobe(mN, r, toM(vc));
            nk::Lobe n = nk::lobe_velvet(gT, N, r, vc);
            setF(m, n, f, scale);
            add("velvet", sN + " r=" + std::to_string(r) + " c=" + std::to_string(c), m, nk::layered(n), mN);
            for (int abs = 0; abs < 2; ++abs) for (float w : {0.3f, 1.f}) {
                ms::VelvetBRDF brdf(mN, r, toM(vc), abs != 0);
                ms::VelvetAttenuator a(gArena, brdf, w);
                auto *under = new ms::FlatDiffuseBsdfLobe(mN, toM(alb), 0.4f, 0.f, 0.f, 0.f, true);
                ms::BsdfLobe *wr = a(gArena, under);
                nk::LayeredLobe l = nk::layered(nk::lobe_flat_diffuse(N, alb, 0.4f, 0.f, 0.f, 0.f));
                nk::layered_push(l, nk::velvet_attenuator(N, r, vc, abs != 0, w));
                add("velvet_atten", sN + " r=" + std::to_string(r) + " abs=" + std::to_string(abs) + " w=" + std::to_string(w), wr, l, mN);
            }
        }
        // ---- transmission under a clearcoat, through MoonRay's own ClearcoatAttenuator
        for (float ccR : {0.f, 0.3f}) for (float thick : {0.f, 0.2f}) for (float r : {0.f, 0.2f, 0.6f}) for (int mir = 0; mir < 2; ++mir) {
            if (mir && r != 0.f) continue;
            const float etaI = 1.3f, etaT = 1.5f;
            float favg, favgInv;
            nk::average_fresnel(etaT / etaI, favg, favgInv);
            Fr top = frDiel(1.f, etaI, 0.8f);
            Fr spec = frDiel(etaI, etaT);
            Fr om = frOneMinus(spec);
            ms::BsdfLobe *m; nk::Lobe n;
            if (mir) { m = new ms::MirrorTransmissionBsdfLobe(mN, etaI, etaT, toM(tint)); n = nk::lobe_mirror_transmission(N, etaI, etaT, tint); }
            else     { m = new ms::TransmissionCookTorranceBsdfLobe(mN, r, etaI, etaT, toM(tint), favg, favgInv);
                       n = nk::lobe_btdf(N, r, etaI, etaT, tint, favg, favgInv); }
            setF(m, n, om);
            ms::ClearcoatAttenuator a(gArena, mN, ccR, 1.f, etaI, true, thick, toM(att), 0.8f, top.mr);
            ms::BsdfLobe *w = a(gArena, m);
            nk::LayeredLobe l = nk::layered(n);
            nk::layered_push(l, nk::clearcoat_attenuator(N, top.n, ccR, 1.f, etaI, true, thick, att, 0.8f, false));
            add("coat_transmission", sN + " ccR=" + std::to_string(ccR) + " t=" + std::to_string(thick) + " r=" + std::to_string(r) + " mir=" + std::to_string(mir), w, l, mN);
        }
        // ---- the real order with everything on: fuzz over clearcoat over a coupled dielectric
        for (float r : {0.3f, 0.7f}) for (int refl = 0; refl < 2; ++refl) {
            const float etaI = 1.3f, etaT = 1.5f;
            float favg, favgInv;
            nk::average_fresnel(etaT / etaI, favg, favgInv);
            Fr spec = frDiel(etaI, etaT);
            Fr coat = frDiel(1.f, etaI, 0.8f);
            ms::VelvetBRDF brdf(mN, 0.4f, Color(0.5f), true);
            ms::VelvetAttenuator aV(gArena, brdf, 0.5f);
            ms::ClearcoatAttenuator aC(gArena, mN, 0.2f, 1.f, etaI, true, 0.1f, toM(att), 0.8f, coat.mr);
            ms::BsdfLobe *m; nk::Lobe n;
            if (refl) { m = makeCT(nk::LOBE_GGX, mN, r, Color(favg), Color(favgInv), etaI, etaT, true);
                        n = nk::lobe_cook_torrance(nk::LOBE_GGX, N, r, nk::col(favg), nk::col(favgInv), etaI, etaT, true);
                        setF(m, n, spec); }
            else      { m = new ms::TransmissionCookTorranceBsdfLobe(mN, r, etaI, etaT, toM(tint), favg, favgInv);
                        n = nk::lobe_btdf(N, r, etaI, etaT, tint, favg, favgInv);
                        setF(m, n, frOneMinus(spec)); }
            ms::BsdfLobe *w = aC(gArena, m);
            w = aV(gArena, w);
            nk::LayeredLobe l = nk::layered(n);
            nk::layered_push(l, nk::velvet_attenuator(N, 0.4f, nk::col(0.5f), true, 0.5f));
            nk::layered_push(l, nk::clearcoat_attenuator(N, coat.n, 0.2f, 1.f, etaI, true, 0.1f, att, 0.8f, refl != 0));
            add("stack_fuzz_coat", sN + " r=" + std::to_string(r) + " refl=" + std::to_string(refl), w, l, mN);
        }
        // ---- iridescence, hue mode, on glossy and mirror, alone and under a coat
        struct Iri { nk::Col p, s; int flip; float thick, exp; };
        const Iri iris[] = {{nk::col(1.f, 0.f, 0.f), nk::col(1.f, 0.f, 0.f), 0, 2.f, 1.f},
                            {nk::col(1.f, 0.f, 0.f), nk::col(1.f, 0.f, 0.f), 1, 2.f, 1.f},
                            {nk::col(0.f, 1.f, 0.f), nk::col(0.f, 0.f, 1.f), 0, 1.5f, 2.f},
                            {nk::col(0.f, 0.f, 1.f), nk::col(1.f, 1.f, 0.f), 0, 3.f, 1.f},
                            {nk::col(0.f, 1.f, 0.f), nk::col(0.f, 0.f, 1.f), 1, 1.5f, 2.f},
                            {nk::col(1.f, 0.5f, 0.f), nk::col(0.2f, 0.f, 0.9f), 1, 0.7f, 0.5f}};
        for (const Iri &ir : iris) for (int mir = 0; mir < 2; ++mir) for (int coatIt = 0; coatIt < 2; ++coatIt) {
            ms::BsdfLobe *child; nk::Lobe n;
            Fr f = frDiel(1, 1.5f);
            if (mir) { child = new ms::MirrorReflectionBsdfLobe(mN); n = nk::lobe_mirror_reflection(N); }
            else     { child = makeCT(nk::LOBE_GGX, mN, 0.3f, sm::sBlack, sm::sBlack, 1.f, 1.5f, false);
                       n = nk::lobe_cook_torrance(nk::LOBE_GGX, N, 0.3f, nk::col(0.f), nk::col(0.f), 1.f, 1.5f, false); }
            setF(child, n, f);
            n.iri.strength = 0.8f; n.iri.N = N; n.iri.primary = ir.p; n.iri.secondary = ir.s; n.iri.flipHue = ir.flip;
            n.iri.thickness = ir.thick; n.iri.exponent = ir.exp; n.iri.at0 = 0.3f; n.iri.at90 = 1.f; n.iri.scale = scale;
            ms::BsdfLobe *w = new ms::IridescenceBsdfLobe(child, mN, 0.8f, ispc::SHADING_IRIDESCENCE_COLOR_USE_HUE_INTERPOLATION,
                toM(ir.p), toM(ir.s), ir.flip != 0, (ispc::ColorRampControlSpace)0, 0, nullptr, nullptr, nullptr, ir.thick, ir.exp, 0.3f, 1.f);
            w->setScale(toM(scale));
            nk::LayeredLobe l = nk::layered(n);
            if (coatIt) {
                Fr top = frDiel(1.f, 1.4f);
                ms::ClearcoatAttenuator a(gArena, mN, 0.2f, 1.f, 1.4f, true, 0.f, sm::sWhite, 1.f, top.mr);
                w = a(gArena, w);
                nk::layered_push(l, nk::clearcoat_attenuator(N, top.n, 0.2f, 1.f, 1.4f, true, 0.f, nk::col(1.f), 1.f));
            }
            add("iridescence", sN + " hue=" + std::to_string(ir.p.r) + std::to_string(ir.s.b) + " flip=" + std::to_string(ir.flip) + " mir=" + std::to_string(mir) + " coat=" + std::to_string(coatIt), w, l, mN);
        }
    }
    // ---- shadow terminator modes, with a shading normal tilted off Ng
    nk::Vec3 N = nk::v3(0.0f, 0.2425356f, 0.9701425f);
    Vec3f mN = toM(N), Ng(0, 0, 1);
    nk::Col alb = nk::col(0.8f, 0.5f, 0.2f);
    for (int fix = 1; fix <= 4; ++fix) {
        auto *m = new ms::LambertBsdfLobe(mN, toM(alb), true);
        add("shadow_fix", "lambert fix=" + std::to_string(fix), m, nk::layered(nk::lobe_lambert(N, alb)), Ng, fix);
        auto *o = new ms::OrenNayarBsdfLobe(mN, toM(alb), 0.5f, true);
        add("shadow_fix", "oren fix=" + std::to_string(fix), o, nk::layered(nk::lobe_oren_nayar(N, alb, 0.5f)), Ng, fix);
        auto *fd = new ms::FlatDiffuseBsdfLobe(mN, toM(alb), 0.5f, 0.2f, 0.5f, 0.5f, true);
        add("shadow_fix", "flat fix=" + std::to_string(fix), fd, nk::layered(nk::lobe_flat_diffuse(N, alb, 0.5f, 0.2f, 0.5f, 0.5f)), Ng, fix);
        for (int kind : {nk::LOBE_GGX, nk::LOBE_BECKMANN}) {
            auto *c = makeCT(kind, mN, 0.4f, sm::sWhite, sm::sBlack, 1.f, 1.5f, false);
            add("shadow_fix", "ct kind=" + std::to_string(kind) + " fix=" + std::to_string(fix), c,
                nk::layered(nk::lobe_cook_torrance(kind, N, 0.4f, nk::col(1.f), nk::col(0.f), 1.f, 1.5f, false)), Ng, fix);
        }
    }
}

// ------------------------------------------------------------------ statistics
struct Stat {
    double sum = 0, maxRel = 0, maxAbs = 0; long long n = 0, over1e3 = 0; std::string worst;
    void add(double a, double b, const std::string *ctx = nullptr) {
        if (!(a == a) && !(b == b)) return;                 // both NaN: same
        double abse = fabs(a - b);
        double rel = abse / std::max({fabs(a), fabs(b), 1e-5});
        if (!(rel == rel) || std::isinf(a) != std::isinf(b)) rel = 1e30;
        if (std::isinf(a) && std::isinf(b)) rel = abse = 0;
        sum += rel; ++n; maxAbs = std::max(maxAbs, abse);
        if (rel > 1e-3) ++over1e3;
        if (rel > maxRel) { maxRel = rel; if (ctx) worst = *ctx + "  mine=" + std::to_string(a) + " moonray=" + std::to_string(b); }
    }
    void addAbs(double e, const std::string *ctx) {        // distance, no denominator
        sum += e; ++n; maxAbs = std::max(maxAbs, e);
        if (e > 1e-3) ++over1e3;
        if (e > maxRel) { maxRel = e; worst = *ctx + "  dist=" + std::to_string(e); }
    }
    void merge(const Stat &o) {
        sum += o.sum; n += o.n; over1e3 += o.over1e3; maxAbs = std::max(maxAbs, o.maxAbs);
        if (o.maxRel > maxRel) { maxRel = o.maxRel; worst = o.worst; }
    }
};
static void addC(Stat &s, const nk::Col &a, const Color &b, const std::string &ctx)
{ s.add(a.r, b.r, &ctx); s.add(a.g, b.g, &ctx); s.add(a.b, b.b, &ctx); }

enum { M_F, M_PDF, M_SAMPLE_WI, M_SAMPLE_F, M_SAMPLE_PDF, M_SAMPLE_WEIGHT, M_ALBEDO, M_SELF_F, M_SELF_W, M_COUNT };
static const char *kMetric[] = {"eval f", "eval pdf", "sample wi", "sample f", "sample pdf", "sample f/pdf", "albedo", "self 1ulp", "self f/pdf"};
typedef std::map<std::string, std::array<Stat, M_COUNT>> Table;

// ------------------------------------------------------------------ direction grids
static nk::Vec3 sphere(const nk::Frame &fr, float thetaDeg, float phi)
{
    const float t = thetaDeg * 3.14159265f / 180.f;
    return nk::frame_to_global(fr, nk::v3(sinf(t) * cosf(phi), sinf(t) * sinf(phi), cosf(t)));
}
static const float kWoTheta[] = {0.f, 1.f, 15.f, 30.f, 45.f, 60.f, 75.f, 85.f, 89.5f, 120.f};
static const float kWiTheta[] = {0.f, 1.f, 5.f, 15.f, 30.f, 45.f, 60.f, 75.f, 85.f, 89.f, 89.9f, 100.f, 135.f};

static void runConfig(const Config &c, Table &tab, std::vector<GpuCase> *cases, int cfgIndex)
{
    auto &row = tab[c.family];
    const nk::Frame fr = nk::frame_from_n(c.n.base.frame.z);
    const nk::Vec3 Ng = toN(c.Ng);
    long long idx = 0;
    unsigned rng = 12345u + cfgIndex;
    auto rnd = [&]() { rng = rng * 1664525u + 1013904223u; return (rng >> 8) * (1.0f / 16777216.0f); };
    for (float tWo : kWoTheta) for (int pWo = 0; pWo < 3; ++pWo) for (int cosFlag = 0; cosFlag < 2; ++cosFlag) {
        const nk::Vec3 wo = sphere(fr, tWo, 0.37f + 2.1f * pWo);
        const nk::Slice ns = nk::make_slice(Ng, wo, cosFlag != 0, c.shadowFix);
        const ms::BsdfSlice ms(c.Ng, toM(wo), cosFlag != 0, true, (ms::ShadowTerminatorFix)c.shadowFix);
        char ctx[256];
        snprintf(ctx, sizeof ctx, "[%s] woT=%g", c.name.c_str(), tWo);
        std::string sctx = ctx;
        addC(row[M_ALBEDO], nk::layered_albedo_from(gT, c.n, 0, ns), c.mr->albedo(ms), sctx);
        for (float tWi : kWiTheta) for (int pWi = 0; pWi < 4; ++pWi) {
            const nk::Vec3 wi = sphere(fr, tWi, 1.1f + 1.3f * pWi);
            float pM = -1.f, pN = -1.f;
            const Color fM = c.mr->eval(ms, toM(wi), &pM);
            const nk::Col fN = nk::layered_eval(gT, c.n, ns, wi, &pN);
            std::string e = sctx + " wiT=" + std::to_string(tWi);
            addC(row[M_F], fN, fM, e);
            // MoonRay against itself with wi moved by one float ulp: the rounding floor
            Vec3f wiU = toM(wi);
            wiU.x = nextafterf(wiU.x, 2.f);
            const Color fU = c.mr->eval(ms, wiU, nullptr);
            row[M_SELF_F].add(fU.g, fM.g, &e);
            row[M_PDF].add(pN, pM, &e);
            if (cases && (idx++ % 97) == 0) {
                GpuCase g{}; g.config = cfgIndex; g.isSample = 0; g.slice = ns; g.wi = wi;
                g.refF = toN(fM); g.refPdf = pM; g.cpuF = fN; g.cpuPdf = pN;
                cases->push_back(g);
            }
        }
        for (int i = 0; i < 12; ++i) for (int j = 0; j < 12; ++j) {
            const float r1 = (i + rnd()) / 12.f, r2 = (j + rnd()) / 12.f;
            Vec3f wiM(0.f); float pM = 0.f;
            const Color fM = c.mr->sample(ms, r1, r2, wiM, pM);
            nk::Vec3 wiN = nk::v3(0, 0, 0); float pN = 0.f;
            const nk::Col fN = nk::layered_sample(gT, c.n, ns, r1, r2, wiN, pN);
            std::string e = sctx + " r1=" + std::to_string(r1) + " r2=" + std::to_string(r2);
            row[M_SAMPLE_PDF].add(pN, pM, &e);
            addC(row[M_SAMPLE_F], fN, fM, e);
            if (pM > 0.f && pN > 0.f) {
                nk::Col wN = nk::col(fN.r / pN, fN.g / pN, fN.b / pN);
                addC(row[M_SAMPLE_WEIGHT], wN, Color(fM.r / pM, fM.g / pM, fM.b / pM), e);
                // rounding floor for the sample weight: MoonRay at its own wi, moved one ulp
                Vec3f wiU = wiM;
                wiU.x = nextafterf(wiU.x, 2.f);
                float pU = 0.f;
                const Color fU = c.mr->eval(ms, wiU, &pU);
                if (pU > 0.f) row[M_SELF_W].add(fU.g / pU, fM.g / pM, &e);
                const Vec3f d = toM(wiN) - wiM;
                row[M_SAMPLE_WI].addAbs(sm::length(d), &e);
            }
            if (cases && (idx++ % 97) == 0) {
                GpuCase g{}; g.config = cfgIndex; g.isSample = 1; g.slice = ns; g.r1 = r1; g.r2 = r2;
                g.refF = toN(fM); g.refPdf = pM; g.refWi = toN(wiM);
                g.cpuF = fN; g.cpuPdf = pN; g.cpuWi = wiN;
                cases->push_back(g);
            }
        }
    }
}

// ------------------------------------------------------------------ white furnace
struct Furn { std::string name; float cosWo; double eM, eN, sM, sN, pdfInt; };

// Energy for one wo: E = integral f dw (includeCosine) on a fine grid, S = mean f/pdf over sample().
static void furnace(const nk::LayeredLobe &l, const ms::BsdfLobe *m, Vec3f mN, const std::string &name, float thetaWo, Furn &o)
{
    const int G = 700;
    const nk::Frame fr = nk::frame_from_n(l.base.frame.z);
    const nk::Vec3 wo = sphere(fr, thetaWo, 0.4f);
    const nk::Slice ns = nk::make_slice(toN(mN), wo, true);
    const ms::BsdfSlice mslice(mN, toM(wo), true, true, (ms::ShadowTerminatorFix)0);
    double eM = 0, eN = 0, pdfI = 0;
    for (int i = 0; i < G; ++i) for (int j = 0; j < G; ++j) {
        // cosine-free uniform hemisphere grid, midpoints
        const float z = (i + 0.5f) / G, phi = 2 * 3.14159265f * (j + 0.5f) / G;
        const float s = sqrtf(1 - z * z);
        const nk::Vec3 wi = nk::frame_to_global(fr, nk::v3(s * cosf(phi), s * sinf(phi), z));
        float p = 0;
        const nk::Col f = nk::layered_eval(gT, l, ns, wi, &p);
        eN += f.g; pdfI += p;
        eM += m->eval(mslice, toM(wi), nullptr).g;
    }
    const double dw = 2 * 3.14159265358979 / (double(G) * G);
    eM *= dw; eN *= dw; pdfI *= dw;
    double sM = 0, sN = 0;
    const int S = 512;
    for (int i = 0; i < S; ++i) for (int j = 0; j < S; ++j) {
        const float r1 = (i + 0.5f) / S, r2 = (j + 0.5f) / S;
        Vec3f wiM; float pM = 0;
        const Color fM = m->sample(mslice, r1, r2, wiM, pM);
        if (pM > 0) sM += fM.g / pM;
        nk::Vec3 wiN; float pN = 0;
        const nk::Col fN = nk::layered_sample(gT, l, ns, r1, r2, wiN, pN);
        if (pN > 0) sN += fN.g / pN;
    }
    o = {name, cosf(thetaWo * 3.14159265f / 180.f), eM, eN, sM / (double(S) * S), sN / (double(S) * S), pdfI};
}

int main()
{
    auto *pool = scene_rdl2::util::alignedMallocCtor<scene_rdl2::alloc::ArenaBlockPool>(CACHE_LINE_SIZE);
    scene_rdl2::alloc::Arena arena;
    arena.init(pool);
    gArena = &arena;
    buildConfigs();
    printf("configs: %zu\n", gConfigs.size());

    // Fresnel, directly
    Table fTab;
    {
        std::vector<Fr> fs = {frDiel(1, 1.33f), frDiel(1, 1.5f), frDiel(1.5f, 1), frDiel(1, 2.5f, 0.7f),
                              frCond(kGoldEta, kGoldK), frCond(kCopperEta, kCopperK), frCond(kIronEta, kIronK, 0.8f)};
        const int nf = (int)fs.size();
        for (int i = 0; i < nf; ++i) { fs.push_back(frOneMinus(fs[i])); for (float r : {0.1f, 0.5f, 1.f}) fs.push_back(frOneMinusRough(fs[i], r)); }
        for (size_t i = 0; i < fs.size(); ++i) for (int c = 0; c <= 4000; ++c) {
            const float cosv = -1.f + c / 2000.f;
            std::string e = "fresnel#" + std::to_string(i) + " c=" + std::to_string(cosv);
            addC(fTab["fresnel"][M_F], nk::fresnel_eval(gT, fs[i].n, cosv), fs[i].mr->eval(cosv), e);
        }
    }

    // albedo tables, directly, through MoonRay's own accessors (MoonRay's arrays, not lobe_tables.h)
    {
        namespace ec = moonray::shading;
        std::string e = "table";
        for (int ri = 0; ri < 320; ++ri) {       // stops short of r=1, where MoonRay reads past its array
            const float r = ri / 320.f;
            fTab["albedo_tables"][M_F].add(nk::reflection_one_minus_avg(gT, 1, r), ec::ReflectionAlbedo::oneMinusAvg(ispc::MICROFACET_DISTRIBUTION_GGX, r), &e);
            fTab["albedo_tables"][M_F].add(nk::reflection_one_minus_avg(gT, 0, r), ec::ReflectionAlbedo::oneMinusAvg(ispc::MICROFACET_DISTRIBUTION_BECKMANN, r), &e);
            for (float n : {0.5f, 0.7f, 0.95f, 1.05f, 1.33f, 1.5f, 2.5f, 3.2f})
                fTab["albedo_tables"][M_F].add(nk::transmission_one_minus_avg(gT, n, r), ec::TransmissionAlbedo::oneMinusAvg(n, r), &e);
            for (int ci = 0; ci <= 400; ++ci) {
                const float cosv = ci == 400 ? 1.0000001f : ci / 399.f;
                fTab["albedo_tables"][M_F].add(nk::reflection_E(gT, 1, cosv, r), ec::ReflectionAlbedo::E(ispc::MICROFACET_DISTRIBUTION_GGX, cosv, r), &e);
                fTab["albedo_tables"][M_F].add(nk::reflection_E(gT, 0, cosv, r), ec::ReflectionAlbedo::E(ispc::MICROFACET_DISTRIBUTION_BECKMANN, cosv, r), &e);
                for (float n : {0.5f, 0.7f, 0.95f, 1.05f, 1.33f, 1.5f, 2.5f, 3.2f})
                    fTab["albedo_tables"][M_F].add(nk::transmission_E(gT, n, cosv, r), ec::TransmissionAlbedo::E(n, cosv, r), &e);
            }
        }
        for (float r : {0.05f, 0.1f, 0.33f, 0.5f, 0.77f, 1.0f}) for (int ci = 0; ci <= 400; ++ci) {
            const float cosv = ci / 400.f;
            fTab["velvet_tables"][M_F].add(nk::velvet_albedo_at(gT, cosv, r), ms::VelvetAlbedo::at(cosv, r), &e);
            for (float w : {0.3f, 1.f}) {
                ms::OneMinusVelvetFresnel of(r);
                of.setWeight(w);
                addC(fTab["velvet_tables"][M_F], nk::fresnel_eval(gT, nk::fresnel_velvet(r, w), cosv), of.eval(cosv), e);
            }
        }
        printf("E(GGX, r=0.7): cos=1 -> %.4f, cos=1.0000001 -> %.4f (MoonRay %.4f)\n", nk::reflection_E(gT, 1, 1.f, 0.7f),
               nk::reflection_E(gT, 1, 1.0000001f, 0.7f), ec::ReflectionAlbedo::E(ispc::MICROFACET_DISTRIBUTION_GGX, 1.0000001f, 0.7f));
    }

    // NK_MAX_LAYERS: a stack can not grow past it, and a bad count can not walk off the array
    {
        const nk::Vec3 N = nk::v3(0, 0, 1);
        nk::LayeredLobe l = nk::layered(nk::lobe_lambert(N, nk::col(0.8f)));
        const nk::UnderLayer u = nk::under_plain(N, nk::fresnel_one_minus(nk::fresnel_dielectric(1.f, 1.5f)), 0.1f, nk::col(0.7f), 1.f);
        int accepted = 0;
        for (int i = 0; i < NK_MAX_LAYERS + 3; ++i) accepted += nk::layered_push(l, u) ? 1 : 0;
        const nk::Slice ns = nk::make_slice(N, nk::v3(0.f, 0.6f, 0.8f), true);
        const nk::Vec3 wi = nk::v3(0.f, -0.6f, 0.8f);
        const float fFull = nk::layered_eval(gT, l, ns, wi, nullptr).g;
        nk::LayeredLobe bad = l;
        bad.count = 1000000;
        const float fBad = nk::layered_eval(gT, bad, ns, wi, nullptr).g;
        nk::Vec3 wiS; float p;
        const float fBadS = nk::layered_sample(gT, bad, ns, 0.3f, 0.6f, wiS, p).g;
        bad.count = -5;
        const float fNeg = nk::layered_eval(gT, bad, ns, wi, nullptr).g;
        nk::LayeredLobe none = nk::layered(l.base);
        const float fNone = nk::layered_eval(gT, none, ns, wi, nullptr).g;
        printf("NK_MAX_LAYERS %d: pushed %d of %d, overflow=%d; f full %.6f, count 1000000 %.6f (sample %.6f), count -5 %.6f, no layers %.6f: %s\n",
               NK_MAX_LAYERS, accepted, NK_MAX_LAYERS + 3, l.overflow, fFull, fBad, fBadS, fNeg, fNone,
               accepted == NK_MAX_LAYERS && l.overflow == 1 && fFull == fBad && fNeg == fNone ? "ok" : "FAILED");
    }

    const unsigned nThreads = 14;
    std::vector<Table> tabs(nThreads);
    std::vector<std::vector<GpuCase>> cases(nThreads);
    std::vector<std::thread> th;
    for (unsigned t = 0; t < nThreads; ++t) th.emplace_back([&, t] {
        for (size_t i = t; i < gConfigs.size(); i += nThreads)
            runConfig(gConfigs[i], tabs[t], &cases[t], (int)i);
    });
    for (auto &x : th) x.join();
    Table total = fTab;
    for (auto &tb : tabs) for (auto &kv : tb) for (int m = 0; m < M_COUNT; ++m) total[kv.first][m].merge(kv.second[m]);

    printf("\n%-18s %-11s %12s %10s %10s %10s %10s\n", "family", "metric", "n", "mean rel", "max rel", "max abs", ">1e-3");
    for (auto &kv : total) for (int m = 0; m < M_COUNT; ++m) {
        const Stat &s = kv.second[m];
        if (!s.n) continue;
        printf("%-18s %-11s %12lld %10.2e %10.2e %10.2e %10lld\n", kv.first.c_str(), kMetric[m], s.n, s.sum / s.n, s.maxRel, s.maxAbs, s.over1e3);
    }
    printf("\nworst cases\n");
    for (auto &kv : total) for (int m = 0; m < M_COUNT; ++m) {
        const Stat &s = kv.second[m];
        if (s.maxRel > 1e-4) printf("  %s / %s: rel %.3g  %s\n", kv.first.c_str(), kMetric[m], s.maxRel, s.worst.c_str());
    }

    // white furnace
    printf("\nwhite furnace: E = integral of f (eval), S = mean f/pdf (sample). Fresnel 1 unless named, green channel\n");
    printf("%-34s %6s %9s %9s %9s %9s %9s\n", "lobe", "cosWo", "E moonray", "E mine", "S moonray", "S mine", "int pdf");
    std::vector<std::tuple<std::string, nk::LayeredLobe, ms::BsdfLobe *>> fl;
    const nk::Vec3 N = nk::v3(0, 0, 1); const Vec3f mN(0, 0, 1);
    { auto *m = new ms::LambertBsdfLobe(mN, sm::sWhite, true); fl.emplace_back("lambert white", nk::layered(nk::lobe_lambert(N, nk::col(1.f))), m); }
    for (float r : {0.3f, 0.8f}) { auto *m = new ms::OrenNayarBsdfLobe(mN, sm::sWhite, r, true);
        fl.emplace_back("oren r=" + std::to_string(r).substr(0, 4), nk::layered(nk::lobe_oren_nayar(N, nk::col(1.f), r)), m); }
    { auto *m = new ms::FlatDiffuseBsdfLobe(mN, sm::sWhite, 0.f, 0.f, 0.f, 0.f, true); fl.emplace_back("flat defaults", nk::layered(nk::lobe_flat_diffuse(N, nk::col(1.f), 0.f, 0.f, 0.f, 0.f)), m); }
    { auto *m = new ms::FlatDiffuseBsdfLobe(mN, sm::sWhite, 0.4f, 0.f, 0.f, 0.f, true); fl.emplace_back("flat r=0.4", nk::layered(nk::lobe_flat_diffuse(N, nk::col(1.f), 0.4f, 0.f, 0.f, 0.f)), m); }
    { auto *m = new ms::FlatDiffuseBsdfLobe(mN, sm::sWhite, 0.f, 0.2f, 0.7f, 0.5f, true); fl.emplace_back("flat npr fl=0.7", nk::layered(nk::lobe_flat_diffuse(N, nk::col(1.f), 0.f, 0.2f, 0.7f, 0.5f)), m); }
    for (float r : {0.3f, 0.5f, 0.51f, 0.7f, 0.9f}) for (int ec = 0; ec < 2; ++ec) {
        nk::Col fa = nk::col(ec ? 1.f : 0.f);
        auto *m = makeCT(nk::LOBE_GGX, mN, r, toM(fa), sm::sBlack, 1.f, 1.5f, false);
        fl.emplace_back(std::string("ggx r=") + std::to_string(r).substr(0, 4) + (ec ? " ec" : " noEC"),
                        nk::layered(nk::lobe_cook_torrance(nk::LOBE_GGX, N, r, fa, nk::col(0.f), 1.f, 1.5f, false)), m);
    }
    for (int b = 0; b < 2; ++b) {
        // clearcoat ior 1.5 over white lambert, and over ggx with energy compensation
        Fr top = frDiel(1.f, 1.5f);
        Fr om = frOneMinusRough(top, 0.1f);
        ms::BsdfLobe *m; nk::Lobe n;
        if (b == 0) { m = new ms::LambertBsdfLobe(mN, sm::sWhite, true); n = nk::lobe_lambert(N, nk::col(1.f)); }
        else { m = makeCT(nk::LOBE_GGX, mN, 0.5f, sm::sWhite, sm::sBlack, 1.f, 1.5f, false);
               n = nk::lobe_cook_torrance(nk::LOBE_GGX, N, 0.5f, nk::col(1.f), nk::col(0.f), 1.f, 1.5f, false); }
        auto *w = new ms::UnderClearcoatBsdfLobe(m, gArena, mN, 1.f, 1.5f, 0.f, sm::sWhite, 1.f, true);
        w->setFresnel(om.mr);
        nk::LayeredLobe l = nk::layered(n); l.count = 1;
        l.layers[0] = nk::under_clearcoat(N, om.n, 1.f, 1.5f, 0.f, nk::col(1.f), 1.f, true);
        fl.emplace_back(b ? "coat 1.5 on ggx.5 ec" : "coat 1.5 on lambert", l, w);
    }
    const float woT[] = {0.f, 45.f, 75.f};
    std::vector<Furn> res(fl.size() * 3);
    std::vector<std::thread> th2;
    for (unsigned t = 0; t < nThreads; ++t) th2.emplace_back([&, t] {
        for (size_t i = t; i < res.size(); i += nThreads)
            furnace(std::get<1>(fl[i / 3]), std::get<2>(fl[i / 3]), mN, std::get<0>(fl[i / 3]), woT[i % 3], res[i]);
    });
    for (auto &x : th2) x.join();
    for (auto &r : res)
        printf("%-34s %6.3f %9.5f %9.5f %9.5f %9.5f %9.5f\n", r.name.c_str(), r.cosWo, r.eM, r.eN, r.sM, r.sN, r.pdfInt);

    printf("\nsizes: Fresnel %d, Lobe %d, UnderLayer %d, LayeredLobe %d bytes\n", (int)sizeof(nk::Fresnel), (int)sizeof(nk::Lobe),
           (int)sizeof(nk::UnderLayer), (int)sizeof(nk::LayeredLobe));

    // dump for the GPU check
    std::vector<GpuCase> all;
    for (auto &c : cases) all.insert(all.end(), c.begin(), c.end());
    std::vector<nk::LayeredLobe> cfg;
    for (auto &c : gConfigs) cfg.push_back(c.n);
    FILE *f = fopen("out\\cases.bin", "wb");
    if (f) {
        GpuHeader h{GPU_MAGIC, (int)sizeof(nk::LayeredLobe), (int)sizeof(GpuCase), (int)cfg.size(), (int)all.size()};
        fwrite(&h, sizeof h, 1, f);
        fwrite(cfg.data(), sizeof(nk::LayeredLobe), cfg.size(), f);
        fwrite(all.data(), sizeof(GpuCase), all.size(), f);
        fclose(f);
        printf("\nwrote out\\cases.bin: %zu configs, %zu cases\n", cfg.size(), all.size());
    }
    fflush(stdout);
    _exit(0);   // MoonRay objects are leaked on purpose
}
