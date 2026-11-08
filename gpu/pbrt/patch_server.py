# Copyright 1998-2020 Matt Pharr, Wenzel Jakob and Greg Humphreys
# Copyright 2026 Kiet Ho
# SPDX-License-Identifier: Apache-2.0
# Modified by Kiet Ho, 2026: holds excerpts of pbrt-v4 (github.com/mmp/pbrt-v4, b0bdf46) and the changed code. See NOTICE.
"""Viewport server on top of patch_pbrt.py + patch_f12.py. Run from the folder that holds pbrt-v4 (a tree those two already patched).

--nanna-serve: one pbrt keeps running and takes commands on stdin, one per line, tab separated:
  scene  ID SPP SEED OUTFILE SNAPS SCENEFILE   parse and build a new scene (BVH, lights, materials, textures), then render it
  camera ID OUTFILE SNAPS FOV LENSRADIUS FOCALDISTANCE M0..M15
                                               same scene, new perspective camera: M is the pbrt "Transform" at the top of the
                                               scene file (camera from world). Film resolution, sampler and spp stay the scene's.
  cancel ID                                    stop every command up to ID, between two sample passes
SNAPS: comma list of spp counts (or "-"); after that many passes the film is written to OUTFILE with "_s<spp>" before the extension.
Replies on stdout: NANNA_READY, NANNA_LOADED id seconds gpubytes, NANNA_PASS id spp file, NANNA_DONE id seconds, NANNA_CANCELLED id.
End of stdin cancels the running frame and pbrt exits by itself (code 0). Errors still end the process, as pbrt always did.
The scene is rendered in world space (--render-coord-sys world), so a camera change needs no new BVH.
Without --nanna-serve nothing changes: the pass hook is empty, so F12 and --nanna-wait render as before.
"""
R = "pbrt-v4/src/pbrt/"


def sub(f, old, new, count=1):
    p = R + f
    t = open(p, encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in t else "\n"
    old, new = old.replace("\n", nl), new.replace("\n", nl)
    assert t.count(old) == 1, (f, old, t.count(old))
    t = t.replace(old, new) if count == 0 else t.replace(old, new, count)
    open(p, "w", encoding="utf-8", newline="").write(t)


# integrator.cpp: a hook after each sample pass (server: snapshots and cancel). Empty unless --nanna-serve set it.
sub("wavefront/integrator.cpp", """// WavefrontPathIntegrator Method Definitions
Float WavefrontPathIntegrator::Render() {""", """// NANNA: called after each sample pass with the passes done; true stops the frame. Set only by NannaServe (patch_server.py).
std::function<bool(int)> NannaPassHook;

// WavefrontPathIntegrator Method Definitions
Float WavefrontPathIntegrator::Render() {""")
sub("wavefront/integrator.cpp", """            progress.Update();
        }
""", """            progress.Update();
            if (NannaPassHook && NannaPassHook(sampleIndex + 1))  // NANNA: server snapshot / cancel
                break;
        }
""")

sub("wavefront/wavefront.cpp", """#include <pbrt/wavefront/integrator.h>
""", """#include <pbrt/wavefront/integrator.h>

#include <pbrt/cameras.h>  // NANNA: server mode, see patch_server.py
#include <pbrt/film.h>
#include <pbrt/parser.h>
#include <pbrt/gpu/util.h>
#include <pbrt/options.h>
#include <pbrt/util/image.h>
#include <pbrt/util/progressreporter.h>
#include <pbrt/util/string.h>
#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <deque>
#include <functional>
#include <iostream>
#include <mutex>
#include <thread>
""")
sub("wavefront/wavefront.cpp", """}  // namespace pbrt
""", """extern std::function<bool(int)> NannaPassHook;  // NANNA: wavefront/integrator.cpp

namespace {
std::vector<std::string> NannaFields(const std::string &line) {
    std::vector<std::string> f;
    for (size_t start = 0, tab;; start = tab + 1) {
        tab = line.find('\\t', start);
        f.push_back(line.substr(start, tab == std::string::npos ? std::string::npos : tab - start));
        if (tab == std::string::npos)
            break;
    }
    return f;
}

// Lines from stdin, read on their own thread so that "cancel" reaches a frame while it renders.
struct NannaInput {
    std::mutex m;
    std::condition_variable cv;
    std::deque<std::string> queue;
    bool eof = false;
    std::atomic<int> cancelUpTo{-1};
    std::atomic<bool> closing{false};
};
}  // namespace

// NANNA: --nanna-serve (patch_server.py). One process, many frames: see the command list there.
int NannaServe() {
    static NannaInput *in = new NannaInput;  // the reader may outlive this function
    std::thread([]() {
        std::string line;
        while (std::getline(std::cin, line)) {
            if (!line.empty() && line.back() == '\\r')
                line.pop_back();
            if (line.rfind("cancel\\t", 0) == 0) {
                int id = atoi(line.c_str() + 7), cur = in->cancelUpTo.load();
                while (id > cur && !in->cancelUpTo.compare_exchange_weak(cur, id)) {
                }
                continue;
            }
            std::lock_guard<std::mutex> l(in->m);
            in->queue.push_back(line);
            in->cv.notify_one();
        }
        std::lock_guard<std::mutex> l(in->m);
        in->eof = true;
        in->closing = true;
        in->cv.notify_one();
    }).detach();

    Options->renderingSpace = RenderingCoordinateSystem::World;
    Options->quiet = true;
    WavefrontPathIntegrator *integrator = nullptr;
    bool filmUsed = false;
    printf("NANNA_READY\\n");
    fflush(stdout);
    while (true) {
        std::string line;
        {
            std::unique_lock<std::mutex> l(in->m);
            in->cv.wait(l, []() { return !in->queue.empty() || in->eof; });
            if (in->queue.empty())
                break;
            line = in->queue.front();
            in->queue.pop_front();
        }
        std::vector<std::string> f = NannaFields(line);
        if (f.size() < 2)
            ErrorExit("--nanna-serve: bad command \\"%s\\"", line);
        int id = atoi(f[1].c_str());
        if (id <= in->cancelUpTo.load() || in->closing) {
            printf("NANNA_CANCELLED %d\\n", id);
            fflush(stdout);
            continue;
        }
        Timer timer;
        std::string outFile, snapText;
        if (f[0] == "scene") {
            if (f.size() != 7)
                ErrorExit("--nanna-serve: scene wants id spp seed outfile snaps scenefile: \\"%s\\"", line);
            Options->pixelSamples = atoi(f[2].c_str());
            Options->seed = atoi(f[3].c_str());
            Options->imageFile = f[4];
            CopyOptionsToGPU();
            outFile = f[4];
            snapText = f[5];
            // The previous scene's objects are left as they are (pbrt has no teardown for them); the delegate
            // starts a new process once NANNA_LOADED reports too much GPU memory.
            BasicScene *scene = new BasicScene;
            BasicSceneBuilder builder(scene);
            ParseFiles(&builder, {f[6]});
            integrator = new WavefrontPathIntegrator(&CUDATrackedMemoryResource::singleton, *scene);
            filmUsed = false;
            printf("NANNA_LOADED %d %.3f %zu\\n", id, timer.ElapsedSeconds(),
                   CUDATrackedMemoryResource::singleton.BytesAllocated());
            fflush(stdout);
        } else if (f[0] == "camera") {
            if (f.size() != 7 + 16)
                ErrorExit("--nanna-serve: camera wants id outfile snaps fov lensradius focaldistance and 16 numbers: \\"%s\\"", line);
            if (!integrator)
                ErrorExit("--nanna-serve: camera before any scene");
            outFile = f[2];
            snapText = f[3];
            Float tr[16];
            for (int i = 0; i < 16; ++i)
                tr[i] = Float(atof(f[7 + i].c_str()));
            pbrt::Transform cameraFromWorld = Transpose(pbrt::Transform(SquareMatrix<4>(pstd::MakeSpan(tr, 16))));
            pbrt::Transform worldFromCamera = Inverse(cameraFromWorld);
            CameraBaseParameters base;
            base.cameraTransform = CameraTransform(AnimatedTransform(worldFromCamera, 0.f, worldFromCamera, 1.f));
            base.film = integrator->film;
            base.medium = nullptr;
            Point2i full = integrator->film.FullResolution();
            Float frame = Float(full.x) / Float(full.y);
            Bounds2f screen = frame > 1.f ? Bounds2f(Point2f(-frame, -1.f), Point2f(frame, 1.f))
                                          : Bounds2f(Point2f(-1.f, -1.f / frame), Point2f(1.f, 1.f / frame));
            Allocator alloc(&CUDATrackedMemoryResource::singleton);
            integrator->camera = alloc.new_object<PerspectiveCamera>(
                base, Float(atof(f[4].c_str())), screen, Float(atof(f[5].c_str())), Float(atof(f[6].c_str())));
        } else
            ErrorExit("--nanna-serve: unknown command \\"%s\\"", line);

        Film film = integrator->film;
        Bounds2i pixelBounds = film.PixelBounds();
        Vector2i resolution = pixelBounds.Diagonal();
        if (filmUsed)
            GPUParallelFor("Nanna reset pixels", resolution.x * resolution.y, [=] PBRT_CPU_GPU(int i) mutable {
                film.ResetPixel(pixelBounds.pMin + Vector2i(i % resolution.x, i / resolution.x));
            });
        GPUWait();  // Windows: the host reads the camera (managed memory) as Render starts, so no kernel may still run
        filmUsed = true;
        std::vector<int> snaps;
        if (snapText != "-" && !snapText.empty())
            for (const std::string &s : SplitString(snapText, ','))
                snaps.push_back(atoi(s.c_str()));
        auto write = [&](int spp, const std::string &name) {
            GPUWait();
            ImageMetadata metadata;
            metadata.samplesPerPixel = spp;
            Image image = integrator->film.GetImage(&metadata);
            image.Write(name, metadata);
        };
        const int spp = integrator->samplesPerPixel;
        bool cancelled = false;
        NannaPassHook = [&](int done) {
            if (id <= in->cancelUpTo.load() || in->closing) {
                cancelled = true;
                return true;
            }
            if (done < spp && std::find(snaps.begin(), snaps.end(), done) != snaps.end()) {
                size_t dot = outFile.rfind('.');
                std::string name = outFile.substr(0, dot) + "_s" + std::to_string(done) +
                                   (dot == std::string::npos ? std::string() : outFile.substr(dot));
                write(done, name);
                printf("NANNA_PASS %d %d %s\\n", id, done, name.c_str());
                fflush(stdout);
            }
            return false;
        };
        Float seconds = integrator->Render();
        NannaPassHook = nullptr;
        if (cancelled) {
            printf("NANNA_CANCELLED %d\\n", id);
            fflush(stdout);
            continue;
        }
        write(spp, outFile);
        printf("NANNA_DONE %d %.3f %.3f\\n", id, seconds, timer.ElapsedSeconds());
        fflush(stdout);
    }
    return 0;
}

}  // namespace pbrt
""")

sub("cmd/pbrt.cpp", "int main(int argc, char *argv[]) {\n",
    "namespace pbrt {\nint NannaServe();  // NANNA: wavefront/wavefront.cpp, see patch_server.py\n}\n\nint main(int argc, char *argv[]) {\n")
sub("cmd/pbrt.cpp", "    bool nannaWait = false;  // NANNA\n", "    bool nannaWait = false;  // NANNA\n    bool nannaServe = false;  // NANNA: patch_server.py\n")
sub("cmd/pbrt.cpp", """            ParseArg(&iter, args.end(), "nanna-wait", &nannaWait, onError)) {  // NANNA""",
    """            ParseArg(&iter, args.end(), "nanna-wait", &nannaWait, onError) ||
            ParseArg(&iter, args.end(), "nanna-serve", &nannaServe, onError)) {  // NANNA""")
sub("cmd/pbrt.cpp", """    } else {
        if (nannaWait) {  // NANNA: warm and waiting, see patch_f12.py""", """    } else {
#ifdef PBRT_BUILD_GPU_RENDERER
        if (nannaServe && Options->useGPU)  // NANNA: patch_server.py
            return NannaServe();
#endif
        if (nannaWait) {  // NANNA: warm and waiting, see patch_f12.py""")
print("patched server")
