#!/usr/bin/env bash
# install.sh — Deploy ob_device_record_mcap_nogui to a Pi.
#
# Idempotent: re-running this on a Pi where some/all steps are already done
# is safe and skips work that's already complete.
#
# Run as the target user (e.g. autonexego15), NOT as root. The script will
# call sudo only where strictly required: apt install, udev rules copy,
# /mnt/ssd directory creation.
#
# Env overrides:
#   SDK_ROOT   default: ~/OrbbecSDK_v2.7.6_202602021228_d712cda_linux_arm64
#   KIT_DIR    default: dir holding this script (must contain the .cpp + CMakeLists.txt)
#   SKIP_SMOKE 1 to skip the camera smoke test (e.g. on a bench Pi without a camera plugged in yet)

set -euo pipefail

# ---------- Logging --------------------------------------------------------

TS="$(date +%Y%m%d_%H%M%S)"
LOG="/tmp/install_mcap_${TS}.log"
# Tee everything (incl. set -x style command output) to the log AND the terminal.
exec > >(tee -a "$LOG") 2>&1

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '[%s] \033[32mOK\033[0m   %s\n' "$(date +%H:%M:%S)" "$*"; }
skip() { printf '[%s] \033[36mSKIP\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '[%s] \033[33mWARN\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { printf '[%s] \033[31mFAIL\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }

# ---------- Paths & constants ---------------------------------------------

KIT_DIR="${KIT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
SDK_ROOT="${SDK_ROOT:-$HOME/OrbbecSDK_v2.7.6_202602021228_d712cda_linux_arm64}"
EXAMPLE_DIR="$SDK_ROOT/examples/src/2.device.record.nogui_mcap"
PARENT_CMAKE="$SDK_ROOT/examples/src/CMakeLists.txt"
THIRDPARTY_DIR="$SDK_ROOT/examples/third_party"
MCAP_REPO_DIR="$THIRDPARTY_DIR/.mcap_repo"   # full sparse clone lives here
MCAP_INCLUDE_DIR="$THIRDPARTY_DIR/mcap"      # symlink target — must be the dir literally named "mcap"
UDEV_SRC="$SDK_ROOT/shared/99-obsensor-libusb.rules"
UDEV_DST="/etc/udev/rules.d/99-obsensor-libusb.rules"
RECORD_DIR="/mnt/ssd/recordings"
BIN_NAME="ob_device_record_mcap_nogui"
BIN_INSTALL="$SDK_ROOT/bin/$BIN_NAME"
BUILD_DIR="$SDK_ROOT/build"

REQUIRED_APT_PKGS=(libzstd-dev nlohmann-json3-dev git cmake build-essential)
ORBBEC_VID="2bc5"
MIN_FREE_MB=2048   # build + headers + smoke .mcap

log "install.sh starting on host=$(hostname) user=$(whoami)"
log "log file: $LOG"
log "SDK_ROOT=$SDK_ROOT"
log "KIT_DIR=$KIT_DIR"

# ---------- 1. Pre-flight --------------------------------------------------

log "=== Pre-flight checks ==="

# Kit files present
[ -f "$KIT_DIR/$BIN_NAME.cpp" ] \
    || die "missing $KIT_DIR/$BIN_NAME.cpp — run install.sh from the unpacked kit dir"
[ -f "$KIT_DIR/CMakeLists.txt" ] \
    || die "missing $KIT_DIR/CMakeLists.txt"

# SDK present
[ -d "$SDK_ROOT" ] \
    || die "SDK not found at $SDK_ROOT (override with SDK_ROOT=...)"
[ -d "$SDK_ROOT/examples/src" ] \
    || die "$SDK_ROOT does not look like the OrbbecSDK (no examples/src/)"
[ -f "$PARENT_CMAKE" ] \
    || die "$PARENT_CMAKE missing — SDK layout unexpected"
[ -f "$UDEV_SRC" ] \
    || die "udev rules file missing at $UDEV_SRC"

ok "SDK at $SDK_ROOT"

# Camera detection (skippable for bench setup)
if [ "${SKIP_SMOKE:-0}" != "1" ]; then
    if ! lsusb 2>/dev/null | grep -qi "ID ${ORBBEC_VID}:"; then
        die "no Orbbec camera detected (lsusb VID $ORBBEC_VID). Plug it in or set SKIP_SMOKE=1 to skip."
    fi
    ok "Orbbec camera detected on USB"
else
    skip "camera detection (SKIP_SMOKE=1)"
fi

# Network reachability for the MCAP sparse clone (only if headers not already present)
if [ ! -f "$MCAP_INCLUDE_DIR/writer.hpp" ]; then
    if ! getent hosts github.com >/dev/null 2>&1; then
        die "MCAP headers not present and github.com is not resolvable"
    fi
    ok "github.com reachable"
else
    skip "network check (MCAP headers already present)"
fi

# Disk space
free_mb="$(df -Pm "$HOME" | awk 'NR==2 {print $4}')"
if [ "$free_mb" -lt "$MIN_FREE_MB" ]; then
    die "insufficient free disk in \$HOME: ${free_mb}MB < ${MIN_FREE_MB}MB"
fi
ok "disk: ${free_mb}MB free in \$HOME"

# sudo non-interactive check (fail fast rather than half-way through apt)
if ! sudo -n true 2>/dev/null; then
    log "sudo will prompt for a password (apt / udev / /mnt/ssd setup)..."
fi

# ---------- 2. Install steps (each idempotent) ----------------------------

log "=== Apt deps ==="

missing_pkgs=()
for p in "${REQUIRED_APT_PKGS[@]}"; do
    if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed"; then
        skip "$p already installed"
    else
        missing_pkgs+=("$p")
    fi
done
if [ "${#missing_pkgs[@]}" -gt 0 ]; then
    log "installing: ${missing_pkgs[*]}"
    sudo apt-get update -y
    sudo apt-get install -y "${missing_pkgs[@]}"
    ok "installed apt deps: ${missing_pkgs[*]}"
fi

log "=== udev rules ==="

if [ -f "$UDEV_DST" ] && cmp -s "$UDEV_SRC" "$UDEV_DST"; then
    skip "udev rules already match $UDEV_DST"
else
    sudo install -m 0644 "$UDEV_SRC" "$UDEV_DST"
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    ok "installed udev rules to $UDEV_DST"
fi

log "=== Recording dir ==="

if [ -d "$RECORD_DIR" ] && [ -O "$RECORD_DIR" ]; then
    skip "$RECORD_DIR already exists and is owned by $(whoami)"
else
    sudo mkdir -p "$RECORD_DIR"
    sudo chown -R "$(id -u):$(id -g)" "$RECORD_DIR"
    ok "created/chowned $RECORD_DIR"
fi

log "=== MCAP headers ==="

# We do a partial sparse clone of foxglove/mcap into THIRDPARTY_DIR/.mcap_repo
# and then symlink THIRDPARTY_DIR/mcap -> .mcap_repo/cpp/mcap/include/mcap.
# That's because gotcha (b) requires the "mcap" directory to live directly
# under THIRDPARTY_DIR so that "#include <mcap/writer.hpp>" resolves via
# the parent's `-I third_party` path.

mkdir -p "$THIRDPARTY_DIR"

if [ -f "$MCAP_INCLUDE_DIR/writer.hpp" ]; then
    skip "MCAP headers already at $MCAP_INCLUDE_DIR/writer.hpp"
else
    if [ ! -d "$MCAP_REPO_DIR/.git" ]; then
        log "sparse-cloning foxglove/mcap into $MCAP_REPO_DIR"
        git clone --depth 1 --filter=blob:none --sparse \
            https://github.com/foxglove/mcap.git "$MCAP_REPO_DIR"
        git -C "$MCAP_REPO_DIR" sparse-checkout set cpp/mcap/include/mcap
    else
        log "mcap repo already cloned; ensuring sparse path is set"
        git -C "$MCAP_REPO_DIR" sparse-checkout set cpp/mcap/include/mcap
    fi

    src="$MCAP_REPO_DIR/cpp/mcap/include/mcap"
    [ -d "$src" ] || die "expected $src after sparse checkout — repo layout changed?"

    # Replace any stale symlink/dir at $MCAP_INCLUDE_DIR with a fresh symlink.
    rm -rf "$MCAP_INCLUDE_DIR"
    ln -s "$src" "$MCAP_INCLUDE_DIR"
    [ -f "$MCAP_INCLUDE_DIR/writer.hpp" ] \
        || die "MCAP writer.hpp not visible at $MCAP_INCLUDE_DIR/writer.hpp after symlink"
    ok "MCAP headers symlinked at $MCAP_INCLUDE_DIR -> $src"
fi

log "=== Source drop-in ==="

mkdir -p "$EXAMPLE_DIR"

# Copy .cpp + CMakeLists.txt only if changed. We use cmp so we don't bump
# mtimes unnecessarily and trigger needless rebuilds on idempotent reruns.
for f in "$BIN_NAME.cpp" CMakeLists.txt; do
    if [ -f "$EXAMPLE_DIR/$f" ] && cmp -s "$KIT_DIR/$f" "$EXAMPLE_DIR/$f"; then
        skip "$f unchanged in $EXAMPLE_DIR"
    else
        cp "$KIT_DIR/$f" "$EXAMPLE_DIR/$f"
        ok "copied $f -> $EXAMPLE_DIR/"
    fi
done

# Sanity-check gotcha (a): defines must be present in the .cpp we just dropped.
if ! grep -qE '^[[:space:]]*#define[[:space:]]+MCAP_IMPLEMENTATION([[:space:]]|$)' "$EXAMPLE_DIR/$BIN_NAME.cpp" \
   || ! grep -qE '^[[:space:]]*#define[[:space:]]+MCAP_COMPRESSION_NO_LZ4([[:space:]]|$)' "$EXAMPLE_DIR/$BIN_NAME.cpp"; then
    die "$BIN_NAME.cpp is missing MCAP_IMPLEMENTATION / MCAP_COMPRESSION_NO_LZ4 defines — wrong file?"
fi

# Sanity-check gotcha (b): include path in the dropped CMakeLists must be ../../third_party.
if ! grep -q '\.\./\.\./third_party' "$EXAMPLE_DIR/CMakeLists.txt"; then
    die "CMakeLists.txt include path is not '../../third_party' — wrong file?"
fi

log "=== Parent CMakeLists.txt subdirectory line ==="

# Idempotent insert of `add_subdirectory(2.device.record.nogui_mcap)` directly
# after the existing `add_subdirectory(2.device.record.nogui)` line.
if grep -qE '^[[:space:]]*add_subdirectory\(2\.device\.record\.nogui_mcap\)[[:space:]]*$' "$PARENT_CMAKE"; then
    skip "parent CMakeLists already contains add_subdirectory(2.device.record.nogui_mcap)"
else
    if ! grep -qE '^[[:space:]]*add_subdirectory\(2\.device\.record\.nogui\)[[:space:]]*$' "$PARENT_CMAKE"; then
        die "parent CMakeLists has no add_subdirectory(2.device.record.nogui) line to insert after"
    fi
    cp "$PARENT_CMAKE" "$PARENT_CMAKE.bak.$TS"
    # Match the existing line preserving its leading whitespace, then append a
    # sibling line with the same indentation. Portable awk (works under mawk
    # and gawk): we re-derive indentation by stripping the trailing token.
    awk '
        BEGIN { inserted = 0 }
        {
            print
            if (!inserted && $0 ~ /^[[:space:]]*add_subdirectory\(2\.device\.record\.nogui\)[[:space:]]*$/) {
                indent = $0
                sub(/add_subdirectory\(2\.device\.record\.nogui\).*$/, "", indent)
                printf "%sadd_subdirectory(2.device.record.nogui_mcap)\n", indent
                inserted = 1
            }
        }
    ' "$PARENT_CMAKE.bak.$TS" > "$PARENT_CMAKE"
    grep -qE '^[[:space:]]*add_subdirectory\(2\.device\.record\.nogui_mcap\)[[:space:]]*$' "$PARENT_CMAKE" \
        || die "failed to insert add_subdirectory line"
    ok "inserted add_subdirectory(2.device.record.nogui_mcap)"
fi

# ---------- 3. Build -------------------------------------------------------

log "=== Build ==="
log "build approach: cmake directly against $SDK_ROOT/examples (NOT build_examples.sh)"
log "  reason: build_examples.sh wipes its build dir at the end (gotcha d),"
log "  which breaks idempotent re-runs. cmake-direct keeps incremental builds."

mkdir -p "$BUILD_DIR"

# Configure (idempotent — cmake re-uses existing CMakeCache when nothing changed)
cmake -S "$SDK_ROOT/examples" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release

# Build only our target.
cmake --build "$BUILD_DIR" --target "$BIN_NAME" -j"$(nproc)"

# Locate the built binary — its path under build/ depends on the SDK's example
# tree layout. Search rather than hardcode.
BUILT_BIN="$(find "$BUILD_DIR" -type f -name "$BIN_NAME" -perm -u+x 2>/dev/null | head -n1)"
[ -n "$BUILT_BIN" ] || die "could not locate built binary $BIN_NAME under $BUILD_DIR"
ok "built: $BUILT_BIN"

mkdir -p "$SDK_ROOT/bin"
install -m 0755 "$BUILT_BIN" "$BIN_INSTALL"
ok "installed: $BIN_INSTALL"

# ---------- 4. Smoke test --------------------------------------------------

if [ "${SKIP_SMOKE:-0}" = "1" ]; then
    log "=== Smoke test SKIPPED (SKIP_SMOKE=1) ==="
    log ""
    log "============================================================"
    log "PASS (build only — smoke skipped)"
    log "binary: $BIN_INSTALL"
    log "log:    $LOG"
    log "============================================================"
    exit 0
fi

log "=== Smoke test (~10s recording) ==="

command -v script >/dev/null 2>&1 \
    || die "util-linux 'script' not available — install bsdmainutils/util-linux"

SMOKE_DIR="$(mktemp -d -t mcap_smoke.XXXXXX)"
SMOKE_MCAP="$SMOKE_DIR/smoke_${TS}.mcap"
SMOKE_OUT="$SMOKE_DIR/out.log"
SMOKE_DURATION="${SMOKE_DURATION:-10}"

log "smoke output dir: $SMOKE_DIR"
log "smoke duration:   ${SMOKE_DURATION}s"

# We pipe two things into the binary's stdin via `script` (which gives us a pty
# so the SDK's keypress reader behaves the same as on the working Pi):
#   1. The output filename + newline (answers the initial getline prompt).
#   2. After SMOKE_DURATION seconds, an ESC byte (\033) which the .cpp's
#      stop loop accepts to exit cleanly, drain the writer, and print the
#      "Summary: ..." line we need to parse.
# We use `script -q -c CMD /dev/null` (util-linux flavor) and feed via a
# subshell that emits the filename, sleeps, then emits ESC.
#
# IMPORTANT: do NOT run under `set -e` failure semantics for the script
# command itself (`|| true`) — we want to inspect SMOKE_OUT regardless.

(
    printf '%s\n' "$SMOKE_MCAP"
    sleep "$SMOKE_DURATION"
    printf '\033'
    # Tiny grace period so the binary actually reads the ESC before the pipe
    # closes; closing too early can race the 200ms key-poll loop.
    sleep 2
) | script -q -c "$BIN_INSTALL" /dev/null > "$SMOKE_OUT" 2>&1 || true

log "--- last 30 lines of binary output ---"
tail -n 30 "$SMOKE_OUT" | sed 's/^/  | /'
log "--------------------------------------"

# ---------- 5. PASS/FAIL evaluation ---------------------------------------

reasons=()

# 5a) Color stream line
if grep -qF "Enabled stream: Color MJPG 1280x800@30" "$SMOKE_OUT"; then
    ok "smoke: color stream enabled at MJPG 1280x800@30"
else
    reasons+=("missing 'Enabled stream: Color MJPG 1280x800@30'")
fi

# 5b) Depth stream line
if grep -qF "Enabled stream: Depth Y16 1280x800@30" "$SMOKE_OUT"; then
    ok "smoke: depth stream enabled at Y16 1280x800@30"
else
    reasons+=("missing 'Enabled stream: Depth Y16 1280x800@30'")
fi

# 5c) Summary line present (means clean shutdown happened)
SUMMARY_LINE="$(grep -E '^Summary: color recv=' "$SMOKE_OUT" | tail -n1 || true)"
if [ -z "$SUMMARY_LINE" ]; then
    reasons+=("no 'Summary: ...' line — binary did not shut down cleanly (check $SMOKE_OUT)")
else
    log "smoke summary: $SUMMARY_LINE"

    # Parse counters out of the summary line. Format from the .cpp:
    #   Summary: color recv=N written=N dropsQ=N dropsW=N | depth recv=N written=N dropsQ=N dropsSize=N dropsW=N
    # The 'color' and 'depth' halves share counter names, so split on '|' first.
    color_part="${SUMMARY_LINE%%|*}"
    depth_part="${SUMMARY_LINE#*|}"

    cR="$(sed -n 's/.*color recv=\([0-9]\+\).*/\1/p' <<< "$color_part")"
    cW="$(sed -n 's/.*written=\([0-9]\+\).*/\1/p' <<< "$color_part")"
    cQ="$(sed -n 's/.*dropsQ=\([0-9]\+\).*/\1/p' <<< "$color_part")"
    cWE="$(sed -n 's/.*dropsW=\([0-9]\+\).*/\1/p' <<< "$color_part")"

    dR="$(sed -n 's/.*depth recv=\([0-9]\+\).*/\1/p' <<< "$depth_part")"
    dW="$(sed -n 's/.*written=\([0-9]\+\).*/\1/p' <<< "$depth_part")"
    dQ="$(sed -n 's/.*dropsQ=\([0-9]\+\).*/\1/p' <<< "$depth_part")"
    dS="$(sed -n 's/.*dropsSize=\([0-9]\+\).*/\1/p' <<< "$depth_part")"
    dWE="$(sed -n 's/.*dropsW=\([0-9]\+\).*/\1/p' <<< "$depth_part")"

    log "parsed: color recv=$cR written=$cW dropsQ=$cQ dropsW=$cWE"
    log "parsed: depth recv=$dR written=$dW dropsQ=$dQ dropsSize=$dS dropsW=$dWE"

    # All counters must be parseable.
    for v in "$cR" "$cW" "$cQ" "$cWE" "$dR" "$dW" "$dQ" "$dS" "$dWE"; do
        [ -n "$v" ] || { reasons+=("could not parse all counters from summary line"); break; }
    done

    if [ -n "${cR:-}" ] && [ "${cR:-0}" -eq 0 ] 2>/dev/null; then
        reasons+=("color recv=0 — no frames received from camera")
    fi
    if [ -n "${dR:-}" ] && [ "${dR:-0}" -eq 0 ] 2>/dev/null; then
        reasons+=("depth recv=0 — no frames received from camera")
    fi
    if [ -n "${cR:-}" ] && [ -n "${cW:-}" ] && [ "${cR:-0}" -ne "${cW:-0}" ] 2>/dev/null; then
        reasons+=("color recv($cR) != written($cW)")
    fi
    if [ -n "${dR:-}" ] && [ -n "${dW:-}" ] && [ "${dR:-0}" -ne "${dW:-0}" ] 2>/dev/null; then
        reasons+=("depth recv($dR) != written($dW)")
    fi
    for name in cQ cWE dQ dS dWE; do
        v="${!name:-0}"
        if [ "${v:-0}" -ne 0 ] 2>/dev/null; then
            reasons+=("non-zero drop counter: $name=$v")
        fi
    done
fi

# 5d) MCAP file produced and non-trivial in size
if [ -f "$SMOKE_MCAP" ]; then
    sz="$(stat -c%s "$SMOKE_MCAP")"
    log "smoke mcap: $SMOKE_MCAP (${sz} bytes)"
    if [ "$sz" -lt 100000 ]; then
        reasons+=("smoke .mcap is suspiciously small (${sz} bytes)")
    fi
else
    reasons+=("smoke .mcap not produced at $SMOKE_MCAP")
fi

# ---------- Final report ---------------------------------------------------

log ""
log "============================================================"
if [ "${#reasons[@]}" -eq 0 ]; then
    log "PASS — $(hostname)"
    log "binary: $BIN_INSTALL"
    log "smoke:  $SMOKE_MCAP"
    log "log:    $LOG"
    log "============================================================"
    # Keep smoke artifacts on PASS too — they're cheap and useful to spot-check.
    exit 0
else
    log "FAIL — $(hostname)"
    log "reasons:"
    for r in "${reasons[@]}"; do log "  - $r"; done
    log "smoke output: $SMOKE_OUT"
    log "log:          $LOG"
    log "============================================================"
    exit 1
fi
