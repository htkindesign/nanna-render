// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright 2026 Kiet Ho
// Records sizeof for USD classes that hold TBB types, so the same headers can be compared under both TBB versions.
// usd_ms.dll was built with oneTBB; hydramoonray and hd_moonray_debug compile these headers against TBB 2020.
//
// build_hd.bat compiles it with MSVC 14.44, only (/c), once under oneTBB and once under TBB 2020, and abi_compare.py compares the two .obj files.
// Nothing here needs linking: the records are constant data with no pointers, found in the .obj by their "ABIREC__" tag.
// Equal sizes don't prove equal layout. A differing size proves a difference.
#include "pxr/pxr.h"
#include "pxr/base/tf/diagnosticMgr.h"
#include "pxr/base/work/dispatcher.h"
#include "pxr/base/work/isolatingDispatcher.h"
#include "pxr/base/work/taskGraph.h"
#include "pxr/imaging/hd/cachingSceneIndex.h"
#include "pxr/imaging/hd/changeTracker.h"
#include "pxr/imaging/hd/dependencyForwardingSceneIndex.h"
#include "pxr/imaging/hd/flatteningSceneIndex.h"
#include "pxr/imaging/hd/primGather.h"
#include "pxr/imaging/hd/renderIndex.h"
#include "pxr/usd/pcp/layerStack.h"
#include "pxr/usd/usd/stage.h"
#include "pxr/usdImaging/usdImaging/primvarDescCache.h"

#include <cstdint>

PXR_NAMESPACE_USING_DIRECTIVE

struct AbiRecord {
    char tag[8];
    char name[56];
    std::uint64_t size;
};

#define ABI_RECORD(T) {{'A', 'B', 'I', 'R', 'E', 'C', '_', '_'}, #T, sizeof(T)}

extern const AbiRecord abi_records[] = {
    ABI_RECORD(TfDiagnosticMgr),
    ABI_RECORD(WorkDispatcher),
    ABI_RECORD(WorkIsolatingDispatcher),
    ABI_RECORD(WorkTaskGraph),
    ABI_RECORD(HdChangeTracker),
    ABI_RECORD(HdRenderIndex),
    ABI_RECORD(HdPrimGather),
    ABI_RECORD(HdCachingSceneIndex),
    ABI_RECORD(HdDependencyForwardingSceneIndex),
    ABI_RECORD(HdFlatteningSceneIndex),
    ABI_RECORD(PcpLayerStack),
    ABI_RECORD(UsdStage),
    ABI_RECORD(UsdImagingPrimvarDescCache),
};
