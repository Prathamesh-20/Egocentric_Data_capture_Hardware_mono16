"""
GPIO Controller for NAYAA Hat Rev 2.

Manages LEDs, buzzer, and button inputs for headless operation.
Uses lgpio library (Pi 5 compatible).

State machine:
  IDLE         → Red LED solid ON. Waiting for SW1.
  RECORDING    → Green LED blinking. Red off.
  SEGMENT_GAP  → Green LED solid. Buzzer 2x beep. 5s pause.
  UPLOADING    → Red LED blinking. Green off.
  IDLE (again) → Red solid. Ready for next session. Uploads continue silently.

Hardening (vs previous build):
  - LED and button loop bodies are wrapped so a transient lgpio.error
    cannot silently kill the feedback thread.
  - shutdown() is idempotent.
  - LED writes are skipped when no lgpio handle is open, even if a stale
    flag was left set.
"""
import logging
import threading
import time

from capture.config import (
    PIN_SW1, PIN_SW2, PIN_LED_GREEN, PIN_LED_RED, PIN_BUZZER, DEBOUNCE_S,
)

log = logging.getLogger(__name__)

try:
    import lgpio
    _GPIO_AVAILABLE = True
except ImportError:
    log.warning("lgpio not available — GPIO disabled, use web UI only")
    _GPIO_AVAILABLE = False


class GPIOController:
    def __init__(self):
        self._h         = None
        self._available = False
        self._stop      = threading.Event()
        self._led_thread:    object = None
        self._button_thread: object = None
        self._shutdown_done = False
        self._handle_lock   = threading.Lock()

        self._green_mode  = "off"   # off | on | blink
        self._red_mode    = "off"   # off | on | blink
        self._buzzer_mode = "off"   # off | continuous

        self.on_sw1_press = None
        self.on_sw2_press = None

        self._init_gpio()

    def _init_gpio(self):
        if not _GPIO_AVAILABLE:
            return
        try:
            self._h = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_output(self._h, PIN_LED_GREEN, 0)
            lgpio.gpio_claim_output(self._h, PIN_LED_RED, 0)
            lgpio.gpio_claim_output(self._h, PIN_BUZZER, 0)
            lgpio.gpio_claim_input(self._h, PIN_SW1, lgpio.SET_PULL_UP)
            lgpio.gpio_claim_input(self._h, PIN_SW2, lgpio.SET_PULL_UP)
            self._available = True
            log.info(f"GPIO ready — SW1=GPIO{PIN_SW1} SW2=GPIO{PIN_SW2} "
                     f"Green=GPIO{PIN_LED_GREEN} Red=GPIO{PIN_LED_RED} "
                     f"Buzzer=GPIO{PIN_BUZZER}")
            self._led_thread = threading.Thread(
                target=self._led_loop, daemon=True, name="gpio-led")
            self._led_thread.start()
            self._button_thread = threading.Thread(
                target=self._button_loop, daemon=True, name="gpio-buttons")
            self._button_thread.start()
        except Exception as e:
            log.warning(f"GPIO init failed: {e} — running without hardware feedback")
            self._available = False
            # Best-effort cleanup of partial state.
            if self._h is not None:
                try: lgpio.gpiochip_close(self._h)
                except Exception: pass
                self._h = None

    @property
    def available(self) -> bool:
        return self._available

    # ── LED states ────────────────────────────────────────────────
    def set_idle(self):
        self._green_mode  = "off"
        self._red_mode    = "on"
        self._buzzer_mode = "off"
        log.info("[GPIO] State: IDLE — Red ON")

    def set_recording(self):
        self._green_mode = "blink"
        self._red_mode   = "off"
        log.info("[GPIO] State: RECORDING — Green BLINK")

    def set_segment_gap(self):
        self._green_mode = "on"
        self._red_mode   = "off"
        log.info("[GPIO] State: SEGMENT_GAP — Green ON")

    def set_uploading(self):
        self._green_mode  = "off"
        self._red_mode    = "blink"
        self._buzzer_mode = "off"
        log.info("[GPIO] State: UPLOADING — Red BLINK")

    def set_upload_complete(self):
        """All uploads done — buzzer fast beep for 5 seconds, then auto-idle."""
        self._buzzer_mode = "continuous"
        log.info("[GPIO] State: UPLOAD_COMPLETE — Buzzer 5s alert")

        def _auto_idle():
            time.sleep(5)
            self._buzzer_mode = "off"
            self._safe_write(PIN_BUZZER, 0)
            self.set_idle()
            log.info("[GPIO] Uploads done, back to IDLE")

        threading.Thread(target=_auto_idle, daemon=True,
                         name="gpio-upload-complete").start()

    def set_error(self):
        """ERROR — both LEDs blink + buzzer 3x. Operator should stop."""
        self._green_mode  = "blink"
        self._red_mode    = "blink"
        self._buzzer_mode = "off"
        self.beep(count=3, on_time=0.3, off_time=0.2)
        log.info("[GPIO] State: ERROR — Both LEDs BLINK + Buzzer 3x")

    def all_off(self):
        self._green_mode  = "off"
        self._red_mode    = "off"
        self._buzzer_mode = "off"
        self._safe_write(PIN_LED_GREEN, 0)
        self._safe_write(PIN_LED_RED,   0)
        self._safe_write(PIN_BUZZER,    0)

    # ── Buzzer patterns ───────────────────────────────────────────
    def beep(self, count=1, on_time=0.15, off_time=0.15):
        if not self._available:
            return

        def _do_beep():
            for i in range(count):
                self._safe_write(PIN_BUZZER, 1)
                time.sleep(on_time)
                self._safe_write(PIN_BUZZER, 0)
                if i < count - 1:
                    time.sleep(off_time)

        threading.Thread(target=_do_beep, daemon=True, name="gpio-beep").start()

    def beep_1x(self):
        self.beep(count=1, on_time=0.2)

    def beep_2x(self):
        self.beep(count=2, on_time=0.15, off_time=0.15)

    # ── FOV-check buzzer control ──────────────────────────────────
    # The FOV check drives the buzzer directly (not via _buzzer_mode)
    # so it doesn't fight the LED loop's recording/uploading state.
    # These methods are safe to call from any thread; they only touch
    # PIN_BUZZER via _safe_write.

    def buzzer_on(self):
        """Turn the buzzer on solid (steady tone). Used for 0-hands FOV alarm."""
        self._safe_write(PIN_BUZZER, 1)

    def buzzer_off(self):
        """Turn the buzzer off immediately. Idempotent."""
        self._safe_write(PIN_BUZZER, 0)

    def start_fov_alarm(self, pattern: str):
        """Start a non-blocking buzzer pattern for the FOV check.

        pattern is one of:
          "continuous"    — both hands missing; fast rhythmic beeping (~4 Hz)
          "intermittent"  — one hand missing; slow beeping with gaps (~1.3 Hz)
          "off"           — both hands present; silent

        The two non-off patterns are deliberately tuned to sound clearly
        different: the both-hands pattern is rapid-fire to convey urgency,
        the one-hand pattern is spaced out so the operator can tell which
        condition they're in without looking at the screen.

        Calling this with a new pattern replaces the previous one.
        Always pair with stop_fov_alarm() when the FOV check ends.
        """
        # Stop any prior alarm thread first.
        self.stop_fov_alarm()

        log.info(f"[GPIO] start_fov_alarm({pattern!r}) — buzzer_mode={self._buzzer_mode}, available={self.available}")

        if not self.available:
            log.warning("[GPIO] start_fov_alarm: GPIO not available, ignoring")
            return

        if pattern == "off":
            return

        stop_evt = threading.Event()
        self._fov_alarm_stop = stop_evt

        def _continuous():
            # "Both hands missing" — fast rhythmic beeping.
            # 120ms on, 120ms off → ~4 beeps per second.
            # Tune knob: lower both numbers (e.g. 0.08/0.08) for more
            # urgency; don't go below 0.08 or it sounds like clicks on
            # most piezo buzzers rather than beeps.
            log.info("[GPIO] FOV alarm thread STARTED (both-hands-missing fast beep)")
            while not stop_evt.is_set():
                self._safe_write(PIN_BUZZER, 1)
                if stop_evt.wait(timeout=0.12):
                    break
                self._safe_write(PIN_BUZZER, 0)
                if stop_evt.wait(timeout=0.12):
                    break
            self._safe_write(PIN_BUZZER, 0)
            log.info("[GPIO] FOV alarm thread EXIT (both-hands-missing)")

        def _intermittent():
            # "One hand missing" — slow beeps with audible gaps.
            # 150ms on, 600ms off → roughly 1.3 beeps per second, clearly
            # distinguishable by ear from the fast both-hands pattern.
            # Tune knob: push the 0.6 up to 0.8 or 1.0 for more spacing
            # between beeps.
            log.info("[GPIO] FOV alarm thread STARTED (one-hand-missing slow beep)")
            while not stop_evt.is_set():
                self._safe_write(PIN_BUZZER, 1)
                if stop_evt.wait(timeout=0.15):
                    break
                self._safe_write(PIN_BUZZER, 0)
                if stop_evt.wait(timeout=0.6):
                    break
            self._safe_write(PIN_BUZZER, 0)
            log.info("[GPIO] FOV alarm thread EXIT (one-hand-missing)")

        target = _continuous if pattern == "continuous" else _intermittent
        t = threading.Thread(
            target=target, daemon=True, name=f"fov-alarm-{pattern}",
        )
        self._fov_alarm_thread = t
        t.start()

    def stop_fov_alarm(self):
        """Stop any running FOV alarm and ensure the buzzer is off."""
        evt = getattr(self, "_fov_alarm_stop", None)
        thr = getattr(self, "_fov_alarm_thread", None)
        if evt is not None or thr is not None:
            log.info(f"[GPIO] stop_fov_alarm — had thread alive={thr.is_alive() if thr else None}")
        if evt is not None:
            evt.set()
        if thr is not None and thr.is_alive():
            thr.join(timeout=1.0)
        self._fov_alarm_stop = None
        self._fov_alarm_thread = None
        self._safe_write(PIN_BUZZER, 0)

    # ── Internals ─────────────────────────────────────────────────
    def _safe_write(self, pin: int, value: int):
        with self._handle_lock:
            if self._h is None:
                return
            try:
                lgpio.gpio_write(self._h, pin, value)
            except Exception as e:
                log.warning(f"gpio_write({pin}, {value}) failed: {e}")

    def _safe_read(self, pin: int) -> int:
        with self._handle_lock:
            if self._h is None:
                return 1
            try:
                return lgpio.gpio_read(self._h, pin)
            except Exception as e:
                log.warning(f"gpio_read({pin}) failed: {e}")
                return 1

    def _led_loop(self):
        blink_state = False
        while not self._stop.is_set():
            try:
                blink_state = not blink_state

                if self._green_mode == "on":
                    self._safe_write(PIN_LED_GREEN, 1)
                elif self._green_mode == "blink":
                    self._safe_write(PIN_LED_GREEN, 1 if blink_state else 0)
                else:
                    self._safe_write(PIN_LED_GREEN, 0)

                if self._red_mode == "on":
                    self._safe_write(PIN_LED_RED, 1)
                elif self._red_mode == "blink":
                    self._safe_write(PIN_LED_RED, 1 if blink_state else 0)
                else:
                    self._safe_write(PIN_LED_RED, 0)

                if self._buzzer_mode == "continuous":
                    self._safe_write(PIN_BUZZER, 1 if blink_state else 0)
            except Exception as e:
                log.warning(f"LED loop iteration failed: {e}")

            time.sleep(0.2)

    def _button_loop(self):
        prev_sw1 = prev_sw2 = 1
        last_sw1 = last_sw2 = 0.0

        while not self._stop.is_set():
            try:
                now     = time.time()
                cur_sw1 = self._safe_read(PIN_SW1)
                cur_sw2 = self._safe_read(PIN_SW2)

                if (prev_sw1 == 1 and cur_sw1 == 0
                        and (now - last_sw1) > DEBOUNCE_S):
                    last_sw1 = now
                    log.info("[GPIO] SW1 pressed (Start)")
                    if self.on_sw1_press:
                        threading.Thread(target=self.on_sw1_press,
                                         daemon=True,
                                         name="sw1-cb").start()

                if (prev_sw2 == 1 and cur_sw2 == 0
                        and (now - last_sw2) > DEBOUNCE_S):
                    last_sw2 = now
                    log.info("[GPIO] SW2 pressed (Stop)")
                    if self.on_sw2_press:
                        threading.Thread(target=self.on_sw2_press,
                                         daemon=True,
                                         name="sw2-cb").start()

                prev_sw1 = cur_sw1
                prev_sw2 = cur_sw2
            except Exception as e:
                log.warning(f"Button loop iteration failed: {e}")
                time.sleep(0.5)

            time.sleep(0.02)

    # ── Cleanup ───────────────────────────────────────────────────
    def shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._stop.set()
        try:
            self.stop_fov_alarm()
        except Exception:
            pass
        try:
            self.all_off()
        except Exception:
            pass
        with self._handle_lock:
            if self._h is not None:
                try:
                    lgpio.gpiochip_close(self._h)
                except Exception:
                    pass
                self._h = None
        log.info("[GPIO] Shutdown complete")
