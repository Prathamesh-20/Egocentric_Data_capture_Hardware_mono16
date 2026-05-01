# Branch merge notes — `mcap-pipeline` ⨉ `fov-per-segment`

This branch merges two upstream branches of the same repo:

- **`mcap-pipeline`** (base) — segments are recorded directly to `.mcap` by the
  C++ recorder `ob_device_record_mcap_nogui`. No `.bag` intermediate, no
  rosbag postprocessing.
- **`fov-per-segment`** (overlay) — runs an FOV (wrist-detection) check
  before *every* segment, including the first. After 3 consecutive FOV
  failures the session aborts with red LEDs and a buzzer alert.

## What changed (vs `mcap-pipeline` base)

| File | Change |
|---|---|
| `capture/config.py` | Enabled `FOV_CHECK_ENABLED=True`; added `FOV_BETWEEN_SEGMENTS=True` and the four tuning constants for the per-segment check (duration, min-detect-frames, max-consecutive-fails, retry wait). |
| `capture/pipeline/session_v2.py` | Added `_run_fov_check_with_retry(seg_idx)` method. Wired it into `_run` at the top of each segment iteration, between the disk-space/duration checks and the `set_recording` GPIO call. On final FOV failure the session is aborted with `set_error` GPIO state. |
| `capture/gpio_controller.py` | Added `set_fov_checking()` (LEDs off — operator watches dashboard) and `set_fov_failed()` (red solid + 1 beep). |
| `capture/ui/server.py` | Added `_update_fov_frame()` (encodes BGR frames to JPEG) and `_mjpeg_generator()` (streams them over HTTP), exposed on `GET /fov-stream`. Wired `on_frame_check=_update_fov_frame` into the `SessionV2` constructor. The legacy `POST /fov-check` endpoint now just acknowledges that FOV runs automatically per-segment. |
| `capture/ui/index.html` | Added `fov_between_segments` status to the pill map. Replaced the old "FOV stream visible only when `status === fov_checking`" logic with one that activates the live MJPEG view during all three FOV-related states (`fov_checking`, `fov_between_segments`, `fov_failed`). |
| `requirements.txt` | Imported from FOV branch (was missing on MCAP branch). Updated stale comment about MCAP libs being for "the MCAP toggle" — they're now just an optional Python-side reader, since recording is C++. |

## What did NOT change

- `capture/cameras/orbbec.py` — kept the MCAP version (uses `output_path` not `bag_path`).
- `capture/cameras/fov_check.py` — identical in both branches.
- `capture/pipeline/postprocess.py` — kept the MCAP version (correctly marked as legacy/deprecated under the direct-MCAP pipeline).
- `capture/pipeline/uploader.py` — kept the MCAP version (`.mcap`/`.csv` upload filter).
- `polling_agent.py` — kept the MCAP version (`.mcap` filename strings throughout).

## Tuning quick-ref (`capture/config.py`)

```
FOV_BETWEEN_SEGMENTS_SECS    = 4    # how long each pre-segment FOV check runs
FOV_BETWEEN_MIN_DETECT_FRAMES= 6    # min frames with hands to pass
FOV_MAX_CONSEC_FAILURES      = 3    # consecutive fails before session abort
FOV_RETRY_WAIT_SECS          = 2    # gap between retries (operator repositions)
```

To temporarily disable per-segment FOV without touching code paths,
set `FOV_BETWEEN_SEGMENTS = False` in config.py. Initial-only FOV is
still controlled by the existing `FOV_CHECK_ENABLED` flag.

## Post-merge fix — `set_upload_complete` GPIO behaviour

The merged `set_upload_complete` previously ran the buzzer in `"continuous"` mode
for 5 seconds (≈12 fast trills, toggled by the LED loop's blink clock) and left
the red LED blinking from the prior `set_uploading` state. After 5 s a daemon
thread called `set_idle` to flip green solid.

This was changed to match the intended operator-facing behaviour:

- **5 distinct beeps** (0.2 s on / 0.2 s off) instead of a continuous trill.
- **Green LED solid + red OFF immediately** — no carry-over of the red blink.
- No deferred `set_idle` thread; the LEDs are already in the idle state.

Net effect on the dashboard / web app: the `complete` status payload is
unchanged, only the on-device feedback differs.
