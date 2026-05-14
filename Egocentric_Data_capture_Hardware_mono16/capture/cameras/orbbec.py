"""
Orbbec MCAP recorder — wraps ob_device_record_mcap_nogui via PTY.

Provides clean start/stop with no MCAP corruption.

Stop is deterministic: we send 'q\\n' to the recorder, then EITHER
wait for it to print its "Summary:" line on stdout (which the
recorder emits only AFTER the writer queue is drained and the MCAP
footer has been written), OR wait for the process to exit on its
own. Whichever happens first within ORBBEC_RECORDER_DRAIN_S means
the file is safe.

Hot-unplug: if the recorder process exits unexpectedly (USB
disconnect, SDK crash), `crashed` is set and the caller
(session_v2.py) treats the segment as failed.
"""
import logging
import os
import pty
import select
import subprocess
import threading
import time
from typing import Optional

from capture.config import (
    ORBBEC_DEVICE_RELEASE_S,
    ORBBEC_RECORDER_DRAIN_S,
    ORBBEC_RECORDER_START_S,
)
from capture.cameras.fov_check import get_last_stop_time

log = logging.getLogger(__name__)


class OrbbecRecorder:
    def __init__(self, output_path: str, orbbec_rec: str, orbbec_lib: str):
        self.output_path = output_path
        self.orbbec_rec  = orbbec_rec
        self.orbbec_lib  = orbbec_lib

        self._proc:        Optional[subprocess.Popen]    = None
        self._master_fd:   Optional[int]                 = None
        self._lock                                       = threading.Lock()
        self._stopped                                    = False
        self._drain_thread: Optional[threading.Thread]   = None
        self._drain_stop                                 = threading.Event()
        self.crashed                                     = False

    # ── Public API ────────────────────────────────────────────────
    def start(self) -> bool:
        """Start recording. Returns True if started successfully."""
        if not os.path.exists(self.orbbec_rec):
            log.error(f"Orbbec recorder binary not found: {self.orbbec_rec}")
            return False

        # Wait for camera USB device to be released by FOV streamer (if any).
        last_stop = get_last_stop_time()
        if last_stop > 0:
            since = time.time() - last_stop
            if since < ORBBEC_DEVICE_RELEASE_S:
                wait = ORBBEC_DEVICE_RELEASE_S - since
                log.info(
                    f"Waiting {wait:.1f}s for Orbbec device to release "
                    f"before recording..."
                )
                time.sleep(wait)

        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = self.orbbec_lib
        env["QT_QPA_PLATFORM"] = "offscreen"

        master_fd, slave_fd = pty.openpty()
        try:
            self._proc = subprocess.Popen(
                [self.orbbec_rec],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                env=env, close_fds=True,
            )
        except Exception as e:
            log.error(f"Failed to launch Orbbec recorder: {e}")
            try: os.close(master_fd)
            except OSError: pass
            try: os.close(slave_fd)
            except OSError: pass
            return False

        # Slave end belongs to the child after fork.
        try:
            os.close(slave_fd)
        except OSError:
            pass

        self._master_fd = master_fd
        self._stopped   = False
        self.crashed    = False

        # Recorder prints "filename" prompt; reply with our path.
        if not self._read_until("filename", timeout=ORBBEC_RECORDER_START_S):
            log.error("Orbbec: no filename prompt within start window")
            self.stop()
            return False
        try:
            os.write(master_fd, f"{self.output_path}\n".encode())
        except OSError as e:
            log.error(f"Orbbec: cannot write filename: {e}")
            self.stop()
            return False

        # The recorder prints "Streams and recorder have started!" once
        # both Color and Depth are streaming. Older builds say "started".
        if not self._read_until("started", timeout=ORBBEC_RECORDER_START_S):
            log.error("Orbbec: recorder did not start")
            self.stop()
            return False

        log.info(f"Orbbec recording -> {self.output_path}")
        self._drain_stop.clear()
        self._drain_thread = threading.Thread(
            target=self._drain, daemon=True, name="orbbec-drain")
        self._drain_thread.start()
        return True

    def stop(self):
        """Send q to finalize MCAP cleanly, wait for Summary:/process exit,
        then terminate. Idempotent; safe to call from multiple threads."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            proc          = self._proc
            master_fd     = self._master_fd
            self._proc      = None
            self._master_fd = None

        # Stop the drain thread BEFORE we read for the Summary line so
        # both threads aren't racing on the same fd.
        self._drain_stop.set()
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=2)
            self._drain_thread = None

        if master_fd is not None and proc is not None:
            try:
                # Send 'q' if the process is still alive.
                if proc.poll() is None:
                    try:
                        os.write(master_fd, b"q\n")
                        log.info(
                            "Orbbec stop: sent q — waiting for MCAP "
                            "finalisation..."
                        )
                    except OSError:
                        pass

                    # Wait for EITHER the Summary: keyword OR for the
                    # process to exit on its own. Both signal a fully
                    # finalized MCAP. Using two signals defends against
                    # firmware/SDK builds that don't print Summary: on
                    # all paths.
                    finalized = self._wait_for_finalize(
                        proc, master_fd, ORBBEC_RECORDER_DRAIN_S
                    )
                    if not finalized:
                        log.warning(
                            f"Orbbec stop: did NOT finalize within "
                            f"{ORBBEC_RECORDER_DRAIN_S}s — MCAP may be "
                            f"unindexed; run `mcap recover` if needed."
                        )
                else:
                    log.info("Orbbec stop: process already exited")
            finally:
                # Terminate child if still alive.
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try: proc.wait(timeout=3)
                        except Exception: pass

                try: os.close(master_fd)
                except OSError: pass

        log.info(f"Orbbec mcap saved -> {self.output_path}")

    def is_alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    # ── Internal helpers ──────────────────────────────────────────
    def _read_until(self, keyword: str, timeout: float) -> bool:
        """Read from master fd until keyword appears or timeout."""
        master_fd = self._master_fd
        if master_fd is None:
            return False
        return self._read_until_fd(master_fd, keyword, timeout)

    def _read_until_fd(self, master_fd: int, keyword: str,
                       timeout: float) -> bool:
        buf      = b""
        deadline = time.time() + timeout
        kw_bytes = keyword.encode()
        while time.time() < deadline:
            try:
                r, _, _ = select.select([master_fd], [], [], 0.1)
            except (OSError, ValueError):
                return False
            if r:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    return False
                if not chunk:
                    return False
                buf += chunk
                txt = chunk.decode("utf-8", errors="ignore").strip()
                if txt:
                    log.debug(f"[orbbec] {txt}")
                if kw_bytes in buf:
                    return True
        return False

    def _wait_for_finalize(self, proc: subprocess.Popen, master_fd: int,
                           timeout: float) -> bool:
        """
        Wait up to `timeout` for either:
          (a) the recorder to print "Summary:" — its own drain-complete signal
          (b) the recorder process to exit on its own (also implies finalized)

        Returns True if either happened, False on timeout.
        """
        deadline = time.time() + timeout
        kw       = b"Summary:"
        buf      = b""

        while time.time() < deadline:
            # Check (b) first — cheapest.
            if proc.poll() is not None:
                log.info("Orbbec stop: process exited cleanly")
                return True

            # Read whatever is on the pty without blocking.
            try:
                r, _, _ = select.select([master_fd], [], [], 0.2)
            except (OSError, ValueError):
                # fd closed under us — fall back to wait()
                try:
                    proc.wait(timeout=max(0.0, deadline - time.time()))
                    return True
                except subprocess.TimeoutExpired:
                    return False

            if r:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    buf += chunk
                    txt = chunk.decode("utf-8", errors="ignore").strip()
                    if txt:
                        log.debug(f"[orbbec] {txt}")
                    if kw in buf:
                        log.info("Orbbec stop: Summary line received")
                        # Give it another moment to actually exit.
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                        return True
        return False

    def _drain(self):
        """
        Background thread that consumes recorder stdout while it's
        running so the kernel pipe buffer cannot fill up and stall the
        writer. Logs the recorder's progress lines at INFO level.
        Detects unexpected exits as crashes.
        """
        master_fd = self._master_fd
        if master_fd is None:
            return
        while not self._drain_stop.is_set():
            proc = self._proc
            if proc is None:
                return
            if proc.poll() is not None:
                # Exited without us calling stop() → crash.
                self.crashed = True
                log.error(
                    f"Orbbec recorder crashed (exit code {proc.returncode})"
                )
                return
            try:
                r, _, _ = select.select([master_fd], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not r:
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            txt = chunk.decode("utf-8", errors="ignore").strip()
            if txt:
                log.info(f"[orbbec] {txt}")
