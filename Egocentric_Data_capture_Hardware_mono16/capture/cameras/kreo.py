"""
Kreo Owl USB camera capture — V4L2 MJPEG.
One instance per camera, takes device path + label.
"""
import cv2, time, threading, logging, os
from datetime import datetime

log  = logging.getLogger(__name__)
FONT = cv2.FONT_HERSHEY_SIMPLEX


def _resolve(device: str) -> str:
    """Resolve symlink to real /dev/videoN — OpenCV V4L2 can't open by symlink name."""
    try:
        real = os.path.realpath(device)
        return real
    except Exception:
        return device


def probe(device: str) -> bool:
    """Check if device exists and can be opened."""
    if not os.path.exists(device):
        return False
    real = _resolve(device)
    cap  = cv2.VideoCapture(real, cv2.CAP_V4L2)
    ok   = cap.isOpened()
    cap.release()
    return ok


class KreoCamera:
    def __init__(self, device: str, label: str, out_path: str,
                 width: int = 1280, height: int = 720, fps: int = 30):
        self.device   = _resolve(device)
        self.label    = label
        self.out_path = out_path
        self.width    = width
        self.height   = height
        self.fps      = fps
        self._stop    = threading.Event()
        self._thread  = None
        self.n_frames = 0
        self.log_ts_cb = None

    def start(self, barrier=None):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(barrier,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self):
        if self._thread:
            self._thread.join()

    def _run(self, barrier):
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC,       cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS,          self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE,   2)

        if not cap.isOpened():
            log.error(f"[{self.label}] Cannot open {self.device}")
            if barrier:
                try: barrier.wait(timeout=2)
                except: pass
            return

        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        writer = cv2.VideoWriter(self.out_path,
                                 cv2.VideoWriter_fourcc(*'mp4v'),
                                 self.fps, (w, h))

        log.info(f"[{self.label}] Ready {w}x{h}, waiting for barrier...")
        if barrier:
            barrier.wait()
        log.info(f"[{self.label}] Recording -> {self.out_path}")

        idx = 0
        while not self._stop.is_set():
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.01)
                continue
            ns = time.time_ns()
            self._burn(frame, ns, idx)
            writer.write(frame)
            if self.log_ts_cb:
                self.log_ts_cb(self.label, ns, idx)
            idx += 1

        cap.release()
        writer.release()
        self.n_frames = idx
        log.info(f"[{self.label}] Stopped. {idx} frames.")

    def _burn(self, frame, unix_ns: int, idx: int):
        dt  = datetime.fromtimestamp(unix_ns / 1e9)
        txt = f"{self.label} {dt.strftime('%H:%M:%S.%f')[:-3]}  f{idx}"
        cv2.putText(frame, txt, (11, 21), FONT, 0.5, (0,   0,   0  ), 2)
        cv2.putText(frame, txt, (10, 20), FONT, 0.5, (255, 255, 255), 1)
