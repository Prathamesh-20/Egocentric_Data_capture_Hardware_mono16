// Copyright (c) Autonex Tools. All Rights Reserved.
// Additive Orbbec SDK example — does NOT modify any existing example.
//
// Purpose:
//   Stream Color frames from an Orbbec device as JPEG to stdout, in a
//   simple framed text+binary protocol that fov_check.py consumes.
//
// Protocol (one frame = one header line + payload):
//   "FRAME COLOR <ts_us> <fid> <w> <h> <fmt> <data_size>\n"
//   <data_size bytes of JPEG>
//
// Notes:
//   - We only enable Color (not Depth). The recorder is the depth consumer;
//     this helper exists for the FOV pre-check, which only needs RGB.
//   - We prefer the same MJPG profile the recorder uses (1280x800@30) to
//     keep ISP state consistent across the FOV→record handoff. If that
//     exact profile is missing, fall back to the first MJPG@30 profile.
//     If no MJPG profile exists, fall back to YUYV/RGB and JPEG-encode
//     in software via OpenCV.
//   - Stops cleanly on EOF on stdin OR when stderr/stdout pipe breaks.
//   - Output is written to fd=1 (stdout) using ::write so OPOST disabling
//     in the parent's PTY is honoured.

#include <libobsensor/ObSensor.hpp>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <thread>
#include <vector>
#include <mutex>

#include <unistd.h>     // ::write, STDOUT_FILENO
#include <signal.h>
#include <sys/select.h>

#ifdef __has_include
#  if __has_include(<opencv2/opencv.hpp>)
#    include <opencv2/opencv.hpp>
#    define HAVE_OPENCV 1
#  endif
#endif

namespace {

static constexpr int COLOR_W   = 1280;
static constexpr int COLOR_H   = 800;
static constexpr int COLOR_FPS = 30;

std::atomic<bool> g_stop{false};

void on_signal(int /*sig*/) { g_stop.store(true); }

std::mutex g_write_mu;

// Write a complete frame (header line + payload) atomically to stdout.
// On any write error, set g_stop so the streaming callback bails out.
bool write_frame(uint64_t ts_us, uint64_t fid, int w, int h,
                 const char *fmt, const uint8_t *data, size_t size) {
    char header[160];
    int n = std::snprintf(header, sizeof(header),
                          "FRAME COLOR %llu %llu %d %d %s %zu\n",
                          (unsigned long long)ts_us,
                          (unsigned long long)fid,
                          w, h, fmt, size);
    if (n <= 0 || (size_t)n >= sizeof(header)) {
        return false;
    }
    std::lock_guard<std::mutex> lk(g_write_mu);
    if (::write(STDOUT_FILENO, header, (size_t)n) != n) {
        g_stop.store(true);
        return false;
    }
    size_t left = size;
    const uint8_t *p = data;
    while (left > 0) {
        ssize_t wrote = ::write(STDOUT_FILENO, p, left);
        if (wrote <= 0) {
            if (wrote < 0 && errno == EINTR) continue;
            g_stop.store(true);
            return false;
        }
        p    += wrote;
        left -= (size_t)wrote;
    }
    return true;
}

// Pick the best matching color profile.
std::shared_ptr<ob::StreamProfile> pick_color_profile(
    const std::shared_ptr<ob::StreamProfileList> &profiles)
{
    // Pass 1: exact MJPG @ COLOR_W x COLOR_H @ COLOR_FPS
    for (uint32_t i = 0; i < profiles->getCount(); ++i) {
        auto p = profiles->getProfile(i)->as<ob::VideoStreamProfile>();
        if (p && p->getFormat() == OB_FORMAT_MJPG
            && (int)p->getWidth()  == COLOR_W
            && (int)p->getHeight() == COLOR_H
            && (int)p->getFps()    == COLOR_FPS) {
            return profiles->getProfile(i);
        }
    }
    // Pass 2: any MJPG profile at COLOR_FPS
    for (uint32_t i = 0; i < profiles->getCount(); ++i) {
        auto p = profiles->getProfile(i)->as<ob::VideoStreamProfile>();
        if (p && p->getFormat() == OB_FORMAT_MJPG
            && (int)p->getFps() == COLOR_FPS) {
            return profiles->getProfile(i);
        }
    }
    // Pass 3: any MJPG profile at all
    for (uint32_t i = 0; i < profiles->getCount(); ++i) {
        auto p = profiles->getProfile(i)->as<ob::VideoStreamProfile>();
        if (p && p->getFormat() == OB_FORMAT_MJPG) {
            return profiles->getProfile(i);
        }
    }
    // Pass 4 (fallback for cameras without native MJPG): pick anything.
#ifdef HAVE_OPENCV
    for (uint32_t i = 0; i < profiles->getCount(); ++i) {
        auto p = profiles->getProfile(i)->as<ob::VideoStreamProfile>();
        if (p) return profiles->getProfile(i);
    }
#endif
    return nullptr;
}

// Watch stdin in a small loop; if EOF (parent closed pipe), trigger stop.
void stdin_watch_thread() {
    while (!g_stop.load()) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(STDIN_FILENO, &rfds);
        struct timeval tv;
        tv.tv_sec  = 0;
        tv.tv_usec = 200 * 1000;
        int s = ::select(STDIN_FILENO + 1, &rfds, nullptr, nullptr, &tv);
        if (s < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (s > 0 && FD_ISSET(STDIN_FILENO, &rfds)) {
            char buf[64];
            ssize_t n = ::read(STDIN_FILENO, buf, sizeof(buf));
            if (n <= 0) break;  // EOF
            // Any byte means parent wants us to stop.
            for (ssize_t i = 0; i < n; ++i) {
                if (buf[i] == 'q' || buf[i] == 'Q' || buf[i] == 0x1b) {
                    g_stop.store(true);
                    return;
                }
            }
        }
    }
    g_stop.store(true);
}

} // anon namespace

int main() try {
    ::signal(SIGINT,  on_signal);
    ::signal(SIGTERM, on_signal);
    ::signal(SIGPIPE, SIG_IGN);

    std::cerr << "ob_color_jpeg_stream: starting" << std::endl;

    auto context    = std::make_shared<ob::Context>();
    auto deviceList = context->queryDeviceList();
    if (deviceList->getCount() < 1) {
        std::cerr << "ob_color_jpeg_stream: no Orbbec device found" << std::endl;
        return EXIT_FAILURE;
    }
    auto device = deviceList->getDevice(0);
    auto pipe   = std::make_shared<ob::Pipeline>(device);

    try { device->timerSyncWithHost(); }
    catch (const ob::Error &e) {
        std::cerr << "ob_color_jpeg_stream: timerSyncWithHost: "
                  << e.what() << std::endl;
    }

    auto config     = std::make_shared<ob::Config>();
    auto sensorList = device->getSensorList();

    std::shared_ptr<ob::StreamProfile> chosen;
    OBFormat chosen_fmt = OB_FORMAT_MJPG;
    int chosen_w = 0, chosen_h = 0;

    for (uint32_t i = 0; i < sensorList->getCount(); ++i) {
        auto sensor = sensorList->getSensor(i);
        if (sensor->getType() != OB_SENSOR_COLOR) continue;
        auto profiles = sensor->getStreamProfileList();
        chosen = pick_color_profile(profiles);
        if (chosen) {
            auto v   = chosen->as<ob::VideoStreamProfile>();
            chosen_fmt = v->getFormat();
            chosen_w   = (int)v->getWidth();
            chosen_h   = (int)v->getHeight();
            config->enableStream(chosen);
            std::cerr << "ob_color_jpeg_stream: enabled Color "
                      << (chosen_fmt == OB_FORMAT_MJPG ? "MJPG" :
                          (chosen_fmt == OB_FORMAT_YUYV ? "YUYV" : "OTHER"))
                      << " " << chosen_w << "x" << chosen_h
                      << "@" << v->getFps() << std::endl;
            break;
        }
    }
    if (!chosen) {
        std::cerr << "ob_color_jpeg_stream: no usable color profile" << std::endl;
        return EXIT_FAILURE;
    }

    std::atomic<uint64_t> fid{0};

    pipe->start(config, [&](std::shared_ptr<ob::FrameSet> fs) {
        if (!fs || g_stop.load()) return;
        auto frame = fs->getFrame(OB_FRAME_COLOR);
        if (!frame) return;
        auto video = frame->as<ob::VideoFrame>();
        if (!video) return;

        const uint8_t *data = reinterpret_cast<const uint8_t*>(video->getData());
        size_t         size = video->getDataSize();
        uint64_t       ts_us = (uint64_t)video->getTimeStampUs();
        if (ts_us == 0) {
            ts_us = (uint64_t)std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::system_clock::now().time_since_epoch()).count();
        }

        if (chosen_fmt == OB_FORMAT_MJPG) {
            write_frame(ts_us, fid.fetch_add(1),
                        chosen_w, chosen_h, "MJPG", data, size);
        } else {
#ifdef HAVE_OPENCV
            // Software-encode whatever the camera gave us into JPEG.
            cv::Mat src;
            try {
                if (chosen_fmt == OB_FORMAT_YUYV) {
                    cv::Mat yuyv(chosen_h, chosen_w, CV_8UC2, (void*)data);
                    cv::cvtColor(yuyv, src, cv::COLOR_YUV2BGR_YUYV);
                } else if (chosen_fmt == OB_FORMAT_RGB) {
                    cv::Mat rgb(chosen_h, chosen_w, CV_8UC3, (void*)data);
                    cv::cvtColor(rgb, src, cv::COLOR_RGB2BGR);
                } else {
                    return;
                }
                std::vector<uchar> jpeg;
                std::vector<int> params = {cv::IMWRITE_JPEG_QUALITY, 80};
                cv::imencode(".jpg", src, jpeg, params);
                if (!jpeg.empty()) {
                    write_frame(ts_us, fid.fetch_add(1),
                                chosen_w, chosen_h, "MJPG",
                                jpeg.data(), jpeg.size());
                }
            } catch (const cv::Exception &e) {
                std::cerr << "ob_color_jpeg_stream: OpenCV encode failed: "
                          << e.what() << std::endl;
            }
#else
            // Without OpenCV we cannot encode non-MJPG frames — drop them.
            (void)data; (void)size;
#endif
        }
    });

    std::cerr << "ob_color_jpeg_stream: streaming. Send 'q' or close stdin "
                 "to stop." << std::endl;

    std::thread stdin_thread(stdin_watch_thread);

    while (!g_stop.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    std::cerr << "ob_color_jpeg_stream: stopping" << std::endl;
    try { pipe->stop(); }
    catch (const ob::Error &e) {
        std::cerr << "ob_color_jpeg_stream: pipe->stop(): "
                  << e.what() << std::endl;
    }

    if (stdin_thread.joinable()) stdin_thread.join();
    std::cerr << "ob_color_jpeg_stream: done" << std::endl;
    return 0;
}
catch (const ob::Error &e) {
    std::cerr << "ob_color_jpeg_stream fatal: " << e.what() << std::endl;
    return EXIT_FAILURE;
}
catch (const std::exception &e) {
    std::cerr << "ob_color_jpeg_stream fatal: " << e.what() << std::endl;
    return EXIT_FAILURE;
}
