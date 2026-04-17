"""
Session V2 — 30-min session orchestrator (Simplified).

Orbbec Gemini 2L only — records RGB + Depth as .mcap files.
No Kreo wrist cameras. No inter-segment wrist/FOV checks.

Flow:
  1. Loop until session time exhausted:
     a. Record 1-min segment (Orbbec .mcap)
     b. Enqueue segment files for S3 upload
  2. Session complete

Outputs per segment:
  orbbec_<session>_seg<N>.mcap
  timestamps_<session>_seg<N>.csv
"""
import os, csv, time, threading, logging, json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Dict, List

from capture.config import (
    OUTPUT_DIR, ORBBEC_REC, ORBBEC_LIB,
    FPS, SEGMENT_DURATION, SESSION_DURATION,
)
from capture.cameras.orbbec import OrbbecRecorder

log = logging.getLogger(__name__)


@dataclass
class SegmentInfo:
    index:     int
    status:    str = "pending"  # pending | recording | uploading | complete | failed
    files:     Dict[str, str] = field(default_factory=dict)
    wrist_ok:  Optional[bool] = None
    start_time: Optional[float] = None
    end_time:   Optional[float] = None


class SessionV2:
    """
    30-minute session with sequential 1-min segments.
    Orbbec Gemini 2L only — RGB + Depth .mcap recording.
    """

    def __init__(self,
                 operator_id: str = "",
                 activity_label: str = "",
                 segment_duration: int = SEGMENT_DURATION,
                 session_duration: int = SESSION_DURATION,
                 mcap_enabled: bool = False,
                 on_state_change: Callable = None,
                 on_segment_update: Callable = None,
                 on_frame_check: Callable = None,
                 on_complete: Callable = None,
                 upload_queue = None,
                 gpio = None):

        self.operator_id      = operator_id
        self.activity_label   = activity_label
        self.segment_duration = segment_duration
        self.session_duration = session_duration
        self.mcap_enabled     = mcap_enabled

        self.on_state_change  = on_state_change
        self.on_segment_update = on_segment_update
        self.on_frame_check   = on_frame_check   # kept for API compat, not called
        self.on_complete      = on_complete
        self.upload_queue     = upload_queue

        self._stop       = threading.Event()
        self._thread     = None
        self.session_id  = None
        self.session_dir = None
        self.segments:   List[SegmentInfo] = []
        self.max_segments = session_duration // segment_duration

    def start(self):
        self._stop.clear()
        self.segments.clear()
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = os.path.join(OUTPUT_DIR, f"session_{self.session_id}")
        os.makedirs(self.session_dir, exist_ok=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop_early(self):
        self._stop.set()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def get_state(self) -> dict:
        return {
            "session_id":       self.session_id,
            "operator_id":      self.operator_id,
            "activity_label":   self.activity_label,
            "segment_duration": self.segment_duration,
            "session_duration": self.session_duration,
            "mcap_enabled":     self.mcap_enabled,
            "max_segments":     self.max_segments,
            "segments": [
                {
                    "index":    s.index,
                    "status":   s.status,
                    "wrist_ok": s.wrist_ok,
                }
                for s in self.segments
            ],
        }

    def _state(self, status: str, detail: str = "", **extra):
        log.info(f"[session-v2] {status}: {detail}")
        if self.on_state_change:
            self.on_state_change(status, detail, **extra)

    def _run(self):
        sid = self.session_id
        session_start = time.time()
        session_deadline = session_start + self.session_duration

        self._state("session_active",
                    f"Session {sid} started — {self.max_segments} segments planned")

        seg_idx = 0

        while (not self._stop.is_set()
               and time.time() < session_deadline
               and seg_idx < self.max_segments):

            seg = SegmentInfo(index=seg_idx)
            seg.wrist_ok = True  # no wrist check — always OK
            self.segments.append(seg)

            # ── Check remaining time ──────────────────────────────────
            remaining = session_deadline - time.time()
            if remaining < 10:
                log.info("Less than 10s remaining, ending session")
                self.segments.pop()
                break
            actual_duration = min(self.segment_duration, remaining)

            # ── Record segment ────────────────────────────────────────
            seg.status = "recording"
            seg.start_time = time.time()
            self._notify_segment(seg)
            self._state("recording",
                        f"Segment {seg_idx + 1}/{self.max_segments} — {int(actual_duration)}s",
                        segment_idx=seg_idx)

            files = self._record_segment(sid, seg_idx, actual_duration)

            seg.end_time = time.time()
            seg.files = files

            # ── Validate mcap file size ───────────────────────────────
            mcap_path = files.get("mcap", "")
            bag_ok = self._validate_bag(mcap_path, seg_idx, actual_duration)

            if not bag_ok:
                seg.status = "failed"
                self._notify_segment(seg)
                self._log_usb_power_diagnostics(seg_idx)
                log.error(f"Segment {seg_idx} FAILED — mcap file is empty/corrupt. "
                          f"Possible USB power issue. Waiting 5s before next segment...")
                time.sleep(5)
                seg_idx += 1
                continue

            seg.status = "uploading"
            self._notify_segment(seg)

            # ── Enqueue upload ────────────────────────────────────────
            if self.upload_queue:
                self.upload_queue.enqueue_segment_files(sid, seg_idx, files)

            seg.status = "complete"
            self._notify_segment(seg)

            seg_idx += 1

        # ── Session complete ──────────────────────────────────────────
        elapsed = time.time() - session_start
        n_complete = sum(1 for s in self.segments if s.status == "complete")

        n_failed = sum(1 for s in self.segments if s.status == "failed")
        manifest = {
            "session_id":       sid,
            "operator_id":      self.operator_id,
            "activity_label":   self.activity_label,
            "segments_complete": n_complete,
            "segments_failed":  n_failed,
            "segments_planned":  self.max_segments,
            "duration_actual":  round(elapsed, 1),
            "mcap_enabled":     True,
            "segments": [
                {"index": s.index, "status": s.status,
                 "files": s.files, "wrist_ok": s.wrist_ok,
                 "mcap_size_mb": round(os.path.getsize(s.files.get("mcap", "")) / 1024 / 1024, 1)
                     if s.files.get("mcap") and os.path.exists(s.files.get("mcap", "")) else 0}
                for s in self.segments
            ],
        }
        manifest_path = os.path.join(self.session_dir, f"manifest_{sid}.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        self._state("complete",
                    f"Session {sid} complete — {n_complete} segments in {elapsed:.0f}s")
        if self.on_complete:
            self.on_complete(sid, n_complete, manifest)

    def _record_segment(self, session_id: str, seg_idx: int,
                        duration: float) -> dict:
        """Record one segment — Orbbec only for `duration` seconds."""
        prefix = f"{self.session_dir}/{session_id}_seg{seg_idx:03d}"
        mcap_out  = f"{prefix}_orbbec.mcap"
        ts_csv    = f"{prefix}_timestamps.csv"

        stop_ev = threading.Event()

        orbbec = OrbbecRecorder(mcap_out, ORBBEC_REC, ORBBEC_LIB)

        orbbec_started = threading.Event()
        orbbec_ok      = [False]

        def orbbec_thread():
            ok = orbbec.start()
            orbbec_ok[0] = ok
            if ok:
                orbbec_started.set()
                stop_ev.wait(timeout=duration)
                stop_ev.set()
                orbbec.stop()
            else:
                orbbec_started.set()
                stop_ev.set()

        threading.Thread(target=orbbec_thread, daemon=True).start()

        if not orbbec_started.wait(timeout=30):
            log.error("Orbbec did not start for segment")
            stop_ev.set()
            return {"mcap": mcap_out}

        if not orbbec_ok[0]:
            log.error("Orbbec recorder failed for segment")
            return {"mcap": mcap_out}

        t0_ns = time.time_ns()

        end_time = time.time() + duration + 2
        while not stop_ev.is_set() and not self._stop.is_set() and time.time() < end_time:
            time.sleep(0.5)

        stop_ev.set()

        with open(ts_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["camera", "event", "unix_ns"])
            w.writerow(["Orbbec", "segment_start", t0_ns])
            w.writerow(["Orbbec", "segment_end", time.time_ns()])

        files = {
            "mcap":       mcap_out,
            "timestamps": ts_csv,
        }

        log.info(f"Segment {seg_idx} recorded — Orbbec mcap: {mcap_out}")
        return files

    def _notify_segment(self, seg: SegmentInfo):
        if self.on_segment_update:
            self.on_segment_update(seg.index, seg.status, seg.wrist_ok)

    def _validate_bag(self, mcap_path: str, seg_idx: int, duration: float) -> bool:
        """
        Check if the .mcap file is a reasonable size.
        At 30fps MJPG color + Y16 depth, expect ~10-15 MB/sec.
        Anything under 1MB for a 60s recording means something went wrong.
        """
        if not mcap_path or not os.path.exists(mcap_path):
            log.error(f"Segment {seg_idx}: mcap file does not exist: {mcap_path}")
            return False

        size_bytes = os.path.getsize(mcap_path)
        size_mb = size_bytes / (1024 * 1024)

        min_expected_mb = max(duration * 1.0, 10)

        log.info(f"Segment {seg_idx} mcap size: {size_mb:.1f} MB "
                 f"(expected ≥ {min_expected_mb:.0f} MB for {duration:.0f}s recording)")

        if size_mb < 0.1:
            log.error(f"MCAP EMPTY — {size_bytes} bytes. "
                      f"USB device likely reset during recording. "
                      f"Check power supply (need 5V/5A for Pi5 + Orbbec Gemini 2L)")
            self._state("mcap_empty_warning",
                        f"Segment {seg_idx + 1}: MCAP EMPTY ({size_bytes} bytes) — "
                        f"USB power issue detected!")
            return False

        if size_mb < min_expected_mb:
            log.warning(f"MCAP SUSPICIOUSLY SMALL — {size_mb:.1f} MB for {duration:.0f}s. "
                        f"Recording may have been interrupted. "
                        f"Expected ≥ {min_expected_mb:.0f} MB")
            self._state("mcap_small_warning",
                        f"Segment {seg_idx + 1}: mcap only {size_mb:.1f} MB "
                        f"(expected ≥ {min_expected_mb:.0f} MB) — partial recording?")
            return True

        log.info(f"Segment {seg_idx} mcap validation PASSED: {size_mb:.1f} MB")
        return True

    def _log_usb_power_diagnostics(self, seg_idx: int):
        """
        Log USB bus and power info to help diagnose empty mcap files.
        """
        import subprocess

        log.info(f"=== USB/POWER DIAGNOSTICS for segment {seg_idx} ===")

        try:
            result = subprocess.run(
                ["dmesg"], capture_output=True, text=True, timeout=5)
            usb_lines = [l for l in result.stdout.split("\n")
                         if "usb" in l.lower() or "USB" in l]
            recent = usb_lines[-15:] if usb_lines else []
            if recent:
                log.warning("Recent USB kernel messages:")
                for line in recent:
                    log.warning(f"  dmesg: {line.strip()}")
            else:
                log.info("  No recent USB messages in dmesg")
        except Exception as e:
            log.warning(f"  Could not read dmesg: {e}")

        try:
            result = subprocess.run(
                ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5)
            throttle = result.stdout.strip()
            log.info(f"  Throttle status: {throttle}")

            if "throttled=0x" in throttle:
                hex_val = int(throttle.split("=")[1], 16)
                if hex_val & 0x1:
                    log.error("  ⚡ UNDER-VOLTAGE DETECTED RIGHT NOW!")
                if hex_val & 0x2:
                    log.warning("  ARM frequency capped")
                if hex_val & 0x4:
                    log.warning("  Currently throttled")
                if hex_val & 0x10000:
                    log.error("  ⚡ UNDER-VOLTAGE HAS OCCURRED since boot!")
                if hex_val & 0x20000:
                    log.warning("  ARM frequency capping has occurred since boot")
                if hex_val & 0x40000:
                    log.warning("  Throttling has occurred since boot")
                if hex_val == 0:
                    log.info("  Power supply looks healthy — no throttle flags")
        except Exception as e:
            log.warning(f"  Could not check throttle: {e}")

        try:
            result = subprocess.run(
                ["lsusb"], capture_output=True, text=True, timeout=5)
            orbbec_lines = [l for l in result.stdout.split("\n")
                            if "orbbec" in l.lower() or "2bc5" in l.lower()]
            if orbbec_lines:
                log.info(f"  Orbbec USB device found: {orbbec_lines[0].strip()}")
            else:
                log.error("  Orbbec USB device NOT FOUND — device disconnected/reset!")
                log.info(f"  All USB devices:\n{result.stdout}")
        except Exception as e:
            log.warning(f"  Could not run lsusb: {e}")

        try:
            for bus_dir in sorted(Path("/sys/bus/usb/devices/").glob("usb*")):
                power_file = bus_dir / "power" / "runtime_status"
                if power_file.exists():
                    status = power_file.read_text().strip()
                    log.info(f"  {bus_dir.name} power status: {status}")
        except Exception as e:
            log.debug(f"  Could not read USB power status: {e}")

        log.info("=== END DIAGNOSTICS ===")
