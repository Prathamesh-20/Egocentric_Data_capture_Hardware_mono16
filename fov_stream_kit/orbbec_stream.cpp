// Copyright (c) Autonex Tools.
// Licensed under the MIT License.
//
// orbbec_stream — minimal headless color streamer for FOVChecker.
//
// Writes color frames to stdout in this exact protocol (one frame per write):
//
//     FRAME COLOR <w> <h> <fps> <ts_ms> <fmt> <size>\n
//     <size bytes of JPEG/MJPG>
//
// fov_check.py reads parts[6] (the 7th whitespace-separated field) as the
// payload byte count, then decodes the bytes as JPEG via cv2.imdecode.
//
// Stream config: MJPG 1280x800 @ 30fps (matches the MCAP recorder smoke test
// output, so the camera can deliver this without negotiation surprises).
//
// Runs forever until terminated (SIGTERM from Python's proc.terminate()).
// On SIGTERM/SIGINT we set a flag, break the loop, and stop the pipeline
// cleanly so the next invocation can re-open the device.

#include <libobsensor/ObSensor.hpp>
#include "utils.hpp"

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <iostream>

static std::atomic<bool> g_stop{false};

static void onSignal(int) {
    g_stop.store(true);
}

int main(void) try {
    // Catch SIGTERM (from Python proc.terminate()) and SIGINT (Ctrl-C) so we
    // can stop the pipeline cleanly. fov_check.py uses SIGTERM.
    std::signal(SIGTERM, onSignal);
    std::signal(SIGINT,  onSignal);

    // stdout must be unbuffered for low-latency frame delivery to Python.
    // stderr is line-buffered by default which is fine for log lines.
    std::setvbuf(stdout, nullptr, _IONBF, 0);

    // Pipeline with default (first attached) device.
    ob::Pipeline pipe;
    auto config = std::make_shared<ob::Config>();

    // MJPG 1280x800 @ 30fps. Matches the mode used by the MCAP recorder so we
    // know the device + USB bus can sustain it.
    //   format = OB_FORMAT_MJPG
    //   width  = 1280
    //   height = 800
    //   fps    = 30
    config->enableVideoStream(OB_STREAM_COLOR, 1280, 800, 30, OB_FORMAT_MJPG);

    // Block on frameset waits for up to 100ms; nullptr means timeout — we just
    // loop and check g_stop. Don't enable depth — we only need color for
    // wrist detection.

    pipe.start(config);

    std::cerr << "[orbbec_stream] started: MJPG 1280x800@30" << std::endl;

    uint64_t frame_idx = 0;
    while (!g_stop.load()) {
        auto frameSet = pipe.waitForFrameset(100);
        if (frameSet == nullptr) {
            continue;
        }

        auto colorFrame = frameSet->getFrame(OB_FRAME_COLOR);
        if (colorFrame == nullptr) {
            continue;
        }

        // Cast to VideoFrame for width/height accessors.
        auto videoFrame = colorFrame->as<ob::VideoFrame>();
        if (videoFrame == nullptr) {
            continue;
        }

        const uint32_t w   = videoFrame->getWidth();
        const uint32_t h   = videoFrame->getHeight();
        const uint32_t fps = 30;  // configured rate; SDK doesn't expose per-frame fps reliably
        const uint64_t ts  = videoFrame->getTimeStampMs();
        const void*    data     = videoFrame->getData();
        const uint32_t dataSize = videoFrame->getDataSize();

        if (data == nullptr || dataSize == 0) {
            continue;
        }

        // Header — must match fov_check.py's parser exactly:
        //   parts = header.split()
        //   data_size = int(parts[6])
        // So fields 0..6 are: FRAME, COLOR, w, h, fps, ts, size
        // i.e. the format token is *not* in the header — we hard-code MJPG.
        // (Matches the original Pi 5 install we're replacing; the script
        //  only cares about parts[6] = size and decodes the body as JPEG.)
        std::printf("FRAME COLOR %u %u %u %llu MJPG %u\n",
                    w, h, fps,
                    static_cast<unsigned long long>(ts),
                    dataSize);

        // Body — raw MJPG bytes.
        std::fwrite(data, 1, dataSize, stdout);

        // No fflush needed — stdout is unbuffered (_IONBF set above).

        ++frame_idx;
    }

    std::cerr << "[orbbec_stream] stopping after " << frame_idx
              << " frames" << std::endl;

    pipe.stop();
    return 0;
}
catch (ob::Error &e) {
    std::cerr << "[orbbec_stream] ob::Error"
              << " function=" << e.getFunction()
              << " args="     << e.getArgs()
              << " message="  << e.what()
              << " type="     << e.getExceptionType()
              << std::endl;
    return EXIT_FAILURE;
}
catch (std::exception &e) {
    std::cerr << "[orbbec_stream] std::exception: " << e.what() << std::endl;
    return EXIT_FAILURE;
}
