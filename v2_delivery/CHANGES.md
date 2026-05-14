# v1 → v2 Changes (Platform Handoff Build)

Production-engineering review of the Orbbec Gemini 2L egocentric
capture stack on Raspberry Pi 5. Prepared for handoff to the
platform team building the cloud control plane.

All SDK changes are **strictly additive**. Recorder behavior is
**byte-identical to v1** when the original `1280x800@30 MJPG`
profile is present. Python defaults (`FOV_CHECK_ENABLED=False`,
`WRIST_CHECK_BETWEEN_SEGMENTS=False`) keep the daemon's runtime
indistinguishable from v1 until the operator opts in.

## Platform-team contract

The HTTP/WebSocket interface is now stable and documented in
`project/API_REFERENCE.md`. Highlights:

- Predictable JSON shapes on every endpoint.
- Real HTTP status codes (`202` for accepted-async, `400`/`409`/
  `412`/`503` for the obvious client errors).
- A single state envelope sent on `GET /status` and pushed on `/ws`.
- **No preview frames are exposed.** The FOV pre-check returns a
  pass/fail signal plus counters (frames checked, frames with
  hands, AE-settled flag, detection method). Inter-segment wrist
  checks return a text result only. The v1 endpoints
  `GET /fov-stream` and `GET /frame-check-img` are removed.
- Versioned via `GET /healthz` (`{"version": "2.0.0"}`).

## Why the FOV check didn't work in v1

Five layered bugs:

1. Hard-disabled at the endpoint (`FOV_CHECK_ENABLED = False`, plus
   the endpoint's second branch also short-circuited).
2. The streamer binary `/mnt/ssd/OrbbecSDK_Pi5/orbbec_stream` did
   not exist anywhere — not in the SDK, not in the install kit, not
   in the project. The protocol it expected was not emitted by any
   official SDK example.
3. `single_frame_check` was orphaned — referenced in v1 README and
   dashboard but never wired into `session_v2.py`.
4. The dashboard rendered `<img src="/fov-stream">` which returned
   JSON; the preview was permanently broken.
5. The SDK's intrinsics API was unused, so even downstream FOV math
   couldn't be done from MCAP data.

### Fix

- New additive SDK example `2.stream.color_jpeg_stdout/` builds
  `ob_color_jpeg_stream`, which emits the exact protocol
  `fov_check.py` parses. `install_kit_v2/install.sh` deploys it.
- `fov_check.py` is rewritten:
  - `subprocess` import at top of file.
  - `_StreamParser` is stateful, capped at 4 MB, resyncs on bad
    headers.
  - `ExposureSettler` handles AE convergence so first dark frames
    aren't false-negatived.
  - Helper-binary lifecycle is owned in `try/finally` so fds and
    children can never leak.
  - YOLO load is thread-safe, cached.
  - `_last_stop_time` is thread-safe via lock + accessor.
  - `progress_cb` takes `FOVProgress` (counters only — no frame
    image is ever exposed).
- `session_v2._maybe_pre_segment_wrist_check()` runs the helper
  briefly between segments to grab one settled frame, runs
  `single_frame_check` on it, surfaces a text-only result via
  `state["frame_check"]`. No image is exposed.
- `server.py:/fov-check` actually runs the FOVChecker in a
  background thread and updates `state["fov"]` with counters; the
  endpoint is poll-friendly and pushes via `/ws` on every update.

## Production correctness fixes (independent of FOV)

### MCAP truncation on segment end (HIGH)

v1 stopped the recorder with `time.sleep(2); proc.terminate()`. The
C++ recorder's drain time at 1280x800@30 zstd-compressed depth is
3–6 s. **Fix:** `OrbbecRecorder.stop()` sends `q\n` and waits for
the recorder's `Summary:` line on stdout (its drain-complete
signal), bounded by `ORBBEC_RECORDER_DRAIN_S = 15s`. Stop is
idempotent and lock-protected.

### Uploader double-start race (HIGH)

`server.py:run()` started the upload worker; `/session/start`
started it again. Two workers consumed the same queue. **Fix:**
`UploadQueue.start()` is idempotent (no-op if alive). Single source
of truth: FastAPI `@app.on_event("startup")`.

### Graceful shutdown (HIGH)

v1 had no signal handler. **Fix:** `capture_daemon.py` installs
SIGINT/SIGTERM handler that calls `shutdown_session_and_uploads()`
(waits up to 30 s for the in-flight session and recorder), then
`gpio.shutdown()`. Double-signal forces hard exit.
`capture-daemon.service` now sets `TimeoutStopSec=60` and
`KillMode=mixed`.

### Manifest never uploaded

v1 wrote `manifest_<sid>.json` to local disk only. **Fix:**
`session_v2._finalize_session()` enqueues the manifest through
`upload_queue.enqueue_segment_files()`. The uploader's filename
filter accepts `manifest_*` in addition to `.mcap` and `.csv`.

### Recorder color-profile fragility

v1's recorder hard-required `Color MJPG 1280x800@30`. Older
firmware on the same Gemini 2L exposed `Color MJPG 1280x720@30`.
**Fix (additive):** Pass-2 fallback to first `MJPG@30` profile when
the exact `1280x800@30` isn't present. Logs a warning. **When the
original profile is present this branch never executes** —
behavior is byte-identical to v1. Depth path unchanged.

### Service unit `LD_LIBRARY_PATH`

v1's unit set `LD_LIBRARY_PATH=/opt/OrbbecSDK/lib`, mismatching the
install kit's deploy location of
`/home/<user>/OrbbecSDK_v2.7.6_…/lib`. v1 worked only because
operators happened to have the var set in their `.bashrc`. **Fix:**
unit now references the install kit's path.

### GPIO loop resilience

v1's LED and button loops had no exception handling around the
iteration body. A transient `lgpio.error` (USB power dip during
camera renumeration) would silently kill the feedback thread.
**Fix:** every iteration body wrapped in `try/except`, `_safe_write`
and `_safe_read` swallow per-call errors, `shutdown()` is
idempotent with a handle lock.

### Crash detection during a segment

v1's drain thread ignored the recorder's exit code. **Fix:**
`OrbbecRecorder._drain` sets `self.crashed = True` on unexpected
exit; `session_v2._record_segment()` polls `is_alive()` at 200 ms
and ends the segment early on crash, recording a `CRASH_DETECTED`
row in the timestamps CSV.

### Logging

v1 logged to stdout only. **Fix:** `RotatingFileHandler` at
`/mnt/ssd/logs/capture_daemon.log` (10 MB × 5).

### Smaller correctness items

- `uploader.py` honors `_stop` during retry backoff (was a fixed
  blocking `time.sleep(delay)` — could hold shutdown for thousands
  of seconds).
- `boto3` client init is behind a lock to prevent two enqueues
  racing to construct it.
- `s3` `Config` has `connect_timeout=60`, `read_timeout=300` so a
  stuck TCP connection eventually fails-and-retries.
- `_StreamParser.feed()` truncates buffer from front preserving
  any in-progress header so a giant burst can't OOM.
- `gpio_controller.set_upload_complete()` checks
  `_session.is_running()` before transitioning to IDLE so a buzzer
  beep mid-session can't leave both LEDs off.
- Dead code path in segment loop (`time.sleep(5); seg_idx += 1;
  continue` after unconditional `break`) removed and replaced with
  a proper retry-step that honors `self._stop.wait()`.

## SDK change scope (auditable)

| File | Type | Change |
|---|---|---|
| `OrbbecSDK_v2.7.6_…/examples/src/2.stream.color_jpeg_stdout/ob_color_jpeg_stream.cpp` | NEW | Standalone Color→stdout MJPEG streamer. Pure addition. |
| `OrbbecSDK_v2.7.6_…/examples/src/2.stream.color_jpeg_stdout/CMakeLists.txt` | NEW | Builds the helper. Optional OpenCV. |
| `OrbbecSDK_v2.7.6_…/examples/CMakeLists.txt` | INSERT | One line: `add_subdirectory(2.stream.color_jpeg_stdout)`. Backed up before edit. |
| `OrbbecSDK_v2.7.6_…/examples/src/2.device.record.nogui_mcap/ob_device_record_mcap_nogui.cpp` | ADDITIVE | Pass-2 color-profile fallback. Byte-identical to v1 when v1 profile present. Depth path untouched. |

No SDK headers, no SDK libraries, no other examples are modified.

## Risk assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| Helper binary fails to build on a site | Low | Install treats helper as optional; recorder still installs. FOV stays disabled. |
| Color profile fallback picks wrong resolution | Very low | Only triggers when `1280x800@30` MJPG isn't present. Logs warning with chosen w×h. |
| Graceful shutdown takes too long, systemd SIGKILLs | Low | `TimeoutStopSec=60`. Recorder drain bounded at 15 s; uploader at 30 s. |
| FOV check rejects valid frames during AE settle | Low | `ExposureSettler` forces settle after 6 s. Counters in `state.fov` let the platform render a sensible UI. |
| Platform-team integration regression | Low | API documented in `API_REFERENCE.md`; v1 endpoints they used are intact; only the unused MJPEG endpoint removed. |

## Removed from v1

- `GET /fov-stream` — MJPEG preview endpoint. Returned JSON, was
  unrenderable, never useful. Replaced by counters in
  `state.fov`.
- `GET /frame-check-img` — referenced in v1 dashboard JS, never
  implemented in v1 server. Replaced by text-only
  `state.frame_check`.

## Files in this delivery

```
v2_delivery/
├── CHANGES.md                            # this file
├── project/                              # → ~/Egocentric_Data_capture_Hardware_mono16
│   ├── README.md                         # overall architecture
│   ├── API_REFERENCE.md                  # platform-team contract
│   ├── capture_daemon.py
│   ├── capture-daemon.service
│   ├── polling_agent.py / .service       # untouched
│   ├── setup-services.sh                 # untouched
│   └── capture/
│       ├── config.py
│       ├── gpio_controller.py
│       ├── cameras/{orbbec,fov_check,kreo}.py
│       ├── pipeline/{session_v2,uploader,postprocess}.py
│       └── ui/{server.py,index.html}
└── install_kit_v2/                       # run on the Pi first
    ├── install.sh
    ├── ob_device_record_mcap_nogui.cpp
    ├── CMakeLists.txt
    ├── README.md
    └── sdk_addon/
        └── 2.stream.color_jpeg_stdout/
            ├── ob_color_jpeg_stream.cpp
            └── CMakeLists.txt
```
