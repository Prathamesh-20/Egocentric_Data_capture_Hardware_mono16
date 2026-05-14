"""
Central configuration — edit this file only for hardware changes.

V2.1 Capture Pipeline (merged build):
  • 30-min sessions, sequential 1-min MCAP segments.
  • Orbbec Gemini 2L only (RGB MJPG + Depth Y16).
  • S3 upload with persistent queue, never-give-up retry, multipart.
  • Optional FOV pre-check with live MJPEG preview (re-enabled in this build).

This file is the SINGLE place to change hardware/site config. Anything else
should be changeable from the dashboard or HTTP API at runtime.
"""
import os
import getpass


# ── Paths ─────────────────────────────────────────────────────────
OUTPUT_DIR = "/mnt/ssd/recordings"

# Resolve the user owning the OrbbecSDK installation. Defaults to the
# currently-logged-in user, but can be overridden by ORBBEC_USER env var
# (useful when running under sudo / systemd as a different user).
_ORBBEC_USER = os.environ.get("ORBBEC_USER") or getpass.getuser()
_ORBBEC_SDK  = f"/home/{_ORBBEC_USER}/OrbbecSDK_v2.7.6_202602021228_d712cda_linux_arm64"

# The C++ recorder binary, built and installed by the install kit.
ORBBEC_REC = f"{_ORBBEC_SDK}/bin/ob_device_record_mcap_nogui"
ORBBEC_LIB = f"{_ORBBEC_SDK}/lib"


# ── Camera devices (Kreo disabled) ────────────────────────────────
KREO_ENABLED = False
KREO1_DEVICE = "/dev/video_kreo_1"
KREO2_DEVICE = "/dev/video_kreo_2"
KREO_W       = 1280
KREO_H       = 720


# ── Capture settings ──────────────────────────────────────────────
FPS              = 30
SEGMENT_DURATION = 60      # seconds per segment (1 min)
SESSION_DURATION = 1800    # seconds total per session (30 min)


# ── FOV / wrist check ────────────────────────────────────────────
# Pre-session pass/fail check that the operator's hands are visible.
# Now ENABLED by default with a live MJPEG preview rendered in the
# dashboard so the operator can see what the camera sees.
#
# Behaviour when on:
#   1. POST /fov-check launches the helper binary (`ob_color_jpeg_stream`)
#   2. The dashboard pulls /fov-stream as multipart/x-mixed-replace MJPEG
#      and renders it in the preview card
#   3. Each decoded frame runs YOLO hand-detection (or HSV fallback)
#   4. After FOV_CHECK_SECS+startup, returns pass/fail based on whether at
#      least FOV_MIN_DETECTION_FRAMES contained both wrists
#
# Set FOV_CHECK_ENABLED=False to skip the check entirely (server returns
# fov_passed immediately, button is hidden).
FOV_CHECK_ENABLED            = True
FOV_CHECK_SECS               = 5     # initial pre-session check duration
FOV_MIN_DETECTION_FRAMES     = 10

# Between-segment one-shot FOV check. Runs after each segment finishes
# (and after the recorder releases the USB device). Shorter than the
# initial check since the camera AE has already settled. Same buzzer
# patterns and live MJPEG preview as the initial check.
WRIST_CHECK_BETWEEN_SEGMENTS = True
INTER_SEGMENT_FOV_CHECK_SECS = 3
# Minimum "both hands visible" frames the inter-segment check needs to
# pass. Lower than the initial check's threshold because the check
# itself is shorter. At ~10 fps YOLO this gives ~50% pass coverage.
INTER_SEGMENT_MIN_DETECTION_FRAMES = 5
# What to do if the inter-segment check fails:
#   "warn"  — log a warning, record the segment anyway (operator hears
#             the buzzer warning during the 3s check window)
#   "skip"  — abandon this segment slot, move on to the next
#   "wait"  — keep beeping and re-running the check until it passes
# Default is "warn" so we never lose a segment to a momentary YOLO miss.
INTER_SEGMENT_ON_FAIL        = "warn"

# YOLO model for hand bounding-box detection (primary).
#
# Was previously yolov8n-pose.pt, which detects a person first and then
# emits 17 COCO keypoints (wrists at indices 9, 10). In an egocentric
# rig the camera rarely sees enough of the body for the person box to
# fire, so the wrist keypoints never come out. We now use a YOLO model
# trained to detect HANDS directly as a single object class — no
# person box required.
#
# Drop a hand-detection weights file at this path. Common choices:
#   - A YOLOv8n model fine-tuned on a hand dataset (egohands, 100DOH,
#     hagrid, etc). Single-class "hand" output works fine.
#   - Any YOLOv8/YOLOv5/YOLOv11 weights as long as "hand" is the only
#     class, or class 0 is hand.
YOLO_MODEL_PATH  = os.path.expanduser("~/models/yolov8n-hand.pt")
YOLO_CONF_THRESH = 0.35       # per-box confidence gate for hand detections

# Left/right assignment uses x-position in the frame. The midline is
# frame_width / 2. If two hands are detected, the one with smaller cx
# is labeled L, the larger cx is R. To keep labels from flipping
# frame-to-frame when hands are very close together (crossed or near
# the midline), we require a minimum x-separation before we'll commit
# to an L/R split. Below this, both boxes are labeled generically
# without committing to a side.
HAND_LR_MARGIN_PX = 30

# ── Legacy (unused after hand-detector switch, kept for import-compat) ──
# These were the COCO wrist keypoint indices and a stricter keypoint
# confidence used by the old YOLOv8-pose path. They are no longer read
# by fov_check.py but remain importable so existing `from capture.config
# import ...` lines elsewhere keep working without edits.
WRIST_KP_INDICES = [9, 10]
WRIST_KP_CONF    = 0.5

# HSV skin-color fallback when YOLO is unavailable.
SKIN_LOWER       = (0,   20,  70)
SKIN_UPPER       = (20, 255, 255)
MIN_REGION_PX    = 800
MIN_REGIONS      = 2


# ── GPIO pins (NAYAA Hat Rev 2) ───────────────────────────────────
PIN_SW1       = 17    # Start session button (header pin 11)
PIN_SW2       = 27    # Stop session button (header pin 13)
PIN_LED_GREEN = 22    # L1 — Green LED (header pin 15)
PIN_LED_RED   = 5     # L2 — Red LED (header pin 29)
PIN_BUZZER    = 26    # Buzzer (header pin 37)
DEBOUNCE_S    = 0.3


# ── Inter-segment gap ─────────────────────────────────────────────
SEGMENT_GAP = 5


# ── Web UI / HTTP API ─────────────────────────────────────────────
UI_HOST = "0.0.0.0"
UI_PORT = 8080


# ── Orbbec MJPEG stdout streamer (additive SDK helper) ────────────
# Built and installed by install_kit_v2. Emits color frames on stdout
# in the FRAME COLOR ... protocol consumed by fov_check.py. Running it
# briefly claims the camera over USB; ORBBEC_DEVICE_RELEASE_S is the
# empirical wait between releasing one Orbbec consumer and starting
# another (Gemini 2L on Pi 5 USB3).
ORBBEC_STREAM           = f"{_ORBBEC_SDK}/bin/ob_color_jpeg_stream"
ORBBEC_STREAM_LIB       = ORBBEC_LIB
ORBBEC_DEVICE_RELEASE_S = 6.0


# ── S3 upload ─────────────────────────────────────────────────────
# Production bucket. The site's actual bucket is in ap-south-1 (Mumbai).
# Region is NOT hardcoded here; boto3 resolves it from (in order):
#   1. AWS_DEFAULT_REGION env var
#   2. ~/.aws/config (configure with `aws configure set region ap-south-1`)
# Set the region one of those two ways or boto3 will get a
# `PermanentRedirect` error on the first upload.
S3_BUCKET = os.environ.get("AWS_BUCKET_NAME") or "ego-data-collection-encord-india"
S3_PREFIX = "raw-feed/egocentric/"

# Retry policy — tuned for unreliable rural / mobile-hotspot links:
# we NEVER give up on an item, only back off harder. The queue can
# hold items for hours/days through full network outages and resume
# cleanly the moment connectivity returns.
S3_RETRY_BACKOFF_INITIAL_S = 5         # first retry waits this long
S3_RETRY_BACKOFF_MAX_S     = 300       # cap at 5 min/attempt
S3_RETRY_BACKOFF_FACTOR    = 2.0       # multiplier per failed attempt

# Persistent queue — survives daemon restart / Pi reboot. JSON file at
# this path tracks every queued/in-flight item; on startup the uploader
# reloads it and resumes any unfinished uploads automatically.
S3_QUEUE_STATE_FILE = os.path.join(OUTPUT_DIR, ".upload_queue_state.json")

# Concurrency — two parallel transfers smooth over per-call overhead on
# slow links without saturating CPU. Each transfer itself is multipart
# (handled by boto3's TransferConfig) so large files stream in chunks.
S3_CONCURRENT_UPLOADS    = 2
S3_MULTIPART_CHUNK_BYTES = 8 * 1024 * 1024     # 8 MB chunks
S3_MULTIPART_THRESHOLD   = 16 * 1024 * 1024    # files > 16 MB use multipart

# Auto-delete local files from SSD after confirmed S3 upload (head_object).
DELETE_AFTER_UPLOAD = True

# Legacy names kept so any external code/import still resolves; uploader.py
# uses the BACKOFF_* triple above, not these.
S3_MAX_RETRIES = 0     # 0 = unlimited (treated as never-give-up)
S3_RETRY_DELAY = S3_RETRY_BACKOFF_INITIAL_S


# ── Combined grid tile size (legacy postprocess.py only) ──────────
TILE_W = 640
TILE_H = 360


# ── MCAP conversion (DISABLED) ────────────────────────────────────
MCAP_ENABLED_DEFAULT = False


# ── Logging ───────────────────────────────────────────────────────
LOG_DIR          = "/mnt/ssd/logs"
LOG_FILE         = os.path.join(LOG_DIR, "capture_daemon.log")
LOG_MAX_BYTES    = 10 * 1024 * 1024   # 10 MB per file
LOG_BACKUP_COUNT = 5


# ── Recorder hardening ────────────────────────────────────────────
# Time to wait for the recorder's "Summary:" line after we send 'q'.
# That line is printed only AFTER the writer queue has drained and the
# MCAP footer is on disk. Empirically a 60s segment at 1280x800@30
# takes 3-6s to drain on Pi 5; 30s gives ample margin and matches the
# generous timeout the platform-side merged build expects.
ORBBEC_RECORDER_DRAIN_S = 30
# Max time to wait for the recorder to print the "started" handshake.
ORBBEC_RECORDER_START_S = 30
# Disk-space floor (GB) below which a session refuses to start the
# next segment. The current segment in progress is allowed to complete.
MIN_DISK_SPACE_GB = 5
# Smallest acceptable MCAP segment in MB. Smaller files are deleted
# locally and never uploaded. Tuned for 1280x800@30 zstd-compressed
# Y16 depth + MJPG color over 60s.
MIN_SEGMENT_SIZE_MB = 600
