# How to Apply the MCAP Patch to OrbbecSDK v2

## New Files to Copy

Copy these into the OrbbecSDK_v2 source tree:

```
sdk-patch/3rdparty/mcap/          → OrbbecSDK_v2/3rdparty/mcap/
sdk-patch/src/media/ros/McapWriter.hpp  → OrbbecSDK_v2/src/media/ros/McapWriter.hpp
sdk-patch/src/media/ros/McapWriter.cpp  → OrbbecSDK_v2/src/media/ros/McapWriter.cpp
sdk-patch/src/media/ros/McapReader.hpp  → OrbbecSDK_v2/src/media/ros/McapReader.hpp
sdk-patch/src/media/ros/McapReader.cpp  → OrbbecSDK_v2/src/media/ros/McapReader.cpp
```

## Existing Files to Modify

### 1. `src/media/CMakeLists.txt`

Add MCAP dependency after the rosbag lines:

```cmake
# After line: target_link_libraries(${OB_TARGET_MEDIA} PUBLIC rosbag::rosbag)
# Add:
add_subdirectory(${OB_3RDPARTY_DIR}/mcap mcap)
target_link_libraries(${OB_TARGET_MEDIA} PUBLIC mcap::mcap)
```

### 2. `src/media/record/RecordDevice.hpp`

Add the McapWriter include:

```cpp
// After line: #include "ros/RosbagWriter.hpp"
// Add:
#include "ros/McapWriter.hpp"
```

### 3. `src/media/record/RecordDevice.cpp`

Change the writer instantiation (line 21):

```cpp
// Replace:
writer_ = std::make_shared<RosWriter>(filePath_, isCompressionsEnabled_);

// With:
if(filePath_.size() >= 5 && filePath_.substr(filePath_.size() - 5) == ".mcap") {
    writer_ = std::make_shared<McapWriter>(filePath_, isCompressionsEnabled_);
}
else {
    writer_ = std::make_shared<RosWriter>(filePath_, isCompressionsEnabled_);
}
```

### 4. `src/media/playback/PlaybackDevicePort.hpp`

Add the McapReader include:

```cpp
// After line: #include "ros/RosbagReader.hpp"
// Add:
#include "ros/McapReader.hpp"
```

### 5. `src/media/playback/PlaybackDevicePort.cpp`

Change the reader instantiation (line 16):

```cpp
// Replace:
: reader_(std::make_shared<RosReader>(filePath)),

// With:
: reader_(filePath.size() >= 5 && filePath.substr(filePath.size() - 5) == ".mcap"
          ? std::static_pointer_cast<IReader>(std::make_shared<McapReader>(filePath))
          : std::static_pointer_cast<IReader>(std::make_shared<RosReader>(filePath))),
```

## Build

After applying all changes, rebuild the SDK normally:

```bash
cd OrbbecSDK_v2
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
cmake --build .
```

## Usage

To record in MCAP format, simply pass a `.mcap` file extension:

```cpp
// C++ API
auto recorder = std::make_shared<RecordDevice>(device, "output.mcap", true);

// The file extension determines the format:
// ".mcap" → McapWriter (MCAP format with LZ4 compression)
// ".bag"  → RosWriter  (legacy rosbag format, unchanged behavior)
```

For playback, the same applies — pass a `.mcap` file and it will use `McapReader`.
