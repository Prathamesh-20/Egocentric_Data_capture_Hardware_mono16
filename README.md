# Egocentric Data Capture V2

Sequential session pipeline for egocentric robotics data collection.

## Architecture

```
30-min Session
├── Initial FOV Check (5s, MJPEG stream to dashboard)
│   └── YOLOv8n-pose primary → HSV skin fallback
├── Segment 1 (1 min)
│   ├── Orbbec bag recording
│   ├── Kreo 1 + 2 MP4 recording
│   ├── Timestamps CSV
│   └── → S3 upload queue
├── Wrist Check (single frame)
├── Segment 2 (1 min)
│   └── ...
├── Wrist Check
├── ...
└── Segment 30
```

## Key Changes from V1

| Feature | V1 | V2 |
|---------|----|----|
| Session model | Single 60s recording | 30-min session → sequential 1-min segments |
| Wrist detection | HSV skin only | YOLOv8n-pose (primary) + HSV (fallback) |
| Inter-segment checks | None | Single-frame wrist check between segments |
| Upload | Manual / none | Auto S3 upload queue with retry |
| MCAP conversion | Always on | Toggle from UI |
| Dashboard | Basic | Modern with timeline, upload stats, history |
| Settings | Hardcoded | Configurable from UI |
| Operator tracking | None | Operator ID + activity label per session |
| Alerts | None | Audio beep + browser notification on wrist fail |

## Setup

### Prerequisites

```bash
# On Raspberry Pi 5
pip install fastapi uvicorn opencv-python-headless numpy boto3 --break-system-packages

# YOLOv8n-pose (optional — falls back to HSV if unavailable)
pip install ultralytics --break-system-packages
mkdir -p ~/models
# Download yolov8n-pose.pt to ~/models/

# MCAP support (optional — only needed if MCAP toggle is used)
pip install rosbags mcap --break-system-packages
```

### AWS S3

Configure credentials via `~/.aws/credentials` or environment variables:
```
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=...
```

### Run

```bash
# Direct
python3 capture_daemon.py

# As systemd service
sudo cp capture-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable capture-daemon
sudo systemctl start capture-daemon
```

Dashboard: `http://<pi-ip>:8080`

## Configuration

Edit `capture/config.py` for hardware paths, or use the Settings gear icon in the dashboard for runtime parameters (segment/session duration, MCAP toggle).

## File Structure

```
egocentric-data-capture-v2/
├── capture_daemon.py              # Entry point
├── capture-daemon.service         # systemd unit
├── capture/
│   ├── config.py                  # Central config
│   ├── cameras/
│   │   ├── fov_check.py           # YOLO + HSV wrist detection
│   │   ├── orbbec.py              # Orbbec bag recorder (PTY)
│   │   └── kreo.py                # Kreo USB camera recorder
│   ├── pipeline/
│   │   ├── session_v2.py          # Session orchestrator
│   │   ├── uploader.py            # S3 upload queue with retry
│   │   └── postprocess.py         # bag→MP4, bag→MCAP conversion
│   └── ui/
│       ├── server.py              # FastAPI backend
│       └── index.html             # Dashboard
```

## Output Structure

```
/mnt/ssd/recordings/
└── session_20260317_143000/
    ├── manifest_20260317_143000.json
    ├── 20260317_143000_seg000_orbbec.bag
    ├── 20260317_143000_seg000_orbbec.mcap  (if MCAP enabled)
    ├── 20260317_143000_seg000_kreo1.mp4
    ├── 20260317_143000_seg000_kreo2.mp4
    ├── 20260317_143000_seg000_timestamps.csv
    ├── 20260317_143000_seg001_orbbec.bag
    └── ...
```
# Egocentric_Data_capture_Hardware_mono16
# Egocentric_Data_capture_Hardware_mono16
