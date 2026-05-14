# Orbbec MCAP Recorder — Install Kit (v2)

Builds and installs the Orbbec headless MCAP recorder used by the egocentric
capture pipeline on Raspberry Pi 5. **v2 adds an optional additive helper
binary (`ob_color_jpeg_stream`) for the FOV pre-check.** All changes are
strictly additive and backward-compatible with v1.

## What this kit installs

```
~/OrbbecSDK_v2.7.6_…linux_arm64/
├── bin/
│   ├── ob_device_record_mcap_nogui      # main recorder (existed in v1)
│   └── ob_color_jpeg_stream             # NEW in v2 — optional, for FOV check
├── examples/src/
│   ├── 2.device.record.nogui_mcap/      # source dropped in by v1
│   └── 2.stream.color_jpeg_stdout/      # NEW in v2 — source for the helper
└── examples/CMakeLists.txt              # backed up + extended with one
                                          # add_subdirectory line per addon
```

The kit performs all changes idempotently: re-running is safe and produces
no diffs once everything is in place.

## What's new vs v1

The recorder source (`ob_device_record_mcap_nogui.cpp`) gets one strictly
additive change: when the exact `Color MJPG 1280x800@30` profile is not
exposed by the firmware, the recorder now falls back to the first
`MJPG@30` profile available (logging a warning) instead of refusing to
start. Older firmware that exposed `Color MJPG 1280x720@30` will now
record successfully. **When the original profile is present, behavior is
byte-identical to v1.** The depth path is untouched.

The new `ob_color_jpeg_stream` is a small Orbbec SDK example that emits
MJPEG color frames on stdout in this protocol:

```
FRAME COLOR <ts_us> <fid> <w> <h> MJPG <data_size>\n
<data_size bytes of JPEG>
```

The egocentric capture pipeline's `fov_check.py` consumes this protocol
to perform a real wrist-presence check before recording. The recorder
itself does not depend on this binary in any way; if the helper fails to
build, the install still succeeds and the recorder remains fully
functional.

## Layout in this kit

```
install_kit_v2/
├── install.sh
├── ob_device_record_mcap_nogui.cpp      # patched (additive color fallback)
├── CMakeLists.txt                       # for the recorder
├── README.md                            # this file
└── sdk_addon/
    └── 2.stream.color_jpeg_stdout/      # NEW additive SDK example
        ├── ob_color_jpeg_stream.cpp
        └── CMakeLists.txt
```

## Usage

Run on the Pi as the same user that will eventually run the capture
daemon:

```bash
cd install_kit_v2
./install.sh
```

The script:

1. Verifies dependencies (`libzstd-dev`, `nlohmann-json3-dev`, `cmake`,
   etc.) and disk space.
2. Confirms the Orbbec device is enumerated over USB (`lsusb` for vid
   `2bc5`).
3. Drops the recorder source into `$SDK_ROOT/examples/src/2.device.record.nogui_mcap/`
   and adds it to the parent `CMakeLists.txt`.
4. **(v2)** Drops the helper source into `$SDK_ROOT/examples/src/2.stream.color_jpeg_stdout/`
   and adds a second `add_subdirectory(...)` line. If `sdk_addon/` is
   missing in the kit (partial unpack), this step is skipped cleanly and
   the recorder still installs.
5. Builds both targets via `cmake`, installs them to
   `$SDK_ROOT/bin/`.
6. Runs a smoke test recording a 5-second MCAP and verifies its size.

A failure of step 4 or 5 for the helper does NOT fail the overall
install — the recorder is the critical path.

## Enabling the FOV check after install

The Python pipeline ships with FOV validation **disabled by default** so
that v2 deployments are behaviorally identical to v1 until the operator
flips a flag. After running this install kit:

```python
# capture/config.py
FOV_CHECK_ENABLED            = True   # was False
WRIST_CHECK_BETWEEN_SEGMENTS = True   # optional inter-segment check
```

Restart `capture-daemon.service` for the change to take effect.

The dashboard's "FOV CHECK" button will now run for `FOV_CHECK_SECS`
(default 5 s), display a live MJPEG preview of detected wrists, and
report `PASS` or `FAIL` depending on whether at least
`FOV_MIN_DETECTION_FRAMES` frames showed both wrists. Detection is
YOLOv8n-pose if the model is present at `~/models/yolov8n-pose.pt`,
otherwise it falls back to HSV skin-blob heuristics.

## Backing out

The install script writes `.bak.<timestamp>.streamhelper` next to the
parent `examples/CMakeLists.txt` before inserting the helper line. To
back the helper out completely:

```bash
# 1. Restore the parent CMakeLists.
cp $SDK_ROOT/examples/CMakeLists.txt.bak.<TIMESTAMP>.streamhelper \
   $SDK_ROOT/examples/CMakeLists.txt
# 2. Remove the source dir and binary.
rm -rf $SDK_ROOT/examples/src/2.stream.color_jpeg_stdout
rm -f  $SDK_ROOT/bin/ob_color_jpeg_stream
# 3. Restart the daemon (and set FOV_CHECK_ENABLED=False if it was True).
```

The recorder is unaffected.

## SDK changes are minimal and audited

| Change | Type | Files | Behavioural impact when v1 profile present |
|---|---|---|---|
| Color profile fallback in recorder | Additive (Pass-2 branch) | `ob_device_record_mcap_nogui.cpp` | None — v1 profile is still chosen first |
| New helper SDK example | Pure addition | `sdk_addon/2.stream.color_jpeg_stdout/` | None — separate target |
| `add_subdirectory(2.stream.color_jpeg_stdout)` | Insertion in parent CMakeLists | `$SDK_ROOT/examples/CMakeLists.txt` | None — adds a sibling target |

Nothing else in the SDK source tree, headers, libraries, or any other
example is modified.
