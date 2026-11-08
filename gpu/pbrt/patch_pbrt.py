# Copyright 1998-2020 Matt Pharr, Wenzel Jakob and Greg Humphreys
# Copyright 2026 Kiet Ho
# SPDX-License-Identifier: Apache-2.0
# Modified by Kiet Ho, 2026: holds excerpts of pbrt-v4 (github.com/mmp/pbrt-v4, b0bdf46) and the changed code. See NOTICE.
"""Applies the Nanna changes to pbrt-v4/src/pbrt once. Every change is marked NANNA. Run with python -I from the folder that holds the
pbrt-v4 checkout (NANNA_PBRT_WORK). The nanna_*.inc files and patch_f12.py are read from this script's folder."""
import os

R = "pbrt-v4/src/pbrt/"
HERE = os.path.dirname(os.path.abspath(__file__))
INC_END = "// ---- end of licence header\n"


def inc(name):
    """An .inc file's text without its licence header, so the patched pbrt sources don't depend on it."""
    return open(os.path.join(HERE, name)).read().split(INC_END, 1)[-1]


def sub(f, old, new, count=1):
    p = R + f
    t = open(p, encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in t else "\n"
    old, new = old.replace("\n", nl), new.replace("\n", nl)
    assert t.count(old) >= 1, (f, old)
    t = t.replace(old, new) if count == 0 else t.replace(old, new, count)
    open(p, "w", encoding="utf-8", newline="").write(t)


# ---- tagged types
sub("base/bxdf.h", "class CoatedDiffuseBxDF;\n", "class CoatedDiffuseBxDF;\nclass NannaBxDF;  // NANNA\n")
sub("base/bxdf.h", ": public TaggedPointer<DiffuseTransmissionBxDF, DiffuseBxDF, CoatedDiffuseBxDF,", ": public TaggedPointer<NannaBxDF, DiffuseTransmissionBxDF, DiffuseBxDF, CoatedDiffuseBxDF,")
sub("base/material.h", "class DiffuseMaterial;\n", "class DiffuseMaterial;\nclass NannaMaterial;  // NANNA\n")
sub("base/material.h", "DielectricMaterial, DiffuseMaterial,", "DielectricMaterial, DiffuseMaterial, NannaMaterial,")

# ---- RGB mode: three fixed channels. A channel is a wavelength, so DenselySampledSpectrum and the film carry it unchanged
sub("util/spectrum.h", "static constexpr int NSpectrumSamples = 4;\n", """// NANNA: fixed RGB. Three channels, each one tied to a fixed wavelength. Spectra answer these
// wavelengths with their RGB, so everything that stores a Spectrum keeps it exactly.
static constexpr int NSpectrumSamples = 3;
constexpr Float Nanna_Lambda_R = 600, Nanna_Lambda_G = 550, Nanna_Lambda_B = 450;
""")
sub("util/spectrum.h", """    PBRT_CPU_GPU
    Float operator()(Float lambda) const { return rsp(lambda); }
""", """    PBRT_CPU_GPU
    Float operator()(Float lambda) const {  // NANNA
        if (lambda == Nanna_Lambda_R) return c[0];
        if (lambda == Nanna_Lambda_G) return c[1];
        if (lambda == Nanna_Lambda_B) return c[2];
        return rsp(lambda);
    }
""")
sub("util/spectrum.h", """        for (int i = 0; i < NSpectrumSamples; ++i)
            s[i] = rsp(lambda[i]);
        return s;
    }

    std::string ToString() const;

  private:
    // RGBAlbedoSpectrum Private Members
    RGBSigmoidPolynomial rsp;
""", """        for (int i = 0; i < NSpectrumSamples; ++i)
            s[i] = (*this)(lambda[i]);  // NANNA
        return s;
    }

    std::string ToString() const;

  private:
    // RGBAlbedoSpectrum Private Members
    RGBSigmoidPolynomial rsp;
    Float c[3];  // NANNA
""")
sub("util/spectrum.h", """    Float operator()(Float lambda) const { return scale * rsp(lambda); }
""", """    Float operator()(Float lambda) const {  // NANNA
        if (lambda == Nanna_Lambda_R) return c[0];
        if (lambda == Nanna_Lambda_G) return c[1];
        if (lambda == Nanna_Lambda_B) return c[2];
        return scale * rsp(lambda);
    }
""")
sub("util/spectrum.h", "    RGBUnboundedSpectrum() : rsp(0, 0, 0), scale(0) {}\n", "    RGBUnboundedSpectrum() : rsp(0, 0, 0), scale(0), c{0, 0, 0} {}\n")
sub("util/spectrum.h", """            s[i] = scale * rsp(lambda[i]);
        return s;
    }

    std::string ToString() const;

  private:
    // RGBUnboundedSpectrum Private Members
    Float scale = 1;
    RGBSigmoidPolynomial rsp;
""", """            s[i] = (*this)(lambda[i]);  // NANNA
        return s;
    }

    std::string ToString() const;

  private:
    // RGBUnboundedSpectrum Private Members
    Float scale = 1;
    RGBSigmoidPolynomial rsp;
    Float c[3];  // NANNA
""")
sub("util/spectrum.h", """        if (!illuminant)
            return 0;
        return scale * rsp(lambda) * (*illuminant)(lambda);
""", """        if (!illuminant)
            return 0;
        if (lambda == Nanna_Lambda_R) return c[0];  // NANNA
        if (lambda == Nanna_Lambda_G) return c[1];
        if (lambda == Nanna_Lambda_B) return c[2];
        return scale * rsp(lambda) * (*illuminant)(lambda);
""")
sub("util/spectrum.h", "    // RGBIlluminantSpectrum Private Members\n", "    // RGBIlluminantSpectrum Private Members\n    Float c[3];  // NANNA\n")
# NANNA: Sample() too. Image lights (the world's infinite light) build this per lookup and call Sample(), which
# otherwise gives rsp x D65 at 600/550/450 nm: the world light came out r 0.94 g 1.19 b 1.49 against MoonRay.
sub("util/spectrum.h", """        SampledSpectrum s;
        for (int i = 0; i < NSpectrumSamples; ++i)
            s[i] = scale * rsp(lambda[i]);
        return s * illuminant->Sample(lambda);
""", """        SampledSpectrum s;
        for (int i = 0; i < NSpectrumSamples; ++i)
            s[i] = (*this)(lambda[i]);  // NANNA
        return s;
""")
sub("util/spectrum.cpp", "    rsp = cs.ToRGBCoeffs(rgb);\n}\n", "    rsp = cs.ToRGBCoeffs(rgb);\n    c[0] = rgb.r; c[1] = rgb.g; c[2] = rgb.b;  // NANNA\n}\n")
sub("util/spectrum.cpp", """    rsp = cs.ToRGBCoeffs(scale ? rgb / scale : RGB(0, 0, 0));
}
""", """    rsp = cs.ToRGBCoeffs(scale ? rgb / scale : RGB(0, 0, 0));
    c[0] = rgb.r; c[1] = rgb.g; c[2] = rgb.b;  // NANNA
}
""", 0)
sub("util/spectrum.cpp", "Float SpectrumToPhotometric(Spectrum s) {\n", "Float SpectrumToPhotometric(Spectrum s) {\n    return 1;  // NANNA: a light's RGB is its radiance, as in MoonRay\n")
sub("util/spectrum.h", """        SampledWavelengths swl;
        // Sample first wavelength using _u_
""", """        SampledWavelengths swl;
        swl = Fixed();  // NANNA
        return swl;
        // Sample first wavelength using _u_
""")
sub("util/spectrum.h", """SampledWavelengths SampleVisible(Float u) {
        SampledWavelengths swl;
""", """SampledWavelengths SampleVisible(Float u) {
        SampledWavelengths swl;
        swl = Fixed();  // NANNA
        return swl;
""")
sub("util/spectrum.h", """    void TerminateSecondary() {
        if (SecondaryTerminated())
            return;
""", """    void TerminateSecondary() {
        return;  // NANNA
        if (SecondaryTerminated())
            return;
""")
sub("util/spectrum.h", """    // SampledWavelengths Private Members
    friend struct SOA<SampledWavelengths>;
""", """    // NANNA: the three channels, with a pdf of 1
    PBRT_CPU_GPU
    static SampledWavelengths Fixed() {
        SampledWavelengths swl;
        swl.lambda[0] = Nanna_Lambda_R;
        swl.lambda[1] = Nanna_Lambda_G;
        swl.lambda[2] = Nanna_Lambda_B;
        for (int i = 0; i < NSpectrumSamples; ++i)
            swl.pdf[i] = 1;
        return swl;
    }

    // SampledWavelengths Private Members
    friend struct SOA<SampledWavelengths>;
""")
sub("film.h", """        L = SafeDiv(L, lambda.PDF());
        return imagingRatio * RGB((r_bar.Sample(lambda) * L).Average(),
""", """        return imagingRatio * RGB(L[0], L[1], L[2]);  // NANNA
        L = SafeDiv(L, lambda.PDF());
        return imagingRatio * RGB((r_bar.Sample(lambda) * L).Average(),
""")
sub("film.cpp", """    // Compute _outputRGBFromSensorRGB_ matrix
    outputRGBFromSensorRGB = colorSpace->RGBFromXYZ * sensor->XYZFromSensorRGB;
""", """    // NANNA: sensor RGB is already output RGB
    outputRGBFromSensorRGB = SquareMatrix<3>();
""", 0)

# ---- BxDF
sub("bxdfs.h", "#include <pbrt/pbrt.h>\n", "#include <pbrt/pbrt.h>\n\n#include \"../../../nk/lobes.h\"  // NANNA\n")
sub("bxdfs.h", "// DiffuseBxDF Definition\n", inc("nanna_bxdf.inc") + "\n// DiffuseBxDF Definition\n")
sub("bxdfs.cpp", "std::string DiffuseBxDF::ToString() const {\n", """std::string NannaBxDF::ToString() const {  // NANNA
    return StringPrintf("[ NannaBxDF lobes: %d ]", count);
}

std::string DiffuseBxDF::ToString() const {
""")
sub("materials.h", "// DiffuseMaterial Definition\n", inc("nanna_material.inc") + "\n// DiffuseMaterial Definition\n")
sub("materials.cpp", "#include <pbrt/materials.h>\n", "#include <pbrt/materials.h>\n\n#include \"../../../nk/lobe_tables.h\"  // NANNA\n")
sub("materials.cpp", "// DiffuseMaterial Method Definitions\n", inc("nanna_create.inc") + "\n// DiffuseMaterial Method Definitions\n")
sub("materials.cpp", """    else if (name == "diffuse")
        material = DiffuseMaterial::Create""", """    else if (name == "nanna")  // NANNA
        material = NannaMaterial::Create(parameters, normalMap, loc, alloc);
    else if (name == "diffuse")
        material = DiffuseMaterial::Create""")

# ---- integrator
sub("wavefront/surfscatter.cpp", """            // Sample BSDF and enqueue indirect ray at intersection point
            Vector3f wo = w.wo;
""", inc("nanna_surfscatter.inc") + """            // Sample BSDF and enqueue indirect ray at intersection point
            Vector3f wo = w.wo;
""")
sub("wavefront/integrator.cpp", """                Float lightPDF = lightChoicePDF * w.areaLight.PDF_Li(ctx, wi, true);

                SampledSpectrum r_u = w.r_u;
""", """                Float lightPDF = lightChoicePDF * w.areaLight.PDF_Li(ctx, wi, true);

                // NANNA: MoonRay's integrateBsdfSample. Power heuristic of this lobe's pdf
                // against the light's, clamped with the lobe choice left out. r_u holds that choice.
                if (w.r_l[0] > 0) {
                    Float pb = w.r_u[0] / w.r_l[0], pl = lightPDF * NannaBxDF::MeshLights;
                    SampledSpectrum c = w.beta * Le * (Sqr(pb) / (Sqr(pb) + Sqr(pl)));
                    if (w.depth - 1 >= NannaBxDF::ClampDepth)
                        c = NannaBxDF::SmartClamp(c, (GetOptions().disableWavelengthJitter ? 0.f : NannaBxDF::ClampValue));
                    L = c / w.r_u.Average();
                    L += pixelSampleState.L[w.pixelIndex];
                    pixelSampleState.L[w.pixelIndex] = L;
                    return;
                }

                SampledSpectrum r_u = w.r_u;
""")

# NANNA: the same rule for a ray that leaves the scene: the world, and the sun baked into it
sub("wavefront/integrator.cpp", """                        LightSampleContext ctx = w.prevIntrCtx;
                        Float lightChoicePDF = lightSampler.PMF(ctx, light);
                        SampledSpectrum r_l =
                            w.r_l * lightChoicePDF * light.PDF_Li(ctx, w.rayd, true);
                        L += w.beta * Le / (w.r_u + r_l).Average();
""", """                        LightSampleContext ctx = w.prevIntrCtx;
                        Float lightChoicePDF = lightSampler.PMF(ctx, light);
                        if (w.r_l[0] > 0) {  // NANNA: as for an area light
                            Float pb = w.r_u[0] / w.r_l[0],
                                  pl = lightChoicePDF * light.PDF_Li(ctx, w.rayd, true) * NannaBxDF::MeshLights;
                            SampledSpectrum c = w.beta * Le * (Sqr(pb) / (Sqr(pb) + Sqr(pl)));
                            if (w.depth - 1 >= NannaBxDF::ClampDepth)
                                c = NannaBxDF::SmartClamp(c, (GetOptions().disableWavelengthJitter ? 0.f : NannaBxDF::ClampValue));
                            L += c / w.r_u.Average();
                        } else {
                        SampledSpectrum r_l =
                            w.r_l * lightChoicePDF * light.PDF_Li(ctx, w.rayd, true);
                        L += w.beta * Le / (w.r_u + r_l).Average();
                        }
""")

# ---- gpu/util.cpp: a sync after every launch (asynchronous launches failed). pbrt's stack limit of 8192 stays
sub("gpu/util.cpp", """        CUDA_CHECK(cudaEventSynchronize(start));
        CUDA_CHECK(cudaEventSynchronize(stop));

        float ms = 0;
        CUDA_CHECK(cudaEventElapsedTime(&ms, start, stop));
""", """        // NANNA: name the kernel whose events fail
        cudaError_t e = cudaEventSynchronize(start);
        if (e == cudaSuccess) e = cudaEventSynchronize(stop);
        float ms = 0;
        if (e == cudaSuccess) e = cudaEventElapsedTime(&ms, start, stop);
        if (e != cudaSuccess)
            LOG_FATAL("NANNA profiler sync of \\"%s\\": %s", stats ? stats->description : "?",
                      cudaGetErrorString(e));
""")
sub("gpu/util.cpp", """        eventPoolOffset = 0;

    ProfilerEvent &pe = eventPool[eventPoolOffset++];
""", """        eventPoolOffset = 0;

    // NANNA: a failed launch or call since the last one, named by the launch before it
    static const char *lastDescription = "start";
    // NANNA: unbounded async queues end in "invalid argument" (the 1024 event ring wrapping, then OptiX "Failed to query
    // command list event" in light-only scenes near sample 31). Sync per launch by default; NANNA_ASYNC=1 syncs every 256
    // launches instead, NANNA_ASYNC=n (n > 1) every n
    static const int syncEvery = [] {
        const char *a = getenv("NANNA_ASYNC");
        return a == nullptr ? 1 : atoi(a) > 1 ? atoi(a) : 256;
    }();
    static long launches = 0;
    if (++launches % syncEvery == 0)
        cudaDeviceSynchronize();
    if (cudaError_t e = cudaPeekAtLastError(); e != cudaSuccess)
        LOG_FATAL("NANNA CUDA error after \\"%s\\", before \\"%s\\": %s", lastDescription,
                  description, cudaGetErrorString(e));
    lastDescription = description;

    ProfilerEvent &pe = eventPool[eventPoolOffset++];
""")
sub("gpu/util.cpp", "        eventPool.resize(1024);  // how many? This is probably more than we need...\n",
    "        eventPool.resize(1 << 16);  // NANNA: 1024 wrapped near sample 22 in light-only scenes and failed there with NANNA_ASYNC\n")
# ---- build: nvcc 12.4 has no --forward-slash-prefix-opts
sub("../../CMakeLists.txt", """                "$<$<CXX_COMPILER_ID:MSVC>:$<$<COMPILE_LANGUAGE:CUDA>:--forward-slash-prefix-opts>>"
""", "")
# ---- hidden light quads ("bool nanna_hidden" true on the shape, export_pbrt.py writes it for Blender area lights):
# unseen by the camera, blocking nothing, still found by bsdf rays after a bounce. A ray through one goes on in the same step.
sub("gpu/optix/optix.h", "    MediumInterface *mediumInterface;\n};\n", "    MediumInterface *mediumInterface;\n    bool nannaHidden;  // NANNA: emitter unseen by the camera, passed by every ray\n};\n")
sub("gpu/optix/aggregate.cpp", "            triangleInputFlags[meshIndex] = getOptixGeometryFlags(true, alphaTexture);\n", """            // NANNA: a light quad hidden as MoonRay hides its lights: needs anyhit so shadow rays pass it.
            bool nannaHidden = shape.parameters.GetOneBool("nanna_hidden", false);
            triangleInputFlags[meshIndex] = nannaHidden ? OPTIX_GEOMETRY_FLAG_NONE : getOptixGeometryFlags(true, alphaTexture);
""")
sub("gpu/optix/aggregate.cpp", "            hgRecord.triRec.alphaTexture = alphaTexture;\n", "            hgRecord.triRec.alphaTexture = alphaTexture;\n            hgRecord.triRec.nannaHidden = nannaHidden;  // NANNA\n")
sub("gpu/optix/optix.cu", "    Material material;\n    MediumInterface mediumInterface;\n", """    Material material;
    MediumInterface mediumInterface;
    // NANNA: set by a hidden light quad, raygen traces nannaNext in the same step
    bool nannaPass = false;
    Ray nannaNext;
""")
sub("gpu/optix/optix.cu", """    Trace(params.traversable, ray, tMax, OPTIX_RAY_FLAG_NONE, p0, p1, missed);

    if (missed)
        EnqueueWorkAfterMiss(r, params.mediumSampleQueue, params.escapedRayQueue);""", """    Trace(params.traversable, ray, tMax, OPTIX_RAY_FLAG_NONE, p0, p1, missed);
    // NANNA: a ray through hidden light quads goes on without using a depth step
    for (int pass = 0; ctx.nannaPass && !missed && pass < 16; ++pass) {
        ctx.nannaPass = false;
        Trace(params.traversable, ctx.nannaNext, tMax, OPTIX_RAY_FLAG_NONE, p0, p1, missed);
    }

    if (missed)
        EnqueueWorkAfterMiss(r, params.mediumSampleQueue, params.escapedRayQueue);""")
sub("gpu/optix/optix.cu", """    intr.material = rec.material;
    if (!rec.areaLights.empty())
        intr.areaLight = rec.areaLights[optixGetPrimitiveIndex()];
""", """    intr.material = rec.nannaHidden ? Material() : rec.material;  // NANNA: no material, so the ray goes on
    if (!rec.areaLights.empty())
        intr.areaLight = rec.areaLights[optixGetPrimitiveIndex()];

    // NANNA: a hidden light quad. Rays after a bounce take its light, the camera does not;
    // then raygen traces on from past it in the same wavefront step.
    if (rec.nannaHidden && !getPayload<ClosestHitContext>()->shadowRay) {
        const RayWorkItem r = (*params.rayQueue)[optixGetLaunchIndex().x];
        if (intr.areaLight && r.depth > 0)
            params.hitAreaLightQueue->Push(HitAreaLightWorkItem{
                intr.areaLight, intr.p(), intr.n, intr.uv, intr.wo, r.lambda, r.depth, r.beta,
                r.r_u, r.r_l, r.prevIntrCtx, (int)r.specularBounce, r.pixelIndex});
        getPayload<ClosestHitContext>()->nannaPass = true;
        getPayload<ClosestHitContext>()->nannaNext = intr.SpawnRay(r.ray.d);
        return;
    }
""")
sub("gpu/optix/optix.cu", """extern "C" __global__ void __anyhit__shadowTriangle() {
    const TriangleMeshRecord &rec = *(const TriangleMeshRecord *)optixGetSbtDataPointer();

    if (alphaKilled(rec))""", """extern "C" __global__ void __anyhit__shadowTriangle() {
    const TriangleMeshRecord &rec = *(const TriangleMeshRecord *)optixGetSbtDataPointer();

    if (rec.nannaHidden || alphaKilled(rec))  // NANNA: a hidden light quad blocks nothing""")
# ---- ray offsets: OptiX's triangle test is not pbrt's exact one. Its error grows with the coordinates even where pbrt's
# bound is 0 (a floor at z = 0): a 200 m floor re-hit itself on 12% of bounce rays and lost 4% of its direct light.
sub("gpu/optix/optix.cu", "    return worldFromInstance(intr);\n", """    intr = worldFromInstance(intr);
    // NANNA: widen the bound by 4e-6 x |p|max, so every spawned and shadow ray clears the surface
    const Point3f p = intr.p();
    const Float big = 4e-6f * std::max(std::abs(p.x), std::max(std::abs(p.y), std::abs(p.z)));
    const Vector3f e = intr.pi.Error();
    intr.pi = Point3fi(p, Vector3f(std::max(e.x, big), std::max(e.y, big), std::max(e.z, big)));
    return intr;
""")
print("patched")
import runpy  # NANNA F12: gbuffer film and --nanna-wait
runpy.run_path(os.path.join(HERE, "patch_f12.py"))
