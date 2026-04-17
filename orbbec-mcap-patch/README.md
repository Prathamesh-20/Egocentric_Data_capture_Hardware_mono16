# OrbbecSDK v2 MCAP Support

Native MCAP recording and playback support for [OrbbecSDK v2](https://github.com/orbbec/OrbbecSDK_v2). Implements `McapWriter` and `McapReader` against the SDK's existing `IWriter`/`IReader` interfaces — a drop-in addition alongside the existing rosbag writer.

## Prerequisites

- CMake 3.10+
- C++ compiler with C++17 support (clang, gcc 7+)
- OpenCV (optional, for GUI examples)

**macOS:**
```bash
brew install cmake opencv
```

**Ubuntu:**
```bash
sudo apt install cmake build-essential libopencv-dev
```

## Build from scratch

```bash
# 1. Clone the SDK and this patch
git clone https://github.com/orbbec/OrbbecSDK_v2.git
git clone https://github.com/vineeth-encord/orbbec-sdk-mcap-support.git

# 2. Apply the patch
cd orbbec-sdk-mcap-support
./apply.sh ../OrbbecSDK_v2

# 3. Build
cd ../OrbbecSDK_v2
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
cmake --build .
```

The patch is idempotent — safe to re-run `apply.sh` if you pull updates.

## Recording

After building, use the GUI or CLI recorder. Just give it a `.mcap` filename:

```bash
cd OrbbecSDK_v2/build/macOS/bin   # or linux equivalent

# GUI recorder (requires OpenCV)
./ob_device_record
# When prompted, enter: recording.mcap

# CLI recorder (no GUI)
./ob_device_record_nogui
# When prompted, enter: recording.mcap
```

Press **S** to pause/resume (GUI only), **ESC** or **q** to stop and save.

Using a `.bag` extension records in the original rosbag format — fully backwards compatible.

## Programmatic usage

```cpp
#include <libobsensor/ObSensor.hpp>

// Record to MCAP — just use .mcap extension
auto recorder = std::make_shared<ob::RecordDevice>(device, "output.mcap");

// Record to bag — same as before
auto recorder = std::make_shared<ob::RecordDevice>(device, "output.bag");
```

## What the patch changes

| File | Change |
|------|--------|
| `CMakeLists.txt` | C++ standard upgraded from 11 to 17 |
| `3rdparty/mcap/` | Header-only MCAP library + CMake target (new) |
| `src/media/ros/McapWriter.{hpp,cpp}` | MCAP writer implementation (new) |
| `src/media/ros/McapReader.{hpp,cpp}` | MCAP reader implementation (new) |
| `src/media/CMakeLists.txt` | Links `mcap::mcap` |
| `src/media/record/RecordDevice.{hpp,cpp}` | Format selection by `.mcap` extension |
| `src/media/playback/PlaybackDevicePort.{hpp,cpp}` | Reader selection by extension |

## Viewing MCAP files

- **[Foxglove Studio](https://foxglove.dev)** — drag and drop `.mcap` files for full visualization
- **CLI:** `pip install mcap-cli && mcap info recording.mcap`

## Why?

The OrbbecSDK records to a proprietary ROS1 bag format with custom message types (extended `sensor_msgs/Image`, `OBDeviceInfo`, `OBStreamProfileInfo`, etc.). Standard tools like `rosbags-convert` and `mcap convert` fail on these files. MCAP is an open, efficient container format with broad tooling support.
