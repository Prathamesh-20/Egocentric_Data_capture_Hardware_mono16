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
"""
import time, threading, logging

log = logging.getLogger(__name__)

from capture.config import (
    PIN_SW1, PIN_SW2, PIN_LED_GREEN, PIN_LED_RED, PIN_BUZZER, DEBOUNCE_S,
)

try:
    import lgpio
    _GPIO_AVAILABLE = True
except ImportError:
    log.warning("lgpio not available — GPIO disabled, use web UI only")
    _GPIO_AVAILABLE = False


class GPIOController:
    def __init__(self):
        self._h = None
        self._available = False
        self._stop = threading.Event()
        self._led_thread = None
        self._button_thread = None

        self._green_mode = "off"   # off, on, blink
        self._red_mode = "off"     # off, on, blink
        self._buzzer_mode = "off"  # off, continuous

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
            self._led_thread = threading.Thread(target=self._led_loop, daemon=True)
            self._led_thread.start()
            self._button_thread = threading.Thread(target=self._button_loop, daemon=True)
            self._button_thread.start()
        except Exception as e:
            log.warning(f"GPIO init failed: {e} — running without hardware feedback")
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    # ── LED states ────────────────────────────────────────────────

    def set_idle(self):
        self._green_mode = "off"
        self._red_mode = "on"
        self._buzzer_mode = "off"
        log.info("[GPIO] State: IDLE — Red ON")

    def set_recording(self):
        self._green_mode = "blink"
        self._red_mode = "off"
        log.info("[GPIO] State: RECORDING — Green BLINK")

    def set_segment_gap(self):
        self._green_mode = "on"
        self._red_mode = "off"
        log.info("[GPIO] State: SEGMENT_GAP — Green ON")

    def set_uploading(self):
        self._green_mode = "off"
        self._red_mode = "blink"
        self._buzzer_mode = "off"
        log.info("[GPIO] State: UPLOADING — Red BLINK")

    def set_upload_complete(self):
        """All uploads done — buzzer fast beep for 5 seconds, then auto-idle."""
        self._buzzer_mode = "continuous"
        log.info("[GPIO] State: UPLOAD_COMPLETE — Buzzer 5s alert")
        def _auto_idle():
            time.sleep(5)
            self._buzzer_mode = "off"
            if self._available:
                lgpio.gpio_write(self._h, PIN_BUZZER, 0)
            self.set_idle()
            log.info("[GPIO] Uploads done, back to IDLE")
        threading.Thread(target=_auto_idle, daemon=True).start()

    def set_error(self):
        """ERROR state — both LEDs blink together + buzzer 3x.
        Operator should stop and report to the team."""
        self._green_mode = "blink"
        self._red_mode = "blink"
        self._buzzer_mode = "off"
        self.beep(count=3, on_time=0.3, off_time=0.2)
        log.info("[GPIO] State: ERROR — Both LEDs BLINK + Buzzer 3x")
    
        
    def all_off(self):
        self._green_mode = "off"
        self._red_mode = "off"
        self._buzzer_mode = "off"
        if self._available:
            lgpio.gpio_write(self._h, PIN_LED_GREEN, 0)
            lgpio.gpio_write(self._h, PIN_LED_RED, 0)
            lgpio.gpio_write(self._h, PIN_BUZZER, 0)

    # ── Buzzer patterns ───────────────────────────────────────────

    def beep(self, count=1, on_time=0.15, off_time=0.15):
        if not self._available:
            return
        def _do_beep():
            for i in range(count):
                lgpio.gpio_write(self._h, PIN_BUZZER, 1)
                time.sleep(on_time)
                lgpio.gpio_write(self._h, PIN_BUZZER, 0)
                if i < count - 1:
                    time.sleep(off_time)
        threading.Thread(target=_do_beep, daemon=True).start()

    def beep_1x(self):
        self.beep(count=1, on_time=0.2)

    def beep_2x(self):
        self.beep(count=2, on_time=0.15, off_time=0.15)

    # ── LED blink loop ────────────────────────────────────────────

    def _led_loop(self):
        blink_state = False
        while not self._stop.is_set():
            blink_state = not blink_state

            if self._green_mode == "on":
                lgpio.gpio_write(self._h, PIN_LED_GREEN, 1)
            elif self._green_mode == "blink":
                lgpio.gpio_write(self._h, PIN_LED_GREEN, 1 if blink_state else 0)
            else:
                lgpio.gpio_write(self._h, PIN_LED_GREEN, 0)

            if self._red_mode == "on":
                lgpio.gpio_write(self._h, PIN_LED_RED, 1)
            elif self._red_mode == "blink":
                lgpio.gpio_write(self._h, PIN_LED_RED, 1 if blink_state else 0)
            else:
                lgpio.gpio_write(self._h, PIN_LED_RED, 0)

            if self._buzzer_mode == "continuous":
                lgpio.gpio_write(self._h, PIN_BUZZER, 1 if blink_state else 0)

            time.sleep(0.2)

    # ── Button polling ────────────────────────────────────────────

    def _button_loop(self):
        prev_sw1 = prev_sw2 = 1
        last_sw1 = last_sw2 = 0.0

        while not self._stop.is_set():
            now = time.time()
            try:
                cur_sw1 = lgpio.gpio_read(self._h, PIN_SW1)
                cur_sw2 = lgpio.gpio_read(self._h, PIN_SW2)
            except Exception as e:
                log.error(f"GPIO read error: {e}")
                time.sleep(0.5)
                continue

            if prev_sw1 == 1 and cur_sw1 == 0 and (now - last_sw1) > DEBOUNCE_S:
                last_sw1 = now
                log.info("[GPIO] SW1 pressed (Start)")
                if self.on_sw1_press:
                    threading.Thread(target=self.on_sw1_press, daemon=True).start()

            if prev_sw2 == 1 and cur_sw2 == 0 and (now - last_sw2) > DEBOUNCE_S:
                last_sw2 = now
                log.info("[GPIO] SW2 pressed (Stop)")
                if self.on_sw2_press:
                    threading.Thread(target=self.on_sw2_press, daemon=True).start()

            prev_sw1 = cur_sw1
            prev_sw2 = cur_sw2
            time.sleep(0.02)

    # ── Cleanup ───────────────────────────────────────────────────

    def shutdown(self):
        self._stop.set()
        self.all_off()
        if self._h is not None:
            try:
                lgpio.gpiochip_close(self._h)
            except Exception:
                pass
        log.info("[GPIO] Shutdown complete")