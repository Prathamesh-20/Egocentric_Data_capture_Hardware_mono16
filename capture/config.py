"""
Central config — edit this file only for hardware changes.
V2-Simplified: 30-min session → sequential 1-min segments.
Orbbec Gemini 2L only (RGB + Depth). No Kreo cameras, no FOV/wrist checks.
"""
import os

# ── Paths ─────────────────────────────────────────────────────────
OUTPUT_DIR   = "/mnt/ssd/recordings"
ORBBEC_REC   = os.path.expanduser("~/OrbbecSDK_v2.7.6_202602021228_d712cda_linux_arm64/bin/ob_device_record_nogui")
ORBBEC_LIB   = os.path.expanduser("~/OrbbecSDK_v2.7.6_202602021228_d712cda_linux_arm64/lib")

# ── Camera devices (Kreo disabled) ────────────────────────────────
KREO_ENABLED = False               # <<< Kreo cameras disabled
KREO1_DEVICE = "/dev/video_kreo_1"
KREO2_DEVICE = "/dev/video_kreo_2"
KREO_W       = 1280
KREO_H       = 720

# ── Capture settings ──────────────────────────────────────────────
FPS                  = 30
SEGMENT_DURATION     = 60          # seconds per video segment (1 min)
SESSION_DURATION     = 1800        # seconds total session (30 min)
FOV_CHECK_SECS       = 5

# ── FOV / wrist check (DISABLED) ─────────────────────────────────
FOV_CHECK_ENABLED    = False       # <<< FOV/wrist check disabled
FOV_MIN_DETECTION_FRAMES = 10
YOLO_MODEL_PATH    = os.path.expanduser("~/models/yolov8n-pose.pt")
YOLO_CONF_THRESH   = 0.3
WRIST_KP_INDICES   = [9, 10]
WRIST_KP_CONF      = 0.3

# HSV fallback skin detection
SKIN_LOWER      = (0,   20,  70)
SKIN_UPPER      = (20, 255, 255)
MIN_REGION_PX   = 800
MIN_REGIONS     = 2

# ── GPIO pins ─────────────────────────────────────────────────────
# ── GPIO pins (NAYAA Hat Rev 2) ───────────────────────────────────
PIN_SW1       = 17    # Start session button (Pin 11)
PIN_SW2       = 27    # Stop session button (Pin 13)
PIN_LED_GREEN = 22    # L1 — Green LED (Pin 15)
PIN_LED_RED   = 5     # L2 — Red LED (Pin 29)
PIN_BUZZER    = 26    # Buzzer (Pin 37)
DEBOUNCE_S    = 0.3   # button debounce (300ms)

# Inter-segment gap
SEGMENT_GAP   = 5     # seconds to wait between segments

# ── Web UI ────────────────────────────────────────────────────────
UI_HOST = "0.0.0.0"
UI_PORT = 8080

# ── Orbbec stream (not used when FOV disabled) ───────────────────
ORBBEC_STREAM    = "/mnt/ssd/OrbbecSDK_Pi5/orbbec_stream"
ORBBEC_STREAM_LIB = "/opt/OrbbecSDK/lib"

# ── S3 upload ─────────────────────────────────────────────────────
S3_BUCKET        = "egocentric-datacollection1"
S3_PREFIX        = "captures/"
S3_MAX_RETRIES   = 3
S3_RETRY_DELAY   = 10

# ── Combined grid tile size ───────────────────────────────────────
TILE_W = 640
TILE_H = 360

# ── MCAP conversion (DISABLED) ───────────────────────────────────
MCAP_ENABLED_DEFAULT = False
