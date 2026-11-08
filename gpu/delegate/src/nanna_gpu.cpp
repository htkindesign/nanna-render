// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright 2026 Kiet Ho
// Nanna GPU as a Hydra render delegate, route 2 of NOTES.md: a thin delegate inside Blender, pbrt-GPU stays a child process.
//
// Hydra gives meshes and the camera. Python (the engine's get_render_settings) gives, in the folder named by nanna:sceneDir:
//   materials.txt  blocks "@<Hydra material name>" then pbrt lines; a line "shape: <params>" is added to that mesh's Shape instead
//   world.pbrt     texture lines and lights, included after WorldBegin
// The delegate writes one PLY per mesh and scene.pbrt there, runs nanna_gpu.exe (a modified pbrt-v4) --gpu --nanna-serve (pbrt\patch_server.py's protocol), reads the
// PFM it writes and fills the render buffers. A pbrt fault is a failed frame, never a fault in Blender's process.
#include "pxr/pxr.h"
#include "pxr/base/gf/half.h"
#include "pxr/base/gf/matrix3d.h"
#include "pxr/base/gf/matrix4d.h"
#include "pxr/base/gf/matrix4f.h"
#include "pxr/base/gf/vec2f.h"
#include "pxr/base/gf/vec3f.h"
#include "pxr/base/gf/vec4f.h"
#include "pxr/base/tf/staticTokens.h"
#include "pxr/base/vt/array.h"
#include "pxr/base/vt/dictionary.h"
#include "pxr/imaging/hd/camera.h"
#include "pxr/imaging/hd/instancer.h"
#include "pxr/base/gf/quatd.h"
#include "pxr/base/gf/quatf.h"
#include "pxr/base/gf/quath.h"
#include "pxr/imaging/hd/material.h"
#include "pxr/imaging/hd/mesh.h"
#include "pxr/imaging/hd/meshUtil.h"
#include "pxr/imaging/hd/renderBuffer.h"
#include "pxr/imaging/hd/renderDelegate.h"
#include "pxr/imaging/hd/renderIndex.h"
#include "pxr/imaging/hd/renderPass.h"
#include "pxr/imaging/hd/renderPassState.h"
#include "pxr/imaging/hd/rendererPlugin.h"
#include "pxr/imaging/hd/rendererPluginRegistry.h"
#include "pxr/imaging/hd/resourceRegistry.h"
#include "pxr/imaging/hd/sceneDelegate.h"
#include "pxr/imaging/hd/tokens.h"

#include <windows.h>
#include <cstdlib>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

PXR_NAMESPACE_USING_DIRECTIVE

namespace {

std::mutex logMutex;
void say(const std::string& text)
{
    std::lock_guard<std::mutex> lock(logMutex);
    std::fprintf(stdout, "[nanna_gpu] %s\n", text.c_str());
    std::fflush(stdout);
}

std::string lastError; // read by the add-on through nannaGpuLastError()
std::mutex errorMutex;
void setError(const std::string& text)
{
    say("ERROR " + text);
    std::lock_guard<std::mutex> lock(errorMutex);
    lastError = text;
}

std::wstring widen(const std::string& s)
{
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), int(s.size()), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), int(s.size()), w.data(), n);
    return w;
}

// Bumped by every mesh or instancer Sync and every mesh made or deleted: PLYs are rewritten only when it moves.
// Hydra's scene state version also moves on a camera change (measured: every viewport orbit step), so it can't say that.
std::atomic<unsigned> geometryEpoch{1};

// ---------------------------------------------------------------- render buffer

class NgBuffer final : public HdRenderBuffer {
public:
    explicit NgBuffer(const SdfPath& id) : HdRenderBuffer(id) {}
    bool Allocate(const GfVec3i& dims, HdFormat format, bool multiSampled) override
    {
        std::lock_guard<std::mutex> lock(mutex);
        width = unsigned(dims[0]); height = unsigned(dims[1]); fmt = format;
        data.assign(size_t(width) * height * HdDataSizeOfFormat(format), 0);
        converged = false;
        return true;
    }
    unsigned int GetWidth() const override { return width; }
    unsigned int GetHeight() const override { return height; }
    unsigned int GetDepth() const override { return 1; }
    HdFormat GetFormat() const override { return fmt; }
    bool IsMultiSampled() const override { return false; }
    void* Map() override { mapped++; return data.data(); }
    void Unmap() override { mapped--; }
    bool IsMapped() const override { return mapped > 0; }
    void Resolve() override {}
    bool IsConverged() const override { return converged; }

    // rgb: width x height x 3 floats, row 0 at the bottom (Hydra's and PFM's order). Depth and other one-channel AOVs get `fill`.
    void Write(const std::vector<float>& rgb, unsigned w, unsigned h, float fill, bool final = true)
    {
        std::lock_guard<std::mutex> lock(mutex);
        if (w != width || h != height) return;
        const HdFormat comp = HdGetComponentFormat(fmt);
        const size_t count = HdGetComponentCount(fmt);
        const size_t pixelBytes = HdDataSizeOfFormat(fmt);
        for (size_t i = 0; i < size_t(w) * h; ++i) {
            float v[4] = {fill, fill, fill, 1.0f};
            if (count >= 3) { v[0] = rgb[3 * i]; v[1] = rgb[3 * i + 1]; v[2] = rgb[3 * i + 2]; }
            uint8_t* p = data.data() + i * pixelBytes;
            for (size_t c = 0; c < count; ++c) {
                if (comp == HdFormatFloat32) reinterpret_cast<float*>(p)[c] = v[c];
                else if (comp == HdFormatFloat16) reinterpret_cast<GfHalf*>(p)[c] = GfHalf(v[c]);
                else if (comp == HdFormatUNorm8) p[c] = uint8_t(std::min(std::max(v[c], 0.0f), 1.0f) * 255.0f + 0.5f);
            }
        }
        converged = final;
    }
    // The cleared picture of a view change: colour black, depth far.
    void Clear(float fill)
    {
        std::lock_guard<std::mutex> lock(mutex);
        const HdFormat comp = HdGetComponentFormat(fmt);
        const size_t count = HdGetComponentCount(fmt), pixelBytes = HdDataSizeOfFormat(fmt);
        for (size_t i = 0; i < size_t(width) * height; ++i) {
            const float v[4] = {fill, fill, fill, 1.0f};
            uint8_t* p = data.data() + i * pixelBytes;
            for (size_t c = 0; c < count; ++c) {
                if (comp == HdFormatFloat32) reinterpret_cast<float*>(p)[c] = v[c];
                else if (comp == HdFormatFloat16) reinterpret_cast<GfHalf*>(p)[c] = GfHalf(v[c]);
                else if (comp == HdFormatUNorm8) p[c] = uint8_t(std::min(std::max(v[c], 0.0f), 1.0f) * 255.0f + 0.5f);
            }
        }
        converged = false;
    }
    void MarkConverged() { converged = true; }

private:
    void _Deallocate() override { data.clear(); }
    std::mutex mutex;
    std::vector<uint8_t> data;
    unsigned width = 0, height = 0;
    HdFormat fmt = HdFormatInvalid;
    std::atomic<int> mapped{0};
    std::atomic<bool> converged{false};
};

// ---------------------------------------------------------------- mesh

class NgMesh final : public HdMesh {
public:
    explicit NgMesh(const SdfPath& id) : HdMesh(id) {}
    HdDirtyBits GetInitialDirtyBitsMask() const override { return HdChangeTracker::AllSceneDirtyBits; }

    void Sync(HdSceneDelegate* scene, HdRenderParam*, HdDirtyBits* dirtyBits, const TfToken&) override
    {
        const SdfPath& id = GetId();
        geometryEpoch++;
        _UpdateVisibility(scene, dirtyBits);
        _UpdateInstancer(scene, dirtyBits);
        HdInstancer::_SyncInstancerAndParents(scene->GetRenderIndex(), GetInstancerId());
        material = scene->GetMaterialId(id);
        xf = scene->GetTransform(id);
        const HdMeshTopology topology = scene->GetMeshTopology(id);
        const VtValue pointsValue = scene->Get(id, HdTokens->points);
        positions.clear(); normals.clear(); uvs.clear();
        if (!pointsValue.IsHolding<VtVec3fArray>()) { *dirtyBits &= ~HdChangeTracker::AllSceneDirtyBits; return; }
        const VtVec3fArray points = pointsValue.UncheckedGet<VtVec3fArray>();
        HdMeshUtil util(&topology, id);
        VtVec3iArray tris;
        VtIntArray params;
        util.ComputeTriangleIndices(&tris, &params);

        // Corner values of a primvar, 3 per triangle, or empty.
        auto corners = [&](const TfToken& name, HdType type, HdInterpolation interp, VtValue& out) {
            const VtValue value = scene->Get(id, name);
            if (value.IsEmpty()) return false;
            if (interp == HdInterpolationFaceVarying) {
                const void* src = nullptr; size_t n = 0;
                if (type == HdTypeFloatVec3 && value.IsHolding<VtVec3fArray>()) { src = value.UncheckedGet<VtVec3fArray>().cdata(); n = value.GetArraySize(); }
                if (type == HdTypeFloatVec2 && value.IsHolding<VtVec2fArray>()) { src = value.UncheckedGet<VtVec2fArray>().cdata(); n = value.GetArraySize(); }
                return src && util.ComputeTriangulatedFaceVaryingPrimvar(src, int(n), type, &out);
            }
            if (interp == HdInterpolationVertex || interp == HdInterpolationVarying) {
                if (type == HdTypeFloatVec3 && value.IsHolding<VtVec3fArray>()) {
                    const VtVec3fArray& a = value.UncheckedGet<VtVec3fArray>(); VtVec3fArray r(tris.size() * 3);
                    for (size_t t = 0; t < tris.size(); ++t) for (int k = 0; k < 3; ++k) r[3 * t + k] = size_t(tris[t][k]) < a.size() ? a[tris[t][k]] : GfVec3f(0);
                    out = r; return true;
                }
                if (type == HdTypeFloatVec2 && value.IsHolding<VtVec2fArray>()) {
                    const VtVec2fArray& a = value.UncheckedGet<VtVec2fArray>(); VtVec2fArray r(tris.size() * 3);
                    for (size_t t = 0; t < tris.size(); ++t) for (int k = 0; k < 3; ++k) r[3 * t + k] = size_t(tris[t][k]) < a.size() ? a[tris[t][k]] : GfVec2f(0);
                    out = r; return true;
                }
            }
            return false;
        };
        VtValue nValue, uvValue;
        bool haveN = false, haveUv = false;
        for (HdInterpolation interp : {HdInterpolationFaceVarying, HdInterpolationVertex, HdInterpolationVarying}) {
            for (const HdPrimvarDescriptor& d : scene->GetPrimvarDescriptors(id, interp)) {
                if (!haveN && d.name == HdTokens->normals) haveN = corners(d.name, HdTypeFloatVec3, interp, nValue);
                if (!haveUv && d.role == HdPrimvarRoleTokens->textureCoordinate) { haveUv = corners(d.name, HdTypeFloatVec2, interp, uvValue); if (haveUv) uvName = d.name; }
            }
        }
        if (!haveUv) { // a float2 primvar with no role
            for (HdInterpolation interp : {HdInterpolationFaceVarying, HdInterpolationVertex}) {
                for (const HdPrimvarDescriptor& d : scene->GetPrimvarDescriptors(id, interp)) {
                    if (!haveUv && d.name != HdTokens->normals && corners(d.name, HdTypeFloatVec2, interp, uvValue) && uvValue.IsHolding<VtVec2fArray>()) { haveUv = true; uvName = d.name; }
                }
            }
        }
        const VtVec3fArray* nArr = haveN && nValue.IsHolding<VtVec3fArray>() ? &nValue.UncheckedGet<VtVec3fArray>() : nullptr;
        const VtVec2fArray* uvArr = haveUv && uvValue.IsHolding<VtVec2fArray>() ? &uvValue.UncheckedGet<VtVec2fArray>() : nullptr;
        if (nArr && nArr->size() != tris.size() * 3) nArr = nullptr;
        if (uvArr && uvArr->size() != tris.size() * 3) uvArr = nullptr;

        // Local space here: the transforms (the mesh's, times each instance's) are applied when the scene is written.
        positions.reserve(tris.size() * 3); normals.reserve(tris.size() * 3); uvs.reserve(tris.size() * 3);
        for (size_t t = 0; t < tris.size(); ++t) {
            GfVec3f p[3], n[3]; GfVec2f uv[3];
            for (int k = 0; k < 3; ++k) {
                const int vi = tris[t][k];
                const GfVec3f local = size_t(vi) < points.size() ? points[vi] : GfVec3f(0);
                p[k] = local;
                uv[k] = uvArr ? (*uvArr)[3 * t + k] : GfVec2f(0);
            }
            const GfVec3f faceN = GfGetNormalized(GfCross(p[1] - p[0], p[2] - p[0]));
            for (int k = 0; k < 3; ++k) n[k] = nArr ? GfGetNormalized((*nArr)[3 * t + k]) : faceN;
            for (int k = 0; k < 3; ++k) { positions.push_back(p[k]); normals.push_back(n[k]); uvs.push_back(uv[k]); }
        }
        *dirtyBits &= ~HdChangeTracker::AllSceneDirtyBits;
    }

    SdfPath material;
    TfToken uvName;
    GfMatrix4d xf{1.0};
    std::vector<GfVec3f> positions, normals;
    std::vector<GfVec2f> uvs;

protected:
    HdDirtyBits _PropagateDirtyBits(HdDirtyBits bits) const override { return bits; }
    void _InitRepr(const TfToken&, HdDirtyBits*) override {}
};

class NgMaterial final : public HdMaterial {
public:
    explicit NgMaterial(const SdfPath& id) : HdMaterial(id) {}
    void Sync(HdSceneDelegate*, HdRenderParam*, HdDirtyBits* dirtyBits) override { *dirtyBits = HdChangeTracker::Clean; }
    HdDirtyBits GetInitialDirtyBitsMask() const override { return HdChangeTracker::AllDirty; }
};

// After hdEmbree's instancer: instancer xf, then per index translate, rotate, scale, instanceTransforms; nested instancers multiply in.
class NgInstancer final : public HdInstancer {
public:
    NgInstancer(HdSceneDelegate* d, const SdfPath& id) : HdInstancer(d, id) {}
    void Sync(HdSceneDelegate* d, HdRenderParam*, HdDirtyBits* dirtyBits) override
    {
        _UpdateInstancer(d, dirtyBits);
        geometryEpoch++;
        primvars.clear();
        for (const HdPrimvarDescriptor& pv : d->GetPrimvarDescriptors(GetId(), HdInterpolationInstance)) primvars[pv.name] = d->Get(GetId(), pv.name);
        *dirtyBits = HdChangeTracker::Clean;
    }
    VtMatrix4dArray Compute(const SdfPath& prototype)
    {
        const GfMatrix4d base = GetDelegate()->GetInstancerTransform(GetId());
        const VtIntArray indices = GetDelegate()->GetInstanceIndices(GetId(), prototype);
        VtMatrix4dArray out(indices.size(), base);
        auto each = [&](const TfToken& name, auto apply) {
            auto it = primvars.find(name);
            if (it != primvars.end()) for (size_t i = 0; i < indices.size(); ++i) apply(it->second, size_t(indices[i]), out[i]);
        };
        each(HdInstancerTokens->instanceTranslations, [](const VtValue& v, size_t k, GfMatrix4d& m) {
            if (v.IsHolding<VtVec3fArray>() && k < v.GetArraySize()) { GfMatrix4d t(1); t.SetTranslate(GfVec3d(v.UncheckedGet<VtVec3fArray>()[k])); m = t * m; } });
        each(HdInstancerTokens->instanceRotations, [](const VtValue& v, size_t k, GfMatrix4d& m) {
            if (v.IsHolding<VtQuathArray>() && k < v.GetArraySize()) { GfMatrix4d r(1); r.SetRotate(GfQuatd(v.UncheckedGet<VtQuathArray>()[k])); m = r * m; }
            else if (v.IsHolding<VtQuatfArray>() && k < v.GetArraySize()) { GfMatrix4d r(1); r.SetRotate(GfQuatd(v.UncheckedGet<VtQuatfArray>()[k])); m = r * m; }
            else if (v.IsHolding<VtVec4fArray>() && k < v.GetArraySize()) { const GfVec4f q = v.UncheckedGet<VtVec4fArray>()[k]; GfMatrix4d r(1); r.SetRotate(GfQuatd(q[0], q[1], q[2], q[3])); m = r * m; } });
        each(HdInstancerTokens->instanceScales, [](const VtValue& v, size_t k, GfMatrix4d& m) {
            if (v.IsHolding<VtVec3fArray>() && k < v.GetArraySize()) { GfMatrix4d sc(1); sc.SetScale(GfVec3d(v.UncheckedGet<VtVec3fArray>()[k])); m = sc * m; } });
        each(HdInstancerTokens->instanceTransforms, [](const VtValue& v, size_t k, GfMatrix4d& m) {
            if (v.IsHolding<VtMatrix4dArray>() && k < v.GetArraySize()) m = v.UncheckedGet<VtMatrix4dArray>()[k] * m;
            else if (v.IsHolding<VtMatrix4fArray>() && k < v.GetArraySize()) m = GfMatrix4d(v.UncheckedGet<VtMatrix4fArray>()[k]) * m; });
        if (GetParentId().IsEmpty()) return out;
        auto* parent = dynamic_cast<NgInstancer*>(GetDelegate()->GetRenderIndex().GetInstancer(GetParentId()));
        if (!parent) return out;
        const VtMatrix4dArray up = parent->Compute(GetId());
        VtMatrix4dArray all(up.size() * out.size());
        for (size_t i = 0; i < up.size(); ++i) for (size_t j = 0; j < out.size(); ++j) all[i * out.size() + j] = out[j] * up[i];
        return all;
    }

private:
    std::map<TfToken, VtValue, TfTokenFastArbitraryLessThan> primvars;
};

// ---------------------------------------------------------------- pbrt child process

double now() // seconds since the DLL loaded, for the logs
{
    static const auto origin = std::chrono::steady_clock::now();
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - origin).count();
}

// One nanna_gpu.exe --gpu --nanna-serve (pbrt\patch_server.py): it loads a scene, renders it, and waits for the next command on stdin
// (a new scene, a camera-only change that keeps the BVH, lights, materials and textures, or a cancel between sample passes).
// Never killed: TerminateProcess on a pbrt in GPU work lingered 1 to 5 minutes in the driver and held Blender's exit up with it (quit1,
// NOTES). To stop one, "cancel" ends its frame after the current sample pass, and closing its stdin makes it exit by itself.
class Pbrt {
public:
    bool Start(const std::string& path)
    {
        exe = path;
        SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
        HANDLE outRead, outWrite, inRead, inWrite;
        if (!CreatePipe(&outRead, &outWrite, &sa, 0)) return false;
        if (!CreatePipe(&inRead, &inWrite, &sa, 0)) { CloseHandle(outRead); CloseHandle(outWrite); return false; }
        SetHandleInformation(outRead, HANDLE_FLAG_INHERIT, 0);
        SetHandleInformation(inWrite, HANDLE_FLAG_INHERIT, 0);
        STARTUPINFOW si{}; si.cb = sizeof(si); si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = inRead; si.hStdOutput = outWrite; si.hStdError = outWrite;
        PROCESS_INFORMATION pi{};
        std::wstring cmd = L"\"" + widen(exe) + L"\" --gpu --nanna-serve";
        wchar_t temp[MAX_PATH]; GetTempPathW(MAX_PATH, temp);
        const BOOL ok = CreateProcessW(widen(exe).c_str(), cmd.data(), nullptr, nullptr, TRUE,
                                       CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS, nullptr, temp, &si, &pi);
        CloseHandle(outWrite); CloseHandle(inRead);
        if (!ok) { CloseHandle(outRead); CloseHandle(inWrite); return false; }
        CloseHandle(pi.hThread);
        process = pi.hProcess; pid = pi.dwProcessId; stdinWrite = inWrite;
        startedAt = now();
        std::shared_ptr<Io> shared = io;
        std::thread([shared, outRead] {
            char buf[4096]; DWORD n = 0;
            while (ReadFile(outRead, buf, sizeof(buf), &n, nullptr) && n) {
                std::lock_guard<std::mutex> l(shared->m);
                shared->text.append(buf, n);
                if (!shared->ready && shared->text.find("NANNA_READY") != std::string::npos) { shared->ready = true; shared->readyAt = now(); }
            }
            CloseHandle(outRead);
        }).detach();
        say("pbrt server started, pid " + std::to_string(pid));
        return true;
    }
    ~Pbrt()
    {
        CloseStdin(); // it cancels its frame, if any, and exits by itself
        if (process) CloseHandle(process);
    }
    std::string Output(size_t from = 0) { std::lock_guard<std::mutex> l(io->m); return from < io->text.size() ? io->text.substr(from) : std::string(); }
    size_t OutputSize() { std::lock_guard<std::mutex> l(io->m); return io->text.size(); }
    bool Ready() { std::lock_guard<std::mutex> l(io->m); return io->ready; }
    double ReadyAt() { std::lock_guard<std::mutex> l(io->m); return io->readyAt; }
    bool Alive() const { return process && WaitForSingleObject(process, 0) == WAIT_TIMEOUT; }
    bool Send(const std::string& line)
    {
        std::lock_guard<std::mutex> l(inMutex);
        DWORD n = 0;
        return stdinWrite && WriteFile(stdinWrite, line.data(), DWORD(line.size()), &n, nullptr) && n == line.size();
    }
    DWORD ExitCode() const { DWORD c = 0; GetExitCodeProcess(process, &c); return c; }
    void CloseStdin() { std::lock_guard<std::mutex> l(inMutex); if (stdinWrite) { CloseHandle(stdinWrite); stdinWrite = nullptr; } }
    DWORD pid = 0;
    std::string exe;
    double startedAt = 0;
    // Used by the one render pass that holds it at a time:
    int nextId = 0;
    std::string loadedKey;  // the scene it holds (folder, geometry, size, spp, seed, depth): same key = a camera command does
    double gpuBytes = 0;    // pbrt's tracked GPU allocations after its last scene (pbrt never frees an old scene)
    int pendingId = 0;      // a scene command cancelled before its NANNA_LOADED was seen
    std::string pendingKey;
    size_t pendingMark = 0;
    std::atomic<bool> rendering{false};

private:
    struct Io { std::mutex m; std::string text; bool ready = false; double readyAt = 0; };
    std::shared_ptr<Io> io = std::make_shared<Io>();
    std::mutex inMutex;
    HANDLE process = nullptr, stdinWrite = nullptr;
};
using PbrtPtr = std::shared_ptr<Pbrt>;

// A server that has loaded scenes past this much GPU memory is told to exit once its frame is done; a spare takes over.
constexpr double kServerBudget = 6e9;

// One warm spare server per Blender (a viewport keeps its own server for as long as it is open, F12 takes one per render and gives it back).
// A new spare starts only when no other is starting and no server is rendering (a start-up beside a render slowed it 3.3 -> 11.7 s, NOTES).
// The keeper thread starts spares and drops servers that have exited.
class Pool {
public:
    void Warm(const std::string& path)
    {
        std::lock_guard<std::mutex> l(m);
        if (exe != path) {
            for (auto& p : waiting) { p->CloseStdin(); retiring.push_back(p); }
            waiting.clear();
            exe = path;
        }
        if (!keeper) { keeper = true; std::thread(&Pool::Keep, this).detach(); }
    }
    // A server: a ready spare, else a starting one, else a new one.
    PbrtPtr Take(const std::string& path)
    {
        std::unique_lock<std::mutex> l(m);
        if (exe != path) { l.unlock(); Warm(path); l.lock(); }
        PbrtPtr p;
        for (auto it = waiting.begin(); it != waiting.end(); ++it)
            if ((*it)->Ready() && (*it)->Alive()) { p = *it; waiting.erase(it); break; }
        if (!p) for (auto it = waiting.begin(); it != waiting.end(); ++it)
            if ((*it)->Alive()) { p = *it; waiting.erase(it); break; }
        if (!p) {
            p = std::make_shared<Pbrt>();
            if (!p->Start(path)) return nullptr;
            say("no spare: cold start");
        } else {
            say(std::string("took server pid ") + std::to_string(p->pid) + (p->Ready() ? " (ready)" : " (still starting)"));
        }
        busy.push_back(p);
        return p;
    }
    // Back as a spare (it may still be finishing a cancelled pass: its next command waits behind it). Over budget, dead or
    // from an old exe: told to exit instead.
    void GiveBack(PbrtPtr p)
    {
        std::lock_guard<std::mutex> l(m);
        busy.erase(std::remove(busy.begin(), busy.end(), p), busy.end());
        if (p->Alive() && p->exe == exe && p->gpuBytes < kServerBudget) waiting.push_back(p);
        else { p->CloseStdin(); retiring.push_back(p); }
    }
    void Retire(PbrtPtr p)
    {
        std::lock_guard<std::mutex> l(m);
        busy.erase(std::remove(busy.begin(), busy.end(), p), busy.end());
        p->CloseStdin();
        retiring.push_back(p);
    }
    // Blender's exit or the add-on's unregister: every server gets its stdin closed (a rendering one stops after its current sample
    // pass, a starting one after its start-up) and exits by itself. Nothing waits for them and nothing is killed. No new spares.
    void Shutdown()
    {
        std::lock_guard<std::mutex> l(m);
        for (auto& p : waiting) { p->CloseStdin(); say("server released at shutdown, pid " + std::to_string(p->pid)); retiring.push_back(p); }
        for (auto& p : busy) { p->CloseStdin(); say("busy server released at shutdown, pid " + std::to_string(p->pid)); }
        waiting.clear();
        exe.clear();
    }
    std::string Info()
    {
        std::lock_guard<std::mutex> l(m);
        std::ostringstream s;
        s << "waiting";
        for (auto& p : waiting) s << ' ' << p->pid << (p->Ready() ? "r" : "s");
        s << " busy";
        for (auto& p : busy) s << ' ' << p->pid << (p->rendering ? "R" : "");
        s << " retiring";
        for (auto& p : retiring) s << ' ' << p->pid << (p->Alive() ? "a" : "x");
        return s.str();
    }

private:
    void Keep()
    {
        while (true) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            std::lock_guard<std::mutex> l(m);
            retiring.erase(std::remove_if(retiring.begin(), retiring.end(), [](const PbrtPtr& p) {
                if (p->Alive()) return false;
                say("pbrt gone, pid " + std::to_string(p->pid) + ", code " + std::to_string(p->ExitCode()));
                return true;
            }), retiring.end());
            waiting.erase(std::remove_if(waiting.begin(), waiting.end(), [](const PbrtPtr& p) {
                if (p->Alive()) return false;
                say("spare died while waiting, pid " + std::to_string(p->pid) + ", code " + std::to_string(p->ExitCode()));
                return true;
            }), waiting.end());
            if (exe.empty()) continue;
            bool starting = false, rendering = false;
            for (auto& p : waiting) starting |= !p->Ready();
            for (auto& p : busy) { rendering |= p->rendering.load(); starting |= !p->Ready(); } // a taken server may still be starting
            if (waiting.empty() && !starting && !rendering) {
                auto p = std::make_shared<Pbrt>();
                if (p->Start(exe)) waiting.push_back(p);
            }
        }
    }
    std::mutex m;
    std::string exe;
    bool keeper = false;
    std::vector<PbrtPtr> waiting, retiring, busy;
};
Pool& pool() { static Pool* p = new Pool; return *p; } // never destroyed: its detached keeper thread outlives static destruction

bool readPfm(const std::filesystem::path& path, std::vector<float>& rgb, unsigned& w, unsigned& h)
{
    std::ifstream in(path, std::ios::binary);
    if (!in) return false;
    std::string magic; double scale = 0;
    in >> magic >> w >> h >> scale;
    in.get();
    if (magic != "PF" || !w || !h) return false;
    rgb.resize(size_t(w) * h * 3);
    in.read(reinterpret_cast<char*>(rgb.data()), std::streamsize(rgb.size() * sizeof(float)));
    if (!in) return false;
    if (scale > 0) { // big endian
        for (float& f : rgb) { uint32_t u; std::memcpy(&u, &f, 4); u = _byteswap_ulong(u); std::memcpy(&f, &u, 4); }
    }
    return true; // PFM rows go bottom up, as Hydra's buffers
}

// The mesh's corners under each transform (row-vector matrices), one after another; empty list: as stored (local space).
void writePly(const std::filesystem::path& path, const NgMesh& mesh, const std::vector<GfMatrix4d>& xfs)
{
    const size_t local = mesh.positions.size();
    const size_t copies = xfs.empty() ? 1 : xfs.size();
    const size_t nv = local * copies;
    std::ofstream f(path, std::ios::binary);
    f << "ply\nformat binary_little_endian 1.0\nelement vertex " << nv
      << "\nproperty float x\nproperty float y\nproperty float z\nproperty float nx\nproperty float ny\nproperty float nz\n"
         "property float u\nproperty float v\nelement face " << nv / 3 << "\nproperty list uchar int vertex_indices\nend_header\n";
    std::vector<float> verts(nv * 8);
    for (size_t c = 0; c < copies; ++c) {
        const GfMatrix4d xf = xfs.empty() ? GfMatrix4d(1.0) : xfs[c];
        const GfMatrix3d linear = xf.ExtractRotationMatrix(); // the upper 3x3
        const GfMatrix3d normalXf = linear.GetInverse().GetTranspose();
        const bool flip = linear.GetDeterminant() < 0; // keep the corner order consistent with the normals, as gpu.py
        for (size_t i = 0; i < local; ++i) {
            const size_t src = flip ? (i - i % 3) + (3 - i % 3) % 3 : i; // corners 0,1,2 -> 0,2,1
            const GfVec3f p = GfVec3f(xf.Transform(GfVec3d(mesh.positions[src])));
            const GfVec3f n = GfVec3f(GfGetNormalized(GfVec3d(mesh.normals[src]) * normalXf));
            float* v = &verts[(c * local + i) * 8];
            v[0] = p[0]; v[1] = p[1]; v[2] = p[2]; v[3] = n[0]; v[4] = n[1]; v[5] = n[2];
            v[6] = mesh.uvs[src][0]; v[7] = mesh.uvs[src][1];
        }
    }
    f.write(reinterpret_cast<const char*>(verts.data()), std::streamsize(verts.size() * sizeof(float)));
    std::vector<char> faces((nv / 3) * 13);
    for (size_t t = 0; t < nv / 3; ++t) {
        char* p = &faces[t * 13]; p[0] = 3;
        int32_t idx[3] = {int32_t(3 * t), int32_t(3 * t + 1), int32_t(3 * t + 2)};
        std::memcpy(p + 1, idx, 12);
    }
    f.write(faces.data(), std::streamsize(faces.size()));
}

// "@name" blocks of materials.txt.
std::map<std::string, std::vector<std::string>> readBlocks(const std::filesystem::path& path)
{
    std::map<std::string, std::vector<std::string>> blocks;
    std::ifstream in(path);
    std::string line, name;
    while (std::getline(in, line)) {
        if (!line.empty() && line[0] == '@') { name = line.substr(1); blocks[name]; }
        else if (!name.empty()) blocks[name].push_back(line);
    }
    return blocks;
}

std::string fmt(double v) { char b[32]; std::snprintf(b, sizeof(b), "%.9g", v); return b; }

class NgDelegate;

// ---------------------------------------------------------------- render pass

// What a picture depends on. A change of any of them clears the buffers and starts a new run (cancelling the running one).
struct Key {
    GfMatrix4d view{1.0}, proj{1.0};
    unsigned w = 0, h = 0, settingsVersion = 0;
    uint64_t geometry = 0; // hash of what the meshes hold: Blender re-sends unchanged meshes on unrelated depsgraph updates (measured)
    bool operator==(const Key& o) const
    {
        return view == o.view && proj == o.proj && w == o.w && h == o.h && geometry == o.geometry && settingsVersion == o.settingsVersion;
    }
};

// One run: its scene file, camera and spp (viewport: a snapshot after the first spp, then the full spp, in one frame of one server).
struct Run {
    int gen = 0;
    std::filesystem::path dir, sceneFile;
    std::string exe;
    std::vector<int> spps;
    int seed = 0;
    unsigned w = 0, h = 0;
    bool viewport = false;
    double changedAt = 0; // when the view change that made this run was seen
    std::string sceneKey; // what the server must hold for a camera-only command: folder, geometry, size, spp, seed, depth
    std::string camera;   // fov, lens radius, focus distance and the 16 numbers of the scene's Transform, tab separated
};

class NgPass final : public HdRenderPass {
public:
    NgPass(HdRenderIndex* index, const HdRprimCollection& collection, NgDelegate* d);
    ~NgPass() override;
    bool IsConverged() const override;

protected:
    void _Execute(const HdRenderPassStateSharedPtr& state, const TfTokenVector&) override;

private:
    void Work(Run run);
    std::string WorkOnce(const Run& run, int attempt, int& stage); // "" = finished (picture, cancel or failure), else why the watchdog fired
    void StopWorker();
    bool Export(const HdRenderPassStateSharedPtr& state, Run& run);
    void Publish() const; // newest result of the current run into the bound buffers, on Blender's thread
    void Clear() const;
    void Fail(int gen, const std::string& error);
    NgDelegate* delegate;
    PbrtPtr server; // the viewport's server, kept between runs (only the worker thread uses it; the destructor after joining it)
    bool viewport = false;
    std::thread worker;
    std::atomic<bool> cancel{false};
    Key key;
    bool haveKey = false, launched = false;
    double changedAt = 0;
    int gen = 0;
    std::atomic<int> runningGen{-1};
    unsigned hashedEpoch = 0;
    uint64_t geometryHash = 0;
    uint64_t HashGeometry();
    // geometry: PLYs and the world body are rewritten only when the scene (or the folder) changes, not on camera moves
    std::string bodyKey, body, objects;
    int geometryCount = 0;
    // results
    mutable std::mutex resultMutex;
    std::vector<float> result;
    unsigned resultW = 0, resultH = 0;
    int resultGen = -1, resultStage = -1;
    bool resultFinal = false, resultFailed = false;
    mutable int writtenGen = -1, writtenStage = -1;
    mutable bool converged = false;
    mutable HdRenderPassAovBindingVector bindings;
};

// ---------------------------------------------------------------- delegate

TF_DEFINE_PRIVATE_TOKENS(NgTokens, ((sceneDir, "nanna:sceneDir"))((exe, "nanna:pbrt"))((spp, "nanna:spp"))((seed, "nanna:seed"))
                         ((maxdepth, "nanna:maxdepth"))((dof, "nanna:dof"))((percentDone, "percentDone"))
                         ((viewport, "nanna:viewport"))((firstSpp, "nanna:firstSpp"))((aux, "nanna:aux")));

class NgDelegate final : public HdRenderDelegate {
public:
    explicit NgDelegate(const HdRenderSettingsMap& settings) : HdRenderDelegate(settings) { live++; say("delegate created"); }
    ~NgDelegate() override { live--; say("delegate deleted"); }
    const TfTokenVector& GetSupportedRprimTypes() const override { static TfTokenVector t{HdPrimTypeTokens->mesh}; return t; }
    const TfTokenVector& GetSupportedSprimTypes() const override { static TfTokenVector t{HdPrimTypeTokens->camera, HdPrimTypeTokens->material}; return t; }
    const TfTokenVector& GetSupportedBprimTypes() const override { static TfTokenVector t{HdPrimTypeTokens->renderBuffer}; return t; }
    HdResourceRegistrySharedPtr GetResourceRegistry() const override { return registry; }
    HdRenderPassSharedPtr CreateRenderPass(HdRenderIndex* index, const HdRprimCollection& collection) override
    {
        return std::make_shared<NgPass>(index, collection, this);
    }
    HdInstancer* CreateInstancer(HdSceneDelegate* d, const SdfPath& id) override { return new NgInstancer(d, id); }
    void DestroyInstancer(HdInstancer* i) override { delete i; }
    HdRprim* CreateRprim(const TfToken& type, const SdfPath& id) override { geometryEpoch++; return type == HdPrimTypeTokens->mesh ? new NgMesh(id) : nullptr; }
    void DestroyRprim(HdRprim* p) override { geometryEpoch++; delete p; }
    HdSprim* CreateSprim(const TfToken& type, const SdfPath& id) override
    {
        if (type == HdPrimTypeTokens->camera) return new HdCamera(id);
        if (type == HdPrimTypeTokens->material) return new NgMaterial(id);
        return nullptr;
    }
    HdSprim* CreateFallbackSprim(const TfToken& type) override { return CreateSprim(type, SdfPath::EmptyPath()); }
    void DestroySprim(HdSprim* p) override { delete p; }
    HdBprim* CreateBprim(const TfToken& type, const SdfPath& id) override { return type == HdPrimTypeTokens->renderBuffer ? new NgBuffer(id) : nullptr; }
    HdBprim* CreateFallbackBprim(const TfToken& type) override { return CreateBprim(type, SdfPath::EmptyPath()); }
    void DestroyBprim(HdBprim* p) override { delete p; }
    void CommitResources(HdChangeTracker*) override {}
    // Blender adds only the AOVs whose default descriptor has a format.
    HdAovDescriptor GetDefaultAovDescriptor(const TfToken& name) const override
    {
        if (name == HdAovTokens->color) return HdAovDescriptor(HdFormatFloat32Vec4, false, VtValue(GfVec4f(0, 0, 0, 1)));
        if (name == HdAovTokens->depth) return HdAovDescriptor(HdFormatFloat32, false, VtValue(1.0f));
        return HdAovDescriptor();
    }
    VtDictionary GetRenderStats() const override
    {
        VtDictionary d; d[NgTokens->percentDone.GetString()] = percent.load(); return d;
    }
    template <class T> T Setting(const TfToken& key, const T& fallback) const
    {
        const VtValue v = GetRenderSetting(key);
        if (v.IsHolding<T>()) return v.UncheckedGet<T>();
        if constexpr (std::is_same_v<T, int>) {
            if (v.IsHolding<int64_t>()) return int(v.UncheckedGet<int64_t>());
            if (v.IsHolding<long>()) return int(v.UncheckedGet<long>());
            if (v.IsHolding<double>()) return int(v.UncheckedGet<double>());
        }
        return fallback;
    }
    std::atomic<double> percent{0.0};
    static std::atomic<int> live;

private:
    HdResourceRegistrySharedPtr registry = std::make_shared<HdResourceRegistry>();
};
std::atomic<int> NgDelegate::live{0};
HdRenderBuffer* bufferOf(HdRenderIndex* index, const HdRenderPassAovBinding& b)
{
    if (b.renderBuffer) return b.renderBuffer;
    return static_cast<HdRenderBuffer*>(index->GetBprim(HdPrimTypeTokens->renderBuffer, b.renderBufferId));
}

NgPass::NgPass(HdRenderIndex* index, const HdRprimCollection& collection, NgDelegate* d) : HdRenderPass(index, collection), delegate(d)
{
    viewport = delegate->Setting<bool>(NgTokens->viewport, false);
}

NgPass::~NgPass()
{
    StopWorker(); // Esc in F12, closing the viewport and Blender's exit come here: the frame is cancelled, nothing waits for pbrt
    if (server) pool().GiveBack(std::move(server)); // a spare again, scene and all
}

void NgPass::Fail(int failedGen, const std::string& error)
{
    setError(error);
    std::lock_guard<std::mutex> l(resultMutex);
    resultGen = failedGen; resultFailed = resultFinal = true;
}

void NgPass::StopWorker()
{
    cancel = true;
    if (worker.joinable()) worker.join();
    cancel = false;
}

void NgPass::Clear() const
{
    for (const auto& b : bindings)
        if (NgBuffer* rb = dynamic_cast<NgBuffer*>(bufferOf(GetRenderIndex(), b))) rb->Clear(b.aovName == HdAovTokens->color ? 0.0f : 1.0f);
}

void NgPass::_Execute(const HdRenderPassStateSharedPtr& state, const TfTokenVector&)
{
    bindings = state->GetAovBindings();
    HdRenderIndex* index = GetRenderIndex();
    Key k;
    for (const auto& b : bindings) if (HdRenderBuffer* rb = bufferOf(index, b)) { k.w = rb->GetWidth(); k.h = rb->GetHeight(); break; }
    k.view = state->GetWorldToViewMatrix();
    k.proj = state->GetProjectionMatrix();
    if (hashedEpoch != geometryEpoch.load()) {
        hashedEpoch = geometryEpoch.load();
        const double t0 = now();
        geometryHash = HashGeometry();
        say("geometry synced (epoch " + std::to_string(hashedEpoch) + "), hashed in " + fmt(now() - t0) + " s");
    }
    k.geometry = geometryHash;
    k.settingsVersion = delegate->GetRenderSettingsVersion();
    if (!haveKey || !(k == key)) {
        // The view changed: the running frame is cancelled (its server stops after the current sample pass) and the old picture goes at once.
        if (worker.joinable()) StopWorker();
        runningGen = -1;
        {
            std::lock_guard<std::mutex> lock(resultMutex);
            result.clear(); resultGen = -1; resultStage = -1; resultFinal = resultFailed = false;
        }
        Clear();
        converged = false; writtenGen = writtenStage = -1;
        if (haveKey) say("view changed (" + std::string(k.w != key.w || k.h != key.h ? "size" : k.geometry != key.geometry ? "geometry" :
                         k.settingsVersion != key.settingsVersion ? "settings" : "camera") + "): picture cleared at " + fmt(now()));
        key = k; haveKey = true; launched = false; changedAt = now();
    }
    Publish();
    if (launched) return;
    // Viewport: while the view keeps changing (a drag) no pbrt is used up; a run starts once it has held still for 80 ms.
    if (viewport && now() - changedAt < 0.08) return;
    launched = true;
    Run run;
    run.gen = ++gen; run.viewport = viewport; run.changedAt = changedAt; run.w = k.w; run.h = k.h;
    if (!Export(state, run)) {
        std::lock_guard<std::mutex> lock(resultMutex);
        resultGen = run.gen; resultFailed = resultFinal = true;
        return;
    }
    runningGen = run.gen;
    worker = std::thread(&NgPass::Work, this, run);
}

bool NgPass::Export(const HdRenderPassStateSharedPtr& state, Run& run)
{
    HdRenderIndex* index = GetRenderIndex();
    const unsigned w = run.w, h = run.h;
    const std::string dirText = delegate->Setting<std::string>(NgTokens->sceneDir, "");
    run.exe = delegate->Setting<std::string>(NgTokens->exe, "");
    const int spp = std::max(1, delegate->Setting<int>(NgTokens->spp, 16));
    const int firstSpp = delegate->Setting<int>(NgTokens->firstSpp, 0);
    run.seed = delegate->Setting<int>(NgTokens->seed, 0);
    const int maxdepth = delegate->Setting<int>(NgTokens->maxdepth, 12);
    // F12 with Denoise: a gbuffer film, so the server writes albedo and normal beside the picture (out_<gen>_aux.exr, pbrt\patch_aux.py).
    const bool aux = !viewport && delegate->Setting<bool>(NgTokens->aux, false);
    if (viewport && firstSpp > 0 && firstSpp < spp) run.spps = {firstSpp, spp}; else run.spps = {spp};
    if (dirText.empty() || run.exe.empty() || !w || !h) {
        setError("no scene folder, pbrt or render buffer (nanna:sceneDir, nanna:pbrt)");
        return false;
    }
    pool().Warm(run.exe);
    run.dir = std::filesystem::u8path(dirText);
    const auto t0 = std::chrono::steady_clock::now();

    // Camera: USD's view matrix (row vectors, camera looks down -z) to pbrt's camera space (looks down +z): negate column 2.
    GfMatrix4d view = state->GetWorldToViewMatrix();
    for (int r = 0; r < 4; ++r) view[r][2] = -view[r][2];
    const GfMatrix4d proj = state->GetProjectionMatrix();
    const double fov = 2.0 * std::atan(1.0 / (w >= h ? proj[1][1] : proj[0][0])) * 180.0 / 3.14159265358979;
    double lensRadius = 0, focus = 1;
    if (const HdCamera* cam = state->GetCamera()) {
        focus = std::max<double>(cam->GetFocusDistance(), 1e-3);
        // Blender's free camera leaves focusOn false and sends fstop only with Depth of Field on. It scales both (measured: lens 100 mm,
        // f/0.25 arrive as focal 10, fstop 25), so focal / (2 fstop) is gpu.py's lens * 1e-3 / (2 fstop) in scene units: 0.2 here.
        if (delegate->Setting<bool>(NgTokens->dof, true) && cam->GetFStop() > 0) lensRadius = cam->GetFocalLength() / (2.0 * cam->GetFStop());
        if (!viewport || run.gen == 1)
            say("camera " + cam->GetId().GetString() + " focal " + fmt(cam->GetFocalLength()) + " fstop " + fmt(cam->GetFStop()) +
                " focus " + fmt(cam->GetFocusDistance()) + " focusOn " + std::to_string(cam->GetFocusOn()) + " fov " + fmt(fov) + " lens radius " + fmt(lensRadius));
    }
    {
        std::ostringstream c;
        const char tab = '\t';
        c << fmt(fov) << tab << fmt(lensRadius) << tab << fmt(focus);
        for (int r = 0; r < 4; ++r) for (int col = 0; col < 4; ++col) c << tab << fmt(view[r][col]);
        run.camera = c.str();
        run.sceneKey = dirText + "|" + std::to_string(key.geometry) + "|" + std::to_string(w) + "x" + std::to_string(h) + "|" +
                       std::to_string(run.spps.back()) + "|" + std::to_string(run.seed) + "|" + std::to_string(maxdepth) + (aux ? "|aux" : "");
    }
    if (std::abs(proj[2][0]) > 1e-6 || std::abs(proj[2][1]) > 1e-6) say("WARNING camera shift isn't carried");
    if (std::abs(proj[3][3]) > 1e-6) say("WARNING orthographic view drawn as perspective");

    // Geometry, once per scene version and folder: PLYs in g<n>\ and the world body. Paths stay relative to the folder (textures are).
    const std::string bk = dirText + "|" + std::to_string(key.geometry);
    if (bk != bodyKey) {
        const std::string sub = "g" + std::to_string(++geometryCount);
        std::error_code ec;
        std::filesystem::create_directories(run.dir / sub, ec);
        std::ostringstream s, obj;
        const auto blocks = readBlocks(run.dir / "materials.txt");
        int meshes = 0, gray = 0, instanced = 0;
        size_t triangles = 0, instances = 0;
        for (const SdfPath& id : index->GetRprimIds()) {
            const NgMesh* mesh = dynamic_cast<const NgMesh*>(index->GetRprim(id));
            if (!mesh || !mesh->IsVisible() || mesh->positions.empty()) continue;
            std::vector<GfMatrix4d> xfs{mesh->xf};
            if (!mesh->GetInstancerId().IsEmpty()) {
                xfs.clear();
                if (auto* inst = dynamic_cast<NgInstancer*>(index->GetInstancer(mesh->GetInstancerId())))
                    for (const GfMatrix4d& t : inst->Compute(id)) xfs.push_back(mesh->xf * t);
                if (xfs.empty()) continue;
            }
            const std::string ply = sub + "/h" + std::to_string(meshes++) + ".ply";
            std::string block, shapeExtra;
            auto it = blocks.find(mesh->material.GetName());
            if (it == blocks.end()) {
                gray++;
                block = "Material \"diffuse\" \"rgb reflectance\" [ 0.5 0.5 0.5 ]\n";
            } else {
                for (const std::string& line : it->second) {
                    if (line.rfind("shape:", 0) == 0) shapeExtra += " " + line.substr(6);
                    else block += line + "\n";
                }
            }
            const std::string shape = "Shape \"plymesh\" \"string filename\" \"" + ply + "\"" + shapeExtra + "\n";
            // pbrt takes no area lights inside object instances, so an emitter is baked out, as is a mesh used once.
            const bool asObject = xfs.size() > 1 && block.find("AreaLightSource") == std::string::npos;
            triangles += mesh->positions.size() / 3 * xfs.size();
            if (asObject) {
                writePly(run.dir / ply, *mesh, {});
                const std::string name = "o" + std::to_string(meshes);
                obj << "ObjectBegin \"" << name << "\"\n" << block << shape << "ObjectEnd\n";
                for (const GfMatrix4d& t : xfs) {
                    s << "AttributeBegin\nTransform [";
                    for (int r = 0; r < 4; ++r) for (int c = 0; c < 4; ++c) s << ' ' << fmt(t[r][c]);
                    s << " ]\nObjectInstance \"" << name << "\"\nAttributeEnd\n";
                }
                instanced++; instances += xfs.size();
            } else {
                writePly(run.dir / ply, *mesh, xfs);
                s << "AttributeBegin\n" << block << shape << "AttributeEnd\n";
            }
        }
        body = s.str(); objects = obj.str(); bodyKey = bk;
        say("geometry " + sub + ": " + std::to_string(meshes) + " meshes, " + std::to_string(triangles) + " triangles, " + std::to_string(gray) +
            " without a material block, " + std::to_string(instanced) + " instanced (" + std::to_string(instances) + " instances), written in " +
            fmt(std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count()) + " s");
    }
    std::ostringstream s;
    s << "Transform [";
    for (int r = 0; r < 4; ++r) for (int c = 0; c < 4; ++c) s << ' ' << fmt(view[r][c]);
    s << " ]\nCamera \"perspective\" \"float fov\" " << fmt(fov) << " \"float lensradius\" " << fmt(lensRadius)
      << " \"float focaldistance\" " << fmt(focus) << "\n";
    s << "Film \"" << (aux ? "gbuffer" : "rgb") << "\" \"integer xresolution\" " << w << " \"integer yresolution\" " << h << " \"string filename\" \"out.pfm\"\n"
      << "PixelFilter \"gaussian\" \"float xradius\" 1.5 \"float yradius\" 1.5\n"
      << "Sampler \"zsobol\" \"integer pixelsamples\" " << run.spps.back() << "\n"
      << "Integrator \"volpath\" \"integer maxdepth\" " << maxdepth << "\nWorldBegin\n" << objects;
    if (std::filesystem::exists(run.dir / "world.pbrt")) s << "Include \"world.pbrt\"\n";
    s << body;
    run.sceneFile = run.dir / ("scene_" + std::to_string(run.gen) + ".pbrt");
    { std::ofstream f(run.sceneFile); f << s.str(); }
    say("run " + std::to_string(run.gen) + ": " + std::to_string(w) + "x" + std::to_string(h) + ", spp " + std::to_string(run.spps.front()) +
        (run.spps.size() > 1 ? " then " + std::to_string(run.spps.back()) : std::string()) + ", exported in " +
        fmt(std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count()) + " s");
    return true;
}

uint64_t NgPass::HashGeometry()
{
    uint64_t h = 1469598103934665603ull;
    auto mix = [&](const void* data, size_t n) {
        const uint8_t* b = static_cast<const uint8_t*>(data);
        for (size_t i = 0; i < n; ++i) { h ^= b[i]; h *= 1099511628211ull; }
    };
    HdRenderIndex* index = GetRenderIndex();
    for (const SdfPath& id : index->GetRprimIds()) {
        const NgMesh* mesh = dynamic_cast<const NgMesh*>(index->GetRprim(id));
        if (!mesh || !mesh->IsVisible() || mesh->positions.empty()) continue;
        const std::string name = id.GetString() + "|" + mesh->material.GetString();
        mix(name.data(), name.size());
        mix(mesh->xf.data(), sizeof(double) * 16);
        mix(mesh->positions.data(), mesh->positions.size() * sizeof(GfVec3f));
        mix(mesh->normals.data(), mesh->normals.size() * sizeof(GfVec3f));
        mix(mesh->uvs.data(), mesh->uvs.size() * sizeof(GfVec2f));
        if (!mesh->GetInstancerId().IsEmpty())
            if (auto* inst = dynamic_cast<NgInstancer*>(index->GetInstancer(mesh->GetInstancerId())))
                for (const GfMatrix4d& m : inst->Compute(id)) mix(m.data(), sizeof(double) * 16);
    }
    return h;
}

// One frame on a server. Same scene as the server holds: a camera command (BVH, lights, materials, textures kept). Else a scene command.
// Viewport: a snapshot after the first spp, then the full frame, from the same frame. Cancel: "cancel" and return at once, never
// waiting for pbrt and never killing it. A server that died (GPU error) fails this frame only; the next run takes a new one.
// Watchdog (vp_pkg1 looked like a freeze for 45 s): a viewport frame that brings no picture in time is never waited on silently.
// The server is retired (stdin closed: pbrt cancels and exits by itself, nothing is killed) and the frame restarts on another
// server, once; a second miss fails the frame with the reason in the viewport's status. Budgets: a server ready within kStartBudget,
// a scene loaded within kLoadBudget, the first picture within NANNA_GPU_WATCHDOG_S (default 20 s) of sending (or of the load), the
// rest within 30 s + 3x the first picture's time scaled by spp. F12 has no picture before its end, so only the start/load budgets apply.
constexpr double kStartBudget = 120.0, kLoadBudget = 90.0;
double envSeconds(const char* name, double fallback)
{
    const char* v = std::getenv(name);
    const double d = v ? std::atof(v) : 0.0;
    return d > 0 ? d : fallback;
}

void NgPass::Work(Run run)
{
    int stage = 0;
    for (int attempt = 1;; ++attempt) {
        const std::string why = WorkOnce(run, attempt, stage);
        if (why.empty()) return;
        if (attempt >= 2) {
            say("WATCHDOG run " + std::to_string(run.gen) + ": " + why + "; second miss, frame failed");
            Fail(run.gen, "no picture from pbrt (watchdog, twice): " + why);
            return;
        }
        say("WATCHDOG run " + std::to_string(run.gen) + ": " + why + "; server retired, restarting the frame on another server");
    }
}

std::string NgPass::WorkOnce(const Run& run, int attempt, int& stage)
{
    const std::string tag = "run " + std::to_string(run.gen);
    const double t0 = now();
    static const double pictureBudget = envSeconds("NANNA_GPU_WATCHDOG_S", 20.0);
    static const int stallGen = int(envSeconds("NANNA_GPU_TEST_STALL", 0.0)); // test hook: this run's first attempt ignores pbrt's answers
    const bool stall = viewport && attempt == 1 && run.gen == stallGen;
    auto retire = [&](const std::string& why) {
        say(tag + ": retiring server pid " + std::to_string(server->pid) + " (" + why + ")");
        server->rendering = false;
        pool().Retire(server); server.reset();
        return why;
    };
    if (server && !server->Alive()) { say(tag + ": server pid " + std::to_string(server->pid) + " has gone, taking another"); pool().Retire(server); server.reset(); }
    if (!server) server = pool().Take(run.exe);
    if (!server) { Fail(run.gen, "can't start " + run.exe); return ""; }
    while (server->Alive() && !server->Ready()) {
        if (cancel) {
            say(tag + " cancelled while its server starts");
            if (!viewport) { pool().GiveBack(std::move(server)); server.reset(); }
            return "";
        }
        if (now() - t0 > kStartBudget) return retire("server not ready after " + fmt(kStartBudget) + " s");
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    // A scene command cancelled before it loaded: pbrt either loads it anyway (then cancels its frame) or skips it. Its answer
    // decides whether this run can be a camera command; pbrt can't take this run's command any sooner.
    auto has = [](const std::string& text, const std::string& what) {
        for (size_t at = text.find(what); at != std::string::npos; at = text.find(what, at + 1)) {
            const size_t end = at + what.size();
            if (end >= text.size() || text[end] == ' ' || text[end] == '\r' || text[end] == '\n') return true;
        }
        return false;
    };
    while (server->pendingId > 0) {
        const std::string text = server->Output(server->pendingMark), pending = std::to_string(server->pendingId);
        if (has(text, "NANNA_LOADED " + pending)) { server->loadedKey = server->pendingKey; server->pendingId = 0; break; }
        if (has(text, "NANNA_CANCELLED " + pending) || !server->Alive()) { server->pendingId = 0; break; }
        if (cancel) return "";
        if (now() - t0 > kLoadBudget) { server->pendingId = 0; return retire("a cancelled scene neither loaded nor cancelled in " + fmt(kLoadBudget) + " s"); }
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    const double ready = now();
    const size_t mark = server->OutputSize();
    const int id = ++server->nextId;
    const bool cameraOnly = !server->loadedKey.empty() && server->loadedKey == run.sceneKey;
    const std::filesystem::path out = run.dir / ("out_" + std::to_string(run.gen) + ".pfm");
    std::error_code ec; std::filesystem::remove(out, ec);
    const std::string snaps = run.spps.size() > 1 ? std::to_string(run.spps.front()) : "-";
    std::string command;
    if (cameraOnly) command = "camera\t" + std::to_string(id) + "\t" + out.u8string() + "\t" + snaps + "\t" + run.camera + "\n";
    else {
        server->loadedKey.clear(); // until its NANNA_LOADED
        command = "scene\t" + std::to_string(id) + "\t" + std::to_string(run.spps.back()) + "\t" + std::to_string(run.seed) + "\t" + out.u8string() +
                  "\t" + snaps + "\t" + run.sceneFile.u8string() + "\n";
    }
    server->rendering = true;
    server->Send(command);
    const double sentAt = now();
    double loadedAt = 0, firstAt = 0;
    bool done = false, cancelled = false, dead = false;
    size_t scan = mark;
    while (!done && !cancelled) {
        const bool alive = server->Alive(); // read before the output, so a dead server's last lines are in it
        const std::string text = stall ? std::string() : server->Output(scan);
        size_t pos = 0, nl;
        while ((nl = text.find('\n', pos)) != std::string::npos) {
            std::string line = text.substr(pos, nl - pos);
            pos = nl + 1;
            if (!line.empty() && line.back() == '\r') line.pop_back();
            std::istringstream ls(line);
            std::string word; int lineId = -1;
            ls >> word >> lineId;
            if (lineId != id) continue;
            if (word == "NANNA_LOADED") {
                double seconds = 0; ls >> seconds >> server->gpuBytes;
                loadedAt = now(); server->loadedKey = run.sceneKey;
                say(tag + ": scene loaded in " + fmt(seconds) + " s, server GPU allocations " + fmt(server->gpuBytes / 1e9) + " GB");
            } else if (word == "NANNA_PASS" || word == "NANNA_DONE") {
                std::filesystem::path file = out;
                int spp = run.spps.back();
                if (word == "NANNA_PASS") { std::string name; ls >> spp >> std::ws; std::getline(ls, name); file = std::filesystem::u8path(name); }
                else done = true;
                const double seenAt = now();
                std::vector<float> rgb; unsigned rw = 0, rh = 0;
                const bool ok = readPfm(file, rgb, rw, rh) && rw == run.w && rh == run.h;
                std::filesystem::remove(file, ec);
                if (!ok) { done = true; dead = true; break; }
                if (firstAt == 0) firstAt = now();
                {
                    std::lock_guard<std::mutex> l(resultMutex);
                    result.swap(rgb); resultW = rw; resultH = rh; resultGen = run.gen; resultStage = stage++; resultFinal = done;
                }
                delegate->percent = done ? 100.0 : 100.0 * double(spp) / double(run.spps.back());
                say(tag + " (" + (cameraOnly ? "camera" : "scene") + ", pid " + std::to_string(server->pid) + "): " + std::to_string(spp) + " spp at " +
                    fmt(now() - run.changedAt) + " s since the view change (waited " + fmt(ready - t0) + " s for a ready server" +
                    (loadedAt > 0 ? ", loaded " + fmt(loadedAt - ready) + " s after sending" : std::string()) + "; sent at " + fmt(sentAt - run.changedAt) +
                    ", pbrt's line seen " + fmt(seenAt - sentAt) + " s after sending, PFM read in " + fmt(now() - seenAt) + " s)");
            } else if (word == "NANNA_CANCELLED") cancelled = true;
        }
        scan += pos;
        if (done || cancelled) break;
        if (!alive) { dead = true; break; }
        if (!cancel) {
            const double t = now();
            std::string late;
            if (!cameraOnly && loadedAt == 0 && t - sentAt > kLoadBudget) late = "scene not loaded " + fmt(t - sentAt) + " s after sending";
            else if (viewport && firstAt == 0 && t - (loadedAt > 0 ? loadedAt : sentAt) > pictureBudget + (cameraOnly || loadedAt > 0 ? 0.0 : kLoadBudget))
                late = "no picture " + fmt(t - sentAt) + " s after sending";
            else if (viewport && firstAt > 0) {
                const double base = loadedAt > 0 ? loadedAt : sentAt;
                const double rest = 30.0 + 3.0 * (firstAt - base) * double(run.spps.back()) / double(run.spps.front());
                if (t - firstAt > rest) late = "no full picture " + fmt(t - firstAt) + " s after the first (budget " + fmt(rest) + " s)";
            }
            if (!late.empty()) {
                server->Send("cancel\t" + std::to_string(id) + "\n");
                return retire(late + ", pid " + std::to_string(server->pid));
            }
        }
        if (cancel) {
            server->Send("cancel\t" + std::to_string(id) + "\n");
            if (!cameraOnly && loadedAt == 0) { server->pendingId = id; server->pendingKey = run.sceneKey; server->pendingMark = mark; }
            server->rendering = false;
            say(tag + " cancelled after " + fmt(now() - ready) + " s on its server (pid " + std::to_string(server->pid) + "), not waited for");
            if (!viewport) { pool().GiveBack(std::move(server)); server.reset(); }
            return "";
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    server->rendering = false;
    if (dead || (!done && !cancelled)) {
        const std::string all = server->Output(mark);
        // pbrt's last "Error"/"FATAL" line without its colour codes, else the end of its output
        std::string plain, line, found;
        for (size_t i = 0; i < all.size(); ++i) {
            if (all[i] == '\x1b') { while (i < all.size() && all[i] != 'm') ++i; continue; }
            plain += all[i];
        }
        std::istringstream lines(plain);
        while (std::getline(lines, line)) if (line.find("Error") != std::string::npos || line.find("FATAL") != std::string::npos) found = line;
        if (found.empty()) found = plain.size() > 300 ? plain.substr(plain.size() - 300) : plain;
        { std::ofstream log(run.dir / ("pbrt_" + std::to_string(run.gen) + ".log"), std::ios::binary); log << all; }
        const bool alive = server->Alive();
        Fail(run.gen, "pbrt stopped without a picture (" + (alive ? std::string("still running") : "exit " + std::to_string(server->ExitCode())) + "): " + found);
        pool().Retire(std::move(server)); server.reset(); // a live one is told to exit; nothing is killed
        return "";
    }
    if (cancelled) say(tag + " was cancelled by pbrt before it started");
    if (!viewport) { pool().GiveBack(std::move(server)); server.reset(); }
    else if (server->gpuBytes >= kServerBudget) {
        say(tag + ": server pid " + std::to_string(server->pid) + " holds " + fmt(server->gpuBytes / 1e9) + " GB of old scenes: told to exit, a spare takes over");
        pool().Retire(std::move(server)); server.reset();
    }
    return "";
}

void NgPass::Publish() const
{
    std::lock_guard<std::mutex> lock(resultMutex);
    if (resultGen < 0 || resultGen != gen) return;
    if (resultFailed) {
        if (!converged) for (const auto& b : bindings) if (NgBuffer* rb = dynamic_cast<NgBuffer*>(bufferOf(GetRenderIndex(), b))) rb->MarkConverged();
        converged = true;
        return;
    }
    if (resultGen == writtenGen && resultStage == writtenStage) return;
    for (const auto& b : bindings) {
        NgBuffer* rb = dynamic_cast<NgBuffer*>(bufferOf(GetRenderIndex(), b));
        if (rb) rb->Write(result, resultW, resultH, b.aovName == HdAovTokens->color ? 0.0f : 1.0f, resultFinal);
    }
    if (writtenGen != resultGen) say("first picture of run " + std::to_string(resultGen) + " drawn " + fmt(now() - changedAt) + " s after the view change");
    writtenGen = resultGen; writtenStage = resultStage;
    converged = resultFinal;
}

bool NgPass::IsConverged() const
{
    Publish(); // F12 calls Execute once, then this until true: results go into the buffers here, on Blender's render thread
    return converged;
}

} // namespace

// ---------------------------------------------------------------- plugin. Global: plugInfo.json names the type by its TfType name.

class HdNannaGpuRendererPlugin final : public HdRendererPlugin {
public:
    HdRenderDelegate* CreateRenderDelegate() override { return new NgDelegate({}); }
    HdRenderDelegate* CreateRenderDelegate(const HdRenderSettingsMap& settings) override { return new NgDelegate(settings); }
    void DeleteRenderDelegate(HdRenderDelegate* d) override { delete d; }
    bool IsSupported(bool) const override { return true; }
};

TF_REGISTRY_FUNCTION(TfType)
{
    HdRendererPluginRegistry::Define<HdNannaGpuRendererPlugin>();
}

extern "C" __declspec(dllexport) const char* nannaGpuLastError()
{
    static std::string copy;
    std::lock_guard<std::mutex> lock(errorMutex);
    copy = lastError; lastError.clear();
    return copy.c_str();
}

extern "C" __declspec(dllexport) int nannaGpuDelegateLive() { return NgDelegate::live.load(); }

// A spare pbrt starts now (engine update), so the next F12 or viewport run skips the 3 to 25 s start-up.
extern "C" __declspec(dllexport) void nannaGpuWarm(const char* exe) { if (exe && *exe) pool().Warm(exe); }

// Blender's exit (atexit) and the add-on's unregister: spares get their stdin closed, a rendering pbrt is killed.
extern "C" __declspec(dllexport) void nannaGpuShutdown() { pool().Shutdown(); }

extern "C" __declspec(dllexport) const char* nannaGpuPoolInfo()
{
    static std::string copy;
    copy = pool().Info();
    return copy.c_str();
}
