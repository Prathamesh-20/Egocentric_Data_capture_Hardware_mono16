"""
FOV check — pre-capture wrist validation.

Two callback channels:
  - frame_cb(annotated_jpeg_bytes, detected: bool)
        Called for each annotated frame. The JPEG bytes go straight
        into the dashboard's /fov-stream MJPEG endpoint, so operators
        can visually confirm hands are in frame. Use None to skip
        annotation/encoding entirely (cheaper).
  - progress_cb(FOVProgress)
        Called ~2 Hz with counters only — no image data. Used by the
        platform layer / state envelope.

Detection method:
  - Primary: YOLOv8 hand-detection (single-class hand bounding boxes,
    no person detection step). Left/right is assigned from box x-position
    in the frame, with a small dead-zone (HAND_LR_MARGIN_PX) to avoid
    L/R label flicker when hands are close together near the midline.
  - Fallback: HSV skin-blob when YOLO is unavailable (model file
    missing or `ultralytics` not installed).

How frames are obtained:
  The pre-built helper binary `ob_color_jpeg_stream` (built by
  install_kit_v2) emits MJPEG color frames to stdout in this protocol:
        FRAME COLOR <ts_us> <fid> <w> <h> <fmt> <data_size>\\n
        <data_size bytes of JPEG>
  fov_check.py parses that stream over a PTY (raw mode) so it can
  co-exist with the SDK's terminal handling. The binary is launched
  briefly and terminated before the recorder claims the camera.

Robustness:
  - Idempotent shutdown — multiple cancel/cleanup calls are safe.
  - Buffer is reset between runs so a stale tail can't desync the
    next invocation.
  - All file descriptors are owned in clearly-defined try/finally
    blocks so a failure in any step cannot leak fds.
"""
import logging
import os
import pty
import select
import subprocess  # MUST be at top — was at bottom in old build
import termios
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import cv2
import numpy as np

from capture.config import (
    YOLO_MODEL_PATH, YOLO_CONF_THRESH, WRIST_KP_INDICES, WRIST_KP_CONF,
    HAND_LR_MARGIN_PX,
    SKIN_LOWER, SKIN_UPPER, MIN_REGION_PX, MIN_REGIONS,
    ORBBEC_STREAM, ORBBEC_STREAM_LIB, ORBBEC_DEVICE_RELEASE_S,
)

log = logging.getLogger(__name__)

SKIN_LOWER_NP = np.array(SKIN_LOWER, dtype=np.uint8)
SKIN_UPPER_NP = np.array(SKIN_UPPER, dtype=np.uint8)

# Time the helper binary needs after launch to enumerate the camera
# and start producing frames. Adds to the wall-clock duration of run().
STARTUP_SECS = 3

# Auto-exposure settling parameters
AE_WINDOW_SIZE     = 5
AE_VARIANCE_THRESH = 15.0
AE_MIN_BRIGHTNESS  = 25
AE_MAX_SETTLE_SEC  = 6.0

# Tracks the wall-clock time at which the FOV streamer last released
# the Orbbec USB device. orbbec.py reads this to decide how long to
# wait before its own pipeline starts.
_last_stop_time      = 0.0
_last_stop_time_lock = threading.Lock()


def _set_last_stop_time(t: float) -> None:
    global _last_stop_time
    with _last_stop_time_lock:
        _last_stop_time = t


def get_last_stop_time() -> float:
    """Public accessor used by orbbec.py for the device-release wait."""
    with _last_stop_time_lock:
        return _last_stop_time


# ── YOLO model lazy-load ──────────────────────────────────────────
_yolo_model: object = None
_yolo_loaded         = False
_yolo_lock           = threading.Lock()


def _load_yolo():
    """Lazy-load YOLO hand-detection model. Returns model or None. Thread-safe."""
    global _yolo_model, _yolo_loaded
    with _yolo_lock:
        if _yolo_loaded:
            return _yolo_model
        _yolo_loaded = True
        try:
            from ultralytics import YOLO
            if os.path.exists(YOLO_MODEL_PATH):
                _yolo_model = YOLO(YOLO_MODEL_PATH)
                log.info(f"YOLO hand-detector loaded from {YOLO_MODEL_PATH}")
            else:
                log.warning(
                    f"YOLO model not found at {YOLO_MODEL_PATH}, "
                    f"using HSV fallback"
                )
        except ImportError:
            log.warning("ultralytics not installed, using HSV fallback")
        except Exception as e:
            log.warning(f"YOLO load failed: {e}, using HSV fallback")
        return _yolo_model


# ── Auto-exposure settling ────────────────────────────────────────
class ExposureSettler:
    """Tracks frame brightness; reports settled when variance drops.

    If `skip_settle=True`, treats every frame as settled from the start.
    Used for inter-segment FOV checks where the camera AE was running
    just seconds ago and doesn't need to re-converge.
    """

    def __init__(self,
                 window_size=AE_WINDOW_SIZE,
                 variance_thresh=AE_VARIANCE_THRESH,
                 min_brightness=AE_MIN_BRIGHTNESS,
                 max_settle_sec=AE_MAX_SETTLE_SEC,
                 skip_settle: bool = False):
        self.window_size     = window_size
        self.variance_thresh = variance_thresh
        self.min_brightness  = min_brightness
        self.max_settle_sec  = max_settle_sec
        self._history: list  = []
        self._settled        = bool(skip_settle)
        self._start_time     = time.time()

    def feed(self, frame: np.ndarray) -> bool:
        if self._settled:
            return True
        try:
            mean = float(np.mean(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))
        except cv2.error as e:
            log.warning(f"ExposureSettler: cvtColor failed: {e}")
            return False

        self._history.append(mean)
        if len(self._history) > self.window_size:
            self._history = self._history[-self.window_size:]

        if len(self._history) < self.window_size:
            return False

        variance   = float(np.var(self._history))
        avg_bright = float(np.mean(self._history))

        if avg_bright >= self.min_brightness and variance <= self.variance_thresh:
            self._settled = True
            log.info(
                f"AE settled — brightness={avg_bright:.0f} "
                f"variance={variance:.1f}"
            )
            return True

        if time.time() - self._start_time > self.max_settle_sec:
            self._settled = True
            log.warning(
                f"AE forced settle after {self.max_settle_sec}s "
                f"brightness={avg_bright:.0f} variance={variance:.1f}"
            )
            return True

        return False

    @property
    def is_settled(self) -> bool:
        return self._settled

    @property
    def status_text(self) -> str:
        if self._settled:
            return "EXPOSURE OK"
        n      = len(self._history)
        bright = self._history[-1] if self._history else 0
        return (f"EXPOSURE SETTLING ({n}/{self.window_size}) "
                f"bright={bright:.0f}")


# ── Result types ──────────────────────────────────────────────────
@dataclass
class FOVResult:
    passed:            bool
    frames_checked:    int
    frames_with_hands: int
    message:           str
    method:            str = "unknown"  # "yolo" or "hsv"


@dataclass
class FrameCheckResult:
    passed:    bool
    message:   str
    method:    str = "unknown"
    annotated: Optional[np.ndarray] = None    # not exposed via API


@dataclass
class FOVProgress:
    """Periodic snapshot for /fov-check polling. No image data."""
    elapsed_sec:        float
    total_sec:          float
    settled:            bool
    frames_checked:     int
    frames_with_hands:  int
    last_n_wrists:      int    # legacy field, equal to hands_in_frame
    hands_in_frame:     int    # 0, 1, or 2 — drives the buzzer alarm
    method:             str


# ── Detection ─────────────────────────────────────────────────────
def detect_wrists_yolo(frame: np.ndarray) -> Tuple[Optional[bool], np.ndarray, int]:
    """Detect hands directly using a YOLO hand-detection model.

    Returns (detected, annotated_frame, hands_in_frame).
      - detected=None signals model unavailable; caller falls back to HSV.
      - hands_in_frame is the COUNT of distinct visible hands, capped at 2.

    Why this no longer uses YOLOv8-pose:
      The previous implementation ran YOLOv8n-pose, which detects a
      PERSON bounding box first and then emits a 17-keypoint skeleton
      including wrists at COCO indices 9 (L) and 10 (R). On an
      egocentric rig the camera rarely sees enough of the body for
      the person detector to fire, so the wrist keypoints never
      materialise. We now use a YOLO model trained to detect HANDS
      as a standalone object class — no person box dependency.

    Left/right assignment:
      The hand-detection model itself does not know which hand is L
      vs R. We assign it from the box centroid's x-position in the
      frame: smaller cx = L-Wrist, larger cx = R-Wrist. To avoid the
      labels flickering when both hands sit near the midline or
      briefly cross over, we require an x-separation of at least
      HAND_LR_MARGIN_PX before committing to the split; below that
      both boxes are labeled "Hand" without a side commitment.

    Return-value contract (unchanged from the pose version, so the
    rest of fov_check.py and session_v2.py don't need to change):
      - hands_in_frame is capped at 2.
      - detected == (hands_in_frame >= MIN_REGIONS).
      - The annotated frame has the accepted hand markers drawn on it.
    """
    model = _load_yolo()
    if model is None:
        return None, frame, 0

    vis = frame.copy()
    try:
        results = model(frame, conf=YOLO_CONF_THRESH, verbose=False)
    except Exception as e:
        log.warning(f"YOLO inference failed: {e}, falling back to HSV")
        return None, frame, 0

    H, W = frame.shape[:2]
    inset = 8         # px from edge — boxes touching the edge are suspect

    # Collect candidate hand boxes: (cx, cy, conf, (x1,y1,x2,y2))
    candidates = []
    for r in results:
        boxes = getattr(r, "boxes", None)
        if boxes is None:
            continue
        for box in boxes:
            try:
                x1, y1, x2, y2 = map(float, box.xyxy[0])
                conf = float(box.conf[0]) if box.conf is not None else 0.0
            except Exception:
                continue

            # Confidence gate already applied by ultralytics via conf=,
            # but re-check defensively in case caller injects a model
            # without that pre-filter.
            if conf < YOLO_CONF_THRESH:
                continue

            cx = 0.5 * (x1 + x2)
            cy = 0.5 * (y1 + y2)

            # Reject boxes whose centroid is essentially off-frame.
            if cx < inset or cy < inset or cx > W - inset or cy > H - inset:
                continue

            candidates.append((cx, cy, conf, (x1, y1, x2, y2)))

    # Keep the two highest-confidence hand boxes. We're tracking the
    # operator's pair of hands, not counting all hands in the scene;
    # a bystander's hand should NOT silence the alarm, so the cap is
    # firm at 2.
    candidates.sort(key=lambda t: t[2], reverse=True)
    top = candidates[:2]

    # Assign L/R from x-position with a dead-zone.
    accepted = []  # list of (cx, cy, label, box)
    if len(top) == 2:
        a, b = top
        # Sort the pair left-to-right by cx so the leftmost is L.
        left, right = sorted([a, b], key=lambda t: t[0])
        if (right[0] - left[0]) >= HAND_LR_MARGIN_PX:
            accepted.append((left[0],  left[1],  "L-Wrist", left[3]))
            accepted.append((right[0], right[1], "R-Wrist", right[3]))
        else:
            # Too close together to commit to a side this frame.
            # Still counts as two hands for hands_in_frame purposes.
            accepted.append((left[0],  left[1],  "Hand", left[3]))
            accepted.append((right[0], right[1], "Hand", right[3]))
    elif len(top) == 1:
        cx, cy, _, box = top[0]
        # With only one hand we still expose a side guess based on
        # which half of the frame it sits in. The egocentric camera
        # midline is frame_width/2.
        label = "L-Wrist" if cx < (W * 0.5) else "R-Wrist"
        accepted.append((cx, cy, label, box))

    # Annotate.
    for cx, cy, label, (x1, y1, x2, y2) in accepted:
        color = (0, 220, 0)
        cv2.rectangle(vis, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
        cv2.circle(vis, (int(cx), int(cy)), 4, (255, 255, 255), -1)
        cv2.putText(
            vis, label, (int(x1), max(0, int(y1) - 6)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2,
        )

    hands_in_frame = min(len(accepted), 2)
    detected = hands_in_frame >= MIN_REGIONS
    return detected, vis, hands_in_frame


def detect_wrists_hsv(frame: np.ndarray) -> Tuple[bool, np.ndarray, int]:
    """HSV skin-blob fallback. Returns (detected, vis, hands_in_frame).
    hands_in_frame = min(qualifying_blobs, 2)."""
    vis  = frame.copy()
    hsv  = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, SKIN_LOWER_NP, SKIN_UPPER_NP)
    k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    qualifying = [c for c in contours if cv2.contourArea(c) >= MIN_REGION_PX]
    n_regions      = len(qualifying)
    hands_in_frame = min(n_regions, 2)
    detected       = hands_in_frame >= MIN_REGIONS
    for cnt in qualifying:
        x, y, w, h = cv2.boundingRect(cnt)
        c = (0, 220, 0) if detected else (0, 120, 220)
        cv2.rectangle(vis, (x, y), (x + w, y + h), c, 2)
    return detected, vis, hands_in_frame


def detect_wrists(frame: np.ndarray) -> Tuple[bool, np.ndarray, int, str]:
    """Returns (detected, annotated_frame, hands_in_frame, method)."""
    yolo_detected, vis, n = detect_wrists_yolo(frame)
    if yolo_detected is not None:
        return yolo_detected, vis, n, "yolo"
    detected, vis, n = detect_wrists_hsv(frame)
    return detected, vis, n, "hsv"


def single_frame_check(frame: np.ndarray) -> FrameCheckResult:
    """Stateless wrist check on a single decoded frame."""
    detected, annotated, n_wrists, method = detect_wrists(frame)
    if detected:
        msg = f"PASS — {n_wrists} wrist(s) detected [{method}]"
    else:
        msg = (f"FAIL — only {n_wrists} wrist(s) [{method}], "
               f"need {MIN_REGIONS}")
    color = (0, 220, 0) if detected else (0, 0, 220)
    label = "WRISTS OK" if detected else f"WRISTS NOT FOUND ({n_wrists}/{MIN_REGIONS})"
    cv2.putText(annotated, label, (20, 45),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 4)
    cv2.putText(annotated, label, (20, 45),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
    cv2.putText(annotated, f"[{method.upper()}]", (20, 80),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180, 180, 180), 1)
    return FrameCheckResult(
        passed=detected, message=msg, method=method, annotated=annotated,
    )


# ── PTY frame stream parser ───────────────────────────────────────
class _StreamParser:
    """Stateful parser for the helper binary's stdout protocol.
    Tolerates partial reads, oversized buffers, and corrupt headers."""

    HEADER_PREFIX = b"FRAME COLOR "
    MAX_BUF       = 4 * 1024 * 1024
    SAFE_TAIL     = 512 * 1024

    def __init__(self):
        self._buf = b""

    def feed(self, chunk: bytes):
        if not chunk:
            return
        self._buf += chunk

        if len(self._buf) > self.MAX_BUF:
            tail_start = len(self._buf) - self.SAFE_TAIL
            last       = self._buf.rfind(self.HEADER_PREFIX, tail_start)
            if last > 0:
                self._buf = self._buf[last:]
            else:
                self._buf = self._buf[-self.SAFE_TAIL:]

        while True:
            idx = self._buf.find(self.HEADER_PREFIX)
            if idx == -1:
                if len(self._buf) > len(self.HEADER_PREFIX):
                    self._buf = self._buf[-len(self.HEADER_PREFIX):]
                return

            nl = self._buf.find(b"\n", idx)
            if nl == -1:
                self._buf = self._buf[idx:]
                return

            header_line = self._buf[idx:nl].decode(
                "utf-8", errors="ignore"
            ).strip()
            try:
                parts = header_line.split()
                data_size = (int(parts[7]) if len(parts) >= 8
                             else int(parts[6]))
            except (IndexError, ValueError):
                self._buf = self._buf[nl + 1:]
                continue

            frame_end = nl + 1 + data_size
            if frame_end > len(self._buf):
                self._buf = self._buf[idx:]
                return

            payload   = self._buf[nl + 1:frame_end]
            self._buf = self._buf[frame_end:]
            yield payload


# ── FOV checker ───────────────────────────────────────────────────
class FOVChecker:
    """
    Runs the helper, decodes color frames, runs wrist detection.
    Aggregates pass/fail across `duration_sec` of detection time.

    Two callback streams (both optional, both can be set):
      - frame_cb(jpeg_bytes, detected) — annotated MJPEG for live preview.
      - progress_cb(FOVProgress)       — counters for the state envelope.

    Buzzer feedback (if `gpio` is provided):
      - 2 hands visible → silent.
      - 1 hand visible  → intermittent beep (200ms on / 400ms off).
      - 0 hands visible → continuous tone.
    The alarm only updates when the hand count CHANGES, so the buzzer
    pattern isn't restarted on every frame. The alarm is always
    stopped in `run()`'s finally block, even on cancel/exception.
    """

    def __init__(self,
                 duration_sec: int = 5,
                 min_detection_frames: int = 10,
                 frame_cb: Optional[Callable[[bytes, bool], None]] = None,
                 progress_cb: Optional[Callable[["FOVProgress"], None]] = None,
                 gpio=None,
                 skip_settle: bool = False):
        self.duration_sec         = duration_sec
        self.min_detection_frames = min_detection_frames
        self.frame_cb             = frame_cb
        self.progress_cb          = progress_cb
        self.gpio                 = gpio
        self.skip_settle          = skip_settle
        self._cancel              = threading.Event()
        # Last hand count we drove the buzzer with. -1 = no alarm set yet.
        self._last_alarm_hands    = -1

    def cancel(self):
        self._cancel.set()

    def _update_alarm(self, hands_in_frame: int):
        """Update the buzzer pattern only when the hand count changes."""
        if self.gpio is None:
            # One-time log per checker instance — make sure we know if
            # gpio is None when it shouldn't be.
            if self._last_alarm_hands == -1:
                log.warning(
                    "[FOV] _update_alarm: gpio is None — "
                    "buzzer alarms DISABLED for this check"
                )
                self._last_alarm_hands = -2  # poison; suppresses re-log
            return
        if hands_in_frame == self._last_alarm_hands:
            return
        self._last_alarm_hands = hands_in_frame
        try:
            if hands_in_frame == 0:
                log.info(f"[FOV] hands_in_frame=0 → buzzer CONTINUOUS")
                self.gpio.start_fov_alarm("continuous")
            elif hands_in_frame == 1:
                log.info(f"[FOV] hands_in_frame=1 → buzzer INTERMITTENT")
                self.gpio.start_fov_alarm("intermittent")
            else:
                log.info(f"[FOV] hands_in_frame=2 → buzzer OFF")
                self.gpio.start_fov_alarm("off")
        except Exception as e:
            log.warning(f"FOV buzzer update failed: {e}")

    def _stop_alarm(self):
        if self.gpio is None:
            return
        try:
            self.gpio.stop_fov_alarm()
        except Exception as e:
            log.warning(f"FOV buzzer stop failed: {e}")
        self._last_alarm_hands = -1

    def _wait_for_device_release(self):
        since_last = time.time() - get_last_stop_time()
        if 0 < since_last < ORBBEC_DEVICE_RELEASE_S:
            wait = ORBBEC_DEVICE_RELEASE_S - since_last
            log.info(f"Waiting {wait:.1f}s for Orbbec device to release")
            self._cancel.wait(timeout=wait)

    def _spawn_helper(self, env: dict):
        master_fd, slave_fd = pty.openpty()
        try:
            attrs    = termios.tcgetattr(slave_fd)
            attrs[1] = attrs[1] & ~termios.OPOST
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        except Exception as e:
            log.warning(f"Could not set PTY attrs: {e}")

        try:
            proc = subprocess.Popen(
                [ORBBEC_STREAM],
                stdout=slave_fd, stderr=slave_fd, stdin=slave_fd,
                env=env, close_fds=True,
            )
        except FileNotFoundError as e:
            os.close(master_fd)
            os.close(slave_fd)
            raise RuntimeError(
                f"Orbbec stream helper not found at {ORBBEC_STREAM}. "
                f"Run install_kit_v2 to deploy it."
            ) from e
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise

        try: os.close(slave_fd)
        except OSError: pass
        return proc, master_fd

    def run(self) -> FOVResult:
        _load_yolo()
        self._wait_for_device_release()
        if self._cancel.is_set():
            return FOVResult(False, 0, 0, "Cancelled before start")

        total_secs = self.duration_sec + STARTUP_SECS
        log.info(f"FOV check — {total_secs}s total")

        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = ORBBEC_STREAM_LIB
        env["QT_QPA_PLATFORM"] = "offscreen"

        proc      = None
        master_fd = None
        try:
            try:
                proc, master_fd = self._spawn_helper(env)
            except Exception as e:
                log.error(f"Failed to launch stream helper: {e}")
                return FOVResult(
                    False, 0, 0,
                    f"Failed to launch stream helper: {e}"
                )
            return self._read_loop(proc, master_fd, total_secs)
        finally:
            # Always stop the buzzer — exception, cancel, or normal exit.
            self._stop_alarm()
            if proc is not None:
                try:
                    proc.terminate()
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try: proc.wait(timeout=2)
                        except Exception: pass
                except Exception as e:
                    log.warning(f"Error terminating stream helper: {e}")
            if master_fd is not None:
                try: os.close(master_fd)
                except OSError: pass
            _set_last_stop_time(time.time())

    def _emit_preview(self, vis: np.ndarray, detected: bool):
        """Encode annotated frame as JPEG and send to frame_cb."""
        if self.frame_cb is None:
            return
        try:
            ok, jpeg = cv2.imencode(
                ".jpg", vis, [int(cv2.IMWRITE_JPEG_QUALITY), 80]
            )
            if not ok:
                return
            self.frame_cb(jpeg.tobytes(), detected)
        except Exception as e:
            log.warning(f"frame_cb encode/emit failed: {e}")

    def _emit_progress(self, t_start, total_secs, settled,
                       frames_checked, frames_with_hands,
                       hands_in_frame, method):
        if self.progress_cb is None:
            return
        try:
            elapsed = time.time() - t_start
            self.progress_cb(FOVProgress(
                elapsed_sec=round(elapsed, 2),
                total_sec=round(total_secs, 2),
                settled=settled,
                frames_checked=frames_checked,
                frames_with_hands=frames_with_hands,
                last_n_wrists=hands_in_frame,    # legacy alias
                hands_in_frame=hands_in_frame,
                method=method,
            ))
        except Exception as e:
            log.warning(f"progress_cb raised: {e}")

    def _read_loop(self, proc: subprocess.Popen, master_fd: int,
                   total_secs: float) -> FOVResult:
        frames_checked    = 0
        frames_with_hands = 0
        hands_in_frame    = 0     # most recent count (0, 1, or 2)
        detection_method  = "hsv"
        t_start           = time.time()
        deadline          = t_start + total_secs
        parser            = _StreamParser()
        settler           = ExposureSettler(skip_settle=self.skip_settle)
        last_progress     = 0.0

        while time.time() < deadline and not self._cancel.is_set():
            if proc.poll() is not None:
                log.error(
                    f"Stream helper exited early (code={proc.returncode})"
                )
                break

            try:
                r, _, _ = select.select([master_fd], [], [], 0.1)
            except (OSError, ValueError):
                break
            if not r:
                # Periodic progress emit even with no new frames.
                now = time.time()
                if now - last_progress >= 0.5:
                    last_progress = now
                    self._emit_progress(
                        t_start, total_secs, settler.is_settled,
                        frames_checked, frames_with_hands, hands_in_frame,
                        detection_method,
                    )
                continue

            try:
                chunk = os.read(master_fd, 131072)
            except OSError:
                break
            if not chunk:
                break

            for jpeg_bytes in parser.feed(chunk):
                frame = cv2.imdecode(
                    np.frombuffer(jpeg_bytes, dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                if frame is None:
                    continue

                settled = settler.feed(frame)
                if not settled:
                    # Still emit a preview during AE settle so the
                    # operator can see the camera is alive. Don't
                    # arm the buzzer until after AE settles, or
                    # the operator gets a false alarm during the
                    # first second of exposure adjustment.
                    if self.frame_cb is not None:
                        vis = frame.copy()
                        cv2.putText(
                            vis, settler.status_text, (20, 45),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4,
                        )
                        cv2.putText(
                            vis, settler.status_text, (20, 45),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 180, 220), 2,
                        )
                        self._emit_preview(vis, False)
                    continue

                detected, vis, n_hands, method = detect_wrists(frame)
                detection_method = method
                hands_in_frame   = n_hands  # already capped 0..2
                if detected:
                    frames_with_hands += 1
                frames_checked += 1

                # Update the buzzer pattern based on the current count.
                # _update_alarm is a no-op when the value hasn't changed,
                # so this is cheap to call every frame.
                self._update_alarm(hands_in_frame)

                if frames_checked % 10 == 1:
                    log.info(
                        f"FOV frame {frames_checked}: "
                        f"hands_in_frame={hands_in_frame} "
                        f"detected={detected} method={method}"
                    )

                # Annotate with status banner + frame counters.
                if self.frame_cb is not None:
                    elapsed  = time.time() - t_start
                    progress = min(elapsed / total_secs, 1.0)
                    if hands_in_frame == 2:
                        color = (0, 220, 0)
                        label = "BOTH HANDS IN FRAME"
                    elif hands_in_frame == 1:
                        color = (0, 180, 220)   # amber-ish (BGR)
                        label = "1 HAND OUT OF FRAME"
                    else:
                        color = (0, 0, 220)
                        label = "BOTH HANDS OUT OF FRAME"
                    cv2.putText(vis, label, (20, 45),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 4)
                    cv2.putText(vis, label, (20, 45),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
                    cv2.putText(
                        vis,
                        f"{frames_with_hands}/{frames_checked} [{method}]",
                        (20, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.65,
                        (200, 200, 200), 1,
                    )
                    h, w = vis.shape[:2]
                    cv2.rectangle(
                        vis, (0, h - 10), (int(w * progress), h),
                        color, -1,
                    )
                    self._emit_preview(vis, detected)

            now = time.time()
            if now - last_progress >= 0.5:
                last_progress = now
                self._emit_progress(
                    t_start, total_secs, settler.is_settled,
                    frames_checked, frames_with_hands, hands_in_frame,
                    detection_method,
                )

        passed = frames_with_hands >= self.min_detection_frames
        message = (
            f"PASS — both hands visible in {frames_with_hands}/"
            f"{frames_checked} frames [{detection_method}]"
            if passed else
            f"FAIL — both hands visible in {frames_with_hands}/"
            f"{frames_checked} frames (need {self.min_detection_frames}) "
            f"[{detection_method}]"
        )
        log.info(f"FOV check: {message}")
        return FOVResult(passed, frames_checked, frames_with_hands,
                         message, detection_method)
