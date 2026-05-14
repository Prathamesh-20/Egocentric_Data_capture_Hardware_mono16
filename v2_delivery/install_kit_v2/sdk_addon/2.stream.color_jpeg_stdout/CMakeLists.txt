# CMakeLists.txt for ob_color_jpeg_stream — additive Orbbec SDK example.
#
# Built and installed by the install kit alongside ob_device_record_mcap_nogui.
# OpenCV is optional: if present, the helper can transcode non-MJPG color
# formats to JPEG. If absent, the helper still works for cameras that
# expose MJPG natively (which is the case for the Gemini 2L).

cmake_minimum_required(VERSION 3.5)

set(SAMPLE_NAME ob_color_jpeg_stream)
add_executable(${SAMPLE_NAME} ob_color_jpeg_stream.cpp)

set_property(TARGET ${SAMPLE_NAME} PROPERTY CXX_STANDARD 14)

target_link_libraries(${SAMPLE_NAME} ob::OrbbecSDK pthread)

# Optional OpenCV — if available, link it. Otherwise the binary builds fine
# but only emits frames when the camera gave us MJPG natively.
find_package(OpenCV QUIET)
if(OpenCV_FOUND)
    target_compile_definitions(${SAMPLE_NAME} PRIVATE HAVE_OPENCV=1)
    target_include_directories(${SAMPLE_NAME} PRIVATE ${OpenCV_INCLUDE_DIRS})
    target_link_libraries(${SAMPLE_NAME} ${OpenCV_LIBS})
    message(STATUS "ob_color_jpeg_stream: building with OpenCV transcode support")
else()
    message(STATUS "ob_color_jpeg_stream: building without OpenCV (MJPG-only)")
endif()

set_target_properties(${SAMPLE_NAME} PROPERTIES
    FOLDER "examples/stream"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
)

install(TARGETS ${SAMPLE_NAME} RUNTIME DESTINATION bin)
