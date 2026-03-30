"""
Orbbec bag recorder — wraps ob_device_record_nogui via PTY.
Provides clean start/stop with no bag corruption.
"""
import os, time, select, subprocess, threading, pty, logging

log = logging.getLogger(__name__)

DEVICE_RELEASE_S = 6.0   # wait after FOV check (orbbec_stream) releases device


class OrbbecRecorder:
    def __init__(self, bag_path: str, orbbec_rec: str, orbbec_lib: str):
        self.bag_path   = bag_path
        self.orbbec_rec = orbbec_rec
        self.orbbec_lib = orbbec_lib
        self._proc      = None
        self._master_fd = None

    def start(self) -> bool:
        """Start recording. Returns True if started successfully."""
        # Wait for device to fully release if FOV check was used
        try:
            from capture.cameras.fov_check import _last_stop_time
            since = time.time() - _last_stop_time
            if since < DEVICE_RELEASE_S:
                wait = DEVICE_RELEASE_S - since
                log.info(f"Waiting {wait:.1f}s for Orbbec device to release before recording...")
                time.sleep(wait)
        except ImportError:
            pass  # FOV check module not available, no wait needed

        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = self.orbbec_lib
        env["QT_QPA_PLATFORM"] = "offscreen"

        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            [self.orbbec_rec],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            env=env, close_fds=True)
        os.close(slave_fd)

        self._proc      = proc
        self._master_fd = master_fd

        if not self._read_until("filename", timeout=15):
            log.error("Orbbec: no filename prompt")
            self.stop()
            return False

        os.write(master_fd, f"{self.bag_path}\n".encode())

        if not self._read_until("started", timeout=15):
            log.error("Orbbec: recorder did not start")
            self.stop()
            return False

        log.info(f"Orbbec recording -> {self.bag_path}")
        threading.Thread(target=self._drain, daemon=True).start()
        return True

    def stop(self):
        """Send q to finalize bag cleanly, then terminate."""
        if self._master_fd:
            try: os.write(self._master_fd, b"q\n"); time.sleep(2)
            except OSError: pass
        if self._proc:
            self._proc.terminate()
            try: self._proc.wait(timeout=5)
            except: self._proc.kill()
        if self._master_fd:
            try: os.close(self._master_fd)
            except: pass
        self._proc = self._master_fd = None
        log.info(f"Orbbec bag saved -> {self.bag_path}")

    def _read_until(self, keyword: str, timeout: int = 30) -> bool:
        buf      = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([self._master_fd], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(self._master_fd, 4096)
                    buf  += chunk
                    txt   = chunk.decode('utf-8', errors='ignore').strip()
                    if txt: log.debug(f"[orbbec] {txt}")
                    if keyword.encode() in buf:
                        return True
                except OSError:
                    break
        return False

    def _drain(self):
        while self._master_fd:
            r, _, _ = select.select([self._master_fd], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(self._master_fd, 4096)
                    txt   = chunk.decode('utf-8', errors='ignore').strip()
                    if txt: log.info(f"[orbbec] {txt}")
                except OSError:
                    break
