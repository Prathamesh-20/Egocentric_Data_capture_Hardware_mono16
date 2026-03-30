"""
FOV Check — pre-capture wrist validation.
Uses YOLOv8n-pose for wrist keypoint detection (primary),
falls back to HSV skin-blob detection if YOLO unavailable.

Also provides single-frame check for between-segment validation.
"""
import cv2, time, logging, threading, os, select, pty, termios
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, Callable, Tuple

log = logging.getLogger(__name__)

from capture.config import (
    YOLO_MODEL_PATH, YOLO_CONF_THRESH, WRIST_KP_INDICES, WRIST_KP_CONF,
    SKIN_LOWER, SKIN_UPPER, MIN_REGION_PX, MIN_REGIONS,
    ORBBEC_STREAM, ORBBEC_STREAM_LIB,
)

SKIN_LOWER_NP = np.array(SKIN_LOWER, dtype=np.uint8)
SKIN_UPPER_NP = np.array(SKIN_UPPER, dtype=np.uint8)
STARTUP_SECS     = 3
DEVICE_RELEASE_S = 2.5

# Auto-exposure settling parameters
AE_WINDOW_SIZE     = 5     # sliding window of brightness samples
AE_VARIANCE_THRESH = 15.0  # max variance to consider "settled"
AE_MIN_BRIGHTNESS  = 25    # minimum mean brightness (reject black frames)
AE_MAX_SETTLE_SEC  = 6.0   # give up waiting after this many seconds

_last_stop_time = 0.0
_yolo_model     = None
_yolo_loaded    = False


class ExposureSettler:
    """
    Tracks mean frame brightness over a sliding window.
    Reports settled once variance drops below threshold
    and brightness is above minimum floor.
    """

    def __init__(self, window_size=AE_WINDOW_SIZE,
                 variance_thresh=AE_VARIANCE_THRESH,
                 min_brightness=AE_MIN_BRIGHTNESS,
                 max_settle_sec=AE_MAX_SETTLE_SEC):
        self.window_size     = window_size
        self.variance_thresh = variance_thresh
        self.min_brightness  = min_brightness
        self.max_settle_sec  = max_settle_sec
        self._history        = []
        self._settled        = False
        self._start_time     = time.time()
        self._settle_time    = None

    def feed(self, frame: np.ndarray) -> bool:
        """Feed a frame. Returns True if exposure is settled."""
        if self._settled:
            return True

        mean_brightness = float(np.mean(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))
        self._history.append(mean_brightness)

        # Keep only last N
        if len(self._history) > self.window_size:
            self._history = self._history[-self.window_size:]

        # Need full window
        if len(self._history) < self.window_size:
            return False

        variance   = float(np.var(self._history))
        avg_bright = float(np.mean(self._history))

        if avg_bright >= self.min_brightness and variance <= self.variance_thresh:
            self._settled = True
            self._settle_time = time.time()
            elapsed = self._settle_time - self._start_time
            log.info(f"Auto-exposure settled — brightness={avg_bright:.0f} "
                     f"variance={variance:.1f} ({elapsed:.1f}s)")
            return True

        # Force settle after timeout
        if time.time() - self._start_time > self.max_settle_sec:
            self._settled = True
            self._settle_time = time.time()
            log.warning(f"Auto-exposure forced settle after {self.max_settle_sec}s — "
                        f"brightness={avg_bright:.0f} variance={variance:.1f}")
            return True

        return False

    @property
    def is_settled(self) -> bool:
        return self._settled

    @property
    def current_brightness(self) -> float:
        return self._history[-1] if self._history else 0.0

    @property
    def status_text(self) -> str:
        if self._settled:
            return "EXPOSURE OK"
        n = len(self._history)
        bright = self._history[-1] if self._history else 0
        return f"EXPOSURE SETTLING ({n}/{self.window_size}) bright={bright:.0f}"


def _load_yolo():
    """Lazy-load YOLOv8n-pose model. Returns model or None."""
    global _yolo_model, _yolo_loaded
    if _yolo_loaded:
        return _yolo_model
    _yolo_loaded = True
    try:
        from ultralytics import YOLO
        if os.path.exists(YOLO_MODEL_PATH):
            _yolo_model = YOLO(YOLO_MODEL_PATH)
            log.info(f"YOLOv8n-pose loaded from {YOLO_MODEL_PATH}")
        else:
            log.warning(f"YOLO model not found at {YOLO_MODEL_PATH}, using HSV fallback")
    except ImportError:
        log.warning("ultralytics not installed, using HSV skin detection fallback")
    except Exception as e:
        log.warning(f"YOLO load failed: {e}, using HSV fallback")
    return _yolo_model


@dataclass
class FOVResult:
    passed:            bool
    frames_checked:    int
    frames_with_hands: int
    message:           str
    method:            str = "unknown"  # "yolo" or "hsv"


@dataclass
class FrameCheckResult:
    passed:       bool
    message:      str
    method:       str = "unknown"
    frame:        Optional[np.ndarray] = field(default=None, repr=False)
    annotated:    Optional[np.ndarray] = field(default=None, repr=False)


def detect_wrists_yolo(frame: np.ndarray) -> Tuple[bool, np.ndarray, int]:
    """
    Detect wrists using YOLOv8n-pose.
    Returns (detected, annotated_frame, n_wrists_found).
    """
    model = _load_yolo()
    if model is None:
        return None, frame, 0  # None = model unavailable

    vis = frame.copy()
    results = model(frame, conf=YOLO_CONF_THRESH, verbose=False)

    n_wrists = 0
    for r in results:
        if r.keypoints is None or r.keypoints.data is None:
            continue
        for person_kps in r.keypoints.data:
            for kp_idx in WRIST_KP_INDICES:
                if kp_idx < len(person_kps):
                    x, y, conf = person_kps[kp_idx]
                    if conf >= WRIST_KP_CONF and x > 0 and y > 0:
                        n_wrists += 1
                        cx, cy = int(x), int(y)
                        color = (0, 220, 0) if n_wrists <= 2 else (0, 180, 220)
                        cv2.circle(vis, (cx, cy), 12, color, 3)
                        cv2.circle(vis, (cx, cy), 4, (255, 255, 255), -1)
                        label = "L-Wrist" if kp_idx == 9 else "R-Wrist"
                        cv2.putText(vis, label, (cx + 15, cy + 5),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        # Draw bounding boxes
        if r.boxes is not None:
            for box in r.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cv2.rectangle(vis, (x1, y1), (x2, y2), (80, 80, 80), 1)

    detected = n_wrists >= MIN_REGIONS
    return detected, vis, n_wrists


def detect_wrists_hsv(frame: np.ndarray) -> Tuple[bool, np.ndarray, int]:
    """HSV skin-blob fallback detection."""
    vis  = frame.copy()
    hsv  = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, SKIN_LOWER_NP, SKIN_UPPER_NP)
    k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    qualifying = [c for c in contours if cv2.contourArea(c) >= MIN_REGION_PX]
    n_regions  = len(qualifying)
    detected   = n_regions >= MIN_REGIONS

    for cnt in qualifying:
        x, y, w, h = cv2.boundingRect(cnt)
        c = (0, 220, 0) if detected else (0, 120, 220)
        cv2.rectangle(vis, (x, y), (x + w, y + h), c, 2)

    return detected, vis, n_regions


def detect_wrists(frame: np.ndarray) -> Tuple[bool, np.ndarray, int, str]:
    """
    Primary: YOLOv8n-pose. Fallback: HSV skin detection.
    Returns (detected, annotated_frame, n_wrists, method).
    """
    result = detect_wrists_yolo(frame)
    if result[0] is not None:
        return result[0], result[1], result[2], "yolo"
    # Fallback
    detected, vis, n = detect_wrists_hsv(frame)
    return detected, vis, n, "hsv"


def single_frame_check(frame: np.ndarray) -> FrameCheckResult:
    """
    Run wrist detection on a single frame (used between segments).
    """
    detected, annotated, n_wrists, method = detect_wrists(frame)

    if detected:
        msg = f"PASS — {n_wrists} wrist(s) detected [{method}]"
    else:
        msg = f"FAIL — only {n_wrists} wrist(s) [{method}], need {MIN_REGIONS}"

    # Overlay result on annotated frame
    color = (0, 220, 0) if detected else (0, 0, 220)
    label = "WRISTS OK" if detected else f"WRISTS NOT FOUND ({n_wrists}/{MIN_REGIONS})"
    cv2.putText(annotated, label, (20, 45),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 4)
    cv2.putText(annotated, label, (20, 45),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
    cv2.putText(annotated, f"[{method.upper()}]", (20, 80),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180, 180, 180), 1)

    return FrameCheckResult(
        passed=detected, message=msg, method=method,
        frame=frame.copy(), annotated=annotated
    )


class FOVChecker:
    """Full FOV check — runs for duration_sec, streams annotated frames."""

    def __init__(self, duration_sec=5, min_detection_frames=10, frame_cb=None):
        self.duration_sec         = duration_sec
        self.min_detection_frames = min_detection_frames
        self.frame_cb             = frame_cb
        self._cancel              = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self) -> FOVResult:
        global _last_stop_time

        # Pre-load YOLO model
        _load_yolo()

        since_last = time.time() - _last_stop_time
        if since_last < DEVICE_RELEASE_S:
            wait = DEVICE_RELEASE_S - since_last
            log.info(f"Waiting {wait:.1f}s for Orbbec device to release...")
            time.sleep(wait)

        total_secs = self.duration_sec + STARTUP_SECS
        log.info(f"FOV check — {total_secs}s total")

        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = ORBBEC_STREAM_LIB
        env["QT_QPA_PLATFORM"] = "offscreen"

        master_fd, slave_fd = pty.openpty()

        try:
            attrs = termios.tcgetattr(slave_fd)
            attrs[1] = attrs[1] & ~termios.OPOST
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        except Exception as e:
            log.warning(f"Could not set PTY attrs: {e}")

        try:
            proc = subprocess.Popen(
                [ORBBEC_STREAM],
                stdout=slave_fd, stderr=slave_fd, stdin=slave_fd,
                env=env, close_fds=True)
            os.close(slave_fd)
        except Exception as e:
            try: os.close(master_fd)
            except: pass
            return FOVResult(False, 0, 0, f"Failed to launch orbbec_stream: {e}")

        frames_checked    = 0
        frames_with_hands = 0
        detection_method  = "hsv"
        t_start           = time.time()
        deadline          = t_start + total_secs
        buf               = b""
        settler           = ExposureSettler()

        try:
            while time.time() < deadline and not self._cancel.is_set():
                r, _, _ = select.select([master_fd], [], [], 0.1)
                if not r:
                    continue
                try:
                    chunk = os.read(master_fd, 131072)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk

                if len(buf) > 4 * 1024 * 1024:
                    last = buf.rfind(b"FRAME ", len(buf) - 512 * 1024)
                    buf  = buf[last:] if last > 0 else buf[-2 * 1024 * 1024:]

                while True:
                    idx = buf.find(b"FRAME COLOR ")
                    if idx == -1:
                        break
                    nl = buf.find(b"\n", idx)
                    if nl == -1:
                        buf = buf[idx:]; break
                    header = buf[idx:nl].decode("utf-8", errors="ignore").strip()
                    try:
                        parts     = header.split()
                        data_size = int(parts[6])
                    except (IndexError, ValueError):
                        buf = buf[nl + 1:]; continue

                    frame_end = nl + 1 + data_size
                    if frame_end > len(buf):
                        buf = buf[idx:]; break

                    data = buf[nl + 1:frame_end]
                    buf  = buf[frame_end:]

                    frame = cv2.imdecode(
                        np.frombuffer(data, dtype=np.uint8),
                        cv2.IMREAD_COLOR)
                    if frame is None:
                        continue

                    # Wait for auto-exposure to settle before running detection
                    settled = settler.feed(frame)

                    if not settled:
                        # Show "settling" overlay on stream but don't count
                        vis = frame.copy()
                        status = settler.status_text
                        cv2.putText(vis, status, (20, 45),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4)
                        cv2.putText(vis, status, (20, 45),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 180, 220), 2)
                        if self.frame_cb:
                            self.frame_cb(vis, False)
                        continue

                    detected, vis, n_wrists, method = detect_wrists(frame)
                    detection_method = method
                    if detected:
                        frames_with_hands += 1
                    frames_checked += 1

                    if frames_checked % 10 == 1:
                        log.info(f"FOV frame {frames_checked}: wrists={n_wrists} "
                                 f"detected={detected} method={method}")

                    elapsed  = time.time() - t_start
                    progress = min(elapsed / total_secs, 1.0)
                    color    = (0, 220, 0) if detected else (0, 0, 220)
                    label    = "BOTH WRISTS IN FOV" if detected else \
                               f"WRISTS NOT IN FOV ({n_wrists}/{MIN_REGIONS})"

                    cv2.putText(vis, label, (20, 45),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 4)
                    cv2.putText(vis, label, (20, 45),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
                    cv2.putText(vis, f"{frames_with_hands}/{frames_checked} [{method}]",
                                (20, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (200, 200, 200), 1)

                    h, w = vis.shape[:2]
                    cv2.rectangle(vis, (0, h - 10), (int(w * progress), h), color, -1)

                    if self.frame_cb:
                        self.frame_cb(vis, detected)

        finally:
            proc.terminate()
            try: proc.wait(timeout=3)
            except Exception: proc.kill()
            try: os.close(master_fd)
            except: pass
            _last_stop_time = time.time()

        passed  = frames_with_hands >= self.min_detection_frames
        message = (
            f"PASS — wrists detected in {frames_with_hands}/{frames_checked} frames [{detection_method}]"
            if passed else
            f"FAIL — wrists in {frames_with_hands}/{frames_checked} frames "
            f"(need {self.min_detection_frames}) [{detection_method}]"
        )
        log.info(f"FOV check: {message}")
        return FOVResult(passed, frames_checked, frames_with_hands, message, detection_method)


# Need subprocess for FOV checker
import subprocess
