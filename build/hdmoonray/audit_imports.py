# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""List USD imports of the built DLLs that name a class holding TBB types, and every USD class imported. Reads files only."""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pelib import PE
CLASSES = ["HdChangeTracker","HdRenderIndex","HdPrimGather","HdCachingSceneIndex","HdDependencyForwardingSceneIndex","HdFlatteningSceneIndex",
           "TfDiagnosticMgr","TfErrorMark","WorkDispatcher","WorkIsolatingDispatcher","WorkTaskGraph","UsdStage","UsdImagingPrimvarDescCache","TfStacked","TfBigRWMutex","TfNoticeRegistry","Tf_NoticeRegistry","Sdf_ChangeManager","Hd_InstanceRegistry","HdInstanceRegistry","HdSceneIndexAdapterSceneDelegate"]
for path in sys.argv[1:]:
    pe = PE(path)
    print(os.path.basename(path))
    for dll, names, ords, kind in pe.imports():
        print(f"  {dll}: {len(names)} names, {len(ords) if ords else 0} ordinals")
        if dll.lower() == "usd_ms.dll":
            for c in CLASSES:
                hit = [n for n in names if f"@{c}@" in n]
                if hit:
                    print(f"    {c}: {len(hit)}")
                    for n in hit: print("      ", n[:120])
    pe.close()
