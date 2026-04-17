// Copyright (c) Orbbec Inc. All Rights Reserved.
// Licensed under the MIT License.

#include "McapReader.hpp"

#include <algorithm>
#include <regex>

namespace libobsensor {

const uint64_t MCAP_READER_INVALID_DURATION = 6ULL * 60ULL * 60ULL * 1000000000ULL;  // 6 hours in ns

template <typename T>
std::shared_ptr<T> McapReader::deserializeMessage(const mcap::Message &msg) {
    auto result = std::make_shared<T>();
    orbbecRosbag::serialization::IStream stream(const_cast<uint8_t *>(reinterpret_cast<const uint8_t *>(msg.data)), msg.dataSize);
    orbbecRosbag::serialization::deserialize(stream, *result);
    return result;
}

bool McapReader::isFrameTopic(const std::string &topic) const {
    static std::regex frameRegex(R"(/cam/sensor_\d+/frameType_\d+)");
    return std::regex_match(topic, frameRegex);
}

McapReader::McapReader(const std::string &filePath)
    : filePath_(filePath), totalDuration_(0), startTimeNs_(0), endTimeNs_(0), frameIndex_(0), unit_(0.0), baseline_(0.0) {
    initView();
    queryDeviceInfo();
    querySreamProfileList();
    queryProperty();
    bindStreamProfileExtrinsic();
}

void McapReader::initView() {
    auto status = reader_.open(filePath_);
    if(!status.ok()) {
        throw io_exception("Failed to open MCAP file: " + status.message);
    }

    auto summaryStatus = reader_.readSummary(mcap::ReadSummaryMethod::AllowFallbackScan);
    if(!summaryStatus.ok()) {
        throw io_exception("Failed to read MCAP summary: " + summaryStatus.message);
    }

    // Build channel lookup maps
    const auto channels = reader_.channels();
    const auto schemas = reader_.schemas();
    for(const auto &[id, channelPtr]: channels) {
        channelTopics_[id] = channelPtr->topic;
        if(schemas.count(channelPtr->schemaId)) {
            channelSchemaNames_[id] = schemas.at(channelPtr->schemaId)->name;
        }
    }

    // Read all frame messages (sensor data) into memory index
    // We iterate all messages and filter for frame topics
    auto messageView = reader_.readMessages();
    startTimeNs_ = UINT64_MAX;
    endTimeNs_ = 0;

    for(auto it = messageView.begin(); it != messageView.end(); ++it) {
        const auto &msgView = *it;
        const auto &topic = msgView.channel->topic;

        if(isFrameTopic(topic)) {
            if(msgView.message.logTime < startTimeNs_) startTimeNs_ = msgView.message.logTime;
            if(msgView.message.logTime > endTimeNs_) endTimeNs_ = msgView.message.logTime;

            FrameEntry entry;
            entry.logTime   = msgView.message.logTime;
            entry.topic     = topic;
            entry.channelId = msgView.message.channelId;
            // Copy the message data since the reader may invalidate it
            entry.message.channelId   = msgView.message.channelId;
            entry.message.sequence    = msgView.message.sequence;
            entry.message.logTime     = msgView.message.logTime;
            entry.message.publishTime = msgView.message.publishTime;
            entry.message.dataSize    = msgView.message.dataSize;
            // We need to copy the data buffer
            auto *dataCopy = new std::byte[msgView.message.dataSize];
            std::memcpy(dataCopy, msgView.message.data, msgView.message.dataSize);
            entry.message.data = dataCopy;

            frameEntries_.push_back(std::move(entry));

            // Track unique frame topics
            if(std::find(enabledStreamsTopics_.begin(), enabledStreamsTopics_.end(), topic) == enabledStreamsTopics_.end()) {
                enabledStreamsTopics_.push_back(topic);
            }
        }
    }

    // Sort by timestamp
    std::sort(frameEntries_.begin(), frameEntries_.end(),
              [](const FrameEntry &a, const FrameEntry &b) { return a.logTime < b.logTime; });

    if(startTimeNs_ != UINT64_MAX && endTimeNs_ > startTimeNs_) {
        totalDuration_ = std::chrono::nanoseconds(endTimeNs_ - startTimeNs_);
    }

    if(static_cast<uint64_t>(totalDuration_.count()) >= MCAP_READER_INVALID_DURATION) {
        throw io_exception("The streaming duration is too long, please check the MCAP file.");
    }

    frameIndex_ = 0;
}

std::chrono::nanoseconds McapReader::getDuration() {
    return totalDuration_;
}

void McapReader::queryDeviceInfo() {
    // Read all messages and find device info topic
    auto messageView = reader_.readMessages();
    for(auto it = messageView.begin(); it != messageView.end(); ++it) {
        const auto &msgView = *it;
        if(msgView.channel->topic == "/cam/deviceInfo") {
            auto deviceInfoMsg = deserializeMessage<custom_msg::DeviceInfo>(msgView.message);
            if(deviceInfoMsg->connectionType == "Ethernet") {
                auto netDeviceInfo        = std::make_shared<NetDeviceInfo>();
                netDeviceInfo->ipAddress_ = deviceInfoMsg->ipAddress;
                netDeviceInfo->localMac_  = deviceInfoMsg->localMac;
                deviceInfo_               = netDeviceInfo;
            }
            else {
                deviceInfo_ = std::make_shared<DeviceInfo>();
            }
            deviceInfo_->name_                = deviceInfoMsg->name;
            deviceInfo_->fullName_            = deviceInfoMsg->fullName + "(Playback)";
            deviceInfo_->asicName_            = deviceInfoMsg->asicName;
            deviceInfo_->vid_                 = deviceInfoMsg->vid;
            deviceInfo_->pid_                 = deviceInfoMsg->pid;
            deviceInfo_->uid_                 = deviceInfoMsg->uid;
            deviceInfo_->deviceSn_            = deviceInfoMsg->sn;
            deviceInfo_->fwVersion_           = deviceInfoMsg->fwVersion;
            deviceInfo_->hwVersion_           = deviceInfoMsg->hwVersion;
            deviceInfo_->supportedSdkVersion_ = deviceInfoMsg->supportedSdkVersion;
            deviceInfo_->connectionType_      = deviceInfoMsg->connectionType;
            deviceInfo_->type_                = deviceInfoMsg->type;
            deviceInfo_->backendType_         = static_cast<OBUvcBackendType>(deviceInfoMsg->backendType);
            break;
        }
    }
}

std::shared_ptr<StreamProfile> McapReader::getStreamProfile(OBStreamType streamType) {
    if(streamProfileList_.count(streamType) == 0) {
        return nullptr;
    }
    return streamProfileList_.at(streamType);
}

void McapReader::queryProperty() {
    auto messageView = reader_.readMessages();
    for(auto it = messageView.begin(); it != messageView.end(); ++it) {
        const auto &msgView = *it;
        if(msgView.channel->topic == "/cam/property") {
            auto propertyMsg = deserializeMessage<custom_msg::property>(msgView.message);
            propertyList_[propertyMsg->propertyId] = propertyMsg->data;
        }
    }
}

std::vector<uint8_t> McapReader::getPropertyData(uint32_t propertyId) {
    if(propertyList_.count(propertyId)) {
        return propertyList_[propertyId];
    }
    return {};
}

void McapReader::querySreamProfileList() {
    static std::regex streamProfileRegex(R"(/cam/streamProfileType_(\d+))");

    auto messageView = reader_.readMessages();
    for(auto it = messageView.begin(); it != messageView.end(); ++it) {
        const auto &msgView = *it;
        const auto &topic = msgView.channel->topic;
        std::smatch match;

        if(!std::regex_match(topic, match, streamProfileRegex)) {
            continue;
        }

        OBStreamType streamType = static_cast<OBStreamType>(std::stoi(match[1].str()));

        if(streamType == OB_STREAM_ACCEL) {
            auto info = deserializeMessage<custom_msg::ImuStreamProfileInfo>(msgView.message);
            auto profile = std::make_shared<AccelStreamProfile>(
                nullptr, static_cast<OBAccelFullScaleRange>(info->accelFullScaleRange),
                static_cast<OBAccelSampleRate>(info->accelSampleRate));
            double bias[3], gravity[3], scaleMisalignment[9], tempSlope[9];
            memcpy(&bias, &info->bias, sizeof(bias));
            memcpy(&gravity, &info->gravity, sizeof(gravity));
            memcpy(&scaleMisalignment, &info->scaleMisalignment, sizeof(scaleMisalignment));
            memcpy(&tempSlope, &info->tempSlope, sizeof(tempSlope));
            OBAccelIntrinsic intrinsic = { info->noiseDensity, info->randomWalk, info->referenceTemp,
                                           *bias, *gravity, *scaleMisalignment, *tempSlope };
            profile->bindIntrinsic(intrinsic);
            streamProfileList_.insert({ static_cast<OBStreamType>(info->streamType), profile });
        }
        else if(streamType == OB_STREAM_GYRO) {
            auto info = deserializeMessage<custom_msg::ImuStreamProfileInfo>(msgView.message);
            auto profile = std::make_shared<GyroStreamProfile>(
                nullptr, static_cast<OBGyroFullScaleRange>(info->gyroFullScaleRange),
                static_cast<OBGyroSampleRate>(info->gyroSampleRate));
            double bias[3], scaleMisalignment[9], tempSlope[9];
            memcpy(&bias, &info->bias, sizeof(bias));
            memcpy(&scaleMisalignment, &info->scaleMisalignment, sizeof(scaleMisalignment));
            memcpy(&tempSlope, &info->tempSlope, sizeof(tempSlope));
            OBGyroIntrinsic intrinsic = { info->noiseDensity, info->randomWalk, info->referenceTemp,
                                          *bias, *scaleMisalignment, *tempSlope };
            profile->bindIntrinsic(intrinsic);
            streamProfileList_.insert({ static_cast<OBStreamType>(info->streamType), profile });
        }
        else if(streamType == OB_STREAM_LIDAR) {
            auto info = deserializeMessage<custom_msg::LiDARStreamProfileInfo>(msgView.message);
            auto profile = std::make_shared<LiDARStreamProfile>(
                nullptr, static_cast<OBLiDARScanRate>(info->scanRate), static_cast<OBFormat>(info->format));
            streamProfileList_.insert({ static_cast<OBStreamType>(info->streamType), profile });
        }
        else if(streamType == OB_STREAM_DEPTH) {
            auto info = deserializeMessage<custom_msg::StreamProfileInfo>(msgView.message);
            auto depthProfile = std::make_shared<DisparityBasedStreamProfile>(
                nullptr, static_cast<OBStreamType>(info->streamType), static_cast<OBFormat>(info->format),
                static_cast<uint32_t>(info->width), static_cast<uint32_t>(info->height), static_cast<uint32_t>(info->fps));
            OBCameraIntrinsic intrinsic = { info->cameraIntrinsic[0], info->cameraIntrinsic[1],
                                            info->cameraIntrinsic[2], info->cameraIntrinsic[3],
                                            static_cast<int16_t>(info->width), static_cast<int16_t>(info->height) };
            depthProfile->bindIntrinsic(intrinsic);
            OBCameraDistortion distortion = {
                info->cameraDistortion[0], info->cameraDistortion[1], info->cameraDistortion[2], info->cameraDistortion[3],
                info->cameraDistortion[4], info->cameraDistortion[5], info->cameraDistortion[6], info->cameraDistortion[7],
                static_cast<OBCameraDistortionModel>(info->distortionModel) };
            depthProfile->bindDistortion(distortion);
            streamProfileList_.insert({ static_cast<OBStreamType>(info->streamType), depthProfile });
        }
        else {
            auto info = deserializeMessage<custom_msg::StreamProfileInfo>(msgView.message);
            auto videoProfile = std::make_shared<VideoStreamProfile>(
                nullptr, static_cast<OBStreamType>(info->streamType), static_cast<OBFormat>(info->format),
                static_cast<uint32_t>(info->width), static_cast<uint32_t>(info->height), static_cast<uint32_t>(info->fps));
            OBCameraIntrinsic intrinsic = { info->cameraIntrinsic[0], info->cameraIntrinsic[1],
                                            info->cameraIntrinsic[2], info->cameraIntrinsic[3],
                                            static_cast<int16_t>(info->width), static_cast<int16_t>(info->height) };
            videoProfile->bindIntrinsic(intrinsic);
            OBCameraDistortion distortion = {
                info->cameraDistortion[0], info->cameraDistortion[1], info->cameraDistortion[2], info->cameraDistortion[3],
                info->cameraDistortion[4], info->cameraDistortion[5], info->cameraDistortion[6], info->cameraDistortion[7],
                static_cast<OBCameraDistortionModel>(info->distortionModel) };
            videoProfile->bindDistortion(distortion);
            streamProfileList_.insert({ static_cast<OBStreamType>(info->streamType), videoProfile });
        }
    }

    // Query disparity params
    auto dispView = reader_.readMessages();
    for(auto it = dispView.begin(); it != dispView.end(); ++it) {
        const auto &msgView = *it;
        if(msgView.channel->topic == "/cam/disparityParam") {
            auto info = deserializeMessage<custom_msg::DisparityParam>(msgView.message);
            auto depthIt = streamProfileList_.find(OB_STREAM_DEPTH);
            if(depthIt != streamProfileList_.end() && depthIt->second != nullptr) {
                auto disparity = depthIt->second->as<VideoStreamProfile>()->as<DisparityBasedStreamProfile>();
                unit_     = info->unit;
                baseline_ = info->baseline;
                OBDisparityParam disparityParam = {
                    info->zpd, info->zpps, info->baseline, info->fx, info->bitSize, info->unit,
                    info->minDisparity, info->packMode, info->dispOffset, info->invalidDisp,
                    info->dispIntPlace, info->isDualCamera };
                disparity->bindDisparityParam(disparityParam);
            }
            break;
        }
    }
}

void McapReader::bindStreamProfileExtrinsic() {
    OBExtrinsic identityExtrinsicsTmp = { { 1, 0, 0, 0, 1, 0, 0, 0, 1 }, { 0, 0, 0 } };

    // Find depth stream profile extrinsic
    static std::regex depthSpRegex(R"(/cam/streamProfileType_)" + std::to_string((uint8_t)OB_STREAM_DEPTH));
    auto messageView = reader_.readMessages();
    for(auto it = messageView.begin(); it != messageView.end(); ++it) {
        const auto &msgView = *it;
        const auto &topic = msgView.channel->topic;

        if(topic == "/cam/streamProfileType_" + std::to_string((uint8_t)OB_STREAM_DEPTH)) {
            auto info = deserializeMessage<custom_msg::StreamProfileInfo>(msgView.message);
            OBExtrinsic d2cExtrinsic;
            memcpy(&d2cExtrinsic.rot, &info->rotationMatrix, sizeof(d2cExtrinsic.rot));
            memcpy(&d2cExtrinsic.trans, &info->translationMatrix, sizeof(d2cExtrinsic.trans));

            auto colorIt   = streamProfileList_.find(OB_STREAM_COLOR);
            auto depthIt   = streamProfileList_.find(OB_STREAM_DEPTH);
            auto irIt      = streamProfileList_.find(OB_STREAM_IR);
            auto irLeftIt  = streamProfileList_.find(OB_STREAM_IR_LEFT);
            auto irRightIt = streamProfileList_.find(OB_STREAM_IR_RIGHT);

            if(colorIt != streamProfileList_.end() && depthIt != streamProfileList_.end()) {
                depthIt->second->bindExtrinsicTo(colorIt->second, d2cExtrinsic);
            }
            if(irIt != streamProfileList_.end() && depthIt != streamProfileList_.end()) {
                irIt->second->bindSameExtrinsicTo(depthIt->second);
            }
            if(irLeftIt != streamProfileList_.end() && depthIt != streamProfileList_.end()) {
                irLeftIt->second->bindSameExtrinsicTo(depthIt->second);
            }
            if(irLeftIt != streamProfileList_.end() && irRightIt != streamProfileList_.end() && baseline_ != 0 && unit_ != 0) {
                auto leftToRight     = identityExtrinsicsTmp;
                leftToRight.trans[0] = -1.0f * baseline_ * unit_;
                irLeftIt->second->bindExtrinsicTo(irRightIt->second, leftToRight);
            }
            break;
        }
    }

    // Find accel stream profile extrinsic
    auto accelView = reader_.readMessages();
    for(auto it = accelView.begin(); it != accelView.end(); ++it) {
        const auto &msgView = *it;
        if(msgView.channel->topic == "/cam/streamProfileType_" + std::to_string((uint8_t)OB_STREAM_ACCEL)) {
            auto info = deserializeMessage<custom_msg::ImuStreamProfileInfo>(msgView.message);
            OBExtrinsic extrinsic;
            memcpy(&extrinsic.rot, &info->rotationMatrix, sizeof(extrinsic.rot));
            memcpy(&extrinsic.trans, &info->translationMatrix, sizeof(extrinsic.trans));

            auto depthIt = streamProfileList_.find(OB_STREAM_DEPTH);
            auto accelIt = streamProfileList_.find(OB_STREAM_ACCEL);
            auto gyroIt  = streamProfileList_.find(OB_STREAM_GYRO);

            if(accelIt != streamProfileList_.end() && depthIt != streamProfileList_.end()) {
                accelIt->second->bindExtrinsicTo(depthIt->second, extrinsic);
                if(gyroIt != streamProfileList_.end()) {
                    gyroIt->second->bindSameExtrinsicTo(accelIt->second);
                }
            }
            break;
        }
    }
}

std::shared_ptr<DeviceInfo> McapReader::getDeviceInfo() {
    return deviceInfo_;
}

std::vector<OBSensorType> McapReader::getSensorTypeList() const {
    std::vector<OBSensorType> sensorTypesList;
    for(auto &item: streamProfileList_) {
        sensorTypesList.push_back(utils::mapStreamTypeToSensorType(item.first));
    }
    return sensorTypesList;
}

void McapReader::seekToTime(const std::chrono::nanoseconds &seekTime) {
    std::lock_guard<std::mutex> lock(readMutex_);
    if(seekTime > totalDuration_) {
        throw invalid_value_exception("Seek time is greater than total duration");
    }
    uint64_t targetNs = startTimeNs_ + seekTime.count();
    // Binary search for the first frame at or after targetNs
    auto it = std::lower_bound(frameEntries_.begin(), frameEntries_.end(), targetNs,
                               [](const FrameEntry &entry, uint64_t t) { return entry.logTime < t; });
    frameIndex_ = std::distance(frameEntries_.begin(), it);
}

void McapReader::stop() {
    std::lock_guard<std::mutex> lock(readMutex_);
    frameIndex_ = 0;
}

bool McapReader::getIsEndOfFile() {
    std::lock_guard<std::mutex> lock(readMutex_);
    return frameIndex_ >= frameEntries_.size();
}

std::shared_ptr<Frame> McapReader::createFrame(const std::string &topic, const mcap::Message &msg, const mcap::Channel &channel) {
    const auto &schemaName = channelSchemaNames_[channel.id];

    if(schemaName == "sensor_msgs/Image") {
        auto imageMsg = deserializeMessage<sensor_msgs::Image>(msg);
        return createVideoFrame(topic, imageMsg);
    }
    else if(schemaName == "sensor_msgs/Imu") {
        auto imuMsg = deserializeMessage<sensor_msgs::Imu>(msg);
        return createImuFrame(topic, imuMsg);
    }
    else if(schemaName == "sensor_msgs/PointCloud2") {
        auto pcMsg = deserializeMessage<sensor_msgs::PointCloud2>(msg);
        return createLiDARPointCloud(topic, pcMsg);
    }

    return nullptr;
}

std::shared_ptr<Frame> McapReader::readNextData() {
    std::lock_guard<std::mutex> lock(readMutex_);
    if(frameIndex_ >= frameEntries_.size()) {
        LOG_DEBUG("End of file reached");
        return nullptr;
    }

    auto &entry = frameEntries_[frameIndex_];
    frameIndex_++;

    // We need to find the channel for this entry
    mcap::Channel dummyChannel;
    dummyChannel.id = entry.channelId;
    // Look up the schema name
    return createFrame(entry.topic, entry.message, dummyChannel);
}

std::vector<std::shared_ptr<Frame>> McapReader::readLastDatas(const std::chrono::nanoseconds &startTime, const std::chrono::nanoseconds &endTime) {
    std::vector<std::shared_ptr<Frame>> result;
    uint64_t startNs = startTimeNs_ + startTime.count();
    uint64_t endNs   = startTimeNs_ + endTime.count();

    // Find the last frame for each topic in the time range
    std::map<std::string, size_t> lastFramePerTopic;
    for(size_t i = 0; i < frameEntries_.size(); i++) {
        auto &entry = frameEntries_[i];
        if(entry.logTime >= startNs && entry.logTime <= endNs) {
            lastFramePerTopic[entry.topic] = i;
        }
    }

    for(auto &[topic, idx]: lastFramePerTopic) {
        auto &entry = frameEntries_[idx];
        mcap::Channel dummyChannel;
        dummyChannel.id = entry.channelId;
        auto frame = createFrame(entry.topic, entry.message, dummyChannel);
        if(frame) {
            result.push_back(frame);
        }
    }
    return result;
}

std::shared_ptr<Frame> McapReader::createVideoFrame(const std::string &topic, std::shared_ptr<sensor_msgs::Image> imagePtr) {
    std::string videoMsgTopic = topic;
    auto frame = libobsensor::FrameFactory::createVideoFrameFromUserBuffer(
        RosTopic::getFrameTypeIdentifier(videoMsgTopic), convertStringToFormat(imagePtr->encoding),
        imagePtr->width, imagePtr->height, (uint8_t *)imagePtr->data.data(), imagePtr->data.size());

    frame->updateMetadata(imagePtr->metadata.data(), imagePtr->metadatasize);
    frame->setNumber(imagePtr->number);
    frame->setTimeStampUsec(imagePtr->timestamp_usec);
    frame->setSystemTimeStampUsec(imagePtr->timestamp_systemusec);
    frame->setGlobalTimeStampUsec(imagePtr->timestamp_globalusec);
    frame->as<VideoFrame>()->setPixelAvailableBitSize(imagePtr->pixel_bit_size);
    if(streamProfileList_.count(utils::mapFrameTypeToStreamType(RosTopic::getFrameTypeIdentifier(videoMsgTopic)))) {
        frame->setStreamProfile(streamProfileList_[utils::mapFrameTypeToStreamType(RosTopic::getFrameTypeIdentifier(videoMsgTopic))]);
    }
    return frame;
}

std::shared_ptr<Frame> McapReader::createImuFrame(const std::string &topic, std::shared_ptr<sensor_msgs::Imu> imuPtr) {
    std::string imuMsgTopic = topic;
    std::shared_ptr<Frame> frame;
    if(imuPtr->linear_acceleration.x != 0 && imuPtr->linear_acceleration.y != 0 && imuPtr->linear_acceleration.z != 0) {
        frame = FrameFactory::createFrameFromUserBuffer(OB_FRAME_ACCEL, OB_FORMAT_ACCEL,
                                                        const_cast<uint8_t *>(imuPtr->data.data()),
                                                        static_cast<size_t>(imuPtr->datasize));
    }
    else {
        frame = FrameFactory::createFrameFromUserBuffer(OB_FRAME_GYRO, OB_FORMAT_GYRO,
                                                        const_cast<uint8_t *>(imuPtr->data.data()),
                                                        static_cast<size_t>(imuPtr->datasize));
    }
    frame->setNumber(imuPtr->number);
    frame->setTimeStampUsec(imuPtr->timestamp_usec);
    frame->setSystemTimeStampUsec(imuPtr->timestamp_systemusec);
    frame->setGlobalTimeStampUsec(imuPtr->timestamp_globalusec);
    if(streamProfileList_.count(utils::mapFrameTypeToStreamType(RosTopic::getFrameTypeIdentifier(imuMsgTopic)))) {
        frame->setStreamProfile(streamProfileList_[utils::mapFrameTypeToStreamType(RosTopic::getFrameTypeIdentifier(imuMsgTopic))]);
    }
    return frame;
}

std::shared_ptr<Frame> McapReader::createLiDARPointCloud(const std::string &topic, std::shared_ptr<sensor_msgs::PointCloud2> framePtr) {
    std::string msgTopic   = topic;
    auto        streamType = utils::mapFrameTypeToStreamType(RosTopic::getFrameTypeIdentifier(msgTopic));
    auto        format     = convertStringToFormat(framePtr->format);

    if(streamType != OB_STREAM_LIDAR) {
        LOG_WARN("Invalid stream type, must be LiDAR stream here");
        return nullptr;
    }
    if(streamProfileList_.count(streamType) == 0) {
        LOG_WARN("Can't get profile");
        return nullptr;
    }
    auto sp = streamProfileList_[streamType];
    if(sp->getFormat() != format) {
        LOG_WARN("Invalid frame format");
        return nullptr;
    }
    auto frame = FrameFactory::createFrameFromStreamProfile(sp);
    frame->updateData(framePtr->data.data(), framePtr->data.size());
    frame->updateMetadata(framePtr->metadata.data(), framePtr->metadata.size());
    frame->setNumber(framePtr->number);
    frame->setTimeStampUsec(framePtr->timestamp_usec);
    frame->setSystemTimeStampUsec(framePtr->timestamp_systemusec);
    frame->setGlobalTimeStampUsec(framePtr->timestamp_globalusec);
    return frame;
}

std::chrono::nanoseconds McapReader::getCurTime() {
    std::lock_guard<std::mutex> lock(readMutex_);
    if(frameIndex_ >= frameEntries_.size()) {
        return getDuration();
    }
    return std::chrono::nanoseconds(frameEntries_[frameIndex_].logTime - startTimeNs_);
}

bool McapReader::isPropertySupported(uint32_t propertyId) const {
    return propertyList_.count(propertyId);
}

}  // namespace libobsensor
