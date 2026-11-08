# Copyright 1998-2020 Matt Pharr, Wenzel Jakob and Greg Humphreys
# Copyright 2026 Kiet Ho
# SPDX-License-Identifier: Apache-2.0
# Modified by Kiet Ho, 2026: holds excerpts of pbrt-v4 (github.com/mmp/pbrt-v4, b0bdf46) and the changed code. See NOTICE.
"""Scene reload without memory growth for --nanna-serve, on top of patch_server.py. Run from the folder that holds pbrt-v4.

Before (measured, mem0.log): every "scene" command left the previous scene in memory: +0.80 GB of pbrt's tracked managed memory,
+1.26 GB on the GPU and +1.3 GB of host private bytes per reload at 1000x641 (OptiX context, module and pipeline made again,
BVH and vertex buffers from plain cudaMalloc, the scene's objects).

Now:
- gpu/memory: CUDATrackedMemoryResource::NannaAllocations() lists the tracked allocations; NannaPermanentMemory() is managed
  memory outside the tracked list, for process-wide caches.
- Process-wide caches that pbrt fills while it builds a scene use the permanent memory, so freeing a scene can't leave them
  pointing at freed memory: Nanna's lobe tables (materials.cpp), the light spectrum intern cache (lights.cpp), spectra read
  from files (paramdict.cpp), gamma color encodings (util/color.cpp).
- gpu/optix/aggregate.cpp: one OptiX device context, module, program groups, pipeline and parameter buffer pool per process
  (made by the first aggregate, reused by the next ones); the BVH and vertex buffers it cudaMallocs are listed, and
  NannaFreeAggregateBuffers() frees them.
- wavefront.cpp (NannaServe): the tracked allocations present before the first scene are the base. A "scene" command after
  a scene waits for the GPU, frees the aggregate buffers and every tracked allocation outside the base, clears the CPU
  MIPMap cache, makes new (empty) triangle and bilinear mesh registries (Triangle::Init, BilinearPatch::Init: their vectors
  grew in scene memory; without this the next scene's area lights crashed, 0xC0000006, mem1v.log), makes new mesh buffer caches
  (util/buffercache: they held pointers into the freed scene, the same crash, mem1v2.log), and then parses the new one. The old integrator and BasicScene host objects are left (small).
One-shot renders and --nanna-wait build one aggregate per process: the caching changes nothing for them.
"""
R = "pbrt-v4/src/pbrt/"


def sub(f, old, new, n=1):
    p = R + f
    t = open(p, encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in t else "\n"
    old, new = old.replace("\n", nl), new.replace("\n", nl)
    assert t.count(old) == n, (f, old, t.count(old))
    t = t.replace(old, new)
    open(p, "w", encoding="utf-8", newline="").write(t)


# gpu/memory.h/.cpp
sub("gpu/memory.h", """    void PrefetchToGPU() const;
    size_t BytesAllocated() const { return bytesAllocated; }
""", """    void PrefetchToGPU() const;
    size_t BytesAllocated() const { return bytesAllocated; }
    // NANNA: the live allocations (pointer, size), for --nanna-serve's scene reload (patch_memory.py)
    std::vector<std::pair<void *, size_t>> NannaAllocations() const;
""")
sub("gpu/memory.h", """#endif

}  // namespace pbrt

#endif  // PBRT_GPU_MEMORY_H""", """// NANNA: managed memory not in the tracked list, for process-wide caches that outlive a scene (patch_memory.py)
CUDAMemoryResource *NannaPermanentMemory();

#endif

}  // namespace pbrt

#endif  // PBRT_GPU_MEMORY_H""")
sub("gpu/memory.cpp", """CUDATrackedMemoryResource CUDATrackedMemoryResource::singleton;
""", """CUDATrackedMemoryResource CUDATrackedMemoryResource::singleton;

// NANNA: patch_memory.py
std::vector<std::pair<void *, size_t>> CUDATrackedMemoryResource::NannaAllocations() const {
    std::lock_guard<std::mutex> lock(mutex);
    return std::vector<std::pair<void *, size_t>>(allocations.begin(), allocations.end());
}

CUDAMemoryResource *NannaPermanentMemory() {
    static CUDAMemoryResource *resource = new CUDAMemoryResource;
    return resource;
}
""")

# Process-wide caches into permanent memory
sub("materials.cpp", """static nk::LobeTables NannaTables(Allocator alloc) {
    static nk::LobeTables tables;
    static bool done = false;
    if (done)
        return tables;
""", """static nk::LobeTables NannaTables(Allocator alloc) {
    static nk::LobeTables tables;
    static bool done = false;
    if (done)
        return tables;
#ifdef PBRT_BUILD_GPU_RENDERER
    // NANNA: outlives the scene (patch_memory.py)
    Allocator permanent = Options->useGPU ? Allocator(NannaPermanentMemory()) : alloc;
#else
    Allocator permanent = alloc;
#endif
""")
sub("materials.cpp", """        float *to = alloc.allocate_object<float>(n);
""", """        float *to = permanent.allocate_object<float>(n);  // NANNA: patch_memory.py
""")
sub("lights.cpp", """            Options->useGPU ? Allocator(&CUDATrackedMemoryResource::singleton) :
""", """            Options->useGPU ? Allocator(NannaPermanentMemory()) :  // NANNA: outlives the scene (patch_memory.py)
""")
sub("paramdict.cpp", """    pstd::optional<Spectrum> pls = PiecewiseLinearSpectrum::Read(fn, alloc);
""", """#ifdef PBRT_BUILD_GPU_RENDERER
    // NANNA: cached for the process, so it outlives the scene (patch_memory.py)
    Allocator permanent = Options->useGPU ? Allocator(NannaPermanentMemory()) : alloc;
#else
    Allocator permanent = alloc;
#endif
    pstd::optional<Spectrum> pls = PiecewiseLinearSpectrum::Read(fn, permanent);
""")
sub("util/color.cpp", """        ColorEncoding enc = alloc.new_object<GammaColorEncoding>(gamma);
""", """#ifdef PBRT_BUILD_GPU_RENDERER
        // NANNA: cached for the process, so it outlives the scene (patch_memory.py)
        Allocator permanent = Options->useGPU ? Allocator(NannaPermanentMemory()) : alloc;
#else
        Allocator permanent = alloc;
#endif
        ColorEncoding enc = permanent.new_object<GammaColorEncoding>(gamma);
""")
sub("materials.cpp", "#include <pbrt/materials.h>\n", "#include <pbrt/materials.h>\n#include <pbrt/gpu/memory.h>  // NANNA: patch_memory.py\n")
sub("paramdict.cpp", "#include <pbrt/paramdict.h>\n", "#include <pbrt/paramdict.h>\n#include <pbrt/gpu/memory.h>  // NANNA: patch_memory.py\n")
sub("util/color.cpp", "#include <pbrt/util/color.h>\n", "#include <pbrt/util/color.h>\n#include <pbrt/gpu/memory.h>  // NANNA: patch_memory.py\n")

# gpu/optix/aggregate.cpp
sub("gpu/optix/aggregate.cpp", """namespace pbrt {
""", """namespace pbrt {

// NANNA: patch_memory.py. Buffers this file cudaMallocs for a scene (BVHs, vertices, indices) are listed so that
// --nanna-serve can free them when it loads the next scene; NannaFreeAggregateBuffers() does that.
static std::mutex nannaBufferMutex;
static std::unordered_map<void *, size_t> nannaBuffers;
static cudaError_t NannaMalloc(void **p, size_t size) {
    cudaError_t e = ::cudaMalloc(p, size);
    if (e == cudaSuccess) {
        std::lock_guard<std::mutex> lock(nannaBufferMutex);
        nannaBuffers[*p] = size;
    }
    return e;
}
static cudaError_t NannaFree(void *p) {
    {
        std::lock_guard<std::mutex> lock(nannaBufferMutex);
        nannaBuffers.erase(p);
    }
    return ::cudaFree(p);
}
static void NannaUntrack(void *p) {
    std::lock_guard<std::mutex> lock(nannaBufferMutex);
    nannaBuffers.erase(p);
}
size_t NannaFreeAggregateBuffers() {
    std::lock_guard<std::mutex> lock(nannaBufferMutex);
    size_t bytes = 0;
    for (auto &b : nannaBuffers) {
        CUDA_CHECK(::cudaFree(b.first));
        bytes += b.second;
    }
    nannaBuffers.clear();
    return bytes;
}
#define cudaMalloc(P, SIZE) NannaMalloc((void **)(P), (SIZE))
#define cudaFree(P) NannaFree((void *)(P))

// One program group per entry point set, made once per process (they belong to the one OptiX context below).
static std::mutex nannaPGMutex;
static std::map<std::string, OptixProgramGroup> nannaPGs;
static std::string NannaPGKey(const OptixProgramGroupDesc &d) {
    auto s = [](const char *c) { return std::string(c ? c : "-"); };
    if (d.kind == OPTIX_PROGRAM_GROUP_KIND_RAYGEN)
        return "R" + s(d.raygen.entryFunctionName);
    if (d.kind == OPTIX_PROGRAM_GROUP_KIND_MISS)
        return "M" + s(d.miss.entryFunctionName);
    return "H" + s(d.hitgroup.entryFunctionNameCH) + "|" + s(d.hitgroup.entryFunctionNameAH) + "|" +
           s(d.hitgroup.entryFunctionNameIS);
}
""")
sub("gpu/optix/aggregate.cpp", """    OptixProgramGroup pg;
    OPTIX_CHECK_WITH_LOG(
        optixProgramGroupCreate(optixContext, &desc, 1, &pgOptions, log, &logSize, &pg),
        log);
""", """    OptixProgramGroup pg;
    std::lock_guard<std::mutex> nannaLock(nannaPGMutex);  // NANNA: made once per process (patch_memory.py)
    auto nannaIter = nannaPGs.find(NannaPGKey(desc));
    if (nannaIter != nannaPGs.end())
        return nannaIter->second;
    OPTIX_CHECK_WITH_LOG(
        optixProgramGroupCreate(optixContext, &desc, 1, &pgOptions, log, &logSize, &pg),
        log);
    nannaPGs[NannaPGKey(desc)] = pg;
""", n=3)
sub("gpu/optix/aggregate.cpp", """    paramsPool.resize(256);  // should be plenty
    for (ParamBufferState &ps : paramsPool) {
        void *ptr;
        CUDA_CHECK(cudaMalloc(&ptr, sizeof(RayIntersectParameters)));
        ps.ptr = (CUdeviceptr)ptr;
        CUDA_CHECK(cudaEventCreate(&ps.finishedEvent));
        CUDA_CHECK(cudaMallocHost(&ps.hostPtr, sizeof(RayIntersectParameters)));
    }
""", """    static std::vector<ParamBufferState> nannaPool;  // NANNA: one pool per process (patch_memory.py)
    if (nannaPool.empty()) {
    paramsPool.resize(256);  // should be plenty
    for (ParamBufferState &ps : paramsPool) {
        void *ptr;
        CUDA_CHECK(cudaMalloc(&ptr, sizeof(RayIntersectParameters)));
        NannaUntrack(ptr);
        ps.ptr = (CUdeviceptr)ptr;
        CUDA_CHECK(cudaEventCreate(&ps.finishedEvent));
        CUDA_CHECK(cudaMallocHost(&ps.hostPtr, sizeof(RayIntersectParameters)));
    }
        nannaPool = paramsPool;
    } else
        paramsPool = nannaPool;
""")
sub("gpu/optix/aggregate.cpp", """    OPTIX_CHECK(optixDeviceContextCreate(cudaContext, &ctxOptions, &optixContext));
""", """    static OptixDeviceContext nannaContext = nullptr;  // NANNA: one per process (patch_memory.py)
    if (!nannaContext)
        OPTIX_CHECK(optixDeviceContextCreate(cudaContext, &ctxOptions, &nannaContext));
    optixContext = nannaContext;
""")
sub("gpu/optix/aggregate.cpp", """    optixModule = createOptiXModule(optixContext, (const char *)PBRT_EMBEDDED_PTX);
""", """    static OptixModule nannaModule = nullptr;  // NANNA: one per process (patch_memory.py)
    if (!nannaModule)
        nannaModule = createOptiXModule(optixContext, (const char *)PBRT_EMBEDDED_PTX);
    optixModule = nannaModule;
""")
sub("gpu/optix/aggregate.cpp", """    OPTIX_CHECK_WITH_LOG(
        optixPipelineCreate(optixContext, &pipelineCompileOptions, &pipelineLinkOptions,
                            allPGs, sizeof(allPGs) / sizeof(allPGs[0]), log, &logSize,
                            &optixPipeline),
        log);
""", """    static OptixPipeline nannaPipeline = nullptr;  // NANNA: one per process (patch_memory.py)
    if (!nannaPipeline)
    OPTIX_CHECK_WITH_LOG(
        optixPipelineCreate(optixContext, &pipelineCompileOptions, &pipelineLinkOptions,
                            allPGs, sizeof(allPGs) / sizeof(allPGs[0]), log, &logSize,
                            &nannaPipeline),
        log);
    optixPipeline = nannaPipeline;
""")

# wavefront.cpp: the server frees the previous scene
sub("wavefront/wavefront.cpp", """#include <pbrt/util/string.h>
#include <algorithm>
""", """#include <pbrt/util/string.h>
#include <pbrt/textures.h>
#include <pbrt/shapes.h>
#include <pbrt/util/buffercache.h>
#include <algorithm>
#include <unordered_set>
""")
sub("wavefront/wavefront.cpp", """extern std::function<bool(int)> NannaPassHook;  // NANNA: wavefront/integrator.cpp
""", """extern std::function<bool(int)> NannaPassHook;  // NANNA: wavefront/integrator.cpp
size_t NannaFreeAggregateBuffers();  // NANNA: gpu/optix/aggregate.cpp, patch_memory.py
""")
sub("wavefront/wavefront.cpp", """    WavefrontPathIntegrator *integrator = nullptr;
    bool filmUsed = false;
    printf("NANNA_READY\\n");
""", """    WavefrontPathIntegrator *integrator = nullptr;
    bool filmUsed = false;
    // patch_memory.py: what is allocated before the first scene stays; a scene reload frees the rest.
    std::unordered_set<void *> base;
    for (const auto &a : CUDATrackedMemoryResource::singleton.NannaAllocations())
        base.insert(a.first);
    printf("NANNA_READY\\n");
""")
sub("wavefront/wavefront.cpp", """            // The previous scene's objects are left as they are (pbrt has no teardown for them); the delegate
            // starts a new process once NANNA_LOADED reports too much GPU memory.
            BasicScene *scene = new BasicScene;
""", """            if (integrator) {
                // patch_memory.py: free the previous scene (tracked allocations made since the base, the OptiX
                // buffers); its host objects are left. Caches that outlive a scene live in NannaPermanentMemory.
                GPUWait();
                size_t freed = NannaFreeAggregateBuffers();
                for (const auto &a : CUDATrackedMemoryResource::singleton.NannaAllocations())
                    if (!base.count(a.first)) {
                        CUDATrackedMemoryResource::singleton.deallocate(a.first, a.second, 1);
                        freed += a.second;
                    }
                ImageTextureBase::ClearCache();
                // The mesh registries (a vector each, read by the GPU through a device pointer) grew in scene memory:
                // new empty ones, which go with the next reload.
                Triangle::Init(Allocator(&CUDATrackedMemoryResource::singleton));
                BilinearPatch::Init(Allocator(&CUDATrackedMemoryResource::singleton));
                // The mesh buffer caches point into the freed scene memory (a lookup memcmps against them: 0xC0000006
                // at the next scene's first mesh, mem1v2.log): new empty ones.
                delete intBufferCache;
                delete point2BufferCache;
                delete point3BufferCache;
                delete vector3BufferCache;
                delete normal3BufferCache;
                intBufferCache = new BufferCache<int>;
                point2BufferCache = new BufferCache<Point2f>;
                point3BufferCache = new BufferCache<Point3f>;
                vector3BufferCache = new BufferCache<Vector3f>;
                normal3BufferCache = new BufferCache<Normal3f>;
                integrator = nullptr;
                printf("NANNA_FREED %d %zu\\n", id, freed);
                fflush(stdout);
            }
            BasicScene *scene = new BasicScene;
""")
print("patched memory")
