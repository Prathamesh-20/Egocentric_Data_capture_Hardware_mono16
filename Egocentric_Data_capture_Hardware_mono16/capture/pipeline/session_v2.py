"""
Session V2.1 — 30-min session orchestrator (merged build).

Sequential 1-min Orbbec MCAP segments inside a 30-minute session.

Folder layout (local mirrors S3):
  <OUTPUT_DIR>/<date>/<task>/<env>/<operator>/<inventory>/
      <operator>_<task>_<HHMMSS>/
          *.mcap  *.csv  manifest_*.json

Per segment:
  1. Optional inter-segment wrist check (one frame, no preview).
  2. Spawn OrbbecRecorder via PTY, record for SEGMENT_DURATION seconds.
  3. Crash-poll the recorder at 200 ms; bail early if it exits.
  4. Validate MCAP file size against MIN_SEGMENT_SIZE_MB. Failed
     segments are deleted locally and never enqueued for upload.
  5. Enqueue good MCAP + timestamps CSV under the hierarchical S3 key.
  6. Pause SEGMENT_GAP seconds, repeat.

After the last segment: write manifest_<sid>.json, enqueue it too.

Optional auto-cleanup: if DELETE_AFTER_UPLOAD is on, a background
thread waits for all uploads of THIS session to complete (verified
in S3) and then removes the session directory and any empty parent
directories up to OUTPUT_DIR.
"""
import csv
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from capture.cameras.orbbec import OrbbecRecorder
from capture.config import (
    DELETE_AFTER_UPLOAD,
    FOV_CHECK_ENABLED,
    INTER_SEGMENT_FOV_CHECK_SECS,
    INTER_SEGMENT_MIN_DETECTION_FRAMES,
    INTER_SEGMENT_ON_FAIL,
    MIN_DISK_SPACE_GB,
    MIN_SEGMENT_SIZE_MB,
    ORBBEC_LIB,
    ORBBEC_REC,
    OUTPUT_DIR,
    S3_PREFIX,
    SEGMENT_DURATION,
    SEGMENT_GAP,
    SESSION_DURATION,
    WRIST_CHECK_BETWEEN_SEGMENTS,
)

log = logging.getLogger(__name__)

MIN_DISK_SPACE_BYTES     = MIN_DISK_SPACE_GB * 1024 * 1024 * 1024
MAX_CONSECUTIVE_FAILURES = 3

# How long the auto-cleanup background thread will wait for THIS
# session's uploads to drain before giving up. With never-give-up
# retries, set this generously — 24h tolerates an overnight outage.
UPLOAD_WAIT_TIMEOUT = 24 * 3600


def _sanitise(text: str) -> str:
    """Replace spaces with underscores, strip non-alphanumeric chars.
    Keep capitalisation as typed."""
    return "".join(
        c if c.isalnum() or c == "_" else "_"
        for c in (text or "").strip()
    )


@dataclass
class SegmentInfo:
    index:      int
    status:     str = "pending"   # pending | recording | uploading | complete | failed
    files:      Dict[str, str] = field(default_factory=dict)
    wrist_ok:   Optional[bool] = None
    start_time: Optional[float] = None
    end_time:   Optional[float] = None


class SessionV2:
    """30-minute session with sequential 1-min Orbbec MCAP segments."""

    def __init__(self,
                 operator_id:       str = "",
                 activity_label:    str = "",
                 environment:       str = "",
                 inventory:         str = "",
                 segment_duration:  int = SEGMENT_DURATION,
                 session_duration:  int = SESSION_DURATION,
                 mcap_enabled:      bool = False,
                 on_state_change:   Optional[Callable] = None,
                 on_segment_update: Optional[Callable] = None,
                 on_frame_check:    Optional[Callable] = None,
                 on_complete:       Optional[Callable] = None,
                 on_fov_progress:   Optional[Callable] = None,
                 on_fov_frame:      Optional[Callable] = None,
                 upload_queue=None,
                 gpio=None):
        # Sanitise all four operator-supplied fields.
        self.operator_id    = _sanitise(operator_id)
        self.activity_label = _sanitise(activity_label)
        self.environment    = _sanitise(environment)
        self.inventory      = _sanitise(inventory)

        self.segment_duration = max(1, int(segment_duration))
        self.session_duration = max(1, int(session_duration))
        self.mcap_enabled     = mcap_enabled

        self.on_state_change   = on_state_change
        self.on_segment_update = on_segment_update
        self.on_frame_check    = on_frame_check
        self.on_complete       = on_complete
        # Optional callbacks for the inter-segment FOV check. Server.py
        # supplies these to forward progress + annotated MJPEG frames to
        # /fov-stream so the dashboard reuses its existing preview card
        # for inter-segment checks too.
        self.on_fov_progress   = on_fov_progress
        self.on_fov_frame      = on_fov_frame
        self.upload_queue      = upload_queue
        self.gpio              = gpio

        self._stop                                       = threading.Event()
        self._thread:     Optional[threading.Thread]     = None
        self.session_id:  Optional[str]                  = None
        self.session_dir: Optional[str]                  = None
        self._session_date_str: Optional[str]            = None
        self.segments:    List[SegmentInfo]              = []
        self.max_segments = max(
            1, self.session_duration // self.segment_duration
        )

    # ── Public API ────────────────────────────────────────────────
    def start(self):
        self._stop.clear()
        self.segments.clear()

        now      = datetime.now()
        date_str = now.strftime("%Y%m%d")
        time_str = now.strftime("%H%M%S")

        # session_id is the leaf folder name and used in manifest/S3 keys
        self.session_id = f"{self.operator_id}_{self.activity_label}_{time_str}"

        # Stash date_str so _s3_key uses the SAME date as the local
        # path (otherwise a session crossing midnight would split
        # between two date folders locally vs on S3).
        self._session_date_str = date_str

        # Folder layout — date at the TOP, then task/env/operator/inv/session.
        # Rationale: groups all sessions for one day under one folder so
        # a day's worth of data can be moved/synced/cleaned as a unit.
        self.session_dir = os.path.join(
            OUTPUT_DIR,
            date_str,
            self.activity_label,
            self.environment,
            self.operator_id,
            self.inventory,
            self.session_id,
        )
        try:
            os.makedirs(self.session_dir, exist_ok=True)
        except OSError as e:
            log.error(f"Cannot create session dir {self.session_dir}: {e}")
            self._state("error", f"Cannot create session dir: {e}")
            return

        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"session-{self.session_id}",
        )
        self._thread.start()

    def stop_early(self):
        self._stop.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: Optional[float] = None):
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def get_state(self) -> dict:
        return {
            "session_id":       self.session_id,
            "operator_id":      self.operator_id,
            "activity_label":   self.activity_label,
            "environment":      self.environment,
            "inventory":        self.inventory,
            "segment_duration": self.segment_duration,
            "session_duration": self.session_duration,
            "mcap_enabled":     self.mcap_enabled,
            "max_segments":     self.max_segments,
            "segments": [
                {"index": s.index, "status": s.status, "wrist_ok": s.wrist_ok}
                for s in self.segments
            ],
        }

    # ── Internals ─────────────────────────────────────────────────
    def _state(self, status: str, detail: str = "", **extra):
        log.info(f"[session] {status}: {detail}")
        if self.on_state_change:
            try:
                self.on_state_change(status, detail, **extra)
            except Exception as e:
                log.warning(f"on_state_change raised: {e}")

    def _check_disk_space(self) -> bool:
        try:
            usage   = shutil.disk_usage(OUTPUT_DIR)
            free_gb = usage.free / (1024 ** 3)
            if usage.free < MIN_DISK_SPACE_BYTES:
                log.error(
                    f"DISK SPACE LOW — only {free_gb:.1f} GB free "
                    f"(need {MIN_DISK_SPACE_GB} GB)."
                )
                self._state(
                    "error",
                    f"Disk space too low: {free_gb:.1f} GB free.",
                )
                if self.gpio:
                    try: self.gpio.set_error()
                    except Exception: pass
                return False
            if free_gb < 10:
                log.warning(f"Disk space getting low: {free_gb:.1f} GB free")
            return True
        except OSError as e:
            log.warning(f"Could not check disk space: {e}")
            return True

    def _s3_key(self, filename: str) -> str:
        """Build the S3 key mirroring the local folder structure.

        Layout: <S3_PREFIX><date>/<task>/<env>/<operator>/<inv>/<session>/<filename>
        Date is taken from the value stashed at session start so that
        a session crossing midnight stays together both locally and on S3.
        """
        date_str = self._session_date_str or datetime.now().strftime("%Y%m%d")
        key_path = "/".join([
            date_str,
            self.activity_label,
            self.environment,
            self.operator_id,
            self.inventory,
            self.session_id or "",
            filename,
        ])
        prefix = S3_PREFIX or ""
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"
        return f"{prefix}{key_path}"

    def _maybe_pre_segment_wrist_check(self, seg: SegmentInfo) -> bool:
        """
        Inter-segment FOV check. Runs a full FOVChecker for
        INTER_SEGMENT_FOV_CHECK_SECS seconds before each segment.

        Same machinery as the initial pre-session check:
          - YOLO (or HSV fallback) wrist detection
          - Buzzer alarm: 0/1/2 hands → continuous/intermittent/silent
          - Live MJPEG preview via /fov-stream (if server provided callbacks)

        Returns True if the check passed OR was skipped (so the caller
        proceeds to record). Behaviour on FAIL depends on
        INTER_SEGMENT_ON_FAIL:
          - "warn": log the warning, return True (record anyway)
          - "skip": return False (caller skips this segment)
          - "wait": loop until it passes or session ends, return True
                    if it eventually passes
        """
        if not WRIST_CHECK_BETWEEN_SEGMENTS or not FOV_CHECK_ENABLED:
            return True

        try:
            from capture.cameras.fov_check import FOVChecker
        except Exception as e:
            log.warning(f"Inter-segment FOV check unavailable: {e}")
            return True

        # Tell the dashboard the check is starting. The /fov-stream
        # MJPEG endpoint is already wired up; we just need the
        # status="fov_checking" transition so the preview card shows.
        self._state(
            "fov_checking",
            f"Pre-segment FOV check ({INTER_SEGMENT_FOV_CHECK_SECS}s) — "
            f"keep both wrists visible",
            segment_idx=seg.index,
        )

        attempt = 0
        while True:
            attempt += 1

            checker = FOVChecker(
                duration_sec         = INTER_SEGMENT_FOV_CHECK_SECS,
                min_detection_frames = INTER_SEGMENT_MIN_DETECTION_FRAMES,
                frame_cb             = self.on_fov_frame,
                progress_cb          = self.on_fov_progress,
                gpio                 = self.gpio,
                # Skip AE re-settle — the recorder just had the camera
                # streaming, so exposure is already converged. Saves
                # ~1.5s of useful detection time inside a 3s budget.
                skip_settle          = True,
            )

            try:
                result = checker.run()
            except Exception as e:
                log.warning(
                    f"Inter-segment FOV check raised: {e} — "
                    f"proceeding with segment"
                )
                return True

            seg.wrist_ok = bool(result.passed)
            if self.on_frame_check:
                try:
                    self.on_frame_check(seg.index, result)
                except Exception as e:
                    log.warning(f"on_frame_check raised: {e}")

            if result.passed:
                log.info(
                    f"Inter-segment FOV PASS seg={seg.index} "
                    f"(attempt {attempt}): {result.message}"
                )
                return True

            log.warning(
                f"Inter-segment FOV FAIL seg={seg.index} "
                f"(attempt {attempt}): {result.message}"
            )

            # Failed — pick behaviour from config.
            if INTER_SEGMENT_ON_FAIL == "skip":
                self._state(
                    "wrist_check_failed",
                    f"Segment {seg.index + 1} skipped: {result.message}",
                    segment_idx=seg.index,
                )
                return False

            if INTER_SEGMENT_ON_FAIL == "wait":
                # Re-run the check until it passes or the session ends.
                if self._stop.is_set():
                    return True
                if time.time() + INTER_SEGMENT_FOV_CHECK_SECS > (
                    self._session_deadline if hasattr(self, "_session_deadline")
                    else float("inf")
                ):
                    log.warning(
                        "Inter-segment FOV: not enough time left to retry, "
                        "proceeding with segment"
                    )
                    return True
                self._state(
                    "wrist_check_failed",
                    f"Segment {seg.index + 1}: hands not visible — "
                    f"retrying check",
                    segment_idx=seg.index,
                )
                continue

            # Default: "warn" — record the segment anyway.
            self._state(
                "wrist_check_failed",
                f"Segment {seg.index + 1}: {result.message} "
                f"(recording anyway)",
                segment_idx=seg.index,
            )
            return True

    def _run(self):
        sid              = self.session_id
        session_start    = time.time()
        session_deadline = session_start + self.session_duration
        # Stash for _maybe_pre_segment_wrist_check's "wait" mode.
        self._session_deadline = session_deadline

        self._state(
            "session_active",
            f"Session {sid} started — {self.max_segments} segments planned",
        )

        seg_idx              = 0
        consecutive_failures = 0

        while (
            not self._stop.is_set()
            and time.time() < session_deadline
            and seg_idx < self.max_segments
        ):
            if not self._check_disk_space():
                break

            remaining = session_deadline - time.time()
            if remaining < 10:
                log.info("Less than 10s remaining, ending session")
                break
            actual_duration = min(self.segment_duration, remaining)

            seg          = SegmentInfo(index=seg_idx)
            seg.wrist_ok = True
            self.segments.append(seg)

            # Inter-segment FOV check. Returns False only if the check
            # failed AND config says "skip" — in which case we mark this
            # segment failed and advance without recording it.
            if not self._maybe_pre_segment_wrist_check(seg):
                seg.status = "failed"
                self._notify_segment(seg)
                log.info(
                    f"Segment {seg_idx} skipped due to inter-segment "
                    f"FOV failure"
                )
                seg_idx += 1
                continue

            if self.gpio:
                try:
                    self.gpio.beep_1x()
                    self.gpio.set_recording()
                except Exception as e:
                    log.warning(f"GPIO recording state failed: {e}")

            seg.status     = "recording"
            seg.start_time = time.time()
            self._notify_segment(seg)
            self._state(
                "recording",
                f"Segment {seg_idx + 1}/{self.max_segments} — "
                f"{int(actual_duration)}s",
                segment_idx=seg_idx,
            )

            files        = self._record_segment(sid, seg_idx, actual_duration)
            seg.end_time = time.time()
            seg.files    = files

            mcap_path = files.get("mcap", "")
            mcap_ok   = self._validate_mcap(mcap_path, seg_idx)

            if not mcap_ok:
                seg.status = "failed"
                self._notify_segment(seg)
                consecutive_failures += 1
                self._log_usb_power_diagnostics(seg_idx)
                log.error(
                    f"Segment {seg_idx} FAILED — consecutive: "
                    f"{consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}"
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error(
                        "Too many consecutive failures — auto-stopping"
                    )
                    self._state(
                        "error",
                        f"Session auto-stopped — "
                        f"{MAX_CONSECUTIVE_FAILURES} consecutive failures. "
                        f"Check camera connection.",
                    )
                    if self.gpio:
                        try: self.gpio.set_error()
                        except Exception: pass
                    break
                self._stop.wait(timeout=5)
                seg_idx += 1
                continue

            consecutive_failures = 0

            if self.gpio:
                try:
                    self.gpio.set_segment_gap()
                    self.gpio.beep_2x()
                except Exception as e:
                    log.warning(f"GPIO segment-gap state failed: {e}")

            seg.status = "uploading"
            self._notify_segment(seg)

            # Enqueue with hierarchical S3 keys.
            if self.upload_queue:
                try:
                    self.upload_queue.enqueue_segment_files(
                        sid, seg_idx, files,
                        s3_key_builder=self._s3_key,
                    )
                except Exception as e:
                    log.error(f"Upload enqueue failed for seg {seg_idx}: {e}")

            seg.status = "complete"
            self._notify_segment(seg)
            seg_idx += 1

            if (
                not self._stop.is_set()
                and time.time() < session_deadline
                and seg_idx < self.max_segments
            ):
                log.info(f"Waiting {SEGMENT_GAP}s before next segment")
                self._stop.wait(timeout=SEGMENT_GAP)

        self._finalize_session(sid, session_start)

    def _finalize_session(self, sid: str, session_start: float):
        elapsed    = time.time() - session_start
        n_complete = sum(1 for s in self.segments if s.status == "complete")
        n_failed   = sum(1 for s in self.segments if s.status == "failed")

        if self.gpio:
            try: self.gpio.set_uploading()
            except Exception: pass

        # Clean up failed mcap files locally so they don't waste disk.
        for seg in self.segments:
            if seg.status == "failed":
                mcap_file = seg.files.get("mcap", "")
                if mcap_file and os.path.exists(mcap_file):
                    try:
                        os.remove(mcap_file)
                        log.info(f"Cleaned up failed mcap: {mcap_file}")
                    except OSError as e:
                        log.warning(f"Could not clean up {mcap_file}: {e}")

        free_gb = None
        try:
            usage   = shutil.disk_usage(OUTPUT_DIR)
            free_gb = round(usage.free / (1024 ** 3), 1)
            log.info(f"Disk space remaining: {free_gb} GB")
        except OSError:
            pass

        manifest = {
            "session_id":        sid,
            "operator_id":       self.operator_id,
            "activity_label":    self.activity_label,
            "environment":       self.environment,
            "inventory":         self.inventory,
            "segments_complete": n_complete,
            "segments_failed":   n_failed,
            "segments_planned":  self.max_segments,
            "duration_actual":   round(elapsed, 1),
            "mcap_enabled":      self.mcap_enabled,
            "disk_free_gb":      free_gb,
            "segments": [
                {
                    "index":        s.index,
                    "status":       s.status,
                    "files":        s.files,
                    "wrist_ok":     s.wrist_ok,
                    "mcap_size_mb": (
                        round(
                            os.path.getsize(s.files.get("mcap", ""))
                            / 1024 / 1024, 1,
                        )
                        if s.files.get("mcap")
                        and os.path.exists(s.files.get("mcap", ""))
                        else 0
                    ),
                }
                for s in self.segments
            ],
        }
        manifest_path = os.path.join(
            self.session_dir, f"manifest_{sid}.json"
        )
        try:
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)
            log.info(f"Manifest written: {manifest_path}")
        except OSError as e:
            log.error(f"Could not write manifest: {e}")
            manifest_path = None

        # Enqueue manifest to S3 alongside the segments.
        if manifest_path and self.upload_queue:
            try:
                self.upload_queue.enqueue(
                    manifest_path,
                    self._s3_key(os.path.basename(manifest_path)),
                    -1, sid,
                )
            except Exception as e:
                log.warning(f"Manifest enqueue failed: {e}")

        self._state(
            "complete",
            f"Session {sid} complete — {n_complete} segments "
            f"({n_failed} failed) in {elapsed:.0f}s",
        )
        if self.on_complete:
            try:
                self.on_complete(sid, n_complete, manifest)
            except Exception as e:
                log.warning(f"on_complete raised: {e}")

        # Background cleanup once uploads of THIS session drain.
        if DELETE_AFTER_UPLOAD and self.upload_queue:
            threading.Thread(
                target=self._wait_for_uploads_and_cleanup,
                args=(sid,),
                daemon=True,
                name=f"cleanup-{sid}",
            ).start()

    # ── Local cleanup post-upload ─────────────────────────────────
    def _prune_empty_parents(self, leaf_dir: str):
        """After deleting a session dir, remove now-empty parent dirs
        up to (but not including) OUTPUT_DIR."""
        try:
            parent      = os.path.dirname(os.path.abspath(leaf_dir))
            output_root = os.path.abspath(OUTPUT_DIR)
            while (
                parent
                and parent != output_root
                and parent.startswith(output_root + os.sep)
            ):
                if not os.path.isdir(parent) or os.listdir(parent):
                    break
                os.rmdir(parent)
                log.info(f"Pruned empty parent dir: {parent}")
                parent = os.path.dirname(parent)
        except Exception as e:
            log.warning(
                f"Could not prune empty parents under {leaf_dir}: {e}"
            )

    def _wait_for_uploads_and_cleanup(self, sid: str):
        """Wait for all queued uploads for this session to finish, then
        remove the session directory from the SSD. The uploader already
        deletes individual files as each one verifies in S3 — this
        removes the now-empty session folder + manifest."""
        if not DELETE_AFTER_UPLOAD or not self.upload_queue:
            return

        log.info(
            f"Waiting for all uploads before cleaning up: {self.session_dir}"
        )
        deadline      = time.time() + UPLOAD_WAIT_TIMEOUT
        poll_interval = 10

        while time.time() < deadline:
            status        = self.upload_queue.get_status()
            session_items = [
                i for i in status.get("items", [])
                if i.get("session_id") == sid
                or sid in i.get("s3_key", "")
            ]
            pending = [
                i for i in session_items
                if i.get("status") in ("queued", "uploading", "retrying")
            ]
            failed = [
                i for i in session_items
                if i.get("status") == "failed"
            ]

            if not pending:
                if failed:
                    log.warning(
                        f"{len(failed)} file(s) failed to upload — "
                        f"session directory will NOT be deleted: "
                        f"{self.session_dir}"
                    )
                    return

                log.info(
                    f"All uploads complete. Removing session directory: "
                    f"{self.session_dir}"
                )
                try:
                    shutil.rmtree(self.session_dir)
                    log.info(
                        f"Session directory removed: {self.session_dir}"
                    )
                    self._prune_empty_parents(self.session_dir)
                except Exception as e:
                    log.warning(
                        f"Could not remove session directory: {e}"
                    )
                return

            log.info(
                f"Waiting for uploads — {len(pending)} pending. "
                f"Re-checking in {poll_interval}s..."
            )
            time.sleep(poll_interval)

        log.warning(
            f"Upload wait timed out — session directory NOT deleted: "
            f"{self.session_dir}"
        )

    # ── Recording ─────────────────────────────────────────────────
    def _record_segment(self, session_id: str, seg_idx: int,
                        duration: float) -> dict:
        prefix   = f"{self.session_dir}/{session_id}_seg{seg_idx:03d}"
        mcap_out = f"{prefix}_orbbec.mcap"
        ts_csv   = f"{prefix}_timestamps.csv"

        orbbec         = OrbbecRecorder(mcap_out, ORBBEC_REC, ORBBEC_LIB)
        orbbec_started = threading.Event()
        orbbec_ok      = [False]

        def orbbec_thread():
            try:
                ok = orbbec.start()
                orbbec_ok[0] = ok
            except Exception as e:
                log.error(f"Orbbec recorder start raised: {e}")
                orbbec_ok[0] = False
            finally:
                orbbec_started.set()

        threading.Thread(
            target=orbbec_thread, daemon=True,
            name=f"orbbec-start-{seg_idx}",
        ).start()

        if not orbbec_started.wait(timeout=45):
            log.error("Orbbec did not start for segment within 45s")
            try: orbbec.stop()
            except Exception: pass
            return {"mcap": mcap_out}

        if not orbbec_ok[0]:
            log.error("Orbbec recorder failed to start for segment")
            try: orbbec.stop()
            except Exception: pass
            return {"mcap": mcap_out}

        t0_ns      = time.time_ns()
        end_time   = time.time() + duration
        crash_seen = False
        while time.time() < end_time:
            if self._stop.is_set():
                log.info("Stop requested during segment, ending early")
                break
            if not orbbec.is_alive() or orbbec.crashed:
                log.error(
                    f"Orbbec recorder exited unexpectedly during "
                    f"segment {seg_idx}"
                )
                crash_seen = True
                break
            time.sleep(0.2)

        try:
            orbbec.stop()
        except Exception as e:
            log.warning(f"Orbbec stop raised: {e}")

        try:
            with open(ts_csv, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["camera", "event", "unix_ns"])
                w.writerow(["Orbbec", "segment_start", t0_ns])
                w.writerow(["Orbbec", "segment_end", time.time_ns()])
                if crash_seen or orbbec.crashed:
                    w.writerow(["Orbbec", "CRASH_DETECTED", time.time_ns()])
        except OSError as e:
            log.warning(f"Could not write timestamps CSV: {e}")

        log.info(f"Segment {seg_idx} recorded -> {mcap_out}")
        return {"mcap": mcap_out, "timestamps": ts_csv}

    def _notify_segment(self, seg: SegmentInfo):
        if self.on_segment_update:
            try:
                self.on_segment_update(
                    seg.index, seg.status, seg.wrist_ok,
                )
            except Exception as e:
                log.warning(f"on_segment_update raised: {e}")

    def _validate_mcap(self, mcap_path: str, seg_idx: int) -> bool:
        if not mcap_path or not os.path.exists(mcap_path):
            log.error(
                f"Segment {seg_idx}: mcap file does not exist: {mcap_path}"
            )
            return False

        try:
            size_bytes = os.path.getsize(mcap_path)
        except OSError as e:
            log.error(f"Segment {seg_idx}: cannot stat mcap: {e}")
            return False

        size_mb = size_bytes / (1024 * 1024)
        log.info(
            f"Segment {seg_idx} mcap: {size_mb:.1f} MB "
            f"(min required {MIN_SEGMENT_SIZE_MB} MB)"
        )

        if size_mb < 0.1:
            log.error(f"MCAP EMPTY — {size_bytes} bytes. USB power issue?")
            self._state(
                "mcap_empty_warning",
                f"Segment {seg_idx + 1}: MCAP EMPTY — USB power issue!",
                segment_idx=seg_idx,
            )
            return False
        if size_mb < MIN_SEGMENT_SIZE_MB:
            log.error(
                f"MCAP TOO SMALL — {size_mb:.1f} MB "
                f"< {MIN_SEGMENT_SIZE_MB} MB"
            )
            self._state(
                "mcap_small_warning",
                f"Segment {seg_idx + 1}: only {size_mb:.1f} MB — "
                f"episode not saved, please record again",
                segment_idx=seg_idx,
            )
            return False

        log.info(f"Segment {seg_idx} mcap PASSED: {size_mb:.1f} MB")
        return True

    def _log_usb_power_diagnostics(self, seg_idx: int):
        log.info(f"=== USB/POWER DIAGNOSTICS for segment {seg_idx} ===")
        try:
            result    = subprocess.run(
                ["dmesg"], capture_output=True, text=True, timeout=5,
            )
            usb_lines = [l for l in result.stdout.split("\n")
                         if "usb" in l.lower()]
            for line in usb_lines[-10:]:
                log.warning(f"  dmesg: {line.strip()}")
        except (FileNotFoundError, subprocess.TimeoutExpired,
                subprocess.SubprocessError, OSError) as e:
            log.warning(f"  Could not read dmesg: {e}")
        try:
            result   = subprocess.run(
                ["vcgencmd", "get_throttled"],
                capture_output=True, text=True, timeout=5,
            )
            throttle = result.stdout.strip()
            log.info(f"  Throttle: {throttle}")
            if "throttled=0x" in throttle:
                val = int(throttle.split("=")[1], 16)
                if val & 0x1:     log.error("  UNDER-VOLTAGE NOW!")
                if val & 0x10000: log.error("  UNDER-VOLTAGE SINCE BOOT!")
                if val == 0:      log.info("  Power supply healthy")
        except (FileNotFoundError, subprocess.TimeoutExpired,
                subprocess.SubprocessError, OSError) as e:
            log.warning(f"  Could not check throttle: {e}")
        try:
            result = subprocess.run(
                ["lsusb"], capture_output=True, text=True, timeout=5,
            )
            orbbec = [l for l in result.stdout.split("\n")
                      if "2bc5" in l.lower()]
            if orbbec:
                log.info(f"  Orbbec: {orbbec[0].strip()}")
            else:
                log.error("  Orbbec NOT FOUND in lsusb!")
        except (FileNotFoundError, subprocess.TimeoutExpired,
                subprocess.SubprocessError, OSError) as e:
            log.warning(f"  Could not run lsusb: {e}")
        log.info("=== END DIAGNOSTICS ===")
