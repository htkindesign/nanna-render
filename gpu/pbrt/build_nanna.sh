#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
# Builds pbrt.exe under a timeout (seconds, default 580) with build.bat. Log in $NANNA_PBRT_WORK/logs.
# NANNA_PBRT_WORK: the folder holding pbrt-v4/ (config.bat).
HERE=$(cd "$(dirname "$0")" && pwd)
B="$HERE/build.bat"; [ -f "$B" ] || B="$HERE/../build.bat"
[ -n "$NANNA_PBRT_WORK" ] || { echo 'NANNA_PBRT_WORK is not set: set it in config.bat, then run "call config.bat".'; exit 2; }
cd "$NANNA_PBRT_WORK" || exit 1
mkdir -p logs
echo "own PID $$"
timeout ${1:-580} cmd //c "$(cygpath -w "$B")" > logs/nanna_build.log 2>&1
echo "exit $?"
grep -a -E "error|Error|FAILED" logs/nanna_build.log | head -20 | cut -c1-220
tail -3 logs/nanna_build.log | cut -c1-200
