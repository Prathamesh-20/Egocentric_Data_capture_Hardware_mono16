#!/usr/bin/env python3
"""
capture_daemon — entry point.

Wires together:
  • Logging (rotating file at LOG_FILE + console)
  • GPIO controller (button → POST /session/start, etc.)
  • Upload queue (started lazily by FastAPI startup hook)
  • FastAPI HTTP server on UI_PORT

Graceful shutdown: SIGTERM/SIGINT → stop in-flight session, drain
the upload queue (workers each finish their current upload), then exit.

This file is intentionally THIN. All the meaningful state lives in
capture/ui/server.py (HTTP), capture/pipeline/session_v2.py (recording),
and capture/pipeline/uploader.py (S3).
"""
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time

# Make sure capture/ is importable when run from the repo root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capture.config import (
    LOG_BACKUP_COUNT, LOG_DIR, LOG_FILE, LOG_MAX_BYTES,
)


def _setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s — %(message)s"
    )
    rh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
    )
    rh.setFormatter(fmt)
    rh.setLevel(logging.INFO)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Replace any pre-existing handlers (uvicorn may add some).
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(rh)
    root.addHandler(sh)

    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("s3transfer").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.INFO)


_setup_logging()
log = logging.getLogger("capture_daemon")


# ── GPIO ──────────────────────────────────────────────────────────
try:
    from capture.gpio_controller import GPIOController
    gpio = GPIOController()
except Exception as e:
    log.warning(f"GPIO unavailable: {e}")
    gpio = None


# ── Server ────────────────────────────────────────────────────────
from capture.ui import server as ui_server
if gpio is not None:
    ui_server._gpio = gpio


# ── Button bridges ────────────────────────────────────────────────
def _start_session_from_button():
    log.info("Hardware button: start session")
    try:
        ui_server.start_session()
    except Exception as e:
        log.error(f"start_session from button failed: {e}")


def _stop_session_from_button():
    log.info("Hardware button: stop session")
    try:
        ui_server.stop_session()
    except Exception as e:
        log.error(f"stop_session from button failed: {e}")


# ── Graceful shutdown ─────────────────────────────────────────────
_shutdown_requested = threading.Event()


def _signal_handler(signum, _frame):
    sig_name = signal.Signals(signum).name
    log.info(f"Received {sig_name} — initiating graceful shutdown")
    _shutdown_requested.set()
    try:
        ui_server.shutdown_session_and_uploads(timeout_s=30.0)
    except Exception as e:
        log.warning(f"Shutdown helper raised: {e}")
    # Re-raise default behaviour — FastAPI/uvicorn will pick up.
    if signum == signal.SIGTERM:
        # Re-arm default handler so a second SIGTERM kills us.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


signal.signal(signal.SIGINT,  _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ── Wire GPIO buttons ─────────────────────────────────────────────
# GPIOController.__init__ already spawns the button-watching thread,
# so we only need to install the callbacks. The attribute names match
# what _button_loop reads: on_sw1_press, on_sw2_press.
if gpio is not None:
    try:
        gpio.on_sw1_press = _start_session_from_button
        gpio.on_sw2_press = _stop_session_from_button
    except Exception as e:
        log.warning(f"GPIO callback install failed: {e}")


# ── Run ───────────────────────────────────────────────────────────
def main():
    log.info("=" * 64)
    log.info(f"capture_daemon starting — version {ui_server.VERSION}")
    log.info("=" * 64)
    try:
        ui_server.run()
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — shutting down")
    finally:
        log.info("capture_daemon exiting")


if __name__ == "__main__":
    main()
