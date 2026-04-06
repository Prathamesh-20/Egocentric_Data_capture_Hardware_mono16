"""
Session V2 — 30-min session orchestrator
"""
import os, csv, time, threading, logging, json, shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Dict, List

from capture.config import (
    OUTPUT_DIR, ORBBEC_REC, ORBBEC_LIB,
    FPS, SEGMENT_DURATION, SESSION_DURATION, SEGMENT_GAP,
)
from capture.cameras.orbbec import OrbbecRecorder

log = logging.getLogger(__name__)

MIN_DISK_SPACE_GB    = 5
MIN_DISK_SPACE_BYTES = MIN_DISK_SPACE_GB * 1024 * 1024 * 1024
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class SegmentInfo:
    index:      int
    status:     str = "pending"
    files:      Dict[str, str] = field(default_factory=dict)
    wrist_ok:   Optional[bool] = None
    start_time: Optional[float] = None
    end_time:   Optional[float] = None


class SessionV2:
    """
    30-minute session with sequential 1-min segments.
    Orbbec Gemini 2L only — RGB + Depth .bag recording.
    """

    def __init__(self,
                 operator_id:       str = "",
                 operator_name:     str = "",   # ← for S3 folder structure
                 task_id:           str = "",   # ← for S3 folder structure
                 activity_label:    str = "",
                 segment_duration:  int = SEGMENT_DURATION,
                 session_duration:  int = SESSION_DURATION,
                 mcap_enabled:      bool = False,
                 on_state_change:   Callable = None,
                 on_segment_update: Callable = None,
                 on_frame_check:    Callable = None,
                 on_complete:       Callable = None,
                 upload_queue=None,
                 gpio=None):

        self.operator_id      = operator_id
        self.operator_name    = operator_name
        self.task_id          = task_id
        self.activity_label   = activity_label
        self.segment_duration = segment_duration
        self.session_duration = session_duration
        self.mcap_enabled     = mcap_enabled

        self.on_state_change   = on_state_change
        self.on_segment_update = on_segment_update
        self.on_frame_check    = on_frame_check
        self.on_complete       = on_complete
        self.upload_queue      = upload_queue
        self.gpio              = gpio

        self._stop        = threading.Event()
        self._thread      = None
        self.session_id   = None
        self.session_dir  = None
        self.segments:    List[SegmentInfo] = []
        self.max_segments = session_duration // segment_duration

    def start(self):
        self._stop.clear()
        self.segments.clear()
        self.session_id  = datetime.now().strftime("%Y%m%d_%H%M%S")
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
            "operator_name":    self.operator_name,
            "task_id":          self.task_id,
            "activity_label":   self.activity_label,
            "segment_duration": self.segment_duration,
            "session_duration": self.session_duration,
            "mcap_enabled":     self.mcap_enabled,
            "max_segments":     self.max_segments,
            "segments": [
                {"index": s.index, "status": s.status, "wrist_ok": s.wrist_ok}
                for s in self.segments
            ],
        }

    def _state(self, status: str, detail: str = "", **extra):
        log.info(f"[session-v2] {status}: {detail}")
        if self.on_state_change:
            self.on_state_change(status, detail, **extra)

    def _check_disk_space(self) -> bool:
        try:
            usage  = shutil.disk_usage(OUTPUT_DIR)
            free_gb = usage.free / (1024 * 1024 * 1024)
            if usage.free < MIN_DISK_SPACE_BYTES:
                log.error(f"DISK SPACE LOW — only {free_gb:.1f} GB free. Stopping session!")
                self._state("error", f"Disk space too low: {free_gb:.1f} GB free.")
                if self.gpio:
                    self.gpio.set_error()
                return False
            if free_gb < 10:
                log.warning(f"Disk space getting low: {free_gb:.1f} GB free")
            return True
        except Exception as e:
            log.warning(f"Could not check disk space: {e}")
            return True

    def _run(self):
        sid              = self.session_id
        session_start    = time.time()
        session_deadline = session_start + self.session_duration

        self._state("session_active",
                    f"Session {sid} started — {self.max_segments} segments planned")

        seg_idx              = 0
        consecutive_failures = 0

        while (not self._stop.is_set()
               and time.time() < session_deadline
               and seg_idx < self.max_segments):

            seg          = SegmentInfo(index=seg_idx)
            seg.wrist_ok = True
            self.segments.append(seg)

            if not self._check_disk_space():
                self.segments.pop()
                break

            remaining = session_deadline - time.time()
            if remaining < 10:
                log.info("Less than 10s remaining, ending session")
                self.segments.pop()
                break
            actual_duration = min(self.segment_duration, remaining)

            # GPIO: segment starting
            if self.gpio:
                self.gpio.beep_1x()
                self.gpio.set_recording()

            seg.status     = "recording"
            seg.start_time = time.time()
            self._notify_segment(seg)
            self._state("recording",
                        f"Segment {seg_idx + 1}/{self.max_segments} — {int(actual_duration)}s",
                        segment_idx=seg_idx)

            files    = self._record_segment(sid, seg_idx, actual_duration)
            seg.end_time = time.time()
            seg.files    = files

            bag_path = files.get("bag", "")
            bag_ok   = self._validate_bag(bag_path, seg_idx, actual_duration)

            if not bag_ok:
                seg.status = "failed"
                self._notify_segment(seg)
                consecutive_failures += 1
                self._log_usb_power_diagnostics(seg_idx)
                log.error(f"Segment {seg_idx} FAILED — consecutive: "
                          f"{consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error("Too many consecutive failures — auto-stopping session.")
                    self._state("error",
                                f"Session auto-stopped — {MAX_CONSECUTIVE_FAILURES} "
                                f"consecutive failures. Check camera connection.")
                    if self.gpio:
                        self.gpio.set_error()
                    break
                time.sleep(5)
                seg_idx += 1
                continue

            consecutive_failures = 0

            # GPIO: segment complete
            if self.gpio:
                self.gpio.set_segment_gap()
                self.gpio.beep_2x()

            seg.status = "uploading"
            self._notify_segment(seg)

            if self.upload_queue:
                self.upload_queue.enqueue_segment_files(
                    sid, seg_idx, files,
                    operator_name=self.operator_name,
                    task_id=self.task_id,
                )

            seg.status = "complete"
            self._notify_segment(seg)
            seg_idx += 1

            # Inter-segment gap
            if (not self._stop.is_set()
                    and time.time() < session_deadline
                    and seg_idx < self.max_segments):
                log.info(f"Waiting {SEGMENT_GAP}s before next segment...")
                self._stop.wait(timeout=SEGMENT_GAP)

        # ── Session complete ──────────────────────────────────────
        elapsed    = time.time() - session_start
        n_complete = sum(1 for s in self.segments if s.status == "complete")
        n_failed   = sum(1 for s in self.segments if s.status == "failed")

        # GPIO: switch to uploading indicator
        if self.gpio:
            self.gpio.set_uploading()

        # Clean up failed bag files
        for seg in self.segments:
            if seg.status == "failed":
                bag_file = seg.files.get("bag", "")
                if bag_file and os.path.exists(bag_file):
                    try:
                        os.remove(bag_file)
                        log.info(f"Cleaned up failed bag: {bag_file}")
                    except Exception as e:
                        log.warning(f"Could not clean up {bag_file}: {e}")

        free_gb = None
        try:
            usage   = shutil.disk_usage(OUTPUT_DIR)
            free_gb = round(usage.free / (1024 * 1024 * 1024), 1)
            log.info(f"Disk space remaining: {free_gb} GB")
        except Exception:
            pass

        manifest = {
            "session_id":        sid,
            "operator_id":       self.operator_id,
            "operator_name":     self.operator_name,
            "task_id":           self.task_id,
            "activity_label":    self.activity_label,
            "segments_complete": n_complete,
            "segments_failed":   n_failed,
            "segments_planned":  self.max_segments,
            "duration_actual":   round(elapsed, 1),
            "mcap_enabled":      self.mcap_enabled,
            "disk_free_gb":      free_gb,
            "segments": [
                {
                    "index":      s.index,
                    "status":     s.status,
                    "files":      s.files,
                    "wrist_ok":   s.wrist_ok,
                    "bag_size_mb": round(
                        os.path.getsize(s.files.get("bag", "")) / 1024 / 1024, 1)
                        if s.files.get("bag") and os.path.exists(s.files.get("bag", ""))
                        else 0,
                }
                for s in self.segments
            ],
        }
        manifest_path = os.path.join(self.session_dir, f"manifest_{sid}.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        self._state("complete",
                    f"Session {sid} complete — {n_complete} segments "
                    f"({n_failed} failed) in {elapsed:.0f}s")
        if self.on_complete:
            self.on_complete(sid, n_complete, manifest)

    def _record_segment(self, session_id: str, seg_idx: int, duration: float) -> dict:
        prefix  = f"{self.session_dir}/{session_id}_seg{seg_idx:03d}"
        bag_out = f"{prefix}_orbbec.bag"
        ts_csv  = f"{prefix}_timestamps.csv"

        stop_ev        = threading.Event()
        orbbec         = OrbbecRecorder(bag_out, ORBBEC_REC, ORBBEC_LIB)
        orbbec_started = threading.Event()
        orbbec_ok      = [False]
        orbbec_crashed = [False]

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
            return {"bag": bag_out}

        if not orbbec_ok[0]:
            log.error("Orbbec recorder failed for segment")
            return {"bag": bag_out}

        t0_ns    = time.time_ns()
        end_time = time.time() + duration + 2
        while not stop_ev.is_set() and not self._stop.is_set() and time.time() < end_time:
            if orbbec._proc and orbbec._proc.poll() is not None:
                log.error(f"Orbbec process crashed! Exit code: {orbbec._proc.returncode}")
                orbbec_crashed[0] = True
                stop_ev.set()
                break
            time.sleep(0.5)
        stop_ev.set()

        with open(ts_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["camera", "event", "unix_ns"])
            w.writerow(["Orbbec", "segment_start", t0_ns])
            w.writerow(["Orbbec", "segment_end", time.time_ns()])
            if orbbec_crashed[0]:
                w.writerow(["Orbbec", "CRASH_DETECTED", time.time_ns()])

        log.info(f"Segment {seg_idx} recorded — {bag_out}")
        return {"bag": bag_out, "timestamps": ts_csv}

    def _notify_segment(self, seg: SegmentInfo):
        if self.on_segment_update:
            self.on_segment_update(seg.index, seg.status, seg.wrist_ok)

    def _validate_bag(self, bag_path: str, seg_idx: int, duration: float) -> bool:
        if not bag_path or not os.path.exists(bag_path):
            log.error(f"Segment {seg_idx}: bag file does not exist: {bag_path}")
            return False
        size_bytes      = os.path.getsize(bag_path)
        size_mb         = size_bytes / (1024 * 1024)
        min_expected_mb = max(duration * 1.0, 10)
        log.info(f"Segment {seg_idx} bag: {size_mb:.1f} MB (expected >= {min_expected_mb:.0f} MB)")
        if size_mb < 0.1:
            log.error(f"BAG EMPTY — {size_bytes} bytes. USB power issue?")
            self._state("bag_empty_warning",
                        f"Segment {seg_idx + 1}: BAG EMPTY — USB power issue!")
            return False
        if size_mb < min_expected_mb:
            log.warning(f"BAG SMALL — {size_mb:.1f} MB for {duration:.0f}s")
            self._state("bag_small_warning",
                        f"Segment {seg_idx + 1}: bag only {size_mb:.1f} MB — partial?")
            return True
        log.info(f"Segment {seg_idx} bag PASSED: {size_mb:.1f} MB")
        return True

    def _log_usb_power_diagnostics(self, seg_idx: int):
        import subprocess
        log.info(f"=== USB/POWER DIAGNOSTICS for segment {seg_idx} ===")
        try:
            result = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=5)
            usb_lines = [l for l in result.stdout.split("\n") if "usb" in l.lower()]
            for line in usb_lines[-10:]:
                log.warning(f"  dmesg: {line.strip()}")
        except Exception as e:
            log.warning(f"  Could not read dmesg: {e}")
        try:
            result = subprocess.run(
                ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5)
            log.info(f"  Throttle: {result.stdout.strip()}")
        except Exception as e:
            log.warning(f"  Could not check throttle: {e}")
        try:
            result = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
            orbbec = [l for l in result.stdout.split("\n") if "2bc5" in l.lower()]
            if orbbec:
                log.info(f"  Orbbec: {orbbec[0].strip()}")
            else:
                log.error("  Orbbec NOT FOUND in lsusb!")
        except Exception as e:
            log.warning(f"  Could not run lsusb: {e}")
        log.info("=== END DIAGNOSTICS ===")
