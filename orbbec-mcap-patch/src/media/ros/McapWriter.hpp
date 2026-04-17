// Copyright (c) Orbbec Inc. All Rights Reserved.
// Licensed under the MIT License.

#pragma once

#include "IWriter.hpp"
#include "RosFileFormat.hpp"
#include "frame/Frame.hpp"
#include "IDevice.hpp"
#include "libobsensor/h/ObTypes.h"
#include "logger/Logger.hpp"
#include "stream/StreamProfile.hpp"

#include "ros/time.h"
#include "sensor_msgs/Image.h"
#include "sensor_msgs/Imu.h"
#include "sensor_msgs/PointCloud2.h"
#include "sensor_msgs/PointField.h"
#include "custom_msg/OBDeviceInfo.h"
#include "custom_msg/OBStreamProfile.h"
#include "custom_msg/OBImuStreamProfile.h"
#include "custom_msg/OBLiDARStreamProfile.h"
#include "custom_msg/OBDisparityParam.h"
#include "custom_msg/OBProperty.h"
#include "ros/serialization.h"

#include "mcap/writer.hpp"

#include <chrono>
#include <iostream>
#include <memory>
#include <string>
#include <vector>
#include <map>
#include <mutex>

namespace libobsensor {

class McapWriter : public IWriter {
public:
    explicit McapWriter(const std::string &file, bool compressWhileRecord);
    virtual ~McapWriter() noexcept override;

    virtual void writeFrame(const OBSensorType &sensorType, std::shared_ptr<const Frame> curFrame) override;
    virtual void writeDeviceInfo(const std::shared_ptr<const DeviceInfo> &deviceInfo) override;
    virtual void writeProperty(uint32_t propertyID, const uint8_t *data, const uint32_t datasize) override;
    virtual void writeStreamProfiles() override;
    virtual void stop(bool hasError) override;

private:
    void writeVideoFrame(const OBSensorType &sensorType, std::shared_ptr<const Frame> curFrame);
    void writeImuFrame(const OBSensorType &sensorType, std::shared_ptr<const Frame> curFrame);
    void writeLiDARFrame(std::shared_ptr<const Frame> curFrame);
    void writeVideoStreamProfile(const OBSensorType sensorType, const std::shared_ptr<const StreamProfile> &streamProfile);
    void writeAccelStreamProfile(const std::shared_ptr<const StreamProfile> &streamProfile);
    void writeGyroStreamProfile(const std::shared_ptr<const StreamProfile> &streamProfile);
    void writeLiDARStreamProfile(const std::shared_ptr<const StreamProfile> &streamProfile);
    void writeDisparityParam(std::shared_ptr<const DisparityBasedStreamProfile> disparityParam);

    // Get or create an MCAP channel for a given topic + message type.
    // Registers the schema (ros1msg definition) on first use.
    template <typename T>
    mcap::ChannelId getOrCreateChannel(const std::string &topic);

    // Write a serialized ROS message to the MCAP file
    template <typename T>
    void writeMcapMessage(const std::string &topic, uint64_t timestampUsec, const T &msg);

private:
    std::string                                                  filePath_;
    mcap::McapWriter                                             writer_;
    bool                                                         isOpen_;
    std::mutex                                                   writeMutex_;
    uint64_t                                                     startTime_;
    std::shared_ptr<const StreamProfile>                         colorStreamProfile_;
    std::shared_ptr<const StreamProfile>                         depthStreamProfile_;
    std::map<OBSensorType, std::shared_ptr<const StreamProfile>> streamProfileMap_;

    // MCAP schema/channel bookkeeping
    std::map<std::string, mcap::SchemaId>  schemaMap_;   // dataType -> schemaId
    std::map<std::string, mcap::ChannelId> channelMap_;  // topic -> channelId
    uint32_t                               sequenceCounter_;

    uint64_t minFrameTime_;
    uint64_t maxFrameTime_;
};

}  // namespace libobsensor
