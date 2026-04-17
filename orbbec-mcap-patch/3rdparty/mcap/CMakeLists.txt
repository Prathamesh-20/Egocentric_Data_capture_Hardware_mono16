cmake_minimum_required(VERSION 3.10)

# MCAP header-only library
# Uses LZ4 from the rosbag 3rdparty, disables ZSTD to avoid an extra dependency
# Requires C++17 (std::byte, std::optional, std::variant)

add_library(mcap INTERFACE)
target_include_directories(mcap INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/include)

# MCAP requires C++17
target_compile_features(mcap INTERFACE cxx_std_17)

# Disable ZSTD compression (not bundled), use LZ4 only (already in rosbag/lz4)
target_compile_definitions(mcap INTERFACE MCAP_COMPRESSION_NO_ZSTD=1)

# LZ4 headers are provided by rosbag's bundled lz4
target_include_directories(mcap INTERFACE ${OB_3RDPARTY_DIR}/rosbag/src/lz4)

add_library(mcap::mcap ALIAS mcap)
