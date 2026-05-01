# MCAP recorder install kit

`install.sh` deploys the `ob_device_record_mcap_nogui` recorder onto a Pi 5
that already has the OrbbecSDK extracted and the Gemini 2L plugged in.

## Contents

```
install_kit_v1/
├── install.sh                          # the deploy script
├── ob_device_record_mcap_nogui.cpp     # known-good source (do not edit)
├── CMakeLists.txt                      # known-good build config (do not edit)
└── README.md                           # this file
```

## Pre-conditions on the target Pi

- Pi 5, arm64, Debian Trixie
- Logged in as the target user (e.g. `autonexego15`), NOT root
- `sudo` works for that user
- OrbbecSDK extracted at:
  `~/OrbbecSDK_v2.7.6_202602021228_d712cda_linux_arm64/`
  (override with `SDK_ROOT=...` if elsewhere)
- Orbbec Gemini 2L plugged into USB (or set `SKIP_SMOKE=1`)
- Network access to `github.com` (only on first install — the MCAP headers
  are sparse-cloned once and re-used)

## Quick start (one Pi)

```bash
# Copy the kit to the Pi and unpack:
scp install_kit_v1.tar.gz autonexego15@<pi>:~/
ssh autonexego15@<pi>
tar xzf install_kit_v1.tar.gz
cd install_kit_v1
./install.sh
```

Expected end state:

```
============================================================
PASS — egoportable15
binary: /home/autonexego15/OrbbecSDK_v2.7.6_.../bin/ob_device_record_mcap_nogui
smoke:  /tmp/mcap_smoke.XXXXX/smoke_20260427_141530.mcap
log:    /tmp/install_mcap_20260427_141530.log
============================================================
```

Exit code: `0` on PASS, non-zero on FAIL.

## What it does

1. **Pre-flight** — fails fast with a clear message if SDK is missing,
   no Orbbec camera on USB, no network for the first-time MCAP clone, or
   under 2 GB free in `$HOME`.
2. **Apt deps** — installs only what's missing from
   `libzstd-dev nlohmann-json3-dev git cmake build-essential`.
3. **udev rules** — copies `99-obsensor-libusb.rules` into `/etc/udev/rules.d/`
   only if the file is missing or differs (`cmp -s`); reloads udev.
4. **Recording dir** — creates `/mnt/ssd/recordings` and chowns it to the
   running user, only if needed.
5. **MCAP headers** — sparse-clones `foxglove/mcap` into
   `<SDK>/examples/third_party/.mcap_repo/` and symlinks the include dir
   to `<SDK>/examples/third_party/mcap/` so that `<mcap/writer.hpp>` resolves
   via the CMakeLists's `-I ../../third_party` path.
6. **Source drop-in** — copies the kit's `.cpp` and `CMakeLists.txt` into
   `<SDK>/examples/src/2.device.record.nogui_mcap/`, but only if changed
   (so reruns don't trigger needless rebuilds). Verifies gotcha-a and
   gotcha-b on the dropped files before continuing.
7. **Parent CMakeLists** — idempotently inserts
   `add_subdirectory(2.device.record.nogui_mcap)` after the existing
   `2.device.record.nogui` line, preserving its indentation, with a
   `.bak.<timestamp>` backup.
8. **Build** — runs `cmake -S <SDK>/examples -B <SDK>/build -DCMAKE_BUILD_TYPE=Release`
   and builds only the `ob_device_record_mcap_nogui` target. Installs the
   binary into `<SDK>/bin/`.
9. **Smoke test** — runs the binary inside a `script(1)` pty for ~10 seconds
   with auto-stop via ESC, then verifies:
   - `Enabled stream: Color MJPG 1280x800@30`
   - `Enabled stream: Depth Y16 1280x800@30`
   - Final `Summary:` line with `recv == written` for both color and depth
   - All drop counters (`dropsQ`, `dropsW`, `dropsSize`) equal 0
   - The smoke `.mcap` file is present and ≥ 100 KB.

## Idempotency

The script is safe to re-run. Each step gates on a real precondition check
(`dpkg-query`, `cmp -s`, `grep -qE`, `test -f`, etc.) and prints `SKIP` for
work that's already done. A second run on a fully-installed Pi only does
the smoke test.

## Why cmake-direct, not `build_examples.sh`

`build_examples.sh` shipped with the SDK wipes its own build directory at
the end. That's fine for one-shot installs but breaks idempotent reruns
(every rerun is a full rebuild from scratch). Calling `cmake` directly
against `<SDK>/examples` keeps the `build/` cache intact, so reruns are
incremental and finish in seconds.

## Environment overrides

| Variable         | Default                                                    | Purpose |
| ---------------- | ---------------------------------------------------------- | ------- |
| `SDK_ROOT`       | `~/OrbbecSDK_v2.7.6_202602021228_d712cda_linux_arm64`      | Override SDK path |
| `KIT_DIR`        | dir of `install.sh`                                        | Override location of the kit's `.cpp`/`CMakeLists.txt` |
| `SKIP_SMOKE`     | `0`                                                        | Set to `1` to skip the camera smoke test (e.g. on a bench Pi without a camera attached). The script still PASSes on a successful build; the final status line says `(build only — smoke skipped)`. |
| `SMOKE_DURATION` | `10`                                                       | Seconds to record during the smoke test |

## Logs

Every run writes a complete log to `/tmp/install_mcap_<timestamp>.log`.
On failure, the smoke output (full binary stdout/stderr) is also kept at
`/tmp/mcap_smoke.XXXXXX/out.log` for inspection.

## Common failure modes and what they mean

| Failure                                              | Likely cause |
| ---------------------------------------------------- | ------------ |
| `no Orbbec camera detected (lsusb VID 2bc5)`         | Camera unplugged, bad USB cable, or not enumerated yet (try `dmesg | tail`) |
| `MCAP headers not present and github.com is not resolvable` | Pi has no internet on first install |
| `<file>.cpp is missing MCAP_IMPLEMENTATION ... defines` | Wrong `.cpp` was put in the kit dir — re-extract the tarball |
| `CMakeLists.txt include path is not '../../third_party'` | Wrong `CMakeLists.txt` was put in the kit dir — re-extract |
| `parent CMakeLists has no add_subdirectory(2.device.record.nogui)` | This Pi's SDK is a different version or has been hand-edited |
| `no 'Summary: ...' line — binary did not shut down cleanly` | Binary crashed before the ESC stop — check the smoke log |
| `color recv=0` / `depth recv=0`                      | Camera enumerated but not delivering frames — udev rules not active yet (a reboot or `udevadm trigger` is sometimes needed) |
| `non-zero drop counter`                              | Frame drops during the 10s smoke — check disk speed and Pipeline frame sync; this is what gotcha (e) was about |

## Multi-Pi rollout

See `rollout.sh` for a parallel SSH rollout snippet over a list of hostnames.
