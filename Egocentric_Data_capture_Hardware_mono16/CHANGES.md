# Capture Pipeline — v2.1.3 changelog

## Critical fix in this build

The v2.1.x daemon was crash-looping every 5 seconds because
`capture_daemon.py` called `gpio.start()` — a method that doesn't
exist on `GPIOController` (the button-watcher is auto-started by
`__init__`). Each crash retriggered the systemd `RestartSec=5`
restart, which killed the running Orbbec recorder mid-segment,
producing short MCAPs that failed the 600 MB validation.

Symptoms in the log:
  • `GPIO start failed: 'GPIOController' object has no attribute 'start'`
  • Daemon banner repeating every ~5 seconds
  • Segments truncated to ~22 seconds instead of 60s
  • `usbfs: process N (ob_device_recor) did not claim interface`
    messages in dmesg (camera being yanked while in use)

### Fix

In `capture_daemon.py`:
  • Remove the `gpio.start()` call — button thread is auto-started.
  • Use the correct callback attribute names:
      `on_sw1_press` (start session) and `on_sw2_press` (stop session)
    instead of the non-existent `on_start_pressed`/`on_stop_pressed`.

## Also in this build (carried forward from v2.1.2)

  • YOLO phantom-wrist filtering:
      - `WRIST_KP_CONF` 0.3 → 0.5
      - in-frame bounds check (8px inset)
      - anti-duplicate check (40px min distance between accepted wrists)
  • Diagnostic logging in `start_fov_alarm`, the alarm threads,
    and `_update_alarm` so we can see exactly what the buzzer
    code is doing on the next test run.
  • `VERSION` bumped to "2.1.2" in `ui/server.py` so the running
    version is visible at startup.

## Carried from v2.1.1

  • Inter-segment FOV check (3s) before each segment, with the same
    buzzer alarms (0/1/2 hands → continuous/intermittent/silent) and
    the same live MJPEG preview on `/fov-stream`.

## After deploy, expect to see in the log

  • `[GPIO] start_fov_alarm('intermittent') — buzzer_mode=off, available=True`
  • `[GPIO] FOV alarm thread STARTED (intermittent)`
  • `[FOV] hands_in_frame=1 → buzzer INTERMITTENT`
  • Daemon banner appears ONCE at startup, not every 5 seconds.
  • Segments validate at ~1.3 GB (not 488 MB).
