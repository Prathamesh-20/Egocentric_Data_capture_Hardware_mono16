#!/usr/bin/env python3
"""
Polling Agent — runs on Pi alongside capture_daemon.
Polls backend for start/stop commands and notifies backend when segments are ready.
"""
import os, sys, time, socket, logging, threading
import requests
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("polling-agent")

# ── Config ─────────────────────────────────────────────────────────
BACKEND_URL        = os.getenv("BACKEND_URL", "").rstrip("/")
POLL_INTERVAL      = int(os.getenv("POLL_INTERVAL_SECS", "2"))
LOCAL_URL          = "http://localhost:8080"
HOSTNAME           = socket.gethostname()
STOP_GRACE_SECONDS = 120  # keep monitoring for 2 mins after stop to catch last segments

if not BACKEND_URL:
    log.error("BACKEND_URL not set — exiting")
    sys.exit(1)


# ── Fetch AWS credentials from backend at startup ──────────────────
def fetch_and_set_credentials():
    """
    Fetch AWS credentials from backend using hostname.
    Non-blocking — if credentials are missing or backend unreachable,
    just log and continue (local mode).
    """
    url = f"{BACKEND_URL}/api/v1/pi-credentials/{HOSTNAME}"
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            creds = r.json()
            if creds.get("aws_access_key_id"):
                os.environ["AWS_ACCESS_KEY_ID"]     = creds["aws_access_key_id"]
                os.environ["AWS_SECRET_ACCESS_KEY"] = creds["aws_secret_access_key"]
                os.environ["AWS_REGION"]            = creds["aws_region"]
                os.environ["AWS_BUCKET_NAME"]       = creds["aws_bucket_name"]
                log.info("AWS credentials fetched from backend ✓")

                # Push credentials to capture_daemon so it can upload to S3
                local_post("/settings", json={
                    "AWS_ACCESS_KEY_ID":     creds["aws_access_key_id"],
                    "AWS_SECRET_ACCESS_KEY": creds["aws_secret_access_key"],
                    "AWS_REGION":            creds["aws_region"],
                    "AWS_BUCKET_NAME":       creds["aws_bucket_name"],
                })
                log.info("AWS credentials pushed to capture_daemon ✓")
            else:
                log.warning("AWS credentials not set in backend — skipping (local mode)")
        elif r.status_code == 404:
            log.error(f"Hostname '{HOSTNAME}' not registered in backend — exiting")
            sys.exit(1)
        else:
            log.warning(f"Credentials fetch failed ({r.status_code}) — continuing anyway")
    except Exception as e:
        log.warning(f"Could not fetch credentials: {e} — continuing anyway")


# ── Shared state ───────────────────────────────────────────────────
_current_task_id       = None
_current_operator_id   = None
_current_operator_name = None
_session_active        = False
_session_stopped_at    = None   # timestamp when session was stopped (for grace period)
_processed_filenames   = {}     # filename -> episode_id (to avoid duplicates + track for s3 update)
_pending_episodes      = []     # retry queue when backend is unreachable
_state_lock            = threading.Lock()


# ── Local server helpers ───────────────────────────────────────────
def local_post(path: str, json: dict = None) -> dict:
    try:
        r = requests.post(f"{LOCAL_URL}{path}", json=json, timeout=5)
        if r.status_code in (200, 201):
            return r.json()
        else:
            log.warning(f"Local POST {path} returned {r.status_code}: {r.text}")
            return {}
    except Exception as e:
        log.error(f"Local POST failed ({path}): {e}")
        return {}


def local_get(path: str) -> dict:
    try:
        r = requests.get(f"{LOCAL_URL}{path}", timeout=5)
        return r.json() if r.status_code == 200 else {}
    except Exception as e:
        log.error(f"Local GET failed ({path}): {e}")
        return {}


# ── Backend helpers ────────────────────────────────────────────────
def backend_post(path: str) -> dict:
    try:
        r = requests.post(f"{BACKEND_URL}{path}", timeout=5)
        return r.json() if r.status_code in (200, 201) else {}
    except requests.exceptions.ConnectionError:
        log.warning("Backend unreachable")
        return {}
    except Exception as e:
        log.error(f"Backend POST failed ({path}): {e}")
        return {}


def backend_post_json(path: str, data: dict) -> dict:
    try:
        r = requests.post(f"{BACKEND_URL}{path}", json=data, timeout=10)
        return r.json() if r.status_code in (200, 201) else {}
    except requests.exceptions.ConnectionError:
        log.warning("Backend unreachable")
        return {}
    except Exception as e:
        log.error(f"Backend POST failed ({path}): {e}")
        return {}


def backend_patch_json(path: str, data: dict) -> dict:
    try:
        r = requests.patch(f"{BACKEND_URL}{path}", json=data, timeout=10)
        return r.json() if r.status_code in (200, 201) else {}
    except requests.exceptions.ConnectionError:
        log.warning("Backend unreachable")
        return {}
    except Exception as e:
        log.error(f"Backend PATCH failed ({path}): {e}")
        return {}


# ── Handle start command ───────────────────────────────────────────
def handle_start(data: dict):
    global _current_task_id, _current_operator_id, _current_operator_name
    global _session_active, _session_stopped_at, _processed_filenames

    task_id       = data.get("task_id", "")
    operator_id   = data.get("operator_id", "")
    operator_name = data.get("operator_name", "")
    command_id    = data.get("command_id", "")

    log.info(f"START — task={task_id} operator={operator_name}")

    with _state_lock:
        _current_task_id       = task_id
        _current_operator_id   = operator_id
        _current_operator_name = operator_name
        _processed_filenames   = {}     # reset for new task
        _session_stopped_at    = None   # reset grace period timer

    # Send task + operator info to capture_daemon before starting session
    local_post("/settings", json={
        "operator_id":           operator_id,
        "operator_name":         operator_name,
        "task_id":               task_id,
        "activity_label":        task_id,
        "AWS_ACCESS_KEY_ID":     os.getenv("AWS_ACCESS_KEY_ID", ""),
        "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY", ""),
        "AWS_REGION":            os.getenv("AWS_REGION", ""),
        "AWS_BUCKET_NAME":       os.getenv("AWS_BUCKET_NAME", ""),
    })

    result = local_post("/session/start")
    if result:
        with _state_lock:
            _session_active = True
        log.info(f"Session started: {result.get('session_id', 'unknown')}")
    else:
        log.error(f"Session start failed: {result}")

    # Mark command as completed in backend
    if command_id:
        backend_post(f"/api/v1/pi-commands/complete/{command_id}")


# ── Handle stop command ────────────────────────────────────────────
def handle_stop(data: dict):
    global _session_active, _session_stopped_at

    command_id = data.get("command_id", "")
    log.info("STOP command received")

    local_post("/session/stop")

    with _state_lock:
        _session_active     = False
        _session_stopped_at = datetime.now(timezone.utc)  # start grace period timer

    # Mark command as completed in backend
    if command_id:
        backend_post(f"/api/v1/pi-commands/complete/{command_id}")

    log.info("Session stopped")


# ── Upload monitor ─────────────────────────────────────────────────
def _upload_monitor():
    """
    Runs in background thread.
    Watches capture_daemon's upload-status endpoint for completed .bag segments.
    Saves episodes to backend DB as soon as segment is locally ready (status=uploading),
    without waiting for S3 upload to finish — so target can be met immediately.
    Once S3 upload completes, updates the episode with the s3_key.
    Continues monitoring for STOP_GRACE_SECONDS after session stops to catch last segments.
    """
    log.info("Upload monitor started")

    while True:
        time.sleep(3)

        with _state_lock:
            active      = _session_active
            stopped_at  = _session_stopped_at
            task_id     = _current_task_id
            operator_id = _current_operator_id
            processed   = dict(_processed_filenames)
            pending     = list(_pending_episodes)

        # Check if we're in grace period (session stopped but still watching for last segments)
        in_grace = (
            not active
            and stopped_at is not None
            and (datetime.now(timezone.utc) - stopped_at).total_seconds() < STOP_GRACE_SECONDS
        )

        # Skip if session not active and not in grace period
        if not active and not in_grace:
            continue

        if not task_id:
            continue

        # ── Retry any episodes that failed due to backend being unreachable ──
        if pending:
            log.info(f"Retrying {len(pending)} pending episodes...")
            still_pending = []
            for ep in pending:
                result = backend_post_json("/api/v1/pi-episodes/", ep)
                if result.get("episode_id"):
                    log.info(f"Retry success: {ep.get('filename')}")
                    with _state_lock:
                        _processed_filenames[ep["filename"]] = result["episode_id"]
                    if result.get("target_met"):
                        log.info("Target met (on retry) — backend will send stop command")
                else:
                    still_pending.append(ep)
            with _state_lock:
                _pending_episodes.clear()
                _pending_episodes.extend(still_pending)

        try:
            items = local_get("/upload-status").get("items", [])

            for item in items:
                filename = item.get("filename", "")
                status   = item.get("status", "")
                s3_key   = item.get("s3_key", "") or ""

                # Only care about .bag files (video segments)
                if not filename.endswith(".bag"):
                    continue

                already_saved = filename in processed

                # Step 1 — Save episode to DB as soon as segment is locally ready
                # "uploading" = local recording done, S3 upload in progress
                # "complete"  = S3 upload also done
                # We save at "uploading" so target can be met without waiting for S3
                if (not already_saved
                        and status in ("complete", "uploading")):

                    log.info(f"Segment ready — saving episode: {filename} (status={status})")

                    ep_data = {
                        "task_id":     task_id,
                        "operator_id": operator_id,
                        "s3_key":      s3_key if s3_key else None,  # may be None if still uploading
                        "filename":    filename,
                        "notes":       None,
                        "hostname":    HOSTNAME,
                    }

                    result = backend_post_json("/api/v1/pi-episodes/", ep_data)

                    if result.get("episode_id"):
                        with _state_lock:
                            _processed_filenames[filename] = result["episode_id"]
                        if result.get("target_met"):
                            log.info(f"Target met for task {task_id} — backend will send stop command")
                    else:
                        # Backend unreachable — queue for retry
                        log.warning(f"Backend unreachable — queuing for retry: {filename}")
                        with _state_lock:
                            _pending_episodes.append(ep_data)

                # Step 2 — S3 upload finished, update episode with s3_key
                elif (already_saved
                        and status == "complete"
                        and s3_key):

                    episode_id = processed.get(filename)
                    if episode_id:
                        log.info(f"S3 upload complete — updating s3_key for episode {episode_id}")
                        backend_patch_json(
                            f"/api/v1/pi-episodes/{episode_id}/update-s3",
                            {"s3_key": s3_key}
                        )
                        # Set to None so we don't update again
                        with _state_lock:
                            _processed_filenames[filename] = None

        except Exception as e:
            log.error(f"Upload monitor error: {e}")


# ── Main polling loop ──────────────────────────────────────────────
def poll():
    log.info("=" * 50)
    log.info("  Polling Agent started")
    log.info(f"  Hostname : {HOSTNAME}")
    log.info(f"  Backend  : {BACKEND_URL}")
    log.info(f"  Interval : {POLL_INTERVAL}s")
    log.info("=" * 50)

    # Start upload monitor in background thread
    threading.Thread(
        target=_upload_monitor, daemon=True, name="upload-monitor"
    ).start()

    while True:
        try:
            r = requests.get(
                f"{BACKEND_URL}/api/v1/pi-commands/poll/{HOSTNAME}",
                timeout=5,
            )

            if r.status_code == 200:
                data    = r.json()
                command = data.get("command")

                # ── TTL check — ignore stale commands if Pi was offline ────
                expires_at_str = data.get("expires_at")
                if command and expires_at_str:
                    expires_at = datetime.fromisoformat(expires_at_str)
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    if now > expires_at:
                        log.warning(f"Stale command '{command}' ignored (expired at {expires_at_str})")
                        command_id = data.get("command_id", "")
                        if command_id:
                            backend_post(f"/api/v1/pi-commands/complete/{command_id}")
                        command = None
                # ──────────────────────────────────────────────────────────

                if command == "start":
                    handle_start(data)
                elif command == "stop":
                    handle_stop(data)

            elif r.status_code == 404:
                log.warning(f"Hostname '{HOSTNAME}' not registered in backend")

        except requests.exceptions.ConnectionError:
            log.warning("Backend unreachable — retrying...")
        except Exception as e:
            log.error(f"Polling error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    fetch_and_set_credentials()
    poll()
