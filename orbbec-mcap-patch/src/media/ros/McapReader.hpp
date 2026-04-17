// Copyright (c) Orbbec Inc. All Rights Reserved.
// Licensed under the MIT License.

#pragma once

#include "IReader.hpp"
#include "frame/Frame.hpp"
#include "frame/FrameFactory.hpp"
#include "IDevice.hpp"
#include "exception/ObException.hpp"
#include "stream/StreamProfile.hpp"
#include "libobsensor/h/ObTypes.h"

#include "RosFileFormat.hpp"
#include "ros/time.h"
#include "ros/serialization.h"
#include "sensor_msgs/Image.h"
#include "sensor_msgs/Imu.h"
#include "sensor_msgs/PointCloud2.h"
#include "custom_msg/OBDeviceInfo.h"
#include "custom_msg/OBStreamProfile.h"
#include "custom_msg/OBImuStreamProfile.h"
#include "custom_msg/OBLiDARStreamProfile.h"
#include "custom_msg/OBDisparityParam.h"
#include "custom_msg/OBProperty.h"

#include "mcap/reader.hpp"

#include <chrono>
#include <iostream>
#include <memory>
#include <string>
#include <vector>
#include <map>
#include <mutex>

namespace libobsensor {

class McapReader : public IReader {
public:
    McapReader(const std::string &file);
    virtual ~McapReader() noexcept override = default;

    virtual std::shared_ptr<DeviceInfo>    getDeviceInfo() override;
    virtual std::chrono::nanoseconds       getDuration() override;
    virtual std::shared_ptr<StreamProfile> getStreamProfile(OBStreamType streamType) override;
    virtual bool                           getIsEndOfFile() override;
    virtual std::vector<OBSensorType>      getSensorTypeList() const override;
    virtual std::chrono::nanoseconds       getCurTime() override;
    virtual std::vector<uint8_t>           getPropertyData(uint32_t propertyId) override;
    virtual bool                           isPropertySupported(uint32_t propertyId) const override;

    virtual std::shared_ptr<Frame> readNextData() override;
    virtual void                   seekToTime(const std::chrono::nanoseconds &seekTime) override;

    virtual std::vector<std::shared_ptr<Frame>> readLastDatas(const std::chrono::nanoseconds &startTime, const std::chrono::nanoseconds &endTime) override;

    virtual void stop() override;

private:
    // Deserialize a ROS message from raw MCAP message bytes
    template <typename T>
    std::shared_ptr<T> deserializeMessage(const mcap::Message &msg);

    void                   initView();
    std::shared_ptr<Frame> createVideoFrame(const std::string &topic, std::shared_ptr<sensor_msgs::Image> imageMsg);
    std::shared_ptr<Frame> createImuFrame(const std::string &topic, std::shared_ptr<sensor_msgs::Imu> imuMsg);
    std::shared_ptr<Frame> createLiDARPointCloud(const std::string &topic, std::shared_ptr<sensor_msgs::PointCloud2> pcMsg);
    void                   queryDeviceInfo();
    void                   querySreamProfileList();
    void                   queryProperty();
    void                   bindStreamProfileExtrinsic();
    std::shared_ptr<Frame> createFrame(const std::string &topic, const mcap::Message &msg, const mcap::Channel &channel);

    // Determine if a topic is a sensor frame topic (matches /cam/sensor_N/frameType_N pattern)
    bool isFrameTopic(const std::string &topic) const;

private:
    std::string                                            filePath_;
    mcap::McapReader                                       reader_;
    std::chrono::nanoseconds                               totalDuration_;
    std::mutex                                             readMutex_;
    uint64_t                                               startTimeNs_;
    uint64_t                                               endTimeNs_;

    // Sorted list of all frame messages for iteration
    struct FrameEntry {
        uint64_t    logTime;
        std::string topic;
        mcap::Message message;
        mcap::ChannelId channelId;
    };
    std::vector<FrameEntry>                                frameEntries_;
    size_t                                                 frameIndex_;

    std::vector<std::string>                               enabledStreamsTopics_;
    std::shared_ptr<DeviceInfo>                            deviceInfo_;
    float                                                  unit_;
    float                                                  baseline_;
    std::map<OBStreamType, std::shared_ptr<StreamProfile>> streamProfileList_;
    std::map<uint32_t, std::vector<uint8_t>>               propertyList_;

    // Store schema name -> dataType mapping from MCAP channels
    std::map<mcap::ChannelId, std::string>                 channelTopics_;
    std::map<mcap::ChannelId, std::string>                 channelSchemaNames_;
};

}  // namespace libobsensor
