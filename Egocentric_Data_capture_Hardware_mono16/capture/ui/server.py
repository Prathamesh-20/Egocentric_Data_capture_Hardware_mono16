"""
FastAPI HTTP + WebSocket service — v2.1 merged build.

Key endpoints:
  GET  /healthz                    -> 200 {"ok": true, "version": ...}
  GET  /status                     -> 200 <full state envelope>
  GET  /settings                   -> 200 settings dict (no secrets)
  POST /settings  {body}           -> 200 {"ok": true, "settings": ...}
  POST /fov-check                  -> 202 {"ok": true} | 409 busy | 503 disabled
  GET  /fov-check                  -> 200 <FOV state>  (poll-friendly)
  GET  /fov-stream                 -> multipart/x-mixed-replace MJPEG
                                      (only while a check is running)
  POST /fov-check/cancel           -> 200 {"ok": true}
  POST /session/start              -> 202 | 400 missing fields | 409 busy
  POST /session/stop               -> 200 | 409 no active session
  GET  /upload-status              -> 200 queue snapshot
  POST /segment/reported/{idx}     -> 200
  GET  /history                    -> 200 today's session history
  WS   /ws                         -> push state envelope on every change

Operator metadata required to start a session:
  operator_id (Operator name) — required
  activity_label (Task)        — required
  environment                  — required
  inventory                    — required

The dashboard form sets all four; the platform layer pushes them via
POST /settings before POST /session/start.
"""
import asyncio
import json
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from capture.config import (
    FOV_CHECK_ENABLED, FOV_CHECK_SECS, FOV_MIN_DETECTION_FRAMES,
    MCAP_ENABLED_DEFAULT, OUTPUT_DIR, SEGMENT_DURATION,
    SESSION_DURATION, UI_HOST, UI_PORT,
)
from capture.pipeline.session_v2 import SessionV2
from capture.pipeline.uploader import UploadQueue

log = logging.getLogger(__name__)

VERSION = "2.1.2"

app = FastAPI(title="Egocentric Capture Node", version=VERSION)


# ── Helpers ───────────────────────────────────────────────────────
def _sanitise(text: str) -> str:
    """Replace spaces with underscores, strip non-alphanumeric chars."""
    return "".join(
        c if c.isalnum() or c == "_" else "_"
        for c in (text or "").strip()
    )


# ── Runtime settings ──────────────────────────────────────────────
settings: Dict[str, Any] = {
    "segment_duration":  SEGMENT_DURATION,
    "session_duration":  SESSION_DURATION,
    "mcap_enabled":      MCAP_ENABLED_DEFAULT,
    "operator_id":       "",
    "activity_label":    "",
    "environment":       "",
    "inventory":         "",
    "fov_check_enabled": FOV_CHECK_ENABLED,
}


# ── Global state ──────────────────────────────────────────────────
state: Dict[str, Any] = {
    "status":          "idle",
    "message":         "Ready — fill in metadata and run FOV check",
    "session_id":      None,
    "current_segment": -1,
    "max_segments":    SESSION_DURATION // SEGMENT_DURATION,
    "segments":        [],
    "fov": {
        "running":           False,
        "passed":            None,
        "frames_checked":    0,
        "frames_with_hands": 0,
        "hands_in_frame":    0,
        "elapsed_sec":       0.0,
        "total_sec":         0.0,
        "settled":           False,
        "method":            "unknown",
        "message":           None,
    },
    "frame_check":     None,
}

_session: Optional[SessionV2] = None
_session_lock = threading.Lock()

_ws_clients = set()
_ws_lock    = threading.Lock()

_upload_queue    = UploadQueue()
_session_history = []

_gpio = None

_failed_segments = []
_failed_seg_lock = threading.Lock()
_upload_tracker  = {"was_pending": False}

# FOV check state
_fov_lock      = threading.Lock()
_fov_thread:   Optional[threading.Thread] = None
_fov_checker = None  # FOVChecker instance while running

# Latest annotated JPEG bytes for /fov-stream MJPEG response.
_fov_latest_jpeg:  Optional[bytes] = None
_fov_frame_event   = threading.Event()

_loop: Optional[asyncio.AbstractEventLoop] = None


def _envelope() -> dict:
    data = state.copy()
    data["upload_status"] = _upload_queue.get_status()
    data["settings"]      = settings.copy()
    return data


def _set_state(**kwargs):
    state.update(kwargs)
    _broadcast()


def _broadcast():
    if _loop is None:
        return
    msg = json.dumps(_envelope(), default=str)
    with _ws_lock:
        dead = set()
        for ws in _ws_clients:
            try:
                asyncio.run_coroutine_threadsafe(ws.send_text(msg), _loop)
            except Exception:
                dead.add(ws)
        _ws_clients.difference_update(dead)


# ── Health & Status ───────────────────────────────────────────────
@app.get("/healthz")
def healthz():
    return {
        "ok":      True,
        "version": VERSION,
        "status":  state["status"],
    }


@app.get("/status")
def get_status():
    return _envelope()


# ── WebSocket ─────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global _loop
    _loop = asyncio.get_event_loop()
    await ws.accept()
    with _ws_lock:
        _ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps(_envelope(), default=str))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        with _ws_lock:
            _ws_clients.discard(ws)
    except Exception as e:
        log.warning(f"WebSocket error: {e}")
        with _ws_lock:
            _ws_clients.discard(ws)


# ── FOV Check ─────────────────────────────────────────────────────
def _fov_progress_cb(progress):
    """FOVChecker progress callback — pushed via state.fov on every WS message."""
    state["fov"].update({
        "running":           True,
        "elapsed_sec":       progress.elapsed_sec,
        "total_sec":         progress.total_sec,
        "settled":           progress.settled,
        "frames_checked":    progress.frames_checked,
        "frames_with_hands": progress.frames_with_hands,
        "hands_in_frame":    progress.hands_in_frame,
        "method":            progress.method,
    })
    _broadcast()


def _fov_frame_callback(jpeg_bytes: bytes, _detected: bool):
    """FOVChecker frame callback — feeds /fov-stream MJPEG endpoint."""
    global _fov_latest_jpeg
    _fov_latest_jpeg = jpeg_bytes
    _fov_frame_event.set()


def _fov_run_thread():
    """Run FOVChecker in a background thread; updates state['fov']
    and pushes annotated JPEGs to /fov-stream."""
    global _fov_checker, _fov_latest_jpeg
    from capture.cameras.fov_check import FOVChecker

    state["fov"] = {
        "running":           True,
        "passed":            None,
        "frames_checked":    0,
        "frames_with_hands": 0,
        "hands_in_frame":    0,
        "elapsed_sec":       0.0,
        "total_sec":         FOV_CHECK_SECS + 3,
        "settled":           False,
        "method":            "unknown",
        "message":           None,
    }
    _set_state(
        status="fov_checking",
        message="Running FOV check — keep both wrists visible",
    )

    checker = FOVChecker(
        duration_sec=FOV_CHECK_SECS,
        min_detection_frames=FOV_MIN_DETECTION_FRAMES,
        frame_cb=_fov_frame_callback,
        progress_cb=_fov_progress_cb,
        gpio=_gpio,
    )
    with _fov_lock:
        _fov_checker = checker
        _fov_latest_jpeg = None
        _fov_frame_event.clear()

    try:
        result = checker.run()
    except Exception as e:
        log.error(f"FOV check raised: {e}")
        state["fov"].update({
            "running": False,
            "passed":  False,
            "message": f"FOV check error: {e}",
        })
        _set_state(status="fov_failed", message=f"FOV check error: {e}")
        with _fov_lock:
            _fov_checker = None
        return

    with _fov_lock:
        _fov_checker = None

    state["fov"].update({
        "running":           False,
        "passed":            bool(result.passed),
        "frames_checked":    result.frames_checked,
        "frames_with_hands": result.frames_with_hands,
        "method":            result.method,
        "message":           result.message,
    })
    if result.passed:
        _set_state(status="fov_passed", message=result.message)
    else:
        _set_state(status="fov_failed", message=result.message)


@app.post("/fov-check")
def start_fov_check():
    """Start a FOV check. 202 on accepted, 409 if busy, 503 if disabled."""
    if not settings.get("fov_check_enabled", FOV_CHECK_ENABLED):
        return JSONResponse(
            {"ok": False, "error": "fov_check_disabled"}, 503,
        )

    if state["status"] in ("recording", "session_active", "fov_checking"):
        return JSONResponse(
            {"ok": False, "error": "busy", "current": state["status"]}, 409,
        )

    with _fov_lock:
        global _fov_thread
        if _fov_thread is not None and _fov_thread.is_alive():
            return JSONResponse(
                {"ok": False, "error": "fov_check_already_running"}, 409,
            )
        _fov_thread = threading.Thread(
            target=_fov_run_thread, daemon=True, name="fov-check",
        )
        _fov_thread.start()

    return JSONResponse({"ok": True}, 202)


@app.get("/fov-check")
def get_fov_check():
    """Poll-friendly FOV state."""
    return state["fov"]


@app.post("/fov-check/cancel")
def cancel_fov_check():
    with _fov_lock:
        if _fov_checker is not None:
            _fov_checker.cancel()
    return {"ok": True}


def _fov_stream_generator():
    """Yield multipart/x-mixed-replace JPEG parts for /fov-stream.

    Closes the stream a few seconds after the FOV check stops, so an
    open browser <img> tag doesn't hold the connection forever.
    """
    boundary       = b"--frame"
    deadline_quiet = time.time() + 30
    while True:
        got = _fov_frame_event.wait(timeout=1.0)
        if got:
            _fov_frame_event.clear()
            with _fov_lock:
                jpeg = _fov_latest_jpeg
                checker_alive = _fov_checker is not None
            if jpeg is None:
                continue
            deadline_quiet = time.time() + 30
            yield (
                boundary + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                + jpeg + b"\r\n"
            )
        else:
            with _fov_lock:
                checker_alive = _fov_checker is not None
            if not checker_alive and time.time() > deadline_quiet:
                return


@app.get("/fov-stream")
def fov_stream():
    """Live MJPEG preview of the FOV check.
    Returns 503 if FOV check is disabled in settings."""
    if not settings.get("fov_check_enabled", FOV_CHECK_ENABLED):
        return JSONResponse(
            {"ok": False, "error": "fov_check_disabled"}, 503,
        )
    return StreamingResponse(
        _fov_stream_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ── Session Control ───────────────────────────────────────────────
def _on_state(status: str, detail: str, **extra):
    seg_idx = extra.get("segment_idx", state.get("current_segment", -1))
    _set_state(status=status, message=detail, current_segment=seg_idx)
    if status in ("mcap_small_warning", "mcap_empty_warning"):
        with _failed_seg_lock:
            _failed_segments.append({
                "seg_idx":    seg_idx,
                "reason":     detail,
                "session_id": state.get("session_id"),
                "reported":   False,
            })
        log.warning(f"Failed segment recorded: seg_{seg_idx} — {detail}")


def _on_segment_update(seg_idx, seg_status, wrist_ok):
    segs  = state.get("segments", [])
    found = False
    for s in segs:
        if s["index"] == seg_idx:
            s["status"]   = seg_status
            s["wrist_ok"] = wrist_ok
            found = True
            break
    if not found:
        segs.append({
            "index": seg_idx, "status": seg_status, "wrist_ok": wrist_ok,
        })
    _set_state(segments=segs, current_segment=seg_idx)


def _on_frame_check(seg_idx: int, result):
    """Inter-segment wrist-check result. NO image is exposed via API."""
    _set_state(frame_check={
        "seg_idx": seg_idx,
        "passed":  bool(result.passed),
        "message": result.message,
        "method":  result.method,
    })


def _on_complete(session_id, n_segments, manifest):
    _session_history.append({
        "session_id":   session_id,
        "operator_id":  settings["operator_id"],
        "activity":     settings["activity_label"],
        "environment":  settings["environment"],
        "inventory":    settings["inventory"],
        "segments":     n_segments,
        "timestamp":    datetime.now().isoformat(),
        "mcap":         settings["mcap_enabled"],
    })
    _set_state(
        status="complete",
        message=f"Session {session_id} — {n_segments} segments complete",
    )


@app.post("/session/start")
def start_session():
    """Start a 30-min capture session.

    Returns 202 on accept, 400 on missing fields, 409 if busy.
    """
    global _session

    with _session_lock:
        if state["status"] in ("recording", "session_active"):
            return JSONResponse(
                {"ok": False, "error": "session_already_running"}, 409,
            )
        if _session and _session.is_running():
            return JSONResponse(
                {"ok": False, "error": "session_already_running"}, 409,
            )

        # Validate all four required fields.
        op  = settings.get("operator_id", "").strip()
        act = settings.get("activity_label", "").strip()
        env = settings.get("environment", "").strip()
        inv = settings.get("inventory", "").strip()
        if not op:  return JSONResponse({"ok": False, "error": "operator_id_required"},    400)
        if not act: return JSONResponse({"ok": False, "error": "activity_label_required"}, 400)
        if not env: return JSONResponse({"ok": False, "error": "environment_required"},    400)
        if not inv: return JSONResponse({"ok": False, "error": "inventory_required"},      400)

        # Sanitise.
        op  = _sanitise(op)
        act = _sanitise(act)
        env = _sanitise(env)
        inv = _sanitise(inv)
        settings["operator_id"]    = op
        settings["activity_label"] = act
        settings["environment"]    = env
        settings["inventory"]      = inv

        with _failed_seg_lock:
            _failed_segments.clear()

        # Reset FOV state for the new session.
        state["fov"] = {
            "running":           False,
            "passed":            None,
            "frames_checked":    0,
            "frames_with_hands": 0,
            "hands_in_frame":    0,
            "elapsed_sec":       0.0,
            "total_sec":         0.0,
            "settled":           False,
            "method":            "unknown",
            "message":           None,
        }
        state["frame_check"] = None

        def _on_upload_status_change(s):
            _broadcast()
            if _gpio is None:
                return
            pending = s["queued"] + s["uploading"] + s["retrying"]
            if pending > 0:
                _upload_tracker["was_pending"] = True
            elif _upload_tracker["was_pending"]:
                if not (_session and _session.is_running()):
                    log.info("All uploads done — set_upload_complete()")
                    try: _gpio.set_upload_complete()
                    except Exception as e:
                        log.warning(f"GPIO set_upload_complete: {e}")
                _upload_tracker["was_pending"] = False

        _upload_queue.on_status_change = _on_upload_status_change

        max_segs = max(
            1, settings["session_duration"] // settings["segment_duration"],
        )
        _set_state(
            segments=[], current_segment=-1, max_segments=max_segs,
        )

        _session = SessionV2(
            operator_id      = op,
            activity_label   = act,
            environment      = env,
            inventory        = inv,
            segment_duration = settings["segment_duration"],
            session_duration = settings["session_duration"],
            mcap_enabled     = settings["mcap_enabled"],
            on_state_change  = _on_state,
            on_segment_update= _on_segment_update,
            on_frame_check   = _on_frame_check,
            on_complete      = _on_complete,
            # Inter-segment FOV check pushes progress and JPEG frames
            # to the SAME handlers as the initial pre-session check, so
            # the dashboard's existing /fov-stream preview card and
            # state.fov fields work transparently for both.
            on_fov_progress  = _fov_progress_cb,
            on_fov_frame     = _fov_frame_callback,
            upload_queue     = _upload_queue,
            gpio             = _gpio,
        )
        _session.start()
        return JSONResponse(
            {"ok": True, "session_id": _session.session_id}, 202,
        )


@app.post("/session/stop")
def stop_session():
    if _session and _session.is_running():
        _session.stop_early()
        return {"ok": True}
    return JSONResponse({"ok": False, "error": "no_active_session"}, 409)


# ── Settings ──────────────────────────────────────────────────────
@app.get("/settings")
def get_settings():
    return settings


@app.post("/settings")
async def update_settings(request: Request):
    try:
        req = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, 400)

    for key in (
        "segment_duration", "session_duration", "mcap_enabled",
        "operator_id", "activity_label", "environment", "inventory",
        "fov_check_enabled",
    ):
        if key in req:
            settings[key] = req[key]

    # AWS creds passed via settings are environment-only.
    for key in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_BUCKET_NAME",
    ):
        v = req.get(key)
        if v:
            os.environ[key] = v

    state["max_segments"] = max(
        1, settings["session_duration"] // settings["segment_duration"],
    )
    _broadcast()
    log.info(f"Settings updated for operator={settings['operator_id']}")
    return {"ok": True, "settings": settings}


# ── Upload Status ─────────────────────────────────────────────────
@app.get("/upload-status")
def upload_status():
    status_data = _upload_queue.get_status()
    with _failed_seg_lock:
        status_data["failed_segments"] = [
            s for s in _failed_segments if not s["reported"]
        ]
    return status_data


@app.post("/segment/reported/{seg_idx}")
def mark_segment_reported(seg_idx: int):
    with _failed_seg_lock:
        for s in _failed_segments:
            if s["seg_idx"] == seg_idx:
                s["reported"] = True
    return {"ok": True}


# ── Session History ───────────────────────────────────────────────
@app.get("/history")
def get_history():
    """Today's session history. In-memory + on-disk manifest scan
    (recursive, since the new layout nests by date/task/env/...)."""
    today          = date.today().isoformat().replace("-", "")
    today_sessions = list(_session_history)

    if os.path.exists(OUTPUT_DIR):
        try:
            for manifest_path in Path(OUTPUT_DIR).rglob("manifest_*.json"):
                try:
                    with open(manifest_path) as f:
                        data = json.load(f)
                    sid = data.get("session_id", "")
                    # The session folder is two levels above the manifest
                    # in the new layout (date/.../session/manifest_*.json).
                    # We just compare the date folder at the top.
                    parts = manifest_path.relative_to(OUTPUT_DIR).parts
                    if not parts or parts[0] != today:
                        continue
                    if not any(
                        h["session_id"] == sid for h in today_sessions
                    ):
                        today_sessions.append({
                            "session_id":  sid,
                            "operator_id": data.get("operator_id", ""),
                            "activity":    data.get("activity_label", ""),
                            "environment": data.get("environment", ""),
                            "inventory":   data.get("inventory", ""),
                            "segments":    data.get("segments_complete", 0),
                            "timestamp":   sid,
                            "mcap":        data.get("mcap_enabled", False),
                        })
                except (OSError, json.JSONDecodeError):
                    pass
        except OSError as e:
            log.warning(f"history scan: {e}")

    return {"sessions": today_sessions[:20]}


# ── Hardware-button bridge ────────────────────────────────────────
@app.post("/gpio/start")
def gpio_start():
    return start_session()


@app.post("/gpio/fov")
def gpio_fov():
    return start_fov_check()


# ── Serve UI ──────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def serve_ui():
    ui_path = Path(__file__).parent / "index.html"
    try:
        return HTMLResponse(ui_path.read_text())
    except OSError as e:
        return HTMLResponse(
            f"<h1>UI failed to load</h1><pre>{e}</pre>", 500,
        )


# ── Lifecycle ─────────────────────────────────────────────────────
@app.on_event("startup")
def _startup():
    _upload_queue.start()


@app.on_event("shutdown")
def _shutdown_event():
    if _session and _session.is_running():
        log.info("Shutdown: stopping in-flight session")
        try:
            _session.stop_early()
            _session.join(timeout=20)
        except Exception as e:
            log.warning(f"session stop_early raised: {e}")
    _upload_queue.stop()


def shutdown_session_and_uploads(timeout_s: float = 30.0):
    """Externally callable graceful-shutdown helper used by the daemon."""
    if _session and _session.is_running():
        log.info("Graceful shutdown: stopping in-flight session")
        try:
            _session.stop_early()
            _session.join(timeout=timeout_s)
        except Exception as e:
            log.warning(f"session stop_early raised: {e}")
    _upload_queue.stop()


def run():
    uvicorn.run(app, host=UI_HOST, port=UI_PORT, log_level="warning")
