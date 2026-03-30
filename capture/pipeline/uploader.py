"""
S3 Upload Queue — background uploader with auto-retry.

Tracks upload status per file and exposes queue state for the UI.
"""
import os, time, logging, threading, json
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Callable
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

from capture.config import S3_BUCKET, S3_PREFIX, S3_MAX_RETRIES, S3_RETRY_DELAY


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
            "filename":       os.path.basename(self.local_path),
        }


class UploadQueue:
    """
    Thread-safe S3 upload queue with retry logic.
    Runs a background worker that processes items sequentially.
    """

    def __init__(self, on_status_change: Optional[Callable] = None):
        self._queue: List[UploadItem] = []
        self._lock      = threading.Lock()
        self._event     = threading.Event()
        self._stop      = threading.Event()
        self._worker    = None
        self._s3_client = None
        self.on_status_change = on_status_change

    def start(self):
        """Start the background upload worker."""
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        log.info("Upload queue worker started")

    def stop(self):
        """Stop the worker (finishes current upload)."""
        self._stop.set()
        self._event.set()
        if self._worker:
            self._worker.join(timeout=10)
        log.info("Upload queue worker stopped")

    def enqueue(self, local_path: str, s3_key: str, segment_idx: int = -1):
        """Add a file to the upload queue."""
        size = 0
        try:
            size = os.path.getsize(local_path)
        except OSError:
            pass

        item = UploadItem(
            local_path=local_path,
            s3_key=s3_key,
            size_bytes=size,
            segment_idx=segment_idx,
        )
        with self._lock:
            self._queue.append(item)
        self._event.set()
        self._notify()
        log.info(f"Enqueued upload: {os.path.basename(local_path)} → s3://{S3_BUCKET}/{s3_key}")

    def enqueue_segment_files(self, session_id: str, segment_idx: int, files: Dict[str, str]):
        """Enqueue all files from a segment."""
        for label, path in files.items():
            if path and os.path.exists(path):
                fname = os.path.basename(path)
                s3_key = f"{S3_PREFIX}{session_id}/seg_{segment_idx:03d}/{fname}"
                self.enqueue(path, s3_key, segment_idx)

    def get_status(self) -> dict:
        """Return queue status summary."""
        with self._lock:
            items = [i.to_dict() for i in self._queue]
        counts = {s.value: 0 for s in UploadStatus}
        for i in items:
            counts[i["status"]] += 1
        return {
            "items":    items,
            "total":    len(items),
            "queued":   counts["queued"],
            "uploading": counts["uploading"],
            "complete": counts["complete"],
            "failed":   counts["failed"],
            "retrying": counts["retrying"],
        }

    def _notify(self):
        if self.on_status_change:
            try:
                self.on_status_change(self.get_status())
            except Exception:
                pass

    def _get_s3(self):
        if self._s3_client is None:
            try:
                import boto3
                self._s3_client = boto3.client("s3")
                log.info("S3 client initialized")
            except ImportError:
                log.error("boto3 not installed — uploads will fail")
                raise
            except Exception as e:
                log.error(f"S3 client init failed: {e}")
                raise
        return self._s3_client

    def _run(self):
        while not self._stop.is_set():
            self._event.wait(timeout=2)
            self._event.clear()

            while not self._stop.is_set():
                item = self._get_next()
                if item is None:
                    break
                self._upload(item)

    def _get_next(self) -> Optional[UploadItem]:
        with self._lock:
            for item in self._queue:
                if item.status in (UploadStatus.QUEUED, UploadStatus.RETRYING):
                    return item
        return None

    def _upload(self, item: UploadItem):
        item.status = UploadStatus.UPLOADING
        item.attempts += 1
        item.bytes_uploaded = 0
        item.progress_pct = 0.0
        self._notify()

        try:
            s3 = self._get_s3()
            log.info(f"Uploading {os.path.basename(item.local_path)} "
                     f"({item.size_bytes / 1024 / 1024:.1f} MB, "
                     f"attempt {item.attempts}/{S3_MAX_RETRIES})")

            last_notify = [0.0]

            def _progress_cb(bytes_transferred):
                item.bytes_uploaded += bytes_transferred
                if item.size_bytes > 0:
                    item.progress_pct = min(
                        (item.bytes_uploaded / item.size_bytes) * 100, 100.0)
                now = time.time()
                if now - last_notify[0] >= 0.5:
                    last_notify[0] = now
                    self._notify()

            s3.upload_file(
                item.local_path, S3_BUCKET, item.s3_key,
                Callback=_progress_cb)

            item.progress_pct = 100.0
            item.status = UploadStatus.COMPLETE
            item.error = None
            log.info(f"Upload complete: {item.s3_key}")
        except Exception as e:
            log.error(f"Upload failed: {e}")
            if item.attempts < S3_MAX_RETRIES:
                item.status = UploadStatus.RETRYING
                item.error = f"Retry {item.attempts}/{S3_MAX_RETRIES}: {e}"
                log.info(f"Will retry in {S3_RETRY_DELAY}s...")
                time.sleep(S3_RETRY_DELAY)
            else:
                item.status = UploadStatus.FAILED
                item.error = f"Failed after {S3_MAX_RETRIES} attempts: {e}"
                log.error(f"Upload permanently failed: {item.local_path}")

        self._notify()
