#!/usr/bin/env python3
"""
Polling Agent — runs on Pi alongside capture_daemon.
"""
import os, sys, time, socket, logging, threading
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("polling-agent")

# ── Config ─────────────────────────────────────────────────────────
BACKEND_URL   = os.getenv("BACKEND_URL", "").rstrip("/")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECS", "2"))
LOCAL_URL     = "http://localhost:8080"
HOSTNAME      = socket.gethostname()

if not BACKEND_URL:
    log.error("BACKEND_URL not set — exiting")
    sys.exit(1)


# ── Fetch AWS credentials from backend at startup ─────────────────

def fetch_and_set_credentials():
    """
    Fetch AWS credentials from backend using hostname.
    Sets them as env vars so uploader.py (boto3) picks them up.
    Retries indefinitely until backend is reachable.
    """
    url = f"{BACKEND_URL}/api/v1/pi-credentials/{HOSTNAME}"
    while True:
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                creds = r.json()
                os.environ["AWS_ACCESS_KEY_ID"]     = creds["aws_access_key_id"]
                os.environ["AWS_SECRET_ACCESS_KEY"] = creds["aws_secret_access_key"]
                os.environ["AWS_REGION"]            = creds["aws_region"]
                os.environ["AWS_BUCKET_NAME"]       = creds["aws_bucket_name"]
                log.info("AWS credentials fetched from backend ✓")
                return
            elif r.status_code == 404:
                log.error(f"Hostname '{HOSTNAME}' not registered in backend — exiting")
                sys.exit(1)
            else:
                log.warning(f"Credentials fetch failed ({r.status_code}) — retrying in 10s")
        except Exception as e:
            log.warning(f"Backend unreachable for credentials: {e} — retrying in 10s")
        time.sleep(10)


# ── Shared state ───────────────────────────────────────────────────
_current_task_id       = None
_current_operator_id   = None
_current_operator_name = None
_session_active        = False
_processed_s3_keys     = set()   # .bag s3_keys already reported to backend
_state_lock            = threading.Lock()


# ── Local server helpers ───────────────────────────────────────────

def local_post(path: str, json: dict = None) -> dict:
    try:
        r = requests.post(f"{LOCAL_URL}{path}", json=json, timeout=5)
        return r.json() if r.status_code == 200 else {}
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


# ── Handle start command ───────────────────────────────────────────

def handle_start(data: dict):
    global _current_task_id, _current_operator_id, _current_operator_name
    global _session_active, _processed_s3_keys

    task_id       = data.get("task_id", "")
    operator_id   = data.get("operator_id", "")
    operator_name = data.get("operator_name", "")
    command_id    = data.get("command_id", "")

    log.info(f"START — task={task_id} operator={operator_name}")

    with _state_lock:
        _current_task_id       = task_id
        _current_operator_id   = operator_id
        _current_operator_name = operator_name
        _processed_s3_keys     = set()

    # 1. Set operator/task on local server
    local_post("/settings", json={
        "operator_id":    operator_id,
        "operator_name":  operator_name,
        "task_id":        task_id,
        "activity_label": task_id,
    })

    # 2. Start session directly — FOV_CHECK_ENABLED=False in new hardware
    result = local_post("/session/start")
    if result.get("ok") or result.get("session_id"):
        with _state_lock:
            _session_active = True
        log.info(f"Session started: {result.get('session_id')}")
    else:
        log.error(f"Session start failed: {result}")

    # 3. Acknowledge command
    if command_id:
        backend_post(f"/api/v1/pi-commands/complete/{command_id}")


# ── Handle stop command ────────────────────────────────────────────

def handle_stop(data: dict):
    global _session_active

    command_id = data.get("command_id", "")
    log.info("STOP command received")

    local_post("/session/stop")

    with _state_lock:
        _session_active = False

    if command_id:
        backend_post(f"/api/v1/pi-commands/complete/{command_id}")

    log.info("Session stopped")


# ── Upload monitor ─────────────────────────────────────────────────

def _upload_monitor():
    """
    Runs in background thread. Polls /upload-status every 3s.
    When a .bag file upload is COMPLETE — notifies backend to create episode.
    Uses s3_key to deduplicate — same file never reported twice.
    """
    log.info("Upload monitor started")

    while True:
        time.sleep(3)

        with _state_lock:
            active      = _session_active
            task_id     = _current_task_id
            operator_id = _current_operator_id
            processed   = set(_processed_s3_keys)

        if not active or not task_id:
            continue

        try:
            items = local_get("/upload-status").get("items", [])

            for item in items:
                filename = item.get("filename", "")
                status   = item.get("status", "")
                s3_key   = item.get("s3_key", "")

                # Only .bag files — complete — not yet reported
                if (filename.endswith(".bag")
                        and status == "complete"
                        and s3_key
                        and s3_key not in processed):

                    log.info(f"Segment complete — notifying backend: {filename}")

                    result = backend_post_json(
                        "/api/v1/pi-episodes/",
                        {
                            "task_id":     task_id,
                            "operator_id": operator_id,
                            "s3_key":      s3_key,
                            "notes":       None,
                            "hostname":    HOSTNAME,
                        }
                    )

                    # Mark processed regardless of backend response
                    with _state_lock:
                        _processed_s3_keys.add(s3_key)

                    if result.get("target_met"):
                        log.info(
                            f"Target met for task {task_id} — "
                            "backend will send stop command"
                        )

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

    # Start upload monitor in background
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
    # Step 1: fetch AWS credentials from backend (sets env vars in memory)
    fetch_and_set_credentials()
    # Step 2: start polling
    poll()