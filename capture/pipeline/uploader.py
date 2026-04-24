"""
S3 Upload Queue — background uploader with improved retry logic.
"""
import os, time, logging, threading, glob, json
from datetime import datetime
from dataclasses import dataclass
from typing import List, Optional, Callable
from enum import Enum
from pathlib import Path

from capture.config import S3_PREFIX, OUTPUT_DIR

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
    def __init__(
        self,
        on_status_change: Optional[Callable] = None,
        on_complete: Optional[Callable[[str, str, int, str], None]] = None,
    ):
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
        if not os.path.exists(local_path):
            log.warning(f"enqueue: file not found — {local_path}")
            return

        # Move file to pending dir so it survives a reboot before upload completes
        pending_path = os.path.join(PENDING_DIR, os.path.basename(local_path))
        try:
            os.rename(local_path, pending_path)
        except OSError as e:
            log.error(f"Could not move to pending: {e}")
            pending_path = local_path

        # Save metadata alongside the file so boot resume restores the correct
        # s3_key, session_id, and segment_idx — without this, resumed files
        # go to the wrong task/operator folder
        meta_path = pending_path + ".meta"
        try:
            with open(meta_path, "w") as f:
                json.dump({
                    "s3_key":      s3_key,
                    "segment_idx": segment_idx,
                    "session_id":  session_id,
                }, f)
        except Exception as e:
            log.warning(f"Could not save upload metadata: {e}")

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
        log.info(f"Enqueued: {os.path.basename(pending_path)} → s3://{os.getenv('AWS_BUCKET_NAME', '?')}/{s3_key}")

    def enqueue_segment_files(self, session_id: str, segment_idx: int, files: dict,
                              operator_name: str = "", task_id: str = "",
                              task_name: str = "", environment: str = ""):
        # Parse date from session_id (format: YYYYMMDD_HHMMSS) → 2025-01-21
        try:
            date_str = datetime.strptime(session_id[:8], "%Y%m%d").strftime("%Y-%m-%d")
        except Exception:
            date_str = session_id[:8]  # fallback if session_id format is unexpected

        # Sanitize path components — replace spaces and slashes to avoid broken S3 keys
        def _safe(s: str) -> str:
            return s.replace("/", "_").replace(" ", "_").strip("_") or "unknown"

        for label, path in files.items():
            if not path or not os.path.exists(path):
                continue

            fname = os.path.basename(path)

            # Only upload .mcap and .csv files — skip everything else
            if not (fname.endswith(".mcap") or fname.endswith(".csv")):
                continue

            if task_name and environment and operator_name:
                # Full structured path:
                # raw-feed/egocentric/{task_name}/{environment}/{operator_name}/{date}/session_{id}/videos/{fname}
                s3_key = (
                    f"{S3_PREFIX}egocentric/"
                    f"{_safe(task_name)}/"
                    f"{_safe(environment)}/"
                    f"{_safe(operator_name)}/"
                    f"{date_str}/"
                    f"session_{session_id}/"
                    f"videos/{fname}"
                )
            else:
                # Fallback to flat structure if metadata is missing
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
        # On boot, re-queue any files that were in .pending_uploads but never finished.
        # Read the .meta file to restore the original s3_key/session_id/segment_idx
        # so the file goes to the correct task and operator folder, not a generic "resumed/" path.
        leftover = glob.glob(os.path.join(PENDING_DIR, "**", "*"), recursive=True)
        leftover = [f for f in leftover if os.path.isfile(f) and not f.endswith(".meta")]
        if not leftover:
            return

        log.info(f"Boot resume: found {len(leftover)} pending file(s) — re-queuing")
        for path in leftover:
            fname     = os.path.basename(path)
            meta_path = path + ".meta"
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                s3_key      = meta["s3_key"]
                segment_idx = meta.get("segment_idx", -1)
                session_id  = meta.get("session_id", "resumed")
                log.info(f"Boot resume: restored metadata for {fname} → {s3_key}")
            except Exception:
                # No meta file — fallback for files enqueued before this fix was deployed
                s3_key      = f"{S3_PREFIX}resumed/{fname}"
                segment_idx = -1
                session_id  = "resumed"
                log.warning(f"Boot resume: no metadata for {fname} — using fallback path")

            item = UploadItem(
                local_path=path,
                s3_key=s3_key,
                size_bytes=os.path.getsize(path),
                segment_idx=segment_idx,
                session_id=session_id,
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
            from botocore.config import Config
            # Timeout config for large files on slow networks.
            # read_timeout=300: if no data arrives for 5 min, upload fails cleanly
            # instead of hanging indefinitely. retries=0 because we handle retries
            # ourselves with exponential backoff below.
            config = Config(
                connect_timeout=60,
                read_timeout=300,
                retries={"max_attempts": 0},
            )
            self._s3 = boto3.client("s3", config=config)
            log.info("S3 client initialized")
        return self._s3

    def _run(self):
        while not self._stop.is_set():
            self._event.wait(timeout=2)
            self._event.clear()
            if not os.getenv("AWS_BUCKET_NAME"):
                continue  # credentials not yet available, wait
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
        # If file was deleted locally, mark as failed and stop retrying
        if not os.path.exists(item.local_path):
            log.warning(f"File no longer exists, skipping: {os.path.basename(item.local_path)}")
            item.status = UploadStatus.FAILED
            item.error  = "File not found locally"
            meta_path   = item.local_path + ".meta"
            if os.path.exists(meta_path):
                os.remove(meta_path)
            self._notify()
            return

        item.status         = UploadStatus.UPLOADING
        item.attempts      += 1
        item.bytes_uploaded = 0
        item.progress_pct   = 0.0
        self._notify()

        try:
            s3     = self._get_s3()
            bucket = os.getenv("AWS_BUCKET_NAME")
            mb     = item.size_bytes / 1024 / 1024
            log.info(f"Uploading {os.path.basename(item.local_path)} ({mb:.1f} MB, attempt {item.attempts}) → {bucket}")

            last_notify = [0.0]

            def _cb(n):
                # Called by boto3 after each chunk is sent — track upload progress
                item.bytes_uploaded += n
                if item.size_bytes > 0:
                    item.progress_pct = min(item.bytes_uploaded / item.size_bytes * 100, 100.0)
                if time.time() - last_notify[0] >= 0.5:
                    last_notify[0] = time.time()
                    self._notify()

            # Multipart upload for large files (> 100 MB).
            # A 1.1 GB file becomes ~22 chunks of 50 MB each.
            # If one chunk fails, only that chunk is retried — not the whole file.
            # max_concurrency=2 is conservative and safe on slow/mobile networks.
            from boto3.s3.transfer import TransferConfig
            transfer_config = TransferConfig(
                multipart_threshold=100 * 1024 * 1024,  # files > 100 MB use multipart
                multipart_chunksize=50 * 1024 * 1024,   # 50 MB per chunk
                max_concurrency=2,
            )

            s3.upload_file(
                item.local_path, bucket, item.s3_key,
                Callback=_cb,
                Config=transfer_config,
            )

            item.progress_pct = 100.0
            item.status       = UploadStatus.COMPLETE
            item.error        = None
            log.info(f"Upload complete: {item.s3_key}")

            # Delete local file and its metadata after successful upload
            try:
                os.remove(item.local_path)
                log.info(f"Local file deleted: {os.path.basename(item.local_path)}")
                meta_path = item.local_path + ".meta"
                if os.path.exists(meta_path):
                    os.remove(meta_path)
            except Exception as e:
                log.warning(f"Could not delete local file: {e}")

            if self.on_complete:
                try:
                    self.on_complete(item.local_path, item.s3_key, item.segment_idx, item.session_id)
                except Exception as e:
                    log.warning(f"on_complete callback failed: {e}")

        except Exception as e:
            # Exponential backoff: 16s, 32s, 64s ... up to ~2.3 hours max
            log.error(f"Upload error: {e}")
            delay = min(16 * (2 ** min(item.attempts - 1, 9)), 8192)
            item.status = UploadStatus.RETRYING
            item.error  = f"Attempt {item.attempts}: {e} — retrying in {delay}s"
            log.info(f"Will retry in {delay}s...")
            time.sleep(delay)

        self._notify()
