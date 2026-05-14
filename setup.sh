#!/usr/bin/env bash
# setup.sh — One-command installer for Egocentric Capture V2 on Raspberry Pi 5.
#
# Run as the target user (e.g. autonexego8), NOT as root. The script calls
# sudo only where strictly required (apt install, systemd service install,
# /etc/udev/rules.d, etc).
#
# Idempotent: re-running this on a Pi where some/all steps are already done
# is safe and skips work that's already complete. If a step fails, fix the
# issue and re-run — completed steps will detect their state and skip.
#
# Prerequisites (the team's responsibility BEFORE running this):
#   1. Pi 5 running Debian 13 (trixie), Python 3.13.
#   2. AWS credentials placed at ~/.aws/credentials  (boto3 standard location).
#   3. Camera physically plugged in (smoke test runs by default).
#   4. Internet reachable (apt, pip, github.com, huggingface.co).
#   5. Repo cloned and you are running from its root: `bash setup.sh`.
#
# Optional env overrides:
#   SKIP_SMOKE=1       Skip the Orbbec camera smoke test during SDK install.
#                      Use this when installing on a bench without the camera.
#   SKIP_AWS_CHECK=1   Don't fail if ~/.aws/credentials is missing.
#                      Useful for dev/testing the script without real creds.
#   SKIP_REBOOT=1      Don't prompt to reboot at the end. (Reboot is needed
#                      for udev rules + 'video' group membership to apply.)

set -euo pipefail

# ────────────────────────────────────────────────────────────────────────
# Logging helpers — colored, timestamped, mirrored to a log file.
# ────────────────────────────────────────────────────────────────────────

TS="$(date +%Y%m%d_%H%M%S)"
LOG="/tmp/egocentric_setup_${TS}.log"
exec > >(tee -a "$LOG") 2>&1

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'
c_cyn=$'\033[36m'; c_off=$'\033[0m'

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '[%s] %sOK%s   %s\n' "$(date +%H:%M:%S)" "$c_grn" "$c_off" "$*"; }
skip() { printf '[%s] %sSKIP%s %s\n' "$(date +%H:%M:%S)" "$c_cyn" "$c_off" "$*"; }
warn() { printf '[%s] %sWARN%s %s\n' "$(date +%H:%M:%S)" "$c_ylw" "$c_off" "$*" >&2; }
die()  { printf '[%s] %sFAIL%s %s\n' "$(date +%H:%M:%S)" "$c_red" "$c_off" "$*" >&2; exit 1; }

section() { printf '\n%s═══ %s ═══%s\n' "$c_cyn" "$*" "$c_off"; }

# ────────────────────────────────────────────────────────────────────────
# Paths & constants
# ────────────────────────────────────────────────────────────────────────

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(whoami)"
HOME_DIR="$HOME"

# Names of the three top-level folders we ship in the repo
SDK_TARBALL_NAME="OrbbecSDK_v2.7.6_202602021228_d712cda_linux_arm64.tar.gz"
SDK_DIR_NAME="OrbbecSDK_v2.7.6_202602021228_d712cda_linux_arm64"

REPO_SDK_TARBALL="$REPO_DIR/orbbec-sdk/$SDK_TARBALL_NAME"
REPO_V2_DELIVERY="$REPO_DIR/v2_delivery"
REPO_PROJECT_DIR="$REPO_DIR/Egocentric_Data_capture_Hardware_mono16"
REPO_REQUIREMENTS="$REPO_DIR/requirements.txt"
REPO_SERVICE_TPL="$REPO_DIR/systemd/capture-daemon.service.template"

# Target locations on the Pi
SDK_INSTALL_DIR="$HOME_DIR/$SDK_DIR_NAME"
PROJECT_INSTALL_DIR="$HOME_DIR/Egocentric_Data_capture_Hardware_mono16"
V2_DELIVERY_INSTALL_DIR="$HOME_DIR/v2_delivery"
MODELS_DIR="$HOME_DIR/models"
YOLO_MODEL_PATH="$MODELS_DIR/yolov8n-hand.pt"
YOLO_MODEL_URL="https://huggingface.co/Bingsu/adetailer/resolve/main/hand_yolov8n.pt"
SSD_DIR="/mnt/ssd"

SERVICE_NAME="capture-daemon"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"

log "setup.sh starting on host=$(hostname) user=$USER_NAME"
log "repo:    $REPO_DIR"
log "logfile: $LOG"

# ────────────────────────────────────────────────────────────────────────
# Step 1 — Pre-flight checks
# ────────────────────────────────────────────────────────────────────────

section "Pre-flight checks"

# Refuse to run as root — we want files owned by the regular user.
if [ "$(id -u)" = "0" ]; then
    die "do not run setup.sh as root. Run as the target user (e.g. ${USER_NAME})."
fi

# Repo layout sanity — bail fast with a clear message if the user is
# running the script from the wrong directory or the repo is incomplete.
[ -f "$REPO_SDK_TARBALL" ]   || die "missing $REPO_SDK_TARBALL — run setup.sh from the repo root"
[ -d "$REPO_V2_DELIVERY" ]   || die "missing $REPO_V2_DELIVERY"
[ -d "$REPO_PROJECT_DIR" ]   || die "missing $REPO_PROJECT_DIR"
[ -f "$REPO_REQUIREMENTS" ]  || die "missing $REPO_REQUIREMENTS"
[ -f "$REPO_SERVICE_TPL" ]   || die "missing $REPO_SERVICE_TPL"
ok "repo layout OK"

# Platform sanity — these checks aren't load-bearing for the script logic,
# but they catch "wrong-shaped Pi" early before we waste 10 minutes building.
arch="$(uname -m)"
[ "$arch" = "aarch64" ] || die "expected aarch64, got $arch"
ok "architecture: $arch"

if [ -f /etc/os-release ]; then
    . /etc/os-release
    log "OS: ${PRETTY_NAME:-unknown}"
    [ "${ID:-}" = "debian" ] || warn "OS is not Debian — script tuned for Debian 13 trixie"
fi

py_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
log "Python: $py_version"
case "$py_version" in
    3.11|3.12|3.13) ok "Python $py_version is supported" ;;
    *) warn "Python $py_version is untested with this stack — proceeding anyway" ;;
esac

# AWS credentials check (the team's responsibility to place these).
if [ "${SKIP_AWS_CHECK:-0}" != "1" ]; then
    if [ ! -f "$HOME_DIR/.aws/credentials" ]; then
        die "AWS credentials missing at $HOME_DIR/.aws/credentials.

       Place the credentials file on the Pi BEFORE running setup, e.g.:
         scp ~/.aws/credentials ${USER_NAME}@$(hostname):~/.aws/credentials
       And the region config:
         scp ~/.aws/config      ${USER_NAME}@$(hostname):~/.aws/config

       Then re-run: bash setup.sh

       (To bypass this check for testing, set SKIP_AWS_CHECK=1.)"
    fi
    ok "AWS credentials found at $HOME_DIR/.aws/credentials"
else
    skip "AWS credentials check (SKIP_AWS_CHECK=1)"
fi

# Network reachability — bail early instead of dying mid-pip.
log "checking internet reachability..."
for host in github.com pypi.org huggingface.co; do
    if ! getent hosts "$host" >/dev/null 2>&1; then
        die "cannot resolve $host — internet required for install"
    fi
done
ok "internet reachable"

# Disk space — building the SDK + pulling pip wheels needs a few GB.
free_mb="$(df -Pm "$HOME_DIR" | awk 'NR==2 {print $4}')"
if [ "$free_mb" -lt 4096 ]; then
    die "insufficient free disk in \$HOME: ${free_mb}MB < 4096MB recommended"
fi
ok "disk: ${free_mb}MB free in \$HOME"

# sudo non-interactive probe — fail fast rather than half-way through apt.
if ! sudo -n true 2>/dev/null; then
    log "sudo will prompt for a password during install (apt, udev, systemd)..."
fi

# ────────────────────────────────────────────────────────────────────────
# Step 2 — Apt packages
# ────────────────────────────────────────────────────────────────────────

section "Apt packages"

APT_PKGS=(
    # build/runtime deps required by install_kit_v2 and the project
    build-essential cmake git pkg-config
    libzstd-dev nlohmann-json3-dev
    libusb-1.0-0 libusb-1.0-0-dev
    # multimedia + camera + Pi GPIO + Python
    ffmpeg libcamera-tools
    python3-pip python3-lgpio
    # OpenCV runtime (project uses cv2)
    libopencv-core410 libopencv-imgproc410 libopencv-imgcodecs410
    libopencv-videoio410 libopencv-highgui410
    # Misc
    util-linux       # provides `script` for the smoke test
    rsync            # used below for idempotent code copying
)

missing_pkgs=()
for p in "${APT_PKGS[@]}"; do
    if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed"; then
        :
    else
        missing_pkgs+=("$p")
    fi
done
if [ "${#missing_pkgs[@]}" -gt 0 ]; then
    log "installing missing apt packages: ${missing_pkgs[*]}"
    sudo apt-get update -y
    sudo apt-get install -y "${missing_pkgs[@]}"
    ok "apt deps installed (${#missing_pkgs[@]} packages)"
else
    skip "all apt packages already installed"
fi

# ────────────────────────────────────────────────────────────────────────
# Step 3 — Extract Orbbec SDK tarball
# ────────────────────────────────────────────────────────────────────────

section "Orbbec SDK"

if [ -d "$SDK_INSTALL_DIR" ] && [ -f "$SDK_INSTALL_DIR/shared/99-obsensor-libusb.rules" ]; then
    skip "SDK already extracted at $SDK_INSTALL_DIR"
else
    log "extracting $SDK_TARBALL_NAME to $HOME_DIR"
    tar -xzf "$REPO_SDK_TARBALL" -C "$HOME_DIR"
    [ -d "$SDK_INSTALL_DIR" ] \
        || die "extraction completed but $SDK_INSTALL_DIR not present — tarball layout unexpected"
    ok "SDK extracted to $SDK_INSTALL_DIR"
fi

# ────────────────────────────────────────────────────────────────────────
# Step 4 — Copy v2_delivery into place
# ────────────────────────────────────────────────────────────────────────

section "v2_delivery (install kit + binaries)"

# rsync -a preserves perms + timestamps; --checksum makes it idempotent
# without depending on clock alignment.
rsync -a --checksum "$REPO_V2_DELIVERY/" "$V2_DELIVERY_INSTALL_DIR/"
chmod +x "$V2_DELIVERY_INSTALL_DIR/install_kit_v2/install.sh" 2>/dev/null || true
ok "v2_delivery synced to $V2_DELIVERY_INSTALL_DIR"

# ────────────────────────────────────────────────────────────────────────
# Step 5 — Copy project code into place
# ────────────────────────────────────────────────────────────────────────

section "Project code"

# Same rsync pattern. Code lives at ~/Egocentric_Data_capture_Hardware_mono16
# because that's what the systemd service WorkingDirectory points to, and
# what your existing Pi 8 setup uses — keeping the path identical across
# all 30 Pis means no per-Pi service file edits.
rsync -a --checksum "$REPO_PROJECT_DIR/" "$PROJECT_INSTALL_DIR/"
ok "code synced to $PROJECT_INSTALL_DIR"

# ────────────────────────────────────────────────────────────────────────
# Step 6 — Run install_kit_v2/install.sh (SDK addon build + udev + smoke)
# ────────────────────────────────────────────────────────────────────────

section "Build Orbbec custom binaries (calls install_kit_v2)"

# The kit's install.sh handles:
#   - apt deps it needs (libzstd-dev, nlohmann-json3-dev, etc — already done above)
#   - udev rules installation
#   - /mnt/ssd/recordings creation
#   - sparse-cloning MCAP headers from foxglove
#   - patching SDK CMakeLists.txt
#   - building ob_device_record_mcap_nogui + ob_color_jpeg_stream
#   - optional smoke test (~10s recording)
#
# We control the smoke test via SKIP_SMOKE env var. Default: smoke ON.

KIT_INSTALL="$V2_DELIVERY_INSTALL_DIR/install_kit_v2/install.sh"
[ -x "$KIT_INSTALL" ] || chmod +x "$KIT_INSTALL"

# Export SDK path so the kit picks up the right SDK location.
export SDK_ROOT="$SDK_INSTALL_DIR"

if [ "${SKIP_SMOKE:-0}" = "1" ]; then
    log "running install_kit_v2 with SKIP_SMOKE=1 (camera test disabled)"
    SKIP_SMOKE=1 bash "$KIT_INSTALL" || die "install_kit_v2 failed (see output above and $LOG)"
else
    log "running install_kit_v2 with camera smoke test enabled"
    log "  if this hangs, the Orbbec camera may not be plugged in."
    log "  Ctrl+C and re-run with SKIP_SMOKE=1 to skip the camera test."
    bash "$KIT_INSTALL" || die "install_kit_v2 failed (see output above and $LOG)"
fi
ok "Orbbec SDK custom binaries built and installed"

# ────────────────────────────────────────────────────────────────────────
# Step 7 — Add user to video + plugdev groups
# ────────────────────────────────────────────────────────────────────────

section "User groups"

# Camera USB access (udev rules grant GROUP=video,plugdev). Membership
# takes effect on next login / reboot.
for grp in video plugdev; do
    if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$grp"; then
        skip "$USER_NAME already in group $grp"
    else
        sudo usermod -aG "$grp" "$USER_NAME"
        ok "added $USER_NAME to group $grp (effective after reboot)"
    fi
done

# ────────────────────────────────────────────────────────────────────────
# Step 8 — Python packages (project deps only — not the kitchen sink)
# ────────────────────────────────────────────────────────────────────────

section "Python packages"

# Debian 13 + Python 3.13 marks the system Python as "externally managed"
# (PEP 668). We bypass with --break-system-packages because this Pi is
# single-purpose: the capture daemon IS the workload. A venv would add
# a layer of indirection to systemd, paths, and team mental model with
# no offsetting benefit for a single-tenant device.
PIP_FLAGS=(--break-system-packages --upgrade --no-warn-script-location)

log "installing project Python deps (this may take 5-15 min on first run)..."
python3 -m pip install "${PIP_FLAGS[@]}" -r "$REPO_REQUIREMENTS" \
    || die "pip install failed (see output above)"
ok "Python deps installed"

# ────────────────────────────────────────────────────────────────────────
# Step 9 — YOLO model weights
# ────────────────────────────────────────────────────────────────────────

section "YOLO hand-detection model"

mkdir -p "$MODELS_DIR"

if [ -f "$YOLO_MODEL_PATH" ] && [ "$(stat -c%s "$YOLO_MODEL_PATH")" -gt 1000000 ]; then
    skip "YOLO model already present at $YOLO_MODEL_PATH ($(stat -c%s "$YOLO_MODEL_PATH") bytes)"
else
    log "downloading YOLO hand model from Hugging Face..."
    # -L follows redirects (HF uses an S3 CDN); --fail makes curl exit non-zero
    # on HTTP errors instead of saving an HTML error page as the .pt file.
    curl -L --fail --progress-bar -o "$YOLO_MODEL_PATH" "$YOLO_MODEL_URL" \
        || die "YOLO model download failed"
    sz="$(stat -c%s "$YOLO_MODEL_PATH")"
    [ "$sz" -gt 1000000 ] \
        || die "downloaded YOLO model is too small ($sz bytes) — probably an error page"
    ok "YOLO model downloaded ($sz bytes) to $YOLO_MODEL_PATH"
fi

# ────────────────────────────────────────────────────────────────────────
# Step 10 — Systemd service
# ────────────────────────────────────────────────────────────────────────

section "Systemd service"

# Template the service file with the current user and install it.
# Pre-existing service files are compared with cmp so we don't reload
# systemd or restart the daemon unless the content actually changed.

TMP_SERVICE="$(mktemp)"
sed -e "s|{{USER}}|$USER_NAME|g" \
    -e "s|{{HOME}}|$HOME_DIR|g" \
    -e "s|{{SDK_LIB}}|$SDK_INSTALL_DIR/lib|g" \
    -e "s|{{PROJECT_DIR}}|$PROJECT_INSTALL_DIR|g" \
    "$REPO_SERVICE_TPL" > "$TMP_SERVICE"

if [ -f "$SERVICE_DST" ] && sudo cmp -s "$TMP_SERVICE" "$SERVICE_DST"; then
    skip "systemd service already up-to-date at $SERVICE_DST"
else
    sudo install -m 0644 "$TMP_SERVICE" "$SERVICE_DST"
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
    ok "systemd service installed at $SERVICE_DST"
fi
rm -f "$TMP_SERVICE"

# ────────────────────────────────────────────────────────────────────────
# Step 11 — Final summary
# ────────────────────────────────────────────────────────────────────────

section "Summary"

cat <<EOF

${c_grn}╔══════════════════════════════════════════════════════════════╗
║  Egocentric Capture V2 — setup complete on $(hostname)
╚══════════════════════════════════════════════════════════════╝${c_off}

  user           : $USER_NAME
  SDK            : $SDK_INSTALL_DIR
  code           : $PROJECT_INSTALL_DIR
  YOLO model     : $YOLO_MODEL_PATH
  systemd unit   : $SERVICE_DST
  logfile        : $LOG

Next steps:
  1. Reboot the Pi (required for udev rules + group membership).
     ${c_ylw}sudo reboot${c_off}

  2. After reboot, the capture-daemon service auto-starts on boot.
     Verify with:
       sudo systemctl status capture-daemon
       sudo journalctl -u capture-daemon -f

  3. Open http://$(hostname).local:8080 in a browser to access the UI.

EOF

if [ "${SKIP_REBOOT:-0}" != "1" ]; then
    read -r -p "Reboot now? [y/N] " ans || true
    case "${ans:-N}" in
        y|Y|yes|YES)
            log "rebooting in 3 seconds..."
            sleep 3
            sudo reboot
            ;;
        *)
            log "skipping reboot — remember to reboot manually before using the camera"
            ;;
    esac
fi
