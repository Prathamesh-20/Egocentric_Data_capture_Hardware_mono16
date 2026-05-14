"""
S3 Upload Queue — resilient background uploader (v2.1 merged build).

Design goals:

  1. NEVER GIVE UP on a file. Failed uploads back off exponentially
     (capped at S3_RETRY_BACKOFF_MAX_S) and retry forever. A Pi can
     sit through hours of network outage and resume cleanly the
     moment connectivity returns.

  2. PERSISTENT QUEUE. The queue is mirrored to a JSON file on disk
     after every state change (atomic write — fsync + rename). If
     the daemon dies or the Pi reboots mid-session, the next start
     picks up exactly where we left off — no lost local files, no
     manual recovery.

  3. CONCURRENT but BOUNDED. S3_CONCURRENT_UPLOADS workers share one
     queue; each upload itself uses multipart (boto3 TransferConfig)
     for chunked, resumable transfers of large MCAP files.

  4. AUTO-DELETE on confirmed S3 success. After the upload completes,
     we head_object to verify the bytes are actually in S3, and only
     then delete the local file. If verification fails, the item
     re-enters the retry queue.

  5. IDEMPOTENT lifecycle. start() is safe to call repeatedly; it
     joins existing workers if they already exist.
"""
import json
import logging
import os
import random
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional

from capture.config import (
    DELETE_AFTER_UPLOAD,
    S3_BUCKET,
    S3_CONCURRENT_UPLOADS,
    S3_MULTIPART_CHUNK_BYTES,
    S3_MULTIPART_THRESHOLD,
    S3_PREFIX,
    S3_QUEUE_STATE_FILE,
    S3_RETRY_BACKOFF_FACTOR,
    S3_RETRY_BACKOFF_INITIAL_S,
    S3_RETRY_BACKOFF_MAX_S,
)

log = logging.getLogger(__name__)


class UploadStatus(str, Enum):
    QUEUED    = "queued"      # waiting to be picked up
    UPLOADING = "uploading"   # actively transferring
    COMPLETE  = "complete"    # done, S3-verified, local deleted
    FAILED    = "failed"      # kept for compatibility; we no longer enter this state
    RETRYING  = "retrying"    # waiting until next_retry_at


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
    # Epoch seconds at which this item is eligible for retry. 0 = ready now.
    next_retry_at:  float = 0.0

    def to_dict(self) -> dict:
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
            "next_retry_at":  self.next_retry_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UploadItem":
        return cls(
            local_path     = d.get("local_path", ""),
            s3_key         = d.get("s3_key", ""),
            status         = UploadStatus(d.get("status", "queued")),
            attempts       = int(d.get("attempts", 0)),
            error          = d.get("error"),
            size_bytes     = int(d.get("size_bytes", 0)),
            bytes_uploaded = int(d.get("bytes_uploaded", 0)),
            progress_pct   = float(d.get("progress_pct", 0.0)),
            segment_idx    = int(d.get("segment_idx", -1)),
            session_id     = d.get("session_id", ""),
            next_retry_at  = float(d.get("next_retry_at", 0.0)),
        )


def _atomic_write_json(path: str, payload: dict):
    """
    Atomic JSON write — write to a tempfile in the same dir, fsync,
    then rename. Avoids leaving a corrupted state file if the Pi loses
    power mid-write.
    """
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".upload_queue_", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


class UploadQueue:
    """
    Thread-safe persistent S3 upload queue with never-give-up retry.

    On start(): loads any items from S3_QUEUE_STATE_FILE (resume from
    previous run), then spawns S3_CONCURRENT_UPLOADS worker threads.
    Each worker pulls the oldest eligible item (status QUEUED, or
    RETRYING with next_retry_at <= now) and uploads it. Failed items
    stay in the queue with an exponentially backed-off next_retry_at;
    they are NOT removed unless they succeed or a caller explicitly
    drops them.

    Public API: start, stop, enqueue, enqueue_segment_files,
    get_status, remove_completed, on_status_change, on_complete.
    """

    def __init__(
        self,
        on_status_change: Optional[Callable[[dict], None]] = None,
        on_complete: Optional[Callable[[str, str, int, str], None]] = None,
    ):
        self._queue: List[UploadItem] = []
        self._lock     = threading.Lock()
        self._event    = threading.Event()
        self._stop     = threading.Event()
        self._workers: List[threading.Thread] = []
        # Per-thread S3 client cache — boto3 clients are NOT thread-safe
        # to share for some operations, so each worker gets its own.
        self._s3_clients: Dict[int, object] = {}
        self._s3_lock = threading.Lock()
        self.on_status_change = on_status_change
        self.on_complete      = on_complete

        self._load_state()

    # ── Lifecycle ─────────────────────────────────────────────────
    def start(self):
        """
        Start N background upload workers (where N = S3_CONCURRENT_UPLOADS).
        Idempotent — calling repeatedly does NOT spawn extra workers.
        """
        with self._lock:
            alive = [t for t in self._workers if t.is_alive()]
            if alive:
                self._workers = alive
                return
            self._workers = []
            self._stop.clear()
            # Reset any items left mid-flight from a previous run back
            # to QUEUED so they get picked up. (boto3 multipart resume
            # isn't applicable across process restarts.)
            for item in self._queue:
                if item.status == UploadStatus.UPLOADING:
                    item.status = UploadStatus.QUEUED
                    item.bytes_uploaded = 0
                    item.progress_pct = 0.0
        self._save_state()

        n = max(1, int(S3_CONCURRENT_UPLOADS))
        for i in range(n):
            t = threading.Thread(
                target=self._run, name=f"uploader-{i}", daemon=True)
            t.start()
            self._workers.append(t)

        self._event.set()
        log.info(
            f"Upload queue started — {n} worker(s), "
            f"{len(self._queue)} item(s) loaded from state file"
        )

    def stop(self):
        """Stop all workers (each finishes its current upload first)."""
        self._stop.set()
        self._event.set()
        for t in self._workers:
            t.join(timeout=15)
        self._save_state()
        log.info("Upload queue stopped")

    # ── Public API ────────────────────────────────────────────────
    def enqueue(self, local_path: str, s3_key: str,
                segment_idx: int = -1, session_id: str = ""):
        """Add a file to the upload queue (or re-add after restart)."""
        size = 0
        try:
            size = os.path.getsize(local_path)
        except OSError:
            pass

        with self._lock:
            # Dedupe: if the same local_path is already queued or in
            # flight, don't add it twice. (We do NOT dedupe on s3_key
            # alone — two different files could legitimately share a
            # key after a path change, and we always trust the most
            # recent local_path.)
            for existing in self._queue:
                if (existing.local_path == local_path
                        and existing.status != UploadStatus.COMPLETE):
                    log.debug(f"Skip enqueue (already queued): {local_path}")
                    return

            item = UploadItem(
                local_path=local_path,
                s3_key=s3_key,
                size_bytes=size,
                segment_idx=segment_idx,
                session_id=session_id,
            )
            self._queue.append(item)

        self._save_state()
        self._event.set()
        self._notify()
        log.info(
            f"Enqueued: {os.path.basename(local_path)} "
            f"({size / 1024 / 1024:.1f} MB) → s3://{S3_BUCKET}/{s3_key}"
        )

    def enqueue_segment_files(
        self, session_id: str, segment_idx: int, files: dict,
        s3_key_builder: Optional[Callable[[str], str]] = None,
    ):
        """
        Enqueue all files from a segment. If s3_key_builder is provided,
        each file's filename is passed through it to compute the S3 key
        (used by SessionV2 to build hierarchical paths). Otherwise we
        fall back to a flat S3_PREFIX/<sid>/seg_NNN/<fname> layout.

        Filters: only .mcap, .csv, and manifest_*.json are accepted —
        prevents accidentally uploading .pyc or temp files.
        """
        for label, path in files.items():
            if not path or not os.path.exists(path):
                continue
            fname = os.path.basename(path)
            if not (fname.endswith(".mcap")
                    or fname.endswith(".csv")
                    or fname.startswith("manifest_")):
                continue

            if s3_key_builder is not None:
                s3_key = s3_key_builder(fname)
            else:
                s3_key = f"{S3_PREFIX}{session_id}/seg_{segment_idx:03d}/{fname}"
            self.enqueue(path, s3_key, segment_idx, session_id)

    def get_status(self) -> dict:
        """Return queue summary for the UI/API. Shape stable across versions."""
        with self._lock:
            items = [i.to_dict() for i in self._queue]
        counts = {s.value: 0 for s in UploadStatus}
        for i in items:
            counts[i["status"]] = counts.get(i["status"], 0) + 1
        return {
            "items":     items,
            "total":     len(items),
            "queued":    counts.get("queued", 0),
            "uploading": counts.get("uploading", 0),
            "complete":  counts.get("complete", 0),
            "failed":    counts.get("failed", 0),
            "retrying":  counts.get("retrying", 0),
        }

    def remove_completed(self, max_keep: int = 200):
        """Trim COMPLETE items so the JSON state file doesn't grow forever.
        Keeps the most recent `max_keep` completions for UI history."""
        with self._lock:
            done = [i for i in self._queue if i.status == UploadStatus.COMPLETE]
            if len(done) > max_keep:
                excess = len(done) - max_keep
                kept_done = done[excess:]
                self._queue = [
                    i for i in self._queue if i.status != UploadStatus.COMPLETE
                ] + kept_done
        self._save_state()

    # ── Persistence ──────────────────────────────────────────────
    def _load_state(self):
        """Load queue from disk on startup. Missing/corrupt → start clean."""
        path = S3_QUEUE_STATE_FILE
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                payload = json.load(f)
            items = payload.get("items", [])
            with self._lock:
                self._queue = [UploadItem.from_dict(d) for d in items]
            n_pending = sum(
                1 for i in self._queue
                if i.status in (
                    UploadStatus.QUEUED,
                    UploadStatus.UPLOADING,
                    UploadStatus.RETRYING,
                )
            )
            log.info(
                f"Loaded upload queue state from {path}: "
                f"{len(self._queue)} item(s), {n_pending} pending"
            )
        except Exception as e:
            log.warning(
                f"Could not load queue state from {path}: {e}. Starting fresh."
            )
            with self._lock:
                self._queue = []

    def _save_state(self):
        """Persist queue to disk. Called after any mutation."""
        try:
            with self._lock:
                payload = {"items": [i.to_dict() for i in self._queue]}
            _atomic_write_json(S3_QUEUE_STATE_FILE, payload)
        except Exception as e:
            log.warning(f"Could not save queue state: {e}")

    # ── Worker loop ──────────────────────────────────────────────
    def _notify(self):
        if self.on_status_change:
            try:
                self.on_status_change(self.get_status())
            except Exception as e:
                log.warning(f"on_status_change callback raised: {e}")

    def _get_s3(self):
        """Return a boto3 S3 client for the current thread (lazy, per-thread)."""
        tid = threading.get_ident()
        with self._s3_lock:
            client = self._s3_clients.get(tid)
            if client is not None:
                return client
            try:
                import boto3
                from botocore.config import Config as BotoConfig
                client = boto3.client(
                    "s3",
                    config=BotoConfig(
                        retries={"max_attempts": 3, "mode": "standard"},
                        connect_timeout=15,
                        read_timeout=60,
                    ),
                )
                self._s3_clients[tid] = client
                log.info(f"S3 client initialized on thread {tid}")
                return client
            except ImportError:
                log.error("boto3 not installed — uploads will fail")
                raise
            except Exception as e:
                log.error(f"S3 client init failed: {e}")
                raise

    def _transfer_config(self):
        """Multipart config — large files stream in chunks."""
        from boto3.s3.transfer import TransferConfig
        return TransferConfig(
            multipart_threshold=S3_MULTIPART_THRESHOLD,
            multipart_chunksize=S3_MULTIPART_CHUNK_BYTES,
            max_concurrency=4,
            use_threads=True,
        )

    def _run(self):
        """Worker loop — runs until self._stop is set."""
        while not self._stop.is_set():
            item = self._claim_next()
            if item is None:
                wait_s = self._seconds_until_next_eligible()
                self._event.wait(timeout=wait_s)
                self._event.clear()
                continue
            try:
                self._upload(item)
            except Exception as e:
                # Defensive — _upload should handle its own errors.
                log.exception(f"Worker exception (recovered): {e}")
                with self._lock:
                    item.status = UploadStatus.RETRYING
                self._schedule_retry(item, str(e))

    def _seconds_until_next_eligible(self) -> float:
        """How long until the next RETRYING item becomes eligible?
        Returns a small floor (0.5s) so we still poll frequently, and
        a ceiling of 30s so we don't sleep through new enqueues."""
        now = time.time()
        with self._lock:
            soonest = None
            for item in self._queue:
                if item.status == UploadStatus.QUEUED:
                    return 0.0
                if item.status == UploadStatus.RETRYING:
                    delta = max(0.0, item.next_retry_at - now)
                    if soonest is None or delta < soonest:
                        soonest = delta
        if soonest is None:
            return 30.0
        return min(30.0, max(0.5, soonest))

    def _claim_next(self) -> Optional[UploadItem]:
        """Atomically pick the oldest eligible item and mark it UPLOADING."""
        now = time.time()
        with self._lock:
            for item in self._queue:
                if item.status == UploadStatus.QUEUED:
                    item.status = UploadStatus.UPLOADING
                    item.bytes_uploaded = 0
                    item.progress_pct = 0.0
                    return item
                if (
                    item.status == UploadStatus.RETRYING
                    and item.next_retry_at <= now
                ):
                    item.status = UploadStatus.UPLOADING
                    item.bytes_uploaded = 0
                    item.progress_pct = 0.0
                    return item
        return None

    def _schedule_retry(self, item: UploadItem, err_msg: str):
        """Compute next_retry_at with exponential backoff and jitter."""
        base = S3_RETRY_BACKOFF_INITIAL_S * (
            S3_RETRY_BACKOFF_FACTOR ** max(0, item.attempts - 1)
        )
        delay = min(base, S3_RETRY_BACKOFF_MAX_S)
        delay = delay * (0.8 + 0.4 * random.random())  # ±20% jitter
        item.next_retry_at = time.time() + delay
        item.status = UploadStatus.RETRYING
        item.error = (
            f"Attempt {item.attempts}: {err_msg} (retry in {delay:.0f}s)"
        )
        log.warning(
            f"Upload failed [{os.path.basename(item.local_path)}] — "
            f"retry #{item.attempts + 1} in {delay:.0f}s. {err_msg}"
        )
        self._save_state()
        self._notify()

    def _upload(self, item: UploadItem):
        """Run one upload attempt. On success: verify + delete. On
        failure: schedule next retry. Never raises out of this method
        (workers must stay alive)."""
        item.attempts += 1
        item.error = None
        self._save_state()
        self._notify()

        # Pre-flight: file must still exist locally.
        if not os.path.exists(item.local_path):
            log.warning(
                f"Local file vanished before upload: {item.local_path} — "
                f"marking complete (assumed already handled)."
            )
            with self._lock:
                item.status = UploadStatus.COMPLETE
                item.error = "local file missing at upload time"
            self._save_state()
            self._notify()
            return

        try:
            s3 = self._get_s3()
            log.info(
                f"Uploading {os.path.basename(item.local_path)} "
                f"({item.size_bytes / 1024 / 1024:.1f} MB, "
                f"attempt {item.attempts}) → s3://{S3_BUCKET}/{item.s3_key}"
            )

            last_notify = [0.0]

            def _progress_cb(bytes_transferred):
                item.bytes_uploaded += bytes_transferred
                if item.size_bytes > 0:
                    item.progress_pct = min(
                        (item.bytes_uploaded / item.size_bytes) * 100, 100.0
                    )
                now = time.time()
                if now - last_notify[0] >= 0.5:
                    last_notify[0] = now
                    self._notify()
                # Fast-stop: if the daemon is shutting down, bail mid-transfer.
                if self._stop.is_set():
                    raise RuntimeError("upload aborted: daemon stopping")

            s3.upload_file(
                item.local_path, S3_BUCKET, item.s3_key,
                Callback=_progress_cb,
                Config=self._transfer_config(),
            )

            item.progress_pct = 100.0
            item.status = UploadStatus.COMPLETE
            item.error = None
            log.info(f"Upload complete: s3://{S3_BUCKET}/{item.s3_key}")

            self._delete_local(item)

            if self.on_complete:
                try:
                    self.on_complete(
                        item.local_path, item.s3_key,
                        item.segment_idx, item.session_id,
                    )
                except Exception as e:
                    log.warning(f"on_complete callback raised: {e}")

        except Exception as e:
            self._schedule_retry(item, str(e))
            return

        self._save_state()
        self._notify()

    def _delete_local(self, item: UploadItem):
        """Verify object in S3 via head_object, then delete the local file.
        If verification fails, leave the local file alone and re-queue."""
        if not DELETE_AFTER_UPLOAD:
            return

        try:
            s3 = self._get_s3()
            s3.head_object(Bucket=S3_BUCKET, Key=item.s3_key)
            log.info(f"S3 verified: s3://{S3_BUCKET}/{item.s3_key}")
        except Exception as e:
            log.error(
                f"S3 verification FAILED for {item.s3_key} — "
                f"will retry upload. Error: {e}"
            )
            self._schedule_retry(item, f"verify failed: {e}")
            return

        try:
            if os.path.exists(item.local_path):
                os.remove(item.local_path)
                log.info(
                    f"Deleted local file after confirmed S3 upload: "
                    f"{item.local_path}"
                )
            else:
                log.warning(
                    f"Local file already gone (nothing to delete): "
                    f"{item.local_path}"
                )
        except OSError as e:
            log.warning(f"Could not delete local file {item.local_path}: {e}")
