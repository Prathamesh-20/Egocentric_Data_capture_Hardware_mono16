#!/usr/bin/env python3
"""
Capture Daemon V2 — entry point with NAYAA Hat GPIO support.

Physical buttons and web UI both control sessions.
SW1 = Start session, SW2 = Stop session (only during recording).
After session ends, immediately ready for next session.
Uploads continue silently in background.
When ALL uploads finish → buzzer beeps 5 seconds → auto-idle.
"""
import sys, os, time, signal, threading, logging

sys.path.insert(0, os.path.dirname(__file__))

from capture.config import UI_PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("daemon")

# ── GPIO Controller ───────────────────────────────────────────────
from capture.gpio_controller import GPIOController
gpio = GPIOController()

# ── Upload monitor ────────────────────────────────────────────────
#_upload_monitor_active = threading.Event()


def _monitor_uploads(upload_queue):
    """
    Called after a session completes.
    Polls upload queue every 2 seconds.
    When all uploads finish → buzzer 5 seconds → auto-idle.
    """
    log.info("[upload-monitor] Watching for uploads to complete...")

    while True:
        time.sleep(2)
        try:
            status = upload_queue.get_status()
        except Exception:
            continue

        pending = status["queued"] + status["uploading"] + status["retrying"]

        if pending == 0 and status["total"] > 0:
            log.info("[upload-monitor] All uploads complete!")
            if gpio.available:
                gpio.set_upload_complete()  # buzzer 5s, then auto-idle
            break

    _upload_monitor_active.clear()


# ── Session control via buttons ───────────────────────────────────
def _start_session_from_button():
    import requests
    try:
        requests.post(f"http://127.0.0.1:{UI_PORT}/settings", json={
            "operator_id": "hw_button_operator",
        }, timeout=3)
        r = requests.post(f"http://127.0.0.1:{UI_PORT}/session/start", timeout=3)
        log.info(f"[SW1] Session start: {r.status_code}")
    except Exception as e:
        log.warning(f"[SW1] Failed to start session: {e}")


def _stop_session_from_button():
    import requests
    try:
        r = requests.post(f"http://127.0.0.1:{UI_PORT}/session/stop", timeout=3)
        log.info(f"[SW2] Session stop: {r.status_code}")
    except Exception as e:
        log.warning(f"[SW2] Failed to stop session: {e}")


gpio.on_sw1_press = _start_session_from_button
gpio.on_sw2_press = _stop_session_from_button


# ── Inject GPIO into server ───────────────────────────────────────
def setup_server_gpio_hooks():
    from capture.ui import server as srv
    srv._gpio = gpio


# ── Shutdown ──────────────────────────────────────────────────────
def shutdown(signum, frame):
    log.info("Shutting down daemon...")
    gpio.shutdown()
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("=" * 56)
    log.info("  Egocentric Capture V2 Daemon")
    log.info(f"  Web UI: http://localhost:{UI_PORT}")
    log.info(f"  GPIO: {'ENABLED' if gpio.available else 'DISABLED'}")
    log.info("  SW1=Start  SW2=Stop(recording only)")
    log.info("=" * 56)

    if gpio.available:
        gpio.set_idle()

    setup_server_gpio_hooks()

    from capture.ui.server import run
    run()


if __name__ == "__main__":
    main()
