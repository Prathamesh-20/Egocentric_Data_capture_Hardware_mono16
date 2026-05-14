# Egocentric Capture V2 — Pi 5 Setup

One-command installer for the Egocentric Capture V2 system. Designed for
batch deployment across 30+ Raspberry Pi 5 devices.

## What this installs

- Orbbec Gemini 2L SDK v2.7.6 with custom recording binaries
- Egocentric capture daemon (recording + S3 upload + FOV check)
- YOLOv8 hand-detection model for FOV verification
- Systemd service so the daemon auto-starts on boot

## Before you begin

On the Pi you're installing on, make sure:

1. It's a **Raspberry Pi 5** running **Debian 13 (trixie)** with **Python 3.13**.
2. The **Orbbec Gemini 2L camera is plugged in** (the installer runs a
   camera smoke test by default — pass `SKIP_SMOKE=1` if it's not connected).
3. You have **internet** (the installer downloads pip packages and the
   YOLO model).
4. You've copied your **AWS credentials** to the Pi:
   ```bash
   # from your laptop, replace <pi-host> with the target hostname
   scp ~/.aws/credentials <user>@<pi-host>:~/.aws/credentials
   scp ~/.aws/config      <user>@<pi-host>:~/.aws/config
   ```

## Install

SSH into the Pi as the target user (e.g. `autonexego8`) and run:

```bash
git clone https://github.com/<your-org>/egocentric-capture.git
cd egocentric-capture
bash setup.sh
```

The script will:

1. Check the Pi is the right shape (Pi 5, aarch64, Debian, Python, AWS creds present)
2. Install apt packages (build tools, OpenCV, libcamera, etc.)
3. Extract the Orbbec SDK
4. Copy `v2_delivery/` and the project code to your home directory
5. Build the custom Orbbec recording binaries (this is where it takes the longest)
6. Run a 10-second camera smoke test
7. Add your user to the `video` and `plugdev` groups
8. Install Python packages from `requirements.txt`
9. Download the YOLO hand-detection model (~6 MB from Hugging Face)
10. Install and enable the systemd service
11. Prompt for reboot

**Total time: 15–25 minutes**, mostly waiting on apt + pip + the SDK build.

## After install

Reboot once. The capture daemon then starts automatically on every boot.

Verify it's running:
```bash
sudo systemctl status capture-daemon
sudo journalctl -u capture-daemon -f
```

Open the web UI at `http://<pi-hostname>.local:8080`.

## Troubleshooting

### `setup.sh` failed partway through

It's safe to fix the issue and re-run. Every step is idempotent — it
detects what's already done and skips it.

### "AWS credentials missing"

You haven't copied `~/.aws/credentials` to the Pi. See "Before you begin".

If you genuinely don't want AWS uploads on this Pi (e.g. for testing):
```bash
SKIP_AWS_CHECK=1 bash setup.sh
```

### "no Orbbec camera detected"

The camera isn't plugged in, or USB enumeration failed. Either:

- Plug the camera in and re-run, or
- Skip the smoke test:
  ```bash
  SKIP_SMOKE=1 bash setup.sh
  ```
  ⚠️ Skipping the smoke test means you won't know if the camera works
  until the daemon tries to use it.

### "FAIL — smoke test"

The camera is plugged in but the recording didn't produce good output.
Check the install log at `/tmp/egocentric_setup_*.log` for the parsed
counter values — usually one of:

- `color recv=0` → camera not enumerating; replug the USB cable
- `dropsW=N` (non-zero) → SDK / SSD write issue; check `/mnt/ssd` mount

### Daemon won't start after reboot

```bash
sudo journalctl -u capture-daemon -n 200 --no-pager
```

Common causes:
- udev rules didn't apply → reboot once more
- `~/.aws/credentials` has the wrong format → check it's `[default]\naws_access_key_id=...`
- camera unplugged → plug it back in, then `sudo systemctl restart capture-daemon`

## Updating an already-installed Pi

```bash
cd egocentric-capture
git pull
bash setup.sh
```

The script will re-sync code, re-install any changed Python deps, and
restart the service. Existing recordings on `/mnt/ssd` are untouched.

## Repository layout

```
egocentric-capture/
├── setup.sh                          # the one-command installer (run this)
├── requirements.txt                   # Python deps
├── README.md                          # this file
├── Egocentric_Data_capture_Hardware_mono16/   # the project code
├── v2_delivery/                       # Orbbec install kit + sources
├── orbbec-sdk/
│   └── OrbbecSDK_v2.7.6_*.tar.gz      # the Orbbec SDK tarball (21 MB)
└── systemd/
    └── capture-daemon.service.template
```
