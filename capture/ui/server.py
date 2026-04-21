"""
FastAPI backend V2 — Simplified for Orbbec-only recording.
"""
import asyncio, json, logging, threading, time, os, glob
from pathlib import Path
from typing import Optional
from datetime import datetime, date

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

from capture.config import (
    UI_HOST, UI_PORT, FOV_CHECK_SECS, FOV_MIN_DETECTION_FRAMES,
    SEGMENT_DURATION, SESSION_DURATION, MCAP_ENABLED_DEFAULT, OUTPUT_DIR,
    FOV_CHECK_ENABLED,
)
from capture.pipeline.session_v2 import SessionV2
from capture.pipeline.uploader import UploadQueue

log = logging.getLogger(__name__)
app = FastAPI()

# ── Runtime settings ──────────────────────────────────────────────
settings = {
    "segment_duration": SEGMENT_DURATION,
    "session_duration": SESSION_DURATION,
    "mcap_enabled":     MCAP_ENABLED_DEFAULT,
    "operator_id":      "",
    "operator_name":    "",
    "task_id":          "",
    "activity_label":   "",
}

# ── Global state ──────────────────────────────────────────────────
state = {
    "status":           "idle",
    "message":          "Ready — press Start to begin recording",
    "fov_result":       None,
    "session_id":       None,
    "current_segment":  -1,
    "max_segments":     SESSION_DURATION // SEGMENT_DURATION,
    "segments":         [],
    "progress":         0,
    "frame_check":      None,
    "detection_method": None,
}

_session:         Optional[SessionV2] = None
_ws_clients       = set()
_ws_lock          = threading.Lock()
_upload_queue     = UploadQueue()
_session_history  = []
_gpio             = None
_failed_segments  = []  # track failed segments to notify polling agent
_failed_seg_lock  = threading.Lock()
_upload_tracker   = {"was_pending": False}


def _set_state(**kwargs):
    state.update(kwargs)
    _broadcast()


def _broadcast():
    data = state.copy()
    data["upload_status"] = _upload_queue.get_status()
    data["settings"]      = settings.copy()
    msg = json.dumps(data, default=str)
    with _ws_lock:
        dead = set()
        for ws in _ws_clients:
            try:
                asyncio.run_coroutine_threadsafe(ws.send_text(msg), _loop)
            except Exception:
                dead.add(ws)
        _ws_clients.difference_update(dead)


_loop: asyncio.AbstractEventLoop = None


# ── WebSocket ─────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global _loop
    _loop = asyncio.get_event_loop()
    await ws.accept()
    with _ws_lock:
        _ws_clients.add(ws)
    try:
        data = state.copy()
        data["upload_status"] = _upload_queue.get_status()
        data["settings"]      = settings.copy()
        await ws.send_text(json.dumps(data, default=str))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        with _ws_lock:
            _ws_clients.discard(ws)


# ── FOV Check ─────────────────────────────────────────────────────
@app.post("/fov-check")
def start_fov_check():
    if not FOV_CHECK_ENABLED:
        _set_state(status="fov_passed",
                   message="FOV check disabled — ready to record",
                   fov_result="FOV check disabled")
        return {"ok": True, "message": "FOV check disabled, ready to record"}

    if state["status"] in ("recording", "converting", "session_active", "frame_check"):
        return JSONResponse({"error": "Cannot run FOV check during active session"}, 400)

    _set_state(status="fov_passed", message="FOV check skipped", fov_result="skipped")
    return {"ok": True}


@app.get("/fov-stream")
def fov_stream():
    return JSONResponse({"message": "FOV check disabled"}, 200)


# ── Session Control ───────────────────────────────────────────────
@app.post("/session/start")
def start_session():
    global _session

    if state["status"] in ("recording", "session_active", "frame_check"):
        return JSONResponse({"error": "Session already running"}, 400)

    if _session and _session.is_running():
        return JSONResponse({"error": "Session already running"}, 400)

    if not settings["operator_id"].strip():
        return JSONResponse({"error": "Operator ID required"}, 400)

    # Reset failed segments for new session
    with _failed_seg_lock:
        _failed_segments.clear()

    def _on_upload_status_change(s):
        _broadcast()
        if _gpio is None:
            return
        pending = s["queued"] + s["uploading"] + s["retrying"]
        if pending > 0:
            _upload_tracker["was_pending"] = True
        elif _upload_tracker["was_pending"]:
            if not (_session and _session.is_running()):
                _gpio.set_upload_complete()
            _upload_tracker["was_pending"] = False

    _upload_queue.on_status_change = _on_upload_status_change
    _upload_queue.start()

    def _on_state(status, detail, **extra):
        seg_idx = extra.get("segment_idx", state.get("current_segment", -1))
        _set_state(status=status, message=detail, current_segment=seg_idx)

        # Track failed segments — bag_small_warning or bag_empty_warning
        if status in ("bag_small_warning", "bag_empty_warning"):
            with _failed_seg_lock:
                _failed_segments.append({
                    "seg_idx":    seg_idx,
                    "reason":     detail,
                    "session_id": state.get("session_id"),
                    "reported":   False,  # set to True once polling agent reports to backend
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
            segs.append({"index": seg_idx, "status": seg_status, "wrist_ok": wrist_ok})
        _set_state(segments=segs, current_segment=seg_idx)

    def _on_complete(session_id, n_segments, manifest):
        _session_history.append({
            "session_id":  session_id,
            "operator_id": settings["operator_id"],
            "activity":    settings["activity_label"],
            "segments":    n_segments,
            "timestamp":   datetime.now().isoformat(),
            "mcap":        settings["mcap_enabled"],
        })
        _set_state(status="complete",
                   message=f"Session {session_id} — {n_segments} segments complete")

    max_segs = settings["session_duration"] // settings["segment_duration"]
    _set_state(segments=[], current_segment=-1, max_segments=max_segs, frame_check=None)

    _session = SessionV2(
        operator_id      = settings["operator_id"],
        operator_name    = settings["operator_name"],
        task_id          = settings["task_id"],
        activity_label   = settings["activity_label"],
        segment_duration = settings["segment_duration"],
        session_duration = settings["session_duration"],
        mcap_enabled     = settings["mcap_enabled"],
        on_state_change  = _on_state,
        on_segment_update= _on_segment_update,
        on_frame_check   = None,
        on_complete      = _on_complete,
        upload_queue     = _upload_queue,
        gpio             = _gpio,
    )
    _session.start()
    return {"ok": True, "session_id": _session.session_id}


@app.post("/session/stop")
def stop_session():
    if _session and _session.is_running():
        _session.stop_early()
        return {"ok": True}
    return JSONResponse({"error": "No active session"}, 400)


# ── Settings ──────────────────────────────────────────────────────
@app.get("/settings")
def get_settings():
    return settings


@app.post("/settings")
async def update_settings(request: Request):
    try:
        req = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, 400)

    for key in ("segment_duration", "session_duration", "mcap_enabled",
                "operator_id", "operator_name", "task_id", "activity_label"):
        if key in req:
            settings[key] = req[key]

    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "AWS_BUCKET_NAME"):
        if req.get(key):
            os.environ[key] = req[key]

    state["max_segments"] = settings["session_duration"] // settings["segment_duration"]
    _broadcast()
    log.info(f"Settings updated: {settings}")
    return {"ok": True, "settings": settings}


# ── Upload Status ─────────────────────────────────────────────────
@app.get("/upload-status")
def upload_status():
    status_data = _upload_queue.get_status()
    # Include failed segments so polling agent can report them to backend
    with _failed_seg_lock:
        status_data["failed_segments"] = [
            s for s in _failed_segments if not s["reported"]
        ]
    return status_data


# ── Mark failed segment as reported ───────────────────────────────
@app.post("/segment/reported/{seg_idx}")
def mark_segment_reported(seg_idx: int):
    """Polling agent calls this after reporting a failed segment to backend."""
    with _failed_seg_lock:
        for s in _failed_segments:
            if s["seg_idx"] == seg_idx:
                s["reported"] = True
    return {"ok": True}


# ── Session History ───────────────────────────────────────────────
@app.get("/history")
def get_history():
    today          = date.today().isoformat()
    today_sessions = [h for h in _session_history if h["timestamp"].startswith(today)]

    if os.path.exists(OUTPUT_DIR):
        for d in sorted(glob.glob(os.path.join(OUTPUT_DIR, "session_*")), reverse=True):
            for m in glob.glob(os.path.join(d, "manifest_*.json")):
                try:
                    with open(m) as f:
                        data = json.load(f)
                    sid = data.get("session_id", "")
                    if not any(h["session_id"] == sid for h in today_sessions):
                        today_sessions.append({
                            "session_id":  sid,
                            "operator_id": data.get("operator_id", ""),
                            "activity":    data.get("activity_label", ""),
                            "segments":    data.get("segments_complete", 0),
                            "timestamp":   sid,
                            "mcap":        data.get("mcap_enabled", False),
                        })
                except Exception:
                    pass

    return {"sessions": today_sessions[:20]}


# ── Status ────────────────────────────────────────────────────────
@app.get("/status")
def get_status():
    data = state.copy()
    data["upload_status"] = _upload_queue.get_status()
    data["settings"]      = settings.copy()
    return data


# ── GPIO bridge ───────────────────────────────────────────────────
@app.post("/gpio/start")
def gpio_start():
    return start_session()


# ── Serve UI ──────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def serve_ui():
    ui_path = Path(__file__).parent / "index.html"
    return HTMLResponse(ui_path.read_text())


# ── Startup / Shutdown ────────────────────────────────────────────
@app.on_event("shutdown")
def shutdown_event():
    _upload_queue.stop()


def run():
    _upload_queue.start()
    uvicorn.run(app, host=UI_HOST, port=UI_PORT, log_level="warning")
