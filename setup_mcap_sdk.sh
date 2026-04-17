#!/bin/bash
# One-command MCAP SDK setup for new Pi devices
# Usage: bash setup_mcap_sdk.sh
set -e
echo "=== Setting up MCAP-patched Orbbec SDK ==="

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PATCH_DIR="$REPO_DIR/orbbec-mcap-patch"
SDK_DIR="$HOME/OrbbecSDK_v2_src"

# 1. Install dependencies
echo "[1/5] Installing dependencies..."
sudo apt update -q
sudo apt install -y git cmake build-essential libopencv-dev python3 liblz4-dev

# 2. Clone SDK source
echo "[2/5] Cloning OrbbecSDK v2 source..."
if [ -d "$SDK_DIR" ]; then
    echo "  OrbbecSDK_v2_src already exists, skipping clone"
else
    git clone --depth 1 https://github.com/orbbec/OrbbecSDK_v2.git "$SDK_DIR"
fi

# 3. Apply MCAP patch
echo "[3/5] Applying MCAP patch..."
cp -r "$PATCH_DIR/src/media/ros/McapWriter.hpp" "$SDK_DIR/src/media/ros/"
cp -r "$PATCH_DIR/src/media/ros/McapWriter.cpp" "$SDK_DIR/src/media/ros/"
cp -r "$PATCH_DIR/src/media/ros/McapReader.hpp" "$SDK_DIR/src/media/ros/"
cp -r "$PATCH_DIR/src/media/ros/McapReader.cpp" "$SDK_DIR/src/media/ros/"
cp -r "$PATCH_DIR/3rdparty/mcap" "$SDK_DIR/3rdparty/"

# Apply CMakeLists patches
cd "$PATCH_DIR"
if [ -f apply.sh ]; then
    chmod +x apply.sh
    ./apply.sh "$SDK_DIR"
fi

# 4. Apply recorder fixes
echo "[4/5] Applying recorder fixes..."
python3 << 'PYEOF'
import os
USER = os.environ['USER']
file = f"/home/{USER}/OrbbecSDK_v2_src/examples/2.device.record.nogui/device_record_nogui.cpp"

with open(file, 'r') as f:
    content = f.read()

# Fix unused pid/vid
if '(void)pid' not in content:
    content = content.replace(
        'auto pid     = devInfo->getPid();\n    auto vid     = devInfo->getVid();',
        'auto pid     = devInfo->getPid();\n    auto vid     = devInfo->getVid();\n    (void)pid;\n    (void)vid;'
    )

# Fix sensor loop - Y16 depth + color only
old = '''    for(uint32_t i = 0; i < count; i++) {
        auto sensor      = sensorList->getSensor(i);
        auto sensorType  = sensor->getType();
        auto profileList = sensor->getStreamProfileList();  // Get profileList to create Sensor object in advance
        if(ob_smpl::isAstraMiniDevice(vid, pid)) {
            if(sensorType == OB_SENSOR_IR) {
                continue;
            }
        }
        config->enableStream(sensorType);
    }'''

new = '''    for(uint32_t i = 0; i < count; i++) {
        auto sensor      = sensorList->getSensor(i);
        auto sensorType  = sensor->getType();
        auto profileList = sensor->getStreamProfileList();
        if(ob_smpl::isAstraMiniDevice(vid, pid)) {
            if(sensorType == OB_SENSOR_IR) { continue; }
        }
        if(sensorType == OB_SENSOR_DEPTH) {
            std::shared_ptr<ob::StreamProfile> y16Profile = nullptr;
            for(uint32_t j = 0; j < profileList->getCount(); j++) {
                auto profile = profileList->getProfile(j)->as<ob::VideoStreamProfile>();
                if(profile->getFormat() == OB_FORMAT_Y16) {
                    y16Profile = profileList->getProfile(j);
                    std::cout << "Found Y16 depth profile: "
                              << profile->getWidth() << "x" << profile->getHeight()
                              << " @ " << profile->getFps() << "fps" << std::endl;
                    break;
                }
            }
            if(y16Profile) {
                config->enableStream(y16Profile);
                std::cout << "Enabled stream: Depth (Y16)" << std::endl;
            } else {
                config->enableStream(sensorType);
                std::cout << "Enabled stream: Depth (default)" << std::endl;
            }
        }
        else if(sensorType == OB_SENSOR_COLOR) {
            config->enableStream(sensorType);
            std::cout << "Enabled stream: Color (default)" << std::endl;
        }
    }'''

if old in content:
    content = content.replace(old, new)

# Auto .mcap extension
if 'Auto-added .mcap' not in content:
    old2 = '    std::string filePath;\n    std::getline(std::cin, filePath);'
    new2 = '''    std::string filePath;
    std::getline(std::cin, filePath);
    if(filePath.size() < 5 || filePath.substr(filePath.size()-5) != ".mcap") {
        if(filePath.size() < 4 || filePath.substr(filePath.size()-4) != ".bag") {
            filePath += ".mcap";
            std::cout << "Auto-added .mcap extension: " << filePath << std::endl;
        }
    }'''
    if old2 in content:
        content = content.replace(old2, new2)

with open(file, 'w') as f:
    f.write(content)
print("  Recorder fixes applied!")
PYEOF

# 5. Build
echo "[5/5] Building SDK (15-20 minutes)..."
cd "$SDK_DIR"
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
cmake --build . -- -j4

echo ""
echo "========================================="
echo "=== DONE! SDK setup complete. ==="
echo "========================================="
echo "Binary: $SDK_DIR/build/linux_arm64/bin/ob_device_record_nogui"
echo ""
echo "Test: cd $SDK_DIR/build/linux_arm64/bin && ./ob_device_record_nogui"
