# orbbec_stream — FOV checker source

These files build the `orbbec_stream` binary used by `capture/cameras/fov_check.py`
for per-segment wrist-in-frame detection.

This is a separate one-time build, in addition to the `install_kit_v1` MCAP
recorder build.

## Files

```
fov_stream_kit/
├── orbbec_stream.cpp           # source — writes MJPG frames to stdout in the
│                                  protocol fov_check.py expects
├── CMakeLists.fov_stream.txt   # build config (renamed at install time)
├── install_fov_stream.sh       # idempotent build + smoke test
└── README.md                   # this file
```

## Install on a Pi

Run **after** `install_kit_v1/install.sh` has succeeded (we reuse the apt deps
and SDK setup it left behind):

```bash
cd ~/Egocentric_Data_capture_Hardware_mono16/fov_stream_kit
chmod +x install_fov_stream.sh
./install_fov_stream.sh
```

Expected end state:

```
============================================================
PASS — egoportable3
binary:  /home/<user>/OrbbecSDK_v2.7.6_.../bin/orbbec_stream
symlink: /mnt/ssd/OrbbecSDK_Pi5/orbbec_stream
log:     /tmp/install_orbbec_stream_<ts>.log
============================================================
```

After this, restart the daemon:

```bash
sudo systemctl restart capture-daemon
journalctl -u capture-daemon -f
```

## What the install script does

1. Drops `orbbec_stream.cpp` + `CMakeLists.txt` into
   `<SDK>/examples/src/orbbec_stream/`.
2. Inserts `add_subdirectory(orbbec_stream)` into the parent
   `examples/src/CMakeLists.txt` (idempotent — checks first).
3. Runs `cmake --build <SDK>/build --target orbbec_stream`.
4. Copies the binary into `<SDK>/bin/orbbec_stream`.
5. Creates symlinks to satisfy the legacy paths hard-coded in
   `capture/config.py`:
   - `/mnt/ssd/OrbbecSDK_Pi5/orbbec_stream` → built binary
   - `/mnt/ssd/OrbbecSDK_Pi5/lib`           → SDK lib dir
   - `/opt/OrbbecSDK/lib`                   → SDK lib dir
6. Smoke-tests the binary for 3 seconds and counts `FRAME COLOR` headers.
   Pass threshold: ≥ 5 frames in 3 seconds (well below the expected ~90).

## The protocol it writes

For each color frame, the binary writes:

```
FRAME COLOR <w> <h> <fps> <ts_ms> MJPG <size>\n
<size bytes of MJPG/JPEG>
```

`fov_check.py` parses `parts[6]` (the size field) and decodes the body via
`cv2.imdecode`. The format token (`MJPG`) is informational only — the script
ignores it and always treats the body as JPEG.

## Smoke failure modes

| Failure                                          | Likely cause |
| ------------------------------------------------ | ------------ |
| `Failed to open device`                          | Camera unplugged or another process holds it (`pkill -f orbbec`) |
| `0 FRAME COLOR headers`                          | Camera enumerated but not delivering — udev rules, permissions, or USB power |
| Build fails on `enableVideoStream`               | SDK version mismatch — confirm `OrbbecSDK_v2.7.6` |
| `add_subdirectory(orbbec_stream)` already there  | Re-running the script — this is fine, the SKIP path handles it |
