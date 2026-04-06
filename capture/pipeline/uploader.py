"""
S3 Upload Queue — background uploader with improved retry logic.
"""
import os, time, logging, threading, glob
from dataclasses import dataclass
from typing import List, Optional, Callable
from enum import Enum
from pathlib import Path

from capture.config import S3_BUCKET, S3_PREFIX, OUTPUT_DIR

log = logging.getLogger(__name__)

PENDING_DIR = os.path.join(OUTPUT_DIR, ".pending_uploads")


class UploadStatus(str, Enum):
    QUEUED    = "queued"
    UPLOADING = "uploading"
    COMPLETE  = "complete"
    FAILED    = "failed"
    RETRYING  = "retrying"


@dataclass
class UploadItem:
    local_path:     str
    s3_key:         str
    status:         UploadStatus = UploadStatus.QUEUED
    attempts:       int = 0
    error:          Optional[str] = None
    size_bytes:     int = 0
    bytes_uploaded: int = 0
    progress_pct:   float = 0.0
    segment_idx:    int = -1
    session_id:     str = ""

    def to_dict(self):
        return {
            "local_path":     self.local_path,
            "s3_key":         self.s3_key,
            "status":         self.status.value,
            "attempts":       self.attempts,
            "error":          self.error,
            "size_bytes":     self.size_bytes,
            "bytes_uploaded": self.bytes_uploaded,
            "progress_pct":   round(self.progress_pct, 1),
            "segment_idx":    self.segment_idx,
            "session_id":     self.session_id,
            "filename":       os.path.basename(self.local_path),
        }


class UploadQueue:
    """
    Thread-safe S3 upload queue with infinite retry + exponential backoff.
    Moves files to .pending_uploads/ before upload — safe against power loss.
    On init, scans .pending_uploads/ and re-queues any leftover files.
    """

    def __init__(
        self,
        on_status_change: Optional[Callable] = None,
        on_complete: Optional[Callable[[str, str, int, str], None]] = None,
    ):
        """
        on_complete(local_path, s3_key, segment_idx, session_id) — called after
        each successful upload. polling_agent hooks this to create episode in backend.
        """
        self._queue: List[UploadItem] = []
        self._lock   = threading.Lock()
        self._event  = threading.Event()
        self._stop   = threading.Event()
        self._worker = None
        self._s3     = None
        self.on_status_change = on_status_change
        self.on_complete      = on_complete

        os.makedirs(PENDING_DIR, exist_ok=True)
        self._resume_pending()

    # ── Public API ────────────────────────────────────────────────

    def start(self):
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True, name="uploader")
        self._worker.start()
        log.info("Upload queue worker started")

    def stop(self):
        self._stop.set()
        self._event.set()
        if self._worker:
            self._worker.join(timeout=10)
        log.info("Upload queue worker stopped")

    def enqueue(self, local_path: str, s3_key: str, segment_idx: int = -1, session_id: str = ""):
        """Move file to pending folder and add to queue."""
        if not os.path.exists(local_path):
            log.warning(f"enqueue: file not found — {local_path}")
            return

        # Move to hidden pending folder so a crash/power-loss doesn't
        # leave the file in the main recordings dir untracked
        pending_path = os.path.join(PENDING_DIR, os.path.basename(local_path))
        try:
            os.rename(local_path, pending_path)
        except OSError as e:
            log.error(f"Could not move to pending: {e}")
            pending_path = local_path  # fall back to original location

        size = 0
        try:
            size = os.path.getsize(pending_path)
        except OSError:
            pass

        item = UploadItem(
            local_path=pending_path,
            s3_key=s3_key,
            size_bytes=size,
            segment_idx=segment_idx,
            session_id=session_id,
        )
        with self._lock:
            self._queue.append(item)
        self._event.set()
        self._notify()
        log.info(f"Enqueued: {os.path.basename(pending_path)} → s3://{S3_BUCKET}/{s3_key}")

    def enqueue_segment_files(self, session_id: str, segment_idx: int, files: dict,
                              operator_name: str = "", task_id: str = ""):
        """Enqueue all files from a completed segment."""
        for label, path in files.items():
            if path and os.path.exists(path):
                fname  = os.path.basename(path)
                # S3 key structure: captures/{operator_name}/{task_id}/session_{id}/seg_{N}/{file}
                if operator_name and task_id:
                    s3_key = f"{S3_PREFIX}{operator_name}/{task_id}/session_{session_id}/seg_{segment_idx:03d}/{fname}"
                else:
                    s3_key = f"{S3_PREFIX}{session_id}/seg_{segment_idx:03d}/{fname}"
                self.enqueue(path, s3_key, segment_idx, session_id)

    def get_status(self) -> dict:
        with self._lock:
            items = [i.to_dict() for i in self._queue]
        counts = {s.value: 0 for s in UploadStatus}
        for i in items:
            counts[i["status"]] += 1
        return {
            "items":     items,
            "total":     len(items),
            "queued":    counts["queued"],
            "uploading": counts["uploading"],
            "complete":  counts["complete"],
            "failed":    counts["failed"],
            "retrying":  counts["retrying"],
        }

    # ── Private ───────────────────────────────────────────────────

    def _resume_pending(self):
        """Re-queue any files left in .pending_uploads/ from a previous run."""
        leftover = glob.glob(os.path.join(PENDING_DIR, "**", "*"), recursive=True)
        leftover = [f for f in leftover if os.path.isfile(f)]
        if not leftover:
            return
        log.info(f"Boot resume: found {len(leftover)} pending file(s) — re-queuing")
        for path in leftover:
            fname  = os.path.basename(path)
            # Reconstruct a best-effort s3 key (session unknown after crash)
            s3_key = f"{S3_PREFIX}resumed/{fname}"
            item   = UploadItem(
                local_path=path,
                s3_key=s3_key,
                size_bytes=os.path.getsize(path),
                segment_idx=-1,
                session_id="resumed",
            )
            self._queue.append(item)
        log.info("Boot resume: re-queued successfully")

    def _notify(self):
        if self.on_status_change:
            try:
                self.on_status_change(self.get_status())
            except Exception:
                pass

    def _get_s3(self):
        if self._s3 is None:
            import boto3
            self._s3 = boto3.client("s3")
            log.info("S3 client initialized")
        return self._s3

    def _run(self):
        while not self._stop.is_set():
            self._event.wait(timeout=2)
            self._event.clear()
            while not self._stop.is_set():
                item = self._next_pending()
                if item is None:
                    break
                self._upload(item)

    def _next_pending(self) -> Optional[UploadItem]:
        with self._lock:
            for item in self._queue:
                if item.status in (UploadStatus.QUEUED, UploadStatus.RETRYING):
                    return item
        return None

    def _upload(self, item: UploadItem):
        item.status        = UploadStatus.UPLOADING
        item.attempts     += 1
        item.bytes_uploaded = 0
        item.progress_pct  = 0.0
        self._notify()

        try:
            s3 = self._get_s3()
            mb = item.size_bytes / 1024 / 1024
            log.info(f"Uploading {os.path.basename(item.local_path)} ({mb:.1f} MB, attempt {item.attempts})")

            last_notify = [0.0]

            def _cb(n):
                item.bytes_uploaded += n
                if item.size_bytes > 0:
                    item.progress_pct = min(item.bytes_uploaded / item.size_bytes * 100, 100.0)
                if time.time() - last_notify[0] >= 0.5:
                    last_notify[0] = time.time()
                    self._notify()

            s3.upload_file(item.local_path, S3_BUCKET, item.s3_key, Callback=_cb)

            item.progress_pct = 100.0
            item.status       = UploadStatus.COMPLETE
            item.error        = None
            log.info(f"Upload complete: {item.s3_key}")

            # Fire on_complete so polling_agent can register episode in backend
            if self.on_complete:
                try:
                    self.on_complete(item.local_path, item.s3_key, item.segment_idx, item.session_id)
                except Exception as e:
                    log.warning(f"on_complete callback failed: {e}")

        except Exception as e:
            log.error(f"Upload error: {e}")
            # Exponential backoff — infinite retry
            delay = min(16 * (2 ** min(item.attempts - 1, 9)), 8192)
            item.status = UploadStatus.RETRYING
            item.error  = f"Attempt {item.attempts}: {e} — retrying in {delay}s"
            log.info(f"Will retry in {delay}s...")
            time.sleep(delay)

        self._notify()
