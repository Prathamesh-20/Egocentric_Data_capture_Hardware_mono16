#!/usr/bin/env bash
# Build and install the orbbec_stream binary used by FOVChecker.
# Idempotent — safe to re-run.
#
# After install:
#   binary at <SDK>/bin/orbbec_stream
#   symlink /mnt/ssd/OrbbecSDK_Pi5/orbbec_stream -> <SDK>/bin/orbbec_stream
#   symlink /mnt/ssd/OrbbecSDK_Pi5/lib            -> <SDK>/lib
#
# (The symlinks let capture/config.py's hardcoded ORBBEC_STREAM /
#  ORBBEC_STREAM_LIB paths keep working without code changes.)
#
# Pre-conditions:
#   - OrbbecSDK extracted at $SDK_ROOT (default: ~/OrbbecSDK_v2.7.6_*_arm64)
#   - install.sh from install_kit_v1 has been run at least once
#     (so apt deps + MCAP headers are already there — we reuse the env)
#   - Orbbec camera plugged in (or set SKIP_SMOKE=1)

set -euo pipefail

# Resolve KIT_DIR — directory holding this script.
KIT_DIR="${KIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# Resolve SDK_ROOT.
if [[ -z "${SDK_ROOT:-}" ]]; then
    SDK_ROOT=$(ls -d "$HOME"/OrbbecSDK_v2.7.6_*_arm64 2>/dev/null | head -n1 || true)
fi
if [[ -z "${SDK_ROOT:-}" || ! -d "$SDK_ROOT" ]]; then
    echo "FAIL: SDK_ROOT not found. Set SDK_ROOT or run install.sh first." >&2
    exit 1
fi

ts() { date +"[%H:%M:%S]"; }

LOG="/tmp/install_orbbec_stream_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

echo "$(ts) install_fov_stream.sh starting on host=$(hostname) user=$(whoami)"
echo "$(ts) log file: $LOG"
echo "$(ts) SDK_ROOT=$SDK_ROOT"
echo "$(ts) KIT_DIR=$KIT_DIR"

# ── Pre-flight ──────────────────────────────────────────────────────
echo "$(ts) === Pre-flight checks ==="
[[ -f "$KIT_DIR/orbbec_stream.cpp" ]] || { echo "FAIL: orbbec_stream.cpp not in $KIT_DIR"; exit 1; }
[[ -f "$KIT_DIR/CMakeLists.fov_stream.txt" ]] || { echo "FAIL: CMakeLists.fov_stream.txt not in $KIT_DIR"; exit 1; }
[[ -d "$SDK_ROOT/examples/src" ]] || { echo "FAIL: $SDK_ROOT has no examples/src"; exit 1; }
echo "$(ts) OK   sources present"

# ── Source drop-in ──────────────────────────────────────────────────
echo "$(ts) === Source drop-in ==="
TARGET_DIR="$SDK_ROOT/examples/src/orbbec_stream"
mkdir -p "$TARGET_DIR"

if ! cmp -s "$KIT_DIR/orbbec_stream.cpp" "$TARGET_DIR/orbbec_stream.cpp" 2>/dev/null; then
    cp "$KIT_DIR/orbbec_stream.cpp" "$TARGET_DIR/orbbec_stream.cpp"
    echo "$(ts) OK   copied orbbec_stream.cpp -> $TARGET_DIR/"
else
    echo "$(ts) SKIP orbbec_stream.cpp unchanged"
fi

# CMakeLists.fov_stream.txt -> CMakeLists.txt in the dropped dir
if ! cmp -s "$KIT_DIR/CMakeLists.fov_stream.txt" "$TARGET_DIR/CMakeLists.txt" 2>/dev/null; then
    cp "$KIT_DIR/CMakeLists.fov_stream.txt" "$TARGET_DIR/CMakeLists.txt"
    echo "$(ts) OK   copied CMakeLists.txt -> $TARGET_DIR/"
else
    echo "$(ts) SKIP CMakeLists.txt unchanged"
fi

# ── Parent CMakeLists registration ──────────────────────────────────
echo "$(ts) === Parent CMakeLists.txt subdirectory line ==="
PARENT_CMAKE="$SDK_ROOT/examples/src/CMakeLists.txt"
[[ -f "$PARENT_CMAKE" ]] || { echo "FAIL: $PARENT_CMAKE missing"; exit 1; }

if grep -qE "^\s*add_subdirectory\(orbbec_stream\)" "$PARENT_CMAKE"; then
    echo "$(ts) SKIP add_subdirectory(orbbec_stream) already present"
else
    # Insert after the first existing add_subdirectory(1.stream.color) line,
    # preserving its indentation. Backup first.
    BAK="$PARENT_CMAKE.bak.$(date +%s)"
    cp "$PARENT_CMAKE" "$BAK"
    if grep -qE "^\s*add_subdirectory\(1\.stream\.color\)" "$PARENT_CMAKE"; then
        # sed -i: insert "<same-indent>add_subdirectory(orbbec_stream)" after match
        sed -i -E '/^[[:space:]]*add_subdirectory\(1\.stream\.color\)/{
                    p
                    s/(^[[:space:]]*)add_subdirectory\(1\.stream\.color\).*/\1add_subdirectory(orbbec_stream)/
                  }' "$PARENT_CMAKE"
        echo "$(ts) OK   inserted add_subdirectory(orbbec_stream) (backup: $BAK)"
    else
        # Fallback: just append at the end
        echo "add_subdirectory(orbbec_stream)" >> "$PARENT_CMAKE"
        echo "$(ts) OK   appended add_subdirectory(orbbec_stream) (backup: $BAK)"
    fi
fi

# ── Build ───────────────────────────────────────────────────────────
echo "$(ts) === Build ==="
cmake -S "$SDK_ROOT/examples" -B "$SDK_ROOT/build" -DCMAKE_BUILD_TYPE=Release > /dev/null
cmake --build "$SDK_ROOT/build" --target orbbec_stream -j"$(nproc)"

BUILT="$SDK_ROOT/build/bin/orbbec_stream"
INSTALLED="$SDK_ROOT/bin/orbbec_stream"

[[ -x "$BUILT" ]] || { echo "FAIL: build did not produce $BUILT"; exit 1; }
echo "$(ts) OK   built: $BUILT"

# Copy into <SDK>/bin/ next to ob_device_record_mcap_nogui.
mkdir -p "$SDK_ROOT/bin"
cp "$BUILT" "$INSTALLED"
chmod +x "$INSTALLED"
echo "$(ts) OK   installed: $INSTALLED"

# ── Compatibility symlinks for capture/config.py ────────────────────
echo "$(ts) === Symlinks for config.py compatibility ==="
LEGACY_DIR="/mnt/ssd/OrbbecSDK_Pi5"
sudo mkdir -p "$LEGACY_DIR"

# orbbec_stream binary symlink
if [[ -L "$LEGACY_DIR/orbbec_stream" && \
      "$(readlink -f "$LEGACY_DIR/orbbec_stream")" == "$(readlink -f "$INSTALLED")" ]]; then
    echo "$(ts) SKIP symlink already correct: $LEGACY_DIR/orbbec_stream"
else
    sudo ln -sfn "$INSTALLED" "$LEGACY_DIR/orbbec_stream"
    echo "$(ts) OK   $LEGACY_DIR/orbbec_stream -> $INSTALLED"
fi

# lib dir symlink (config.py uses ORBBEC_STREAM_LIB = /opt/OrbbecSDK/lib,
# but capture/config.py on this branch may also reference $LEGACY_DIR/lib —
# create both for safety)
SDK_LIB_DIR="$SDK_ROOT/lib"
if [[ -d "$SDK_LIB_DIR" ]]; then
    if [[ -L "$LEGACY_DIR/lib" && \
          "$(readlink -f "$LEGACY_DIR/lib")" == "$(readlink -f "$SDK_LIB_DIR")" ]]; then
        echo "$(ts) SKIP symlink already correct: $LEGACY_DIR/lib"
    else
        sudo ln -sfn "$SDK_LIB_DIR" "$LEGACY_DIR/lib"
        echo "$(ts) OK   $LEGACY_DIR/lib -> $SDK_LIB_DIR"
    fi
    # /opt/OrbbecSDK/lib is the path config.py uses for ORBBEC_STREAM_LIB
    sudo mkdir -p /opt/OrbbecSDK
    if [[ -L /opt/OrbbecSDK/lib && \
          "$(readlink -f /opt/OrbbecSDK/lib)" == "$(readlink -f "$SDK_LIB_DIR")" ]]; then
        echo "$(ts) SKIP symlink already correct: /opt/OrbbecSDK/lib"
    else
        sudo ln -sfn "$SDK_LIB_DIR" /opt/OrbbecSDK/lib
        echo "$(ts) OK   /opt/OrbbecSDK/lib -> $SDK_LIB_DIR"
    fi
else
    echo "$(ts) WARN $SDK_LIB_DIR does not exist — skipping lib symlinks"
fi

# ── Smoke test ──────────────────────────────────────────────────────
if [[ "${SKIP_SMOKE:-0}" == "1" ]]; then
    echo "$(ts) === Smoke skipped (SKIP_SMOKE=1) ==="
else
    echo "$(ts) === Smoke test (3s) ==="
    SMOKE_OUT=$(mktemp -d /tmp/orbbec_stream_smoke.XXXXXX)
    export LD_LIBRARY_PATH="$SDK_ROOT/lib:${LD_LIBRARY_PATH:-}"

    # Run the binary briefly, write its stdout to a file, then kill it.
    # We expect at least one "FRAME COLOR" header in the first 3 seconds.
    timeout --signal=TERM 3s "$INSTALLED" > "$SMOKE_OUT/stream.bin" 2> "$SMOKE_OUT/err.log" || true

    BYTES=$(stat -c %s "$SMOKE_OUT/stream.bin" 2>/dev/null || echo 0)
    HEADER_COUNT=$(grep -c "^FRAME COLOR " "$SMOKE_OUT/stream.bin" 2>/dev/null || echo 0)

    echo "$(ts) smoke output:  $SMOKE_OUT/stream.bin ($BYTES bytes)"
    echo "$(ts) smoke headers: $HEADER_COUNT FRAME COLOR lines"
    echo "$(ts) --- last 10 lines of stderr ---"
    tail -n 10 "$SMOKE_OUT/err.log" || true
    echo "$(ts) -------------------------------"

    if [[ "$HEADER_COUNT" -lt 5 ]]; then
        echo "$(ts) FAIL: expected at least 5 FRAME COLOR headers in 3s, got $HEADER_COUNT"
        echo "$(ts) check $SMOKE_OUT/err.log for SDK error messages"
        exit 1
    fi
    echo "$(ts) OK   smoke: $HEADER_COUNT frames in 3s"
fi

echo
echo "============================================================"
echo "PASS — $(hostname)"
echo "binary:  $INSTALLED"
echo "symlink: $LEGACY_DIR/orbbec_stream"
echo "log:     $LOG"
echo "============================================================"
