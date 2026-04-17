#!/bin/bash
# Apply MCAP support patch to OrbbecSDK v2
# Usage: ./apply.sh /path/to/OrbbecSDK_v2
# Works on both macOS (BSD sed) and Linux (GNU sed)

set -e

SDK_DIR="${1:?Usage: ./apply.sh /path/to/OrbbecSDK_v2}"
PATCH_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$SDK_DIR/CMakeLists.txt" ]; then
    echo "Error: $SDK_DIR does not look like OrbbecSDK_v2 (no CMakeLists.txt)"
    exit 1
fi

# Cross-platform in-place sed (macOS BSD sed vs GNU sed)
sedi() {
    if sed --version >/dev/null 2>&1; then
        # GNU sed
        sed -i "$@"
    else
        # BSD sed (macOS)
        sed -i '' "$@"
    fi
}

echo "Applying MCAP patch to: $SDK_DIR"

# --- Step 1: Copy new files ---
echo "[1/3] Copying new files..."

cp -r "$PATCH_DIR/3rdparty/mcap" "$SDK_DIR/3rdparty/mcap"
echo "  + 3rdparty/mcap/"

cp "$PATCH_DIR/src/media/ros/McapWriter.hpp" "$SDK_DIR/src/media/ros/McapWriter.hpp"
cp "$PATCH_DIR/src/media/ros/McapWriter.cpp" "$SDK_DIR/src/media/ros/McapWriter.cpp"
cp "$PATCH_DIR/src/media/ros/McapReader.hpp" "$SDK_DIR/src/media/ros/McapReader.hpp"
cp "$PATCH_DIR/src/media/ros/McapReader.cpp" "$SDK_DIR/src/media/ros/McapReader.cpp"
echo "  + src/media/ros/Mcap{Writer,Reader}.{hpp,cpp}"

# --- Step 2: Patch existing files using Python (avoids sed portability issues) ---
echo "[2/3] Patching existing files..."

python3 -c "
import sys, re

patches = [
    # (file, check_string, find_pattern, replacement)
    (
        '$SDK_DIR/CMakeLists.txt',
        'CMAKE_CXX_STANDARD 17',
        r'set\(CMAKE_CXX_STANDARD 11\)',
        r'set(CMAKE_CXX_STANDARD 17)  # Upgraded from 11 for MCAP support (std::byte, std::optional, std::variant)'
    ),
    (
        '$SDK_DIR/src/media/CMakeLists.txt',
        'mcap::mcap',
        r'(target_link_libraries\(\\\$\{OB_TARGET_MEDIA\} PUBLIC rosbag::rosbag\))',
        r'''\1

# MCAP support
add_subdirectory(\${OB_3RDPARTY_DIR}/mcap mcap)
target_link_libraries(\${OB_TARGET_MEDIA} PUBLIC mcap::mcap)'''
    ),
    (
        '$SDK_DIR/src/media/record/RecordDevice.hpp',
        'McapWriter.hpp',
        r'(#include \"ros/RosbagWriter\.hpp\")',
        r'''\1
#include \"ros/McapWriter.hpp\"'''
    ),
    (
        '$SDK_DIR/src/media/record/RecordDevice.cpp',
        'McapWriter',
        r'writer_ = std::make_shared<RosWriter>\(filePath_, isCompressionsEnabled_\);',
        r'''if(filePath_.size() >= 5 && filePath_.substr(filePath_.size() - 5) == \".mcap\") {
        writer_ = std::make_shared<McapWriter>(filePath_, isCompressionsEnabled_);
    } else {
        writer_ = std::make_shared<RosWriter>(filePath_, isCompressionsEnabled_);
    }'''
    ),
    (
        '$SDK_DIR/src/media/playback/PlaybackDevicePort.hpp',
        'McapReader.hpp',
        r'(#include \"ros/RosbagReader\.hpp\")',
        r'''\1
#include \"ros/McapReader.hpp\"'''
    ),
    (
        '$SDK_DIR/src/media/playback/PlaybackDevicePort.cpp',
        'McapReader',
        r': reader_\(std::make_shared<RosReader>\(filePath\)\),',
        r''': reader_(filePath.size() >= 5 && filePath.substr(filePath.size() - 5) == \".mcap\" ? std::static_pointer_cast<IReader>(std::make_shared<McapReader>(filePath)) : std::static_pointer_cast<IReader>(std::make_shared<RosReader>(filePath))),'''
    ),
]

for filepath, check, pattern, replacement in patches:
    with open(filepath, 'r') as f:
        content = f.read()
    if check in content:
        print(f'  ~ {filepath.split(\"OrbbecSDK_v2/\")[-1]} (already patched)')
        continue
    new_content = re.sub(pattern, replacement, content, count=1)
    if new_content == content:
        print(f'  ! {filepath.split(\"OrbbecSDK_v2/\")[-1]} (pattern not found, patch manually)')
        continue
    with open(filepath, 'w') as f:
        f.write(new_content)
    print(f'  + {filepath.split(\"OrbbecSDK_v2/\")[-1]}')
"

# --- Step 3: Done ---
echo "[3/3] Done!"
echo ""
echo "Build with:"
echo "  cd $SDK_DIR"
echo "  mkdir -p build && cd build"
echo "  cmake .. -DCMAKE_BUILD_TYPE=Release"
echo "  cmake --build ."
echo ""
echo "To record as MCAP, use a .mcap file extension:"
echo '  recorder = std::make_shared<RecordDevice>(device, "output.mcap", true);'
