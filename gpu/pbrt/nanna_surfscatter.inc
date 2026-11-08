// Copyright 1998-2020 Matt Pharr, Wenzel Jakob and Greg Humphreys
// Copyright 2023-2024 DreamWorks Animation LLC
// Copyright 2026 Kiet Ho
// SPDX-License-Identifier: Apache-2.0
// Modified by Kiet Ho, 2026: code added to pbrt-v4 by patch_pbrt.py, built on pbrt-v4's classes and following
// MoonRay's integrator rules. See NOTICE.
// ---- end of licence header
            // NANNA: MoonRay's rules for a Nanna surface, see gpu/kernel/SPEC_closure.md. r_u holds the lobe choices
            // and beta leaves them out, so the clamp and the roulette see what MoonRay sees.
            // etaScale - 1 carries the path's diffuse depth, 16 x its glossy depth, 256 if it's over.
            (void)nextRayQueue; (void)shadowRayQueue; (void)lightSampler;  // nvcc: no first capture inside if constexpr
            if constexpr (std::is_same_v<ConcreteBxDF, NannaBxDF>) {
                const int code = int(w.etaScale - 1 + .5f);
                if (code & 256)
                    return;  // MoonRay's last bsdf sample only looks for lights
                const int diffuseDepth = code & 15, glossyDepth = code >> 4;
                Vector3f wo = w.wo;
                RaySamples raySamples = pixelSampleState.samples[w.pixelIndex];
                const Vector3f woLocal = bsdf.RenderToLocal(wo);
                NannaBxDF::LobeSample s;
                if (bxdf.SampleLobe(woLocal, raySamples.indirect.uc, raySamples.indirect.u, &s)) {
                    Vector3f wi = bsdf.LocalToRender(s.wi);
                    SampledSpectrum beta = w.beta * s.fCos / s.pdf;
                    SampledSpectrum r_u = w.r_u * s.q, r_l = r_u / s.pdf;
                    // applyRussianRoulette
                    const Float lum = .2126f * beta[0] + .7152f * beta[1] + .0722f * beta[2];
                    if (w.depth > 0 && lum < NannaBxDF::RRThreshold) {
                        if (lum < raySamples.indirect.rr * NannaBxDF::RRThreshold)
                            beta = SampledSpectrum(0.f);
                        else
                            beta *= NannaBxDF::RRThreshold / lum;
                    }
                    if (beta) {
                        // Mirrors count toward neither: MoonRay's own mirror depth is not modelled
                        const int d = diffuseDepth + (s.diffuse ? 1 : 0), g = glossyDepth + (s.diffuse || s.mirror ? 0 : 1);
                        const bool over = s.mirror ? false : s.diffuse ? diffuseDepth >= bxdf.maxDiffuseDepth
                                                                       : glossyDepth >= bxdf.maxGlossyDepth;
                        Ray ray = SpawnRay(w.pi, w.n, w.time, wi);
                        LightSampleContext ctx(w.pi, w.n, ns);
                        nextRayQueue->PushIndirectRay(ray, w.depth + 1, ctx, beta, r_u, r_l, lambda,
                                                      Float(1 + d + 16 * g + (over ? 256 : 0)), s.mirror, true,
                                                      w.pixelIndex);
                    }
                }
                // Light sample
                LightSampleContext ctx(w.pi, w.n, ns);
                ctx.pi = OffsetRayOrigin(ctx.pi, w.n, wo);
                pstd::optional<SampledLight> sampledLight = lightSampler.Sample(ctx, raySamples.direct.uc);
                if (!sampledLight)
                    return;
                Light light = sampledLight->light;
                pstd::optional<LightLiSample> ls = light.SampleLi(ctx, raySamples.direct.u, lambda, true);
                if (!ls || !ls->L || ls->pdf == 0)
                    return;
                const Float lightPDF = ls->pdf * sampledLight->p * NannaBxDF::MeshLights;
                const SampledSpectrum t = bxdf.DirectLight(
                    woLocal, bsdf.RenderToLocal(ls->wi), w.beta * ls->L / lightPDF, lightPDF,
                    w.depth >= NannaBxDF::ClampDepth ? (GetOptions().disableWavelengthJitter ? 0.f : NannaBxDF::ClampValue) : 0.f);
                if (!t)
                    return;
                Ray ray = SpawnRayTo(w.pi, w.n, w.time, ls->pLight.pi, ls->pLight.n);
                shadowRayQueue->Push(ShadowRayWorkItem{ray, 1 - ShadowEpsilon, lambda,
                                                       t * NannaBxDF::MeshLights, w.r_u,
                                                       SampledSpectrum(0.f), w.pixelIndex});
                return;
            }

