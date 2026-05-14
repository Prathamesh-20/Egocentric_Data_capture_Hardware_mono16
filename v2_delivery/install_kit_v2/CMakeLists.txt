# Copyright (c) Autonex Tools. All Rights Reserved.
# Direct Orbbec -> MCAP recorder.
cmake_minimum_required(VERSION 3.10)
project(ob_device_record_mcap_nogui)

# zstd via pkg-config
find_package(PkgConfig REQUIRED)
pkg_check_modules(ZSTD REQUIRED libzstd)

add_executable(${PROJECT_NAME} ob_device_record_mcap_nogui.cpp)

set_property(TARGET ${PROJECT_NAME} PROPERTY CXX_STANDARD 17)
set_property(TARGET ${PROJECT_NAME} PROPERTY CXX_STANDARD_REQUIRED ON)

target_include_directories(${PROJECT_NAME} PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../../third_party
    ${ZSTD_INCLUDE_DIRS}
)

target_link_libraries(${PROJECT_NAME}
    ob::${OB_SDK_LIB_NAME}
    ob::examples::utils
    ${ZSTD_LIBRARIES}
    pthread
)

set_target_properties(${PROJECT_NAME} PROPERTIES FOLDER "examples")

install(TARGETS ${PROJECT_NAME} RUNTIME DESTINATION bin)
