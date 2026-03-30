#!/usr/bin/env python3
"""
Capture Daemon V2 — entry point, runs on Pi boot as systemd service.

Starts the FastAPI web server and watches GPIO buttons.
Physical buttons hit the same HTTP endpoints as the UI buttons.

Wiring:
  Button 1 (FOV Check)  : GPIO 17 (Pin 11) → GND (Pin 9)
  Button 2 (Start Rec)  : GPIO 27 (Pin 13) → GND (Pin 14)
"""
import sys, os, time, signal, threading, logging, requests

sys.path.insert(0, os.path.dirname(__file__))

from capture.config import PIN_FOV_CHECK, PIN_START_REC, DEBOUNCE_S, UI_PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("daemon")

BASE_URL = f"http://127.0.0.1:{UI_PORT}"

# ── GPIO ──────────────────────────────────────────────────────────
try:
    import lgpio
    h = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_input(h, PIN_FOV_CHECK, lgpio.SET_PULL_UP)
    lgpio.gpio_claim_input(h, PIN_START_REC, lgpio.SET_PULL_UP)
    GPIO_OK = True
    log.info(f"GPIO ready — FOV=GPIO{PIN_FOV_CHECK}  START=GPIO{PIN_START_REC}")
except Exception as e:
    log.warning(f"GPIO unavailable: {e} — buttons disabled, use web UI only")
    GPIO_OK = False
    h = None

_daemon_stop = threading.Event()


def _post(path: str):
    try:
        r = requests.post(f"{BASE_URL}{path}", timeout=3)
        log.info(f"GPIO → {path}: {r.status_code}")
    except Exception as e:
        log.warning(f"GPIO → {path} failed: {e}")


def gpio_loop():
    if not GPIO_OK: return
    prev_fov   = prev_start = 1
    last_fov   = last_start = 0

    while not _daemon_stop.is_set():
        now = time.time()
        try:
            cur_fov   = lgpio.gpio_read(h, PIN_FOV_CHECK)
            cur_start = lgpio.gpio_read(h, PIN_START_REC)
        except Exception as e:
            log.error(f"GPIO read error: {e}"); time.sleep(0.5); continue

        if prev_fov == 1 and cur_fov == 0 and (now - last_fov) > DEBOUNCE_S:
            last_fov = now
            log.info("Button 1 pressed → FOV check")
            threading.Thread(target=_post, args=("/gpio/fov",), daemon=True).start()

        if prev_start == 1 and cur_start == 0 and (now - last_start) > DEBOUNCE_S:
            last_start = now
            log.info("Button 2 pressed → start session")
            threading.Thread(target=_post, args=("/gpio/start",), daemon=True).start()

        prev_fov   = cur_fov
        prev_start = cur_start
        time.sleep(0.01)


def shutdown(signum, frame):
    global GPIO_OK
    log.info("Shutting down daemon...")
    _daemon_stop.set()
    if GPIO_OK:
        try:
            lgpio.gpiochip_close(h)
        except Exception:
            pass
        GPIO_OK = False  # prevent double-close
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("=" * 56)
    log.info("  Egocentric Capture V2 Daemon")
    log.info(f"  Web UI: http://localhost:{UI_PORT}")
    log.info(f"  Session: sequential 1-min segments")
    log.info("=" * 56)

    threading.Thread(target=gpio_loop, daemon=True).start()

    from capture.ui.server import run
    run()


if __name__ == "__main__":
    main()
