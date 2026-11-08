# Copyright 2023-2024 DreamWorks Animation LLC
# Copyright 2026 Kiet Ho
# SPDX-License-Identifier: Apache-2.0
# Modified by Kiet Ho, 2026: holds excerpts of hdMoonray (github.com/mnraker/hdMoonray, a71790f) as *_OLD strings
# and the changed code that replaces them as *_NEW strings. See NOTICE.
"""Copy the hdMoonray clone to hdMoonray_src beside this script and apply the source edits an MSVC build needs. The clone isn't touched.

Each edit must match exactly once, or the script stops.

Usage: python -I edit_hdmoonray.py <hdMoonray clone> <copy>
"""
import os
import shutil
import sys

# Blender gets the vertical shift and Render Region right without MoonRay's aspect factor, shift_y overshot Cycles by 16:9 with it.
CAMERA_OFFSET_OLD = """        double adjustedAr = adjustedAperture[0]/apertureHeight;
        vertOffset *= adjustedAr;
"""
CAMERA_OFFSET_NEW = """        double adjustedAr = adjustedAperture[0]/apertureHeight;
        if (!std::getenv("HDMOONRAY_BLENDER_UNITS")) {
            vertOffset *= adjustedAr;
        }
"""

# Normalize off gives MoonRay an unnormalized light with Blender's watts, which is close to no light. Stay normalized, the add-on warns.
LIGHT_NORMALIZED_OLD = """        if (attrName == "on" || attrName == "node_xform" || attrName == "intensity") continue;
"""
LIGHT_NORMALIZED_NEW = """        if (attrName == "on" || attrName == "node_xform" || attrName == "intensity") continue;
        if (attrName == "normalized" && isBlender()) continue;
"""

# Blender makes an SdfPath from the AOV name, so a name with a colon crashes it. This one is the shader:albedo material AOV, for the denoiser.
AOV_ALBEDO_OLD = """    const std::string s(token.GetString());
    for (size_t i = 0; i < s.size(); ++i) {
        if (s[i] == ':') {
"""
AOV_ALBEDO_NEW = """    const std::string s(token.GetString());
    if (s == "blenderAlbedo") {
        prefix = pxr::HdAovTokens->shader;
        suffix = pxr::TfToken("albedo");
        return;
    }
    if (s == "blenderDiffuse") {
        prefix = pxr::HdAovTokens->shader;
        suffix = pxr::TfToken("D.albedo");
        return;
    }
    for (size_t i = 0; i < s.size(); ++i) {
        if (s[i] == ':') {
"""

# A dump of the scene carries this name, and moonray.exe on that dump wrote to \tmp on the render's drive on Windows. Under Blender: the temp folder.
OUTPUT_NAME_OLD = """            ro.setFileName("/tmp/scene.exr");
"""
OUTPUT_NAME_NEW = """            const char* temp = std::getenv("HDMOONRAY_BLENDER_UNITS") ? std::getenv("TEMP") : nullptr;
            ro.setFileName(temp ? std::string(temp) + "/scene.exr" : std::string("/tmp/scene.exr"));
"""

ATOMIC_OLD = "if (NOT IsDarwinPlatform)\n    set(PlatformSpecificLibs atomic)"
ATOMIC_NEW = "if (NOT IsDarwinPlatform AND NOT MSVC)  # MSVC has no atomic library\n    set(PlatformSpecificLibs atomic)"

VECTOR_OLD = """            typedef float v4sf __attribute__ ((vector_size (16)));
            v4sf* buffer = reinterpret_cast<v4sf*>(mPixelData.mData);
            if (clearValue[0] || clearValue[1] || clearValue[2] || clearValue[3] < 1.0f) {
                const v4sf& cv = reinterpret_cast<const v4sf&>(clearValue[0]);
                pxr::WorkParallelForN(count, [buffer, cv](size_t begin, size_t end) {
                    for (size_t i = begin; i < end; ++i) {
                        v4sf& pixel = *(buffer + i);
                        if (pixel[3] < 1.0f) {
                            if (pixel[3] > 0.0f) {
                                const float m = 1.0f - pixel[3];
                                pixel += cv * m;
                            } else {
                                pixel += cv;
                            }
                        }
                    }
                });
            } else {
                // composite is simpler when clearValue is opaque black
                pxr::WorkParallelForN(count, [buffer](size_t begin, size_t end) {
                    for (size_t i = begin; i < end; ++i) {
                        v4sf& pixel = *(buffer + i);
                        pixel[3] = 1.0f;
                    }
                });
            }
"""
VECTOR_NEW = """            struct Rgba { float c[4]; };  // GCC's vector_size extension doesn't exist in MSVC
            Rgba* buffer = reinterpret_cast<Rgba*>(mPixelData.mData);
            if (clearValue[0] || clearValue[1] || clearValue[2] || clearValue[3] < 1.0f) {
                const Rgba cv = {{clearValue[0], clearValue[1], clearValue[2], clearValue[3]}};
                pxr::WorkParallelForN(count, [buffer, cv](size_t begin, size_t end) {
                    for (size_t i = begin; i < end; ++i) {
                        Rgba& pixel = buffer[i];
                        if (pixel.c[3] < 1.0f) {
                            const float m = pixel.c[3] > 0.0f ? 1.0f - pixel.c[3] : 1.0f;
                            for (int j = 0; j < 4; ++j) {
                                pixel.c[j] += cv.c[j] * m;
                            }
                        }
                    }
                });
            } else {
                // composite is simpler when clearValue is opaque black
                pxr::WorkParallelForN(count, [buffer](size_t begin, size_t end) {
                    for (size_t i = begin; i < end; ++i) {
                        buffer[i].c[3] = 1.0f;
                    }
                });
            }
"""

PLUGINFO_OLD = "set(LIBRARY_PATH ../../${component}${CMAKE_SHARED_LIBRARY_SUFFIX})"
PLUGINFO_NEW = "set(LIBRARY_PATH ../../../bin/${component}${CMAKE_SHARED_LIBRARY_SUFFIX})  # Windows puts the DLL in bin"

# Blender's scene delegate has no lightLink or shadowLink param, and Get<TfToken>() on an empty VtValue is a coding error.
LIGHTLINK_OLD = "sceneDelegate->GetLightParamValue(id, pxr::HdTokens->lightLink).Get<pxr::TfToken>();"
LIGHTLINK_NEW = "sceneDelegate->GetLightParamValue(id, pxr::HdTokens->lightLink).GetWithDefault<pxr::TfToken>();"
SHADOWLINK_OLD = "t = sceneDelegate->GetLightParamValue(id, pxr::HdTokens->shadowLink).Get<pxr::TfToken>();"
SHADOWLINK_NEW = "t = sceneDelegate->GetLightParamValue(id, pxr::HdTokens->shadowLink).GetWithDefault<pxr::TfToken>();"

# MoonRay's ImageDistribution drops a 1x1 mip level, so a 1x1 file has no level left and the frame fails. Blender writes every
# plain-colour world as a 1x1 EXR. Such a texture is the same light as its colour with no texture, so hand MoonRay that.
LIGHT_INCLUDE_OLD = "#include <pxr/imaging/hd/sceneDelegate.h>\n#include <pxr/base/gf/matrix4d.h>\n"
LIGHT_INCLUDE_NEW = LIGHT_INCLUDE_OLD + "#include <pxr/imaging/hio/image.h>\n"
LIGHT_HELPER_OLD = "    return it->second;\n}\n\n}\n\nnamespace hdMoonray {\n"
LIGHT_HELPER_NEW = """    return it->second;
}

bool
isBlender()
{
    static const bool blender = std::getenv("HDMOONRAY_BLENDER_UNITS") != nullptr;
    return blender;
}

// Blender sends light energy as it stands, and these lights come out far from Cycles. MoonRay / Cycles ratios measured on one
// grey ground: 3138 to 3153 for point, spot and area, so 1000 pi, and 0.7735 for sun. The add-on opts in.
float
blenderUnitScale(const pxr::TfToken& type)
{
    if (!isBlender() || type == pxr::HdPrimTypeTokens->domeLight) return 1.0f;
    if (type == pxr::HdPrimTypeTokens->distantLight) return 1.0f / 0.7735f;
    return 1.0f / (1000.0f * scene_rdl2::math::sPi);
}

// MoonRay can't build a light distribution from a 1x1 image. True, with the texel, if the file is one.
bool
readConstantTexture(const std::string& file, Rgb& texel)
{
    if (file.empty()) return false;
    pxr::HioImageSharedPtr image =
        pxr::HioImage::OpenForReading(file, 0, 0, pxr::HioImage::SourceColorSpace::Raw, /* suppressErrors */ true);
    if (!image || image->GetWidth() != 1 || image->GetHeight() != 1) return false;
    float rgba[4] = {0.0f, 0.0f, 0.0f, 1.0f};
    pxr::HioImage::StorageSpec storage;
    storage.width = storage.height = storage.depth = 1;
    storage.format = pxr::HioFormatFloat32Vec4;
    storage.data = rgba;
    if (!image->Read(storage)) return false;
    texel = Rgb(rgba[0], rgba[1], rgba[2]);
    return true;
}

}

namespace hdMoonray {
"""
LIGHT_FLATTEN_OLD = """        // no setting, so reset to default
        ValueConverter::setDefault(mLight, *it);
    }
}
"""
LIGHT_FLATTEN_NEW = """        // no setting, so reset to default
        ValueConverter::setDefault(mLight, *it);
    }

    // After the loop, because the attribute order isn't known: this needs both color and texture set.
    SceneClass& writableClass = const_cast<SceneClass&>(sceneClass); // hasAttribute() only reads, but isn't const
    if (writableClass.hasAttribute("texture") && writableClass.hasAttribute("color")) {
        const AttributeKey<String> textureKey = sceneClass.getAttributeKey<String>("texture");
        const AttributeKey<Rgb> colorKey = sceneClass.getAttributeKey<Rgb>("color");
        Rgb texel;
        if (readConstantTexture(mLight->get(textureKey), texel)) {
            mLight->set(colorKey, mLight->get(colorKey) * texel);
            mLight->set(textureKey, String());
        }
    }

    // The world is Blender's background, and MoonRay hides lights from the camera by default.
    if (isBlender() && mType == pxr::HdPrimTypeTokens->domeLight && writableClass.hasAttribute("visible_in_camera")) {
        static const pxr::TfToken filmTransparent("blender:filmTransparent");
        const bool hidden = renderDelegate.GetRenderSetting(filmTransparent).GetWithDefault<bool>(false);
        mLight->set(sceneClass.getAttributeKey<Int>("visible_in_camera"), hidden ? 0 : 1);
    }

    // Blender folds a plain world's strength into its colour, but drops it for an image, so the add-on sends it. 1.2 measured 0.836.
    if (isBlender() && mType == pxr::HdPrimTypeTokens->domeLight && writableClass.hasAttribute("texture") &&
        !mLight->get(sceneClass.getAttributeKey<String>("texture")).empty()) {
        static const pxr::TfToken worldStrength("blender:worldStrength");
        const pxr::VtValue strength = pxr::VtValue::Cast<float>(renderDelegate.GetRenderSetting(worldStrength));
        if (strength.IsHolding<float>()) {
            mLight->set(scene_rdl2::rdl2::Light::sIntensityKey, strength.UncheckedGet<float>());
        }
    }

    // MoonRay's default is nearest neighbour, which shows a world image's pixels in the background. Cycles interpolates.
    if (isBlender() && writableClass.hasAttribute("texture_filter")) {
        mLight->set(sceneClass.getAttributeKey<Int>("texture_filter"), 1); // bilinear
    }

    // A light of size zero has no emitting area and MoonRay renders it black. Blender allows it, and Cycles renders it.
    for (const char* name : {"radius", "lens_radius", "angular_extent"}) {
        if (writableClass.hasAttribute(name)) {
            const AttributeKey<Float> key = sceneClass.getAttributeKey<Float>(name);
            if (!(mLight->get(key) > 0.0f)) mLight->set(key, 0.001f);
        }
    }
}
"""

LIGHT_UNITS_OLD = "        mLight->set(scene_rdl2::rdl2::Light::sIntensityKey, intensity);\n"
# Measured on axis, MoonRay's SpotLight is Blender's spot times 4, and off axis times cos(angle) more, because its lens emits
# like a surface. cos can't be removed, so divide by its mean over the cone: the same total light, a little high on the axis, a
# little low at the edge.
LIGHT_UNITS_NEW = """        float unitScale = blenderUnitScale(mType);
        if (isBlender() && mLight->getSceneClass().getName() == "SpotLight") {
            pxr::VtValue cone = shapingParam(sceneDelegate, id, pxr::HdLightTokens->shapingConeAngle);
            const float halfAngle = cone.IsHolding<float>() ? cone.UncheckedGet<float>() : 90.0f;
            unitScale /= 2.0f * (1.0f + scene_rdl2::math::cos(scene_rdl2::math::deg2rad(halfAngle)));
        }
        mLight->set(scene_rdl2::rdl2::Light::sIntensityKey, intensity * unitScale);
"""

# Blender names the cone inputs:shaping:cone:*, the USD attribute, not shaping:cone:* as Hd does, so spot lights lost their cone.
SHAPING_HELPER_OLD = "pxr::HdDirtyBits\nLight::GetInitialDirtyBitsMask() const\n"
SHAPING_HELPER_NEW = """static pxr::VtValue
shapingParam(pxr::HdSceneDelegate* sceneDelegate, const pxr::SdfPath& id, const pxr::TfToken& name)
{
    pxr::VtValue val = sceneDelegate->GetLightParamValue(id, name);
    return val.IsEmpty() ? sceneDelegate->GetLightParamValue(id, pxr::TfToken("inputs:" + name.GetString())) : val;
}

""" + SHAPING_HELPER_OLD
SHAPING_CLASS_OLD = """    v = sceneDelegate->GetLightParamValue(id, pxr::HdLightTokens->shapingConeAngle);
    if (v.IsHolding<float>() && v.UncheckedGet<float>() < 90.0f) {
        if (mType != pxr::HdPrimTypeTokens->diskLight) {
"""
SHAPING_CLASS_NEW = """    v = shapingParam(sceneDelegate, id, pxr::HdLightTokens->shapingConeAngle);
    if (v.IsHolding<float>() && v.UncheckedGet<float>() < 90.0f) {
        if (mType != pxr::HdPrimTypeTokens->diskLight && mType != pxr::HdPrimTypeTokens->sphereLight) {
"""
SHAPING_ANGLE_OLD = """                float coneAngle = 90; // Lux default value
                val = sceneDelegate->GetLightParamValue(id, pxr::HdLightTokens->shapingConeAngle);
                if (val.IsHolding<float>()) coneAngle = val.UncheckedGet<float>();
                mLight->set(AttributeKey<float>(**it), 2 * coneAngle);
"""
SHAPING_ANGLE_NEW = """                float coneAngle = 90; // Lux default value
                val = shapingParam(sceneDelegate, id, pxr::HdLightTokens->shapingConeAngle);
                if (val.IsHolding<float>()) coneAngle = val.UncheckedGet<float>();
                mLight->set(AttributeKey<float>(**it), 2 * coneAngle);
"""
SHAPING_SOFT_OLD = """                val = sceneDelegate->GetLightParamValue(id, luxName);
                if (val.IsHolding<float>()) softness = val.UncheckedGet<float>();
                float coneAngle = 90; // Lux default value
                val = sceneDelegate->GetLightParamValue(id, pxr::HdLightTokens->shapingConeAngle);
                if (val.IsHolding<float>()) coneAngle = val.UncheckedGet<float>();
                float innerConeAngle = coneAngle;
                if (softness > 0) {
                    innerConeAngle = (softness < 1) ? coneAngle * (1 - softness) : 0.0f;
                }
"""
SHAPING_SOFT_NEW = """                val = shapingParam(sceneDelegate, id, luxName);
                if (val.IsHolding<float>()) softness = val.UncheckedGet<float>();
                float coneAngle = 90; // Lux default value
                val = shapingParam(sceneDelegate, id, pxr::HdLightTokens->shapingConeAngle);
                if (val.IsHolding<float>()) coneAngle = val.UncheckedGet<float>();
                float innerConeAngle = coneAngle;
                if (softness > 0) {
                    innerConeAngle = (softness < 1) ? coneAngle * (1 - softness) : 0.0f;
                }
                if (isBlender() && softness > 0) {
                    // Cycles blends over a fraction of the cone's cosine range, not of its angle.
                    const float cosOuter = scene_rdl2::math::cos(scene_rdl2::math::deg2rad(coneAngle));
                    const float cosInner = cosOuter + (1.0f - cosOuter) * scene_rdl2::math::min(softness, 1.0f);
                    innerConeAngle = scene_rdl2::math::rad2deg(scene_rdl2::math::acos(cosInner));
                }
"""

# Blender waits on IsConverged() and nothing else, so a frame that can't finish has to report complete, with the reason kept.
RENDERER_FAIL_OLD = "    /// True when converged\n    virtual bool isFrameComplete() const=0;\n"
RENDERER_FAIL_NEW = RENDERER_FAIL_OLD + """
    /// The frame can't finish: isFrameComplete() must return true from now on, so the host stops waiting.
    virtual void fail(const std::string& message) const {}
"""
PASS_CAMERA_OLD = """        Logger::error("RenderPassState without camera is unsupported");
"""
PASS_CAMERA_NEW = PASS_CAMERA_OLD + """        renderDelegate.renderer().fail("RenderPassState without camera is unsupported");
"""
RR_H_FAILURE_OLD = "    bool mFailure = false;\n"
RR_H_FAILURE_NEW = "    mutable bool mFailure = false;\n"
RR_H_FAIL_OLD = "    bool isFrameComplete() const override;\n"
RR_H_FAIL_NEW = RR_H_FAIL_OLD + "    void fail(const std::string& message) const override;\n"
RR_H_STOP_OLD = "    void stopFrame() const;\n"
RR_H_STOP_NEW = RR_H_STOP_OLD + "    friend void shutdownDriver();\n"
RR_H_DECL_OLD = "class RndrRenderer : public Renderer\n{\n"
RR_H_DECL_NEW = "void shutdownDriver();\n\n" + RR_H_DECL_OLD
RR_STATE_OLD = "using scene_rdl2::logging::Logger;\n\nRndrRenderer::RndrRenderer(uint32_t numThreads)\n"
RR_STATE_NEW = """using scene_rdl2::logging::Logger;

namespace {

std::mutex liveMutex; // guards liveRenderers and lastError
std::set<RndrRenderer*> liveRenderers;
std::string lastError;

}

extern "C" int Py_AtExit(void (*)(void));

// ~RenderDriver aborts if it first runs from the DLL's global destructors: the process has already killed the render
// threads by then. Py_FinalizeEx is the last moment Blender can guarantee them alive, and it runs on every normal exit.
void
shutdownDriver()
{
    std::set<RndrRenderer*> renderers;
    {
        std::lock_guard<std::mutex> guard(liveMutex);
        renderers = liveRenderers;
    }
    for (RndrRenderer* renderer : renderers) {
        std::lock_guard<std::mutex> guard(renderer->mMutex);
        renderer->stopFrame();
    }
    moonray::rndr::cleanUpGlobalDriver();
}

RndrRenderer::RndrRenderer(uint32_t numThreads)
"""
RR_CTOR_OLD = "    std::call_once(onceFlag, [this](){ moonray::rndr::initGlobalDriver(*mRenderOptions); });\n"
RR_CTOR_NEW = """    std::call_once(onceFlag, [this](){
        moonray::rndr::initGlobalDriver(*mRenderOptions);
        Py_AtExit([]() { shutdownDriver(); });
    });
"""
RR_CTOR2_OLD = "    mSceneContext = &mRenderContext->getSceneContext();\n}\n"
RR_CTOR2_NEW = """    mSceneContext = &mRenderContext->getSceneContext();

    std::lock_guard<std::mutex> guard(liveMutex);
    liveRenderers.insert(this);
    lastError.clear();
}
"""
RR_DTOR_OLD = "    invalidateAllTextureResources(); // cause textures to reload if run again\n}\n"
RR_DTOR_NEW = """    {
        std::lock_guard<std::mutex> guard(liveMutex);
        liveRenderers.erase(this);
    }
    if (mFailure) {
        // After startFrame() throws, deleting the context leaves stale MoonRay state and the next frame crashes in
        // Material::allocShadeQueues (3 of 3 runs). Keeping it costs one scene's memory per failed frame.
        mRenderContext.release();
        return;
    }
    {
        std::lock_guard<std::mutex> guard(mMutex);
        stopFrame(); // Blender deletes the delegate on a cancelled render with the frame still running
    }
    invalidateAllTextureResources(); // cause textures to reload if run again
}
"""
RR_START_OLD = """            mRenderContext->setSceneUpdated();
            if (!mRenderContext->isInitialized()) {
                std::stringstream initmessages; // dummy
                mRenderContext->initialize(initmessages);
            }
            try {
"""
RR_START_NEW = """            mRenderContext->setSceneUpdated();
            try {
                if (!mRenderContext->isInitialized()) {
                    std::stringstream initmessages; // dummy
                    mRenderContext->initialize(initmessages);
                }
                // The scene as MoonRay gets it, the one way to see what Hydra handed over.
                if (const char* dump = std::getenv("HDMOONRAY_WRITE_RDLA")) {
                    scene_rdl2::rdl2::writeSceneToFile(*mSceneContext, dump);
                }
"""
RR_CATCH_OLD = """                mFailure = true;
                Logger::error(e.what());
            }
"""
RR_CATCH_NEW = """                fail(e.what());
            }
"""
RR_COMPLETE_OLD = """    setPrevRo = false; // HDM-183: detect Houdini is not "stuck"
    if (!mFrameComplete && !mUpdateActive) {
        std::lock_guard<std::mutex> guard(mMutex);
        if (!mUpdateActive && mRenderContext->isFrameComplete()) {
            mFrameComplete = true;
            stopFrame();
        }
    }
"""
RR_COMPLETE_NEW = """    setPrevRo = false; // HDM-183: detect Houdini is not "stuck"
    if (mFailure) return true; // the host's wait must end, see fail()
    if (!mFrameComplete && !mUpdateActive) {
        std::lock_guard<std::mutex> guard(mMutex);
        if (!mUpdateActive && mRenderContext->isFrameComplete()) {
            mFrameComplete = true;
            stopFrame();
        } else if (!mUpdateActive && !mPaused && !mRenderContext->isFrameRendering()) {
            fail("MoonRay stopped before the frame was complete");
            return true;
        }
    }
"""
RR_FAIL_OLD = "// This is not thread safe, caller must hold mMutex\nvoid\nRndrRenderer::stopFrame() const\n"
RR_FAIL_NEW = """void
RndrRenderer::fail(const std::string& message) const
{
    Logger::error(message);
    mFailure = true;
    std::lock_guard<std::mutex> guard(liveMutex);
    if (lastError.empty()) lastError = message;
}

""" + RR_FAIL_OLD
RR_INCLUDE_OLD = "#include <iostream>\n\n//#define DEBUG_MSG\n\nnamespace hdMoonray {\n"
RR_INCLUDE_NEW = "#include <iostream>\n#include <mutex>\n#include <set>\n\n//#define DEBUG_MSG\n\nnamespace hdMoonray {\n"
RR_EXPORT_OLD = "    return mFrameComplete;\n}\n\n}\n"
RR_EXPORT_NEW = """    return mFrameComplete;
}

// For the add-on, through ctypes: the reason the last frame failed, or an empty string.
extern "C" const char*
hdMoonrayLastError()
{
    static std::string copy;
    std::lock_guard<std::mutex> guard(liveMutex);
    copy = lastError;
    return copy.c_str();
}

}
"""

# Blender's clear value is always transparent, and MoonRay's background has alpha 0 even with the world in view.
CLEAR_OLD = """            mPixelData.filmActivity = ~0; // make it recomposite even if render has finished
        }
    }
"""
CLEAR_NEW = CLEAR_OLD + """
    static const pxr::TfToken filmTransparent("blender:filmTransparent");
    const pxr::VtValue film = mRenderDelegate->GetRenderSetting(filmTransparent);
    if (film.IsHolding<bool>() && !film.UncheckedGet<bool>()) clearValue[3] = 1.0f;
"""

# MoonRay's texture shaders only sample tiled, mip-mapped files. The add-on converts Blender's images at render time and hands
# over a manifest, one "original<TAB>tiled" per line. A file that isn't in it is reported, not left to fail silently.
MATERIAL_INCLUDE_OLD = """#include <scene_rdl2/render/logging/logging.h>
#include <iostream>
"""
MATERIAL_INCLUDE_NEW = """#include <scene_rdl2/render/logging/logging.h>
#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <unordered_map>
#include <vector>
"""
MATERIAL_HELPER_OLD = """SceneObject*
getCoordSysBinding(
"""
MATERIAL_HELPER_NEW = """namespace {

std::mutex textureMutex; // guards everything in this block
std::string textureManifestFile;
std::unordered_map<std::string, std::string> textureManifest;
struct Refractive { float transmission, alpha; };
std::unordered_map<std::string, Refractive> materialManifest;
std::set<std::string> unmappedTextures;
std::string unmappedCopy;

// Windows paths don't care about case. ASCII case only, so UTF-8 names survive.
std::string
texturePathKey(std::string path)
{
    std::replace(path.begin(), path.end(), '/', '\\\\');
    for (char& c : path) {
        if (c >= 'A' && c <= 'Z') c = char(c + ('a' - 'A'));
    }
    return path;
}

// Reads the add-on's manifest when the setting names a new one. Lines are "original<TAB>tiled", or for a material with Principled
// transmission, "material<TAB>M_<pointer><TAB>transmission<TAB>alpha". False if there's no manifest. Hold textureMutex.
bool
loadManifest(hdMoonray::RenderDelegate& renderDelegate)
{
    static const pxr::TfToken manifestToken("blender:textureManifest");
    const pxr::VtValue setting = renderDelegate.GetRenderSetting(manifestToken);
    if (!setting.IsHolding<std::string>()) return false;
    const std::string& manifest = setting.UncheckedGet<std::string>();
    if (manifest != textureManifestFile) {
        textureManifestFile = manifest;
        textureManifest.clear();
        materialManifest.clear();
        unmappedTextures.clear();
        std::ifstream in(std::filesystem::u8path(manifest));
        std::string line;
        while (std::getline(in, line)) {
            const size_t tab = line.find('\t');
            if (tab == std::string::npos) continue;
            if (line.compare(0, tab, "material") == 0) {
                std::istringstream fields(line.substr(tab + 1));
                std::string name;
                float transmission, alpha;
                if (fields >> name >> transmission >> alpha) materialManifest[name] = {transmission, alpha};
            } else {
                textureManifest[texturePathKey(line.substr(0, tab))] = line.substr(tab + 1);
            }
        }
    }
    return true;
}

// The tiled copy of a texture, or the same path if there is no manifest or the file has no entry, which is reported.
std::string
tiledTexture(hdMoonray::RenderDelegate& renderDelegate, const std::string& file)
{
    std::lock_guard<std::mutex> guard(textureMutex);
    if (file.empty() || !loadManifest(renderDelegate)) return file;
    auto it = textureManifest.find(texturePathKey(file));
    if (it == textureManifest.end()) {
        // Blender gives images with no file of their own a made-up relative name, which resolves to nothing. The stem survives.
        const std::filesystem::path name = std::filesystem::u8path(file).stem();
        it = textureManifest.find(texturePathKey("*" + name.u8string()));
    }
    if (it != textureManifest.end()) return it->second;
    unmappedTextures.insert(file);
    return file;
}

// What Blender put in a texture node's file parameter: the resolved path, or as written if nothing resolved.
std::string
textureFile(const pxr::HdMaterialNode& node)
{
    auto it = node.parameters.find(pxr::TfToken("file"));
    if (it == node.parameters.end() || !it->second.IsHolding<pxr::SdfAssetPath>()) return std::string();
    const pxr::SdfAssetPath& path = it->second.UncheckedGet<pxr::SdfAssetPath>();
    return path.GetResolvedPath().empty() ? path.GetAssetPath() : path.GetResolvedPath();
}

// Blender's USD export turns transmission into opacity, and MoonRay's UsdPreviewSurface renders opacity as a tinted hole. Give such
// a material, named by the add-on, a DwaBaseMaterial, with real refraction and with alpha as presence. The rest of the network
// feeds it as before. An alpha below 0 means linked: presence then comes through the opacity input, not as a number.
pxr::HdMaterialNetwork
refractiveNetwork(hdMoonray::RenderDelegate& renderDelegate, const pxr::SdfPath& id, const pxr::HdMaterialNetwork& network)
{
    Refractive refractive;
    {
        std::lock_guard<std::mutex> guard(textureMutex);
        if (!loadManifest(renderDelegate)) return network;
        auto it = materialManifest.find(id.GetName());
        if (it == materialManifest.end()) return network;
        refractive = it->second;
    }
    static const std::map<std::string, std::vector<std::string>> inputs = {
        {"diffuseColor", {"albedo", "transmission_color"}}, {"emissiveColor", {"emission"}},
        {"metallic", {"metallic"}}, {"roughness", {"roughness"}}, {"ior", {"refractive_index"}}};
    pxr::HdMaterialNetwork result = network;
    pxr::SdfPath surface;
    for (pxr::HdMaterialNode& node : result.nodes) {
        if (node.identifier != "UsdPreviewSurface") continue;
        std::map<pxr::TfToken, pxr::VtValue> parameters;
        for (const auto& input : inputs) {
            auto it = node.parameters.find(pxr::TfToken(input.first));
            if (it == node.parameters.end()) continue;
            for (const std::string& name : input.second) parameters[pxr::TfToken(name)] = it->second;
        }
        // Measured against Cycles: MoonRay tints at each surface a ray crosses, Cycles by the square root of the colour.
        const pxr::TfToken tintToken("transmission_color");
        auto tint = parameters.find(tintToken);
        if (tint != parameters.end() && tint->second.IsHolding<pxr::GfVec3f>()) {
            const pxr::GfVec3f color = tint->second.UncheckedGet<pxr::GfVec3f>();
            tint->second = pxr::VtValue(pxr::GfVec3f(scene_rdl2::math::sqrt(color[0]), scene_rdl2::math::sqrt(color[1]), scene_rdl2::math::sqrt(color[2])));
        }
        parameters[pxr::TfToken("transmission")] = pxr::VtValue(refractive.transmission);
        if (refractive.alpha >= 0.0f) parameters[pxr::TfToken("presence")] = pxr::VtValue(refractive.alpha);
        parameters[pxr::TfToken("casts_caustics")] = pxr::VtValue(true); // otherwise the sun doesn't come through a window
        parameters[pxr::TfToken("show_emission")] = pxr::VtValue(parameters.count(pxr::TfToken("emission")) > 0); // white if unset
        node.identifier = pxr::TfToken("DwaBaseMaterial");
        node.parameters = parameters;
        surface = node.path;
    }
    result.relationships.clear();
    for (const pxr::HdMaterialRelationship& rel : network.relationships) {
        if (rel.outputId != surface) {
            result.relationships.push_back(rel);
            continue;
        }
        if (rel.outputName == "opacity" && refractive.alpha < 0.0f) {
            pxr::HdMaterialRelationship renamed = rel;
            renamed.outputName = pxr::TfToken("presence");
            result.relationships.push_back(renamed);
            continue;
        }
        auto it = inputs.find(rel.outputName.GetString());
        if (it == inputs.end()) continue; // opacity holds the transmission; a normal map would need a NormalMap object
        for (const std::string& name : it->second) {
            pxr::HdMaterialRelationship renamed = rel;
            renamed.outputName = pxr::TfToken(name);
            result.relationships.push_back(renamed);
        }
    }
    return result;
}

}

// For the add-on, through ctypes: textures the last sync found no tiled copy for, one per line.
extern "C" __declspec(dllexport) const char*
hdMoonrayUnmappedTextures()
{
    std::lock_guard<std::mutex> guard(textureMutex);
    unmappedCopy.clear();
    for (const std::string& file : unmappedTextures) unmappedCopy += file + "\\n";
    return unmappedCopy.c_str();
}

""" + MATERIAL_HELPER_OLD
MATERIAL_TILED_OLD = """                        hdMoonray::ValueConverter::setDefault(shaderObj, attribute);
                    }
                }
            }
        } catch (const std::exception& e) {
            Logger::error(node.path, ": ", e.what());
            return nullptr;
"""
MATERIAL_TILED_NEW = """                        hdMoonray::ValueConverter::setDefault(shaderObj, attribute);
                    }
                }
            }
            if (node.identifier == "UsdUVTexture") {
                const AttributeKey<String> fileKey = sceneClass.getAttributeKey<String>("file");
                const std::string tiled = tiledTexture(renderDelegate, textureFile(node));
                shaderObj->set(fileKey, tiled);
                static const std::string linear("_linear.tx"); // see LINEAR in textures.py
                if (tiled.size() > linear.size() && tiled.compare(tiled.size() - linear.size(), linear.size(), linear) == 0) {
                    shaderObj->set(sceneClass.getAttributeKey<Int>("sourceColorSpace"), 0); // raw
                }
            }
        } catch (const std::exception& e) {
            Logger::error(node.path, ": ", e.what());
            return nullptr;
"""
MATERIAL_NETWORK_OLD = """    const pxr::HdMaterialNetwork& network = i->second;
"""
MATERIAL_NETWORK_NEW = """    const pxr::HdMaterialNetwork network = refractiveNetwork(renderDelegate, GetId(), i->second);
"""
MATERIAL_SKIP_OLD = """                if (valIt->second.UncheckedGet<pxr::SdfAssetPath>().GetResolvedPath().empty()) {
                    continue;
                }
"""
MATERIAL_SKIP_NEW = """                if (valIt->second.UncheckedGet<pxr::SdfAssetPath>().GetResolvedPath().empty() &&
                    tiledTexture(renderDelegate, textureFile(node)) == textureFile(node)) {
                    continue;
                }
"""
MATERIAL_DUMP_OLD = """    // dumpMaterialNetworkMap(networkmap);
"""
MATERIAL_DUMP_NEW = """    if (std::getenv("HDMOONRAY_DUMP_MATERIALS")) dumpMaterialNetworkMap(networkmap);
"""

# rdl2 Long is int64_t. MSVC's long is 32 bits: the cast read 8 bytes from 4, and SceneObject::set<long> doesn't exist.
PLUGIN_STATE_OLD = """#include <iostream>

PXR_NAMESPACE_OPEN_SCOPE // this does not work unless inside the pxr namespace
"""
PLUGIN_STATE_NEW = """#include <iostream>
#include <mutex>

namespace {

// MoonRay has one global render driver, and each RndrRenderer makes a RenderContext on it. Two at once have never been run, and the
// texture manifests are shared too, so a second delegate is refused while one is live. Blender treats that as a failed engine.
std::mutex delegateMutex;
pxr::HdRenderDelegate* liveDelegate = nullptr;

}

// For the add-on, through ctypes: true while a delegate is live.
extern "C" int
hdMoonrayDelegateLive()
{
    std::lock_guard<std::mutex> guard(delegateMutex);
    return liveDelegate != nullptr;
}

PXR_NAMESPACE_OPEN_SCOPE // this does not work unless inside the pxr namespace
"""
PLUGIN_CREATE_OLD = """        return new hdMoonray::RenderDelegate(new hdMoonray::RndrRenderer(0));
"""
PLUGIN_CREATE_NEW = """        std::lock_guard<std::mutex> guard(delegateMutex);
        if (liveDelegate) return refuse();
        liveDelegate = new hdMoonray::RenderDelegate(new hdMoonray::RndrRenderer(0));
        return liveDelegate;
"""
PLUGIN_CREATE2_OLD = """        return new hdMoonray::RenderDelegate(new hdMoonray::RndrRenderer(threads),settings);
"""
PLUGIN_CREATE2_NEW = """        std::lock_guard<std::mutex> guard(delegateMutex);
        if (liveDelegate) return refuse();
        liveDelegate = new hdMoonray::RenderDelegate(new hdMoonray::RndrRenderer(threads),settings);
        return liveDelegate;
"""
PLUGIN_DELETE_OLD = """        delete renderDelegate;
    }
"""
PLUGIN_DELETE_NEW = """        std::lock_guard<std::mutex> guard(delegateMutex);
        delete renderDelegate;
        if (renderDelegate == liveDelegate) liveDelegate = nullptr;
    }
"""
PLUGIN_PRIVATE_OLD = """private:
    // uncopyable
"""
PLUGIN_PRIVATE_NEW = """private:
    static pxr::HdRenderDelegate* refuse() {
        std::cerr << "hdMoonray: a MoonRay delegate is already live, a second one was refused" << std::endl;
        return nullptr;
    }

    // uncopyable
"""

# ---- Native materials. This block goes with the
# seven NATIVE_ lines at the end of EDITS. addon\native.py describes the format.
NATIVE_STATE_OLD = """std::unordered_map<std::string, Refractive> materialManifest;
"""
NATIVE_STATE_NEW = NATIVE_STATE_OLD + """std::unordered_map<std::string, pxr::HdMaterialNetwork> nativeManifest; // by material, the last node being the root
"""
NATIVE_PARSER_OLD = """// Reads the add-on's manifest when the setting names a new one."""
NATIVE_PARSER_NEW = """// A native-network line after its first field: material, then node, attr or bind and their fields. See addon\\native.py.
void
addNativeLine(const std::string& line)
{
    std::vector<std::string> fields;
    std::istringstream in(line);
    std::string field;
    while (std::getline(in, field, '\\t')) fields.push_back(field);
    if (fields.size() < 4) return;
    pxr::HdMaterialNetwork& network = nativeManifest[fields[0]];
    const std::string base = "/native/" + fields[0] + "/";
    if (fields[1] == "node") {
        pxr::HdMaterialNode node;
        node.path = pxr::SdfPath(base + fields[2]);
        node.identifier = pxr::TfToken(fields[3]);
        network.nodes.push_back(node);
    } else if (fields[1] == "attr" && fields.size() >= 6) {
        const pxr::SdfPath path(base + fields[2]);
        const std::string& kind = fields[4];
        const std::string text = fields.size() > 5 ? fields[5] : std::string();
        std::istringstream value(text);
        pxr::VtValue parsed;
        if (kind == "f") { float v = 0; value >> v; parsed = v; }
        else if (kind == "F") { pxr::VtArray<float> v; float x; while (value >> x) v.push_back(x); parsed = v; }
        else if (kind == "I") { pxr::VtArray<int> v; int x; while (value >> x) v.push_back(x); parsed = v; }
        else if (kind == "C") { pxr::VtArray<pxr::GfVec3f> v; pxr::GfVec3f x; while (value >> x[0] >> x[1] >> x[2]) v.push_back(x); parsed = v; }
        else if (kind == "b") { int v = 0; value >> v; parsed = v != 0; }
        else if (kind == "s") parsed = text;
        else if (kind == "c") { pxr::GfVec3f v; value >> v[0] >> v[1] >> v[2]; parsed = v; }
        else if (kind == "v") { pxr::GfVec2f v; value >> v[0] >> v[1]; parsed = v; }
        for (pxr::HdMaterialNode& node : network.nodes) {
            if (node.path == path) node.parameters[pxr::TfToken(fields[3])] = parsed;
        }
    } else if (fields[1] == "bind" && fields.size() >= 5) {
        pxr::HdMaterialRelationship rel;
        rel.inputId = pxr::SdfPath(base + fields[2]);
        rel.inputName = pxr::TfToken("out"); // one output, so one scene object for each node
        rel.outputId = pxr::SdfPath(base + fields[3]);
        rel.outputName = pxr::TfToken(fields[4]);
        network.relationships.push_back(rel);
    }
}

""" + NATIVE_PARSER_OLD
NATIVE_CLEAR_OLD = """        materialManifest.clear();
"""
NATIVE_CLEAR_NEW = NATIVE_CLEAR_OLD + """        nativeManifest.clear();
"""
NATIVE_LINE_OLD = """            if (line.compare(0, tab, "material") == 0) {
"""
NATIVE_LINE_NEW = """            if (line.compare(0, tab, "native") == 0) {
                addNativeLine(line.substr(tab + 1));
            } else if (line.compare(0, tab, "material") == 0) {
"""
NATIVE_LOOKUP_OLD = """}

// For the add-on, through ctypes: textures the last sync found no tiled copy for, one per line.
"""
NATIVE_LOOKUP_NEW = """// The network the add-on wrote for this material, if it wrote one.
bool
nativeNetwork(hdMoonray::RenderDelegate& renderDelegate, const pxr::SdfPath& id, pxr::HdMaterialNetwork& network)
{
    std::lock_guard<std::mutex> guard(textureMutex);
    if (!loadManifest(renderDelegate)) return false;
    auto it = nativeManifest.find(id.GetName());
    if (it == nativeManifest.end()) return false;
    network = it->second;
    return true;
}

""" + NATIVE_LOOKUP_OLD
NATIVE_TERMINAL_OLD = """    auto i = networkmap.map.find(terminalName);
    if (i == networkmap.map.end()) {
        return nullptr;
    }
"""
NATIVE_TERMINAL_NEW = """    auto i = networkmap.map.find(terminalName);
    pxr::HdMaterialNetwork native;
    const bool isNative = terminalName == pxr::HdMaterialTerminalTokens->surface && nativeNetwork(renderDelegate, GetId(), native);
    if (i == networkmap.map.end() && !isNative) {
        return nullptr;
    }
"""
NATIVE_USE_OLD = """    const pxr::HdMaterialNetwork network = refractiveNetwork(renderDelegate, GetId(), i->second);
"""
NATIVE_USE_NEW = """    const pxr::HdMaterialNetwork network = isNative ? native : refractiveNetwork(renderDelegate, GetId(), i->second);
"""

# ---- Render tab: addon\panels.py sends all of these. This block goes with the RT_ lines at the end of EDITS.
# Blender makes the delegate without settings, so the count comes through the environment. initGlobalDriver only reads it once per Blender.
RT_THREADS_OLD = "    if (numThreads == 0) numThreads = mRenderOptions->getThreads() - 1;\n"
RT_THREADS_NEW = """    const char* threadLimit = std::getenv("HDMOONRAY_THREADS"); // Performance > Threads
    if (numThreads == 0 && threadLimit) numThreads = std::strtoul(threadLimit, nullptr, 10);
    if (numThreads == 0) numThreads = mRenderOptions->getThreads() - 1;
"""
RT_THREADS_INCLUDE_OLD = "#include <iostream>\n#include <mutex>\n#include <set>\n"
RT_THREADS_INCLUDE_NEW = "#include <cstdlib>\n#include <iostream>\n#include <mutex>\n#include <set>\n"

# Blender's Hydra export gives every object one sample, so MoonRay sees nothing move and no shutter. The add-on evaluates the shutter's two ends
# and sends them: blender:motionEnd is lines of prim name, tab, 32 numbers, blender:shutter is the length in frames.
RT_MOTION_DECL_OLD = "class RenderDelegate final: public pxr::HdRenderDelegate\n"
RT_MOTION_DECL_NEW = """class RenderDelegate;

/// Where this prim is when the shutter opens and closes, if the add-on sent it.
bool blenderMotion(RenderDelegate& renderDelegate, const pxr::SdfPath& id, pxr::GfMatrix4d& begin, pxr::GfMatrix4d& end);
/// Shutter length in frames. 0 if the add-on sent none.
float blenderShutter(RenderDelegate& renderDelegate);

""" + RT_MOTION_DECL_OLD
RT_MOTION_INCLUDE_OLD = "#include <iostream>\n#include <chrono>\n"
RT_MOTION_INCLUDE_NEW = "#include <iostream>\n#include <chrono>\n#include <mutex>\n#include <sstream>\n#include <unordered_map>\n"
RT_MOTION_STEPS_OLD = """        motionSteps[1] = interval.second;
    }
"""
RT_MOTION_STEPS_NEW = """        motionSteps[1] = interval.second;
        static const pxr::TfToken frameToken("blender:frame"); // MoonRay seeds its noise with the frame. Without this it's 0 on every frame
        const pxr::VtValue blenderFrame = pxr::VtValue::Cast<float>(renderDelegate.GetRenderSetting(frameToken));
        if (!blenderFrame.IsEmpty()) frame = blenderFrame.UncheckedGet<float>();
        const float shutter = blenderShutter(renderDelegate);
        if (shutter > 0) {
            motionSteps[0] = -shutter / 2;
            motionSteps[1] = shutter / 2;
        }
    }
"""
RT_MOTION_DEF_OLD = "void\nRenderPass::_Execute("
RT_MOTION_DEF_NEW = """bool
blenderMotion(RenderDelegate& renderDelegate, const pxr::SdfPath& id, pxr::GfMatrix4d& begin, pxr::GfMatrix4d& end)
{
    static const pxr::TfToken token("blender:motionEnd");
    static std::mutex mutex; // prims sync on several threads
    static std::string text;
    static std::unordered_map<std::string, std::pair<pxr::GfMatrix4d, pxr::GfMatrix4d>> motion;
    const std::string now = renderDelegate.GetRenderSetting(token).GetWithDefault<std::string>();
    std::lock_guard<std::mutex> guard(mutex);
    if (now != text) {
        text = now;
        motion.clear();
        std::istringstream lines(text);
        std::string line;
        while (std::getline(lines, line)) {
            const size_t tab = line.find('\\t');
            if (tab == std::string::npos) continue;
            std::istringstream numbers(line.substr(tab + 1));
            pxr::GfMatrix4d first, last;
            for (int i = 0; i < 16; ++i) numbers >> first.data()[i];
            for (int i = 0; i < 16; ++i) numbers >> last.data()[i];
            if (numbers) motion[line.substr(0, tab)] = {first, last};
        }
    }
    auto it = motion.find(id.GetName());
    if (it == motion.end()) it = motion.find(id.GetParentPath().GetName()); // Blender's mesh is one prim per material, under the object
    if (it == motion.end()) return false;
    begin = it->second.first;
    end = it->second.second;
    return true;
}

float
blenderShutter(RenderDelegate& renderDelegate)
{
    static const pxr::TfToken token("blender:shutter");
    const pxr::VtValue value = pxr::VtValue::Cast<float>(renderDelegate.GetRenderSetting(token));
    return value.IsEmpty() ? 0 : value.UncheckedGet<float>();
}

""" + RT_MOTION_DEF_OLD
RT_MOTION_MESH_OLD = """            mGeometry->set(mGeometry->sNodeXformKey,
                           reinterpret_cast<const Mat4d&>(sampledXforms.values[0]));
            mMirror = sampledXforms.values[0].GetDeterminant() < 0; // workaround for MOONRAY-3512
        } else {
"""
RT_MOTION_MESH_NEW = """            mGeometry->set(mGeometry->sNodeXformKey,
                           reinterpret_cast<const Mat4d&>(sampledXforms.values[0]));
            GfMatrix4d motionBegin, motionEnd;
            if (blenderMotion(renderDelegate, id, motionBegin, motionEnd)) {
                mGeometry->set(mGeometry->sNodeXformKey, reinterpret_cast<const Mat4d&>(motionBegin));
                mGeometry->set(mGeometry->sNodeXformKey, reinterpret_cast<const Mat4d&>(motionEnd), TIMESTEP_END);
            }
            mMirror = sampledXforms.values[0].GetDeterminant() < 0; // workaround for MOONRAY-3512
        } else {
"""
RT_MOTION_CAMERA_OLD = """            mCamera->set(mCamera->sNodeXformKey, reinterpret_cast<const scene_rdl2::rdl2::Mat4d&>(xform));""" + 8 * " " + """
        } else {
"""
RT_MOTION_CAMERA_NEW = """            mCamera->set(mCamera->sNodeXformKey, reinterpret_cast<const scene_rdl2::rdl2::Mat4d&>(xform));
            pxr::GfMatrix4d motionBegin, motionEnd;
            if (blenderMotion(renderDelegate, id, motionBegin, motionEnd)) {
                mCamera->set(mCamera->sNodeXformKey, reinterpret_cast<const scene_rdl2::rdl2::Mat4d&>(motionBegin));
                mCamera->set(mCamera->sNodeXformKey, reinterpret_cast<const scene_rdl2::rdl2::Mat4d&>(motionEnd),
                             scene_rdl2::rdl2::TIMESTEP_END);
            }
        } else {
"""
RT_MOTION_SHUTTER_OLD = """            mCamera->set(mCamera->sMbShutterCloseKey, shutterClose);
        }
"""
RT_MOTION_SHUTTER_NEW = """            mCamera->set(mCamera->sMbShutterCloseKey, shutterClose);
        }

        const float blenderOpen = blenderShutter(renderDelegate);
        if (blenderOpen > 0) {
            mCamera->set(mCamera->sMbShutterOpenKey, -blenderOpen / 2);
            mCamera->set(mCamera->sMbShutterCloseKey, blenderOpen / 2);
        }
"""

# Blender's light passes, as light path expressions. Their names hold no colon, see AOV_ALBEDO.
RT_LIGHT_PASSES_OLD = """    if (s == "blenderAlbedo") {
"""
RT_LIGHT_GROUP_OLD = """    if (isBlender() && mType == pxr::HdPrimTypeTokens->domeLight && writableClass.hasAttribute("visible_in_camera")) {
"""
RT_LIGHT_GROUP_NEW = """    const std::string lightGroup =
        renderDelegate.GetRenderSetting(pxr::TfToken("blender:lightGroup:" + id.GetName())).GetWithDefault<std::string>();
    if (!lightGroup.empty()) mLight->set(writableClass.getAttributeKey<scene_rdl2::rdl2::String>("label"), lightGroup);

""" + RT_LIGHT_GROUP_OLD
RT_LIGHT_PASSES_NEW = """    if (s.rfind("blenderGroup_", 0) == 0) { // one light group, by the label Light.cc gave its lights
        prefix = pxr::HdAovTokens->lpe;
        suffix = pxr::TfToken("C.*<L.'" + s.substr(13) + "'>");
        return;
    }
    static const std::map<std::string, std::string> lightPasses = {
        {"blenderDiffuseDirect", "C<RD>[LO]"}, {"blenderDiffuseIndirect", "C<RD>.+[LO]"},
        {"blenderGlossyDirect", "C<RG>[LO]"}, {"blenderGlossyIndirect", "C<RG>.+[LO]"},
        {"blenderTransmission", "C<T.>.*[LO]"}, {"blenderEmission", "C[LO]"},
    };
    const auto lightPass = lightPasses.find(s);
    if (lightPass != lightPasses.end()) {
        prefix = pxr::HdAovTokens->lpe;
        suffix = pxr::TfToken(lightPass->second);
        return;
    }
    if (s == "blenderAlbedo") {
"""

# The world, when Hydra can't carry it: the add-on's Cycles bake of it. Only after the flatten above, and the strength below stays 1.
HL_WORLD_OLD = """    // Blender folds a plain world's strength into its colour, but drops it for an image, so the add-on sends it. 1.2 measured 0.836.
"""
HL_WORLD_NEW = """    if (isBlender() && mType == pxr::HdPrimTypeTokens->domeLight && writableClass.hasAttribute("texture")) {
        static const pxr::TfToken worldTexture("blender:worldTexture");
        const std::string baked = renderDelegate.GetRenderSetting(worldTexture).GetWithDefault<std::string>();
        if (!baked.empty()) {
            mLight->set(sceneClass.getAttributeKey<String>("texture"), baked);
            mLight->set(sceneClass.getAttributeKey<Rgb>("color"), Rgb(1.0f, 1.0f, 1.0f));
        }
    }

""" + HL_WORLD_OLD

# Blender has no default light, as Cycles has none. Emitters come from the add-on as a file of triangles, see addon\\lights.py.
HL_DEFAULT_OLD = """    if (!mNumLights && !mDefaultLight) {"""
HL_DEFAULT_NEW = """    static const bool blender = std::getenv("HDMOONRAY_BLENDER_UNITS") != nullptr;
    if (blender && !mMeshLightsLoaded) {
        std::lock_guard<std::mutex> lock(mCreateMutex);
        if (!mMeshLightsLoaded) loadMeshLights();
    }
    if (!blender && !mNumLights && !mDefaultLight) {"""
HL_MEMBER_OLD = "    unsigned mNumLights = 0;\n"
HL_MEMBER_NEW = HL_MEMBER_OLD + """    bool mMeshLightsLoaded = false;
    void loadMeshLights();
"""
HL_INCLUDE_OLD = "#include <pxr/imaging/hd/extComputation.h>\n"
HL_INCLUDE_NEW = "#include <scene_rdl2/scene/rdl2/Geometry.h>\n#include <cstdint>\n#include <cstdlib>\n#include <fstream>\n" + HL_INCLUDE_OLD
HL_LOAD_OLD = "void RenderDelegate::addLight()\n"
HL_LOAD_NEW = """// The add-on's emitters. Each is a mesh of its own, in the geometry set and in no layer, that only the MeshLight uses. Called with mCreateMutex held,
// once per delegate, before any light set is made.
void
RenderDelegate::loadMeshLights()
{
    const std::string path = GetRenderSetting(pxr::TfToken("blender:meshLights")).GetWithDefault<std::string>();
    if (!path.empty()) {
        std::ifstream in(path, std::ios::binary);
        uint32_t count = 0;
        in.read(reinterpret_cast<char*>(&count), sizeof(count));
        scene_rdl2::rdl2::SceneContext& wsc = acquireSceneContext();
        for (uint32_t i = 0; i < count && in; ++i) {
            float color[3];
            uint32_t sizes[2];
            in.read(reinterpret_cast<char*>(color), sizeof(color));
            in.read(reinterpret_cast<char*>(sizes), sizeof(sizes));
            if (!in) break;
            scene_rdl2::rdl2::Vec3fVector points(sizes[0]);
            scene_rdl2::rdl2::IntVector corners(size_t(sizes[1]) * 3);
            in.read(reinterpret_cast<char*>(points.data()), size_t(sizes[0]) * 3 * sizeof(float));
            in.read(reinterpret_cast<char*>(corners.data()), corners.size() * sizeof(int));
            if (!in) break;
            const std::string name = "/blenderGlow/" + std::to_string(i);
            scene_rdl2::rdl2::Geometry* mesh = wsc.createSceneObject("RdlMeshGeometry", name + "/mesh")->asA<scene_rdl2::rdl2::Geometry>();
            {
                UpdateGuard guard(*this, mesh);
                mesh->set("vertices_by_index", corners);
                mesh->set("vertex_list_0", points);
                mesh->set("face_vertex_count", scene_rdl2::rdl2::IntVector(sizes[1], 3));
                mesh->set("is_subd", false);
            }
            addUnassigned(mesh);
            scene_rdl2::rdl2::Light* light = wsc.createSceneObject("MeshLight", name)->asA<scene_rdl2::rdl2::Light>();
            {
                UpdateGuard guard(*this, light);
                light->set("geometry", static_cast<scene_rdl2::rdl2::SceneObject*>(mesh));
                light->set("normalized", false);
                light->set("visible_in_camera", 1);
                light->set("color", scene_rdl2::rdl2::Rgb(color[0], color[1], color[2]));
            }
            setCategory(light, CategoryType::LightLink, pxr::TfToken());
            setCategory(light, CategoryType::ShadowLink, pxr::TfToken());
        }
    }
    // In the 17.14 package LightTree_sample in LightTree.ispc has no guard for an empty tree, and MeshLight, DistantLight and EnvLight stay out of the tree,
    // so Adaptive light sampling crashes in vector mode without one bounded light. This one is in the tree and seen by no ray: every visible_ flag is off,
    // so integrateLightSetSample skips it before a shadow ray. Radiance 0 would turn it off, SphereLight::update, and that's the crash again.
    if (GetRenderSetting(pxr::TfToken("blender:lightTreeAnchor")).GetWithDefault<bool>(false)) {
        scene_rdl2::rdl2::SceneContext& wsc = acquireSceneContext();
        scene_rdl2::rdl2::Light* anchor = wsc.createSceneObject("SphereLight", "/blenderLightTreeAnchor")->asA<scene_rdl2::rdl2::Light>();
        {
            UpdateGuard guard(*this, anchor);
            anchor->set("radius", 0.001f);
            anchor->set("intensity", 0.001f);
            anchor->set("normalized", false);
            anchor->set("visible_in_camera", 0);
            for (const char* ray : {"diffuse_reflection", "diffuse_transmission", "glossy_reflection", "glossy_transmission", "mirror_reflection", "mirror_transmission"}) {
                anchor->set(std::string("visible_") + ray, false);
            }
        }
        setCategory(anchor, CategoryType::LightLink, pxr::TfToken());
        setCategory(anchor, CategoryType::ShadowLink, pxr::TfToken());
    }
    mMeshLightsLoaded = true;
}

""" + HL_LOAD_OLD

# Blender's dome arrives with no rotation, and MoonRay's EnvLight has a different frame from an Environment Texture: Z, X, Y on X, Y, Z.
# Without it the world turned the wrong way: sky corr 0.46, sun behind the camera.
HL_DOME_OLD = """    pxr::HdTimeSampleArray<pxr::GfMatrix4d, 4> sampledXforms;
"""
HL_DOME_NEW = """    if (isBlender() && mType == pxr::HdPrimTypeTokens->domeLight) {
        mLight->set(mLight->sNodeXformKey, scene_rdl2::rdl2::Mat4d(0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 1));
        return;
    }
""" + HL_DOME_OLD

# See-through glass under Blender, native.Network.clear_shadow: its shadow rays stop at the glass itself, so a transmission lobe doesn't add the sky at each surface,
# and nothing else takes a shadow from it. A complemented ShadowReceiverSet of the geometry alone says that. A mesh, so that nested glass objects don't shade each other.
CLEAR_SHADOW_OLD = """            // add the assignment to the Layer table
            renderDelegate.assign(mGeometry, assignment);
"""
CLEAR_SHADOW_NEW = """            const std::string clearShadow = renderDelegate.GetRenderSetting(TfToken("blender:clearShadowMaterials")).GetWithDefault<std::string>();
            if (!clearShadow.empty() && !isVolume()) {
                const std::string self = rprim.GetMaterialId().GetName();
                std::string word;
                bool caster = false;
                std::istringstream names(clearShadow);
                while (names >> word) caster = caster || word == self;
                if (caster) {
                    auto* set = renderDelegate.createSceneObject("ShadowReceiverSet", id.GetString() + "/clearShadow")->asA<scene_rdl2::rdl2::ShadowReceiverSet>();
                    UpdateGuard guard(set);
                    set->set("complement", true);
                    set->add(mGeometry->asA<scene_rdl2::rdl2::Geometry>());
                    assignment.mShadowReceiverSet = set;
                }
            }

""" + CLEAR_SHADOW_OLD
CLEAR_SHADOW_INCLUDE_OLD = '#include "GeometryMixin.h"\n'
CLEAR_SHADOW_INCLUDE_NEW = CLEAR_SHADOW_INCLUDE_OLD + "#include <scene_rdl2/scene/rdl2/ShadowReceiverSet.h>\n#include <sstream>\n"

# A view change in Blender's Rendered viewport restarts the frame, and resolve() keeps the last frame's pixels until the new one is
# ready for display, so the old picture stays up, turned the wrong way. The add-on calls hdMoonrayBlankUntilFresh() when the view
# changes; the frame that starts next shows nothing (transparent, Blender's background) until it has pixels. Other restarts are as before.
BLANK_PIXEL_OLD = "    unsigned filmActivity = ~0; // renderer can use this to check if image has changed\n"
BLANK_PIXEL_NEW = BLANK_PIXEL_OLD + """    unsigned blankedFrame = 0; // the frame this buffer was cleared for, see RndrRenderer::resolve
    bool blank = false; // cleared, no picture: RenderBuffer::Resolve leaves it transparent
"""
BLANK_STATE_OLD = "std::string lastError;\n\n}\n"
BLANK_STATE_NEW = """std::string lastError;
std::atomic<unsigned> framesStarted{0}; // startFrame() calls, by every renderer
std::atomic<bool> blankWanted{false}; // set by hdMoonrayBlankUntilFresh(), taken by the next startFrame()
std::atomic<unsigned> blankFrame{0}; // the frame that shows no picture until it is ready for display

}
"""
BLANK_INCLUDE_OLD = "#include <cstdlib>\n"
BLANK_INCLUDE_NEW = "#include <atomic>\n#include <cstdlib>\n#include <cstring>\n"
BLANK_START_OLD = "                mRenderContext->startFrame();\n"
BLANK_START_NEW = BLANK_START_OLD + """                const unsigned started = ++framesStarted;
                if (blankWanted.exchange(false)) blankFrame = started;
"""
BLANK_RESOLVE_OLD = """    if (!mRenderContext->isFrameReadyForDisplay()) {
        // tiles can"""
BLANK_RESOLVE_NEW = """    if (!mRenderContext->isFrameReadyForDisplay()) {
        // The view this buffer holds has gone: clear it once, then keep it clear until the new frame has pixels.
        const unsigned blank = blankFrame;
        if (isBeauty(ro) && pd.mData && blank && blank == framesStarted && pd.blankedFrame != blank) {
            std::memset(pd.mData, 0, size_t(pd.mWidth) * pd.mHeight * pd.mChannels * sizeof(float));
            pd.blankedFrame = blank;
            pd.blank = true;
            return true;
        }
        // tiles can"""
BLANK_SHOWN_OLD = "    mResized = false;\n\n    if (isBeauty(ro)) {\n"
BLANK_SHOWN_NEW = "    mResized = false;\n    pd.blank = false;\n\n    if (isBeauty(ro)) {\n"
BLANK_EXPORT_OLD = "// For the add-on, through ctypes: the reason the last frame failed, or an empty string.\n"
BLANK_EXPORT_NEW = """// For the add-on, through ctypes: the viewport's view changed, so the frame that starts next shows no picture until it has one.
extern "C" void
hdMoonrayBlankUntilFresh()
{
    blankWanted = true;
}

""" + BLANK_EXPORT_OLD
BLANK_COMPOSITE_OLD = "        if (clearValue[3] > 0.0f) {\n"
BLANK_COMPOSITE_NEW = "        if (clearValue[3] > 0.0f && !mPixelData.blank) { // a cleared buffer stays transparent\n"

EDITS = [
    ("lib/hydramoonray/GeometryMixin.cc", CLEAR_SHADOW_OLD, CLEAR_SHADOW_NEW),
    ("lib/hydramoonray/GeometryMixin.cc", CLEAR_SHADOW_INCLUDE_OLD, CLEAR_SHADOW_INCLUDE_NEW),
    ("plugin/hd_moonray_debug/RendererPlugin.cc", PLUGIN_STATE_OLD, PLUGIN_STATE_NEW),
    ("plugin/hd_moonray_debug/RendererPlugin.cc", PLUGIN_CREATE_OLD, PLUGIN_CREATE_NEW),
    ("plugin/hd_moonray_debug/RendererPlugin.cc", PLUGIN_CREATE2_OLD, PLUGIN_CREATE2_NEW),
    ("plugin/hd_moonray_debug/RendererPlugin.cc", PLUGIN_DELETE_OLD, PLUGIN_DELETE_NEW),
    ("plugin/hd_moonray_debug/RendererPlugin.cc", PLUGIN_PRIVATE_OLD, PLUGIN_PRIVATE_NEW),
    ("lib/hydramoonray/RenderDelegate.cc", "stats[percentDone] = progress * 100;",
     "stats[percentDone] = double(progress) * 100;  // Blender reads this as a double"),
    ("lib/hydramoonray/CMakeLists.txt", ATOMIC_OLD, ATOMIC_NEW),
    ("plugin/hd_moonray_debug/CMakeLists.txt", ATOMIC_OLD, ATOMIC_NEW),
    ("plugin/hd_moonray_debug/CMakeLists.txt", PLUGINFO_OLD, PLUGINFO_NEW),
    ("lib/hydramoonray/RenderBuffer.cc", VECTOR_OLD, VECTOR_NEW),
    ("lib/hydramoonray/ValueConverter.cc", "PTRCAST(Long, long);\n", ""),
    ("lib/hydramoonray/ValueConverter.cc", "_setAttributeRef<Long, long>", "_setAttributeRef<Long, Long>"),
    ("lib/hydramoonray/ValueConverter.cc", "pxr::VtArray<long>>(", "pxr::VtArray<Long>>("),
    ("lib/hydramoonray/ValueConverter.cc", "_set(sceneObj, attr, (long)1)", "_set(sceneObj, attr, (Long)1)"),
    ("lib/hydramoonray/RenderBuffer.h", "    void* Map() override { mMappers++; return mPixelData.mData; }\n",
     "    /// Blender reads AOVs through Map() and never calls Resolve(), so do it here.\n"
     "    void* Map() override { Resolve(); mMappers++; return mPixelData.mData; }\n"),
    ("lib/hydramoonray/Light.cc", LIGHTLINK_OLD, LIGHTLINK_NEW),
    ("lib/hydramoonray/Light.cc", SHADOWLINK_OLD, SHADOWLINK_NEW),
    ("lib/hydramoonray/Light.cc", LIGHT_INCLUDE_OLD, LIGHT_INCLUDE_NEW),
    ("lib/hydramoonray/Light.cc", LIGHT_HELPER_OLD, LIGHT_HELPER_NEW),
    ("lib/hydramoonray/Light.cc", LIGHT_FLATTEN_OLD, LIGHT_FLATTEN_NEW),
    ("lib/hydramoonray/Light.cc", LIGHT_UNITS_OLD, LIGHT_UNITS_NEW),
    ("lib/hydramoonray/Light.cc", SHAPING_HELPER_OLD, SHAPING_HELPER_NEW),
    ("lib/hydramoonray/Light.cc", SHAPING_CLASS_OLD, SHAPING_CLASS_NEW),
    ("lib/hydramoonray/Light.cc", SHAPING_ANGLE_OLD, SHAPING_ANGLE_NEW),
    ("lib/hydramoonray/Light.cc", SHAPING_SOFT_OLD, SHAPING_SOFT_NEW),
    ("lib/hydramoonray/RenderBuffer.cc", CLEAR_OLD, CLEAR_NEW),
    ("lib/hydramoonray/Renderer.h", RENDERER_FAIL_OLD, RENDERER_FAIL_NEW),
    ("lib/hydramoonray/RenderPass.cc", PASS_CAMERA_OLD, PASS_CAMERA_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.h", RR_H_FAILURE_OLD, RR_H_FAILURE_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.h", RR_H_FAIL_OLD, RR_H_FAIL_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.h", RR_H_STOP_OLD, RR_H_STOP_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.h", RR_H_DECL_OLD, RR_H_DECL_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", RR_INCLUDE_OLD, RR_INCLUDE_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", RR_STATE_OLD, RR_STATE_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", RR_CTOR_OLD, RR_CTOR_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", RR_CTOR2_OLD, RR_CTOR2_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", RR_DTOR_OLD, RR_DTOR_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", RR_START_OLD, RR_START_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", RR_CATCH_OLD, RR_CATCH_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", RR_COMPLETE_OLD, RR_COMPLETE_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", RR_FAIL_OLD, RR_FAIL_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", RR_EXPORT_OLD, RR_EXPORT_NEW),
    ("lib/hydramoonray/RenderBuffer.cc", AOV_ALBEDO_OLD, AOV_ALBEDO_NEW),
    ("lib/hydramoonray/RenderBuffer.cc", OUTPUT_NAME_OLD, OUTPUT_NAME_NEW),
    ("lib/hydramoonray/Camera.cc", CAMERA_OFFSET_OLD, CAMERA_OFFSET_NEW),
    ("lib/hydramoonray/Light.cc", LIGHT_NORMALIZED_OLD, LIGHT_NORMALIZED_NEW),
    ("lib/hydramoonray/Material.cc", MATERIAL_INCLUDE_OLD, MATERIAL_INCLUDE_NEW),
    ("lib/hydramoonray/Material.cc", MATERIAL_HELPER_OLD, MATERIAL_HELPER_NEW),
    ("lib/hydramoonray/Material.cc", MATERIAL_TILED_OLD, MATERIAL_TILED_NEW),
    ("lib/hydramoonray/Material.cc", MATERIAL_DUMP_OLD, MATERIAL_DUMP_NEW),
    ("lib/hydramoonray/Material.cc", MATERIAL_SKIP_OLD, MATERIAL_SKIP_NEW),
    ("lib/hydramoonray/Material.cc", MATERIAL_NETWORK_OLD, MATERIAL_NETWORK_NEW),
    ("lib/hydramoonray/Material.cc", NATIVE_STATE_OLD, NATIVE_STATE_NEW),
    ("lib/hydramoonray/Material.cc", NATIVE_PARSER_OLD, NATIVE_PARSER_NEW),
    ("lib/hydramoonray/Material.cc", NATIVE_CLEAR_OLD, NATIVE_CLEAR_NEW),
    ("lib/hydramoonray/Material.cc", NATIVE_LINE_OLD, NATIVE_LINE_NEW),
    ("lib/hydramoonray/Material.cc", NATIVE_LOOKUP_OLD, NATIVE_LOOKUP_NEW),
    ("lib/hydramoonray/Material.cc", NATIVE_TERMINAL_OLD, NATIVE_TERMINAL_NEW),
    ("lib/hydramoonray/Material.cc", NATIVE_USE_OLD, NATIVE_USE_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", RT_THREADS_OLD, RT_THREADS_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", RT_THREADS_INCLUDE_OLD, RT_THREADS_INCLUDE_NEW),
    ("lib/hydramoonray/RenderDelegate.h", RT_MOTION_DECL_OLD, RT_MOTION_DECL_NEW),
    ("lib/hydramoonray/RenderPass.cc", RT_MOTION_INCLUDE_OLD, RT_MOTION_INCLUDE_NEW),
    ("lib/hydramoonray/RenderPass.cc", RT_MOTION_STEPS_OLD, RT_MOTION_STEPS_NEW),
    ("lib/hydramoonray/RenderPass.cc", RT_MOTION_DEF_OLD, RT_MOTION_DEF_NEW),
    ("lib/hydramoonray/GeometryMixin.cc", RT_MOTION_MESH_OLD, RT_MOTION_MESH_NEW),
    ("lib/hydramoonray/Camera.cc", RT_MOTION_CAMERA_OLD, RT_MOTION_CAMERA_NEW),
    ("lib/hydramoonray/Camera.cc", RT_MOTION_SHUTTER_OLD, RT_MOTION_SHUTTER_NEW),
    ("lib/hydramoonray/RenderBuffer.cc", RT_LIGHT_PASSES_OLD, RT_LIGHT_PASSES_NEW),
    ("lib/hydramoonray/Light.cc", RT_LIGHT_GROUP_OLD, RT_LIGHT_GROUP_NEW),
    ("lib/hydramoonray/Light.cc", HL_WORLD_OLD, HL_WORLD_NEW),
    ("lib/hydramoonray/Light.cc", HL_DOME_OLD, HL_DOME_NEW),
    ("lib/hydramoonray/RenderDelegate.cc", HL_DEFAULT_OLD, HL_DEFAULT_NEW),
    ("lib/hydramoonray/RenderDelegate.h", HL_MEMBER_OLD, HL_MEMBER_NEW),
    ("lib/hydramoonray/RenderDelegate.cc", HL_INCLUDE_OLD, HL_INCLUDE_NEW),
    ("lib/hydramoonray/RenderDelegate.cc", HL_LOAD_OLD, HL_LOAD_NEW),
    ("lib/hydramoonray/PixelData.h", BLANK_PIXEL_OLD, BLANK_PIXEL_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", BLANK_STATE_OLD, BLANK_STATE_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", BLANK_INCLUDE_OLD, BLANK_INCLUDE_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", BLANK_START_OLD, BLANK_START_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", BLANK_RESOLVE_OLD, BLANK_RESOLVE_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", BLANK_SHOWN_OLD, BLANK_SHOWN_NEW),
    ("plugin/hd_moonray_debug/RndrRenderer.cc", BLANK_EXPORT_OLD, BLANK_EXPORT_NEW),
    ("lib/hydramoonray/RenderBuffer.cc", BLANK_COMPOSITE_OLD, BLANK_COMPOSITE_NEW),
]


def main():
    source, copy = sys.argv[1:3]
    if os.path.exists(copy):
        raise SystemExit(f"{copy} exists; remove it first")
    shutil.copytree(source, copy, ignore=shutil.ignore_patterns(".git"))
    for rel, old, new in EDITS:
        path = os.path.join(copy, *rel.split("/"))
        with open(path, newline="") as f:
            text = f.read()
        crlf = "\r\n" in text
        if crlf:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        count = text.count(old)
        if count != 1:
            raise SystemExit(f"{rel}: expected one match, found {count}")
        with open(path, "w", newline="") as f:
            f.write(text.replace(old, new))
        print("edited", rel)


if __name__ == "__main__":
    main()
