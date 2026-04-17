// Copyright (c) Orbbec Inc. All Rights Reserved.
// Licensed under the MIT License.

#define MCAP_IMPLEMENTATION
#include "McapWriter.hpp"
#include "mcap/reader.hpp"  // Include reader here so MCAP_IMPLEMENTATION covers both writer and reader symbols

#include <cstdio>

namespace libobsensor {

const uint64_t MCAP_INVALID_DIFF = 6ULL * 60ULL * 60ULL * 1000000ULL;  // 6 hours

McapWriter::McapWriter(const std::string &file, bool compressWhileRecord)
    : filePath_(file), isOpen_(false), startTime_(0), sequenceCounter_(0), minFrameTime_(0), maxFrameTime_(0) {
    auto options = mcap::McapWriterOptions("ros1");
    options.library = "OrbbecSDK_v2-mcap";
    if(compressWhileRecord) {
        options.compression = mcap::Compression::Lz4;
    }
    else {
        options.compression = mcap::Compression::None;
    }

    auto status = writer_.open(filePath_, options);
    if(!status.ok()) {
        LOG_ERROR("Failed to open MCAP file {}: {}", filePath_, status.message);
        return;
    }
    isOpen_ = true;
}

McapWriter::~McapWriter() {
    stop(false);
}

void McapWriter::stop(bool hasError) {
    if(isOpen_) {
        writer_.close();
        isOpen_ = false;

        auto markFileAsError = [](const std::string &path) {
            const std::string errPath = path + "_error";
            if(std::rename(path.c_str(), errPath.c_str()) != 0) {
                LOG_ERROR("Failed to rename file {} -> {}", path, errPath);
            }
            else {
                LOG_ERROR("Recording failed, file renamed to: {}", errPath);
            }
        };

        if(hasError) {
            LOG_DEBUG("Error occurred during recording! file: {}", filePath_);
            markFileAsError(filePath_);
        }
        else if(maxFrameTime_ - minFrameTime_ >= MCAP_INVALID_DIFF) {
            LOG_DEBUG("Error timestamp data frames during recording! maxFrametime: {}, minFrameTime: {}, diff: {}", maxFrameTime_, minFrameTime_,
                      maxFrameTime_ - minFrameTime_);
            LOG_WARN("Error when saving MCAP file! There are abnormal timestamp data frames during recording!");
            markFileAsError(filePath_);
        }
    }
}

template <typename T>
mcap::ChannelId McapWriter::getOrCreateChannel(const std::string &topic) {
    auto channelIt = channelMap_.find(topic);
    if(channelIt != channelMap_.end()) {
        return channelIt->second;
    }

    // Get the ROS message type name and definition from message traits
    std::string dataType   = orbbecRosbag::message_traits::DataType<T>::value();
    std::string definition = orbbecRosbag::message_traits::Definition<T>::value();

    // Get or create the schema
    mcap::SchemaId schemaId;
    auto schemaIt = schemaMap_.find(dataType);
    if(schemaIt != schemaMap_.end()) {
        schemaId = schemaIt->second;
    }
    else {
        mcap::Schema schema(dataType, "ros1msg", definition);
        writer_.addSchema(schema);
        schemaId = schema.id;
        schemaMap_[dataType] = schemaId;
    }

    // Create the channel
    mcap::Channel channel(topic, "ros1", schemaId);
    writer_.addChannel(channel);
    channelMap_[topic] = channel.id;
    return channel.id;
}

template <typename T>
void McapWriter::writeMcapMessage(const std::string &topic, uint64_t timestampUsec, const T &msg) {
    if(!isOpen_) {
        return;
    }

    mcap::ChannelId channelId = getOrCreateChannel<T>(topic);

    // Serialize the ROS message to bytes
    uint32_t serLen = orbbecRosbag::serialization::serializationLength(msg);
    std::vector<uint8_t> buffer(serLen);
    orbbecRosbag::serialization::OStream stream(buffer.data(), serLen);
    orbbecRosbag::serialization::serialize(stream, msg);

    // Convert microseconds to nanoseconds for MCAP
    uint64_t timestampNs = timestampUsec * 1000ULL;

    mcap::Message mcapMsg;
    mcapMsg.channelId   = channelId;
    mcapMsg.sequence    = sequenceCounter_++;
    mcapMsg.logTime     = timestampNs;
    mcapMsg.publishTime = timestampNs;
    mcapMsg.data        = reinterpret_cast<const std::byte *>(buffer.data());
    mcapMsg.dataSize    = serLen;

    auto status = writer_.write(mcapMsg);
    if(!status.ok()) {
        LOG_WARN("Failed to write MCAP message on {}: {}", topic, status.message);
    }
}

void McapWriter::writeFrame(const OBSensorType &sensorType, std::shared_ptr<const Frame> curFrame) {
    std::lock_guard<std::mutex> lock(writeMutex_);
    auto                        curTime = curFrame->getTimeStampUsec();
    if(curTime == 0) {
        LOG_WARN("Invalid timestamp frame! curFrame device timestamp: {}", curFrame->getTimeStampUsec());
        return;
    }

    if(minFrameTime_ == 0 && maxFrameTime_ == 0) {
        minFrameTime_ = maxFrameTime_ = curTime;
    }
    else {
        minFrameTime_ = std::min(minFrameTime_, curTime);
        maxFrameTime_ = std::max(maxFrameTime_, curTime);
    }

    if(sensorType == OB_SENSOR_GYRO || sensorType == OB_SENSOR_ACCEL) {
        writeImuFrame(sensorType, curFrame);
    }
    else if(sensorType == OB_SENSOR_LIDAR) {
        writeLiDARFrame(curFrame);
    }
    else {
        writeVideoFrame(sensorType, curFrame);
    }
}

void McapWriter::writeVideoFrame(const OBSensorType &sensorType, std::shared_ptr<const Frame> curFrame) {
    if(startTime_ == 0) {
        startTime_ = curFrame->getTimeStampUsec();
    }
    auto imageTopic = RosTopic::frameDataTopic((uint8_t)sensorType, (uint8_t)curFrame->getType());
    streamProfileMap_.insert({ sensorType, curFrame->getStreamProfile() });
    try {
        sensor_msgs::ImagePtr imageMsg(new sensor_msgs::Image());
        std::chrono::duration<double, std::micro> timestampUs(curFrame->getTimeStampUsec());
        imageMsg->header.stamp = orbbecRosbag::Time(std::chrono::duration<double>(timestampUs).count());

        imageMsg->width                = curFrame->as<VideoFrame>()->getWidth();
        imageMsg->height               = curFrame->as<VideoFrame>()->getHeight();
        imageMsg->number               = curFrame->getNumber();
        imageMsg->timestamp_usec       = curFrame->getTimeStampUsec();
        imageMsg->timestamp_systemusec = curFrame->getSystemTimeStampUsec();
        imageMsg->timestamp_globalusec = curFrame->getGlobalTimeStampUsec();
        imageMsg->step                 = curFrame->as<VideoFrame>()->getStride();
        imageMsg->metadatasize         = static_cast<uint32_t>(curFrame->getMetadataSize());
        float bytesPerPixel            = 0.0f;
        if(utils::getBytesPerPixelNoexcept(curFrame->getFormat(), bytesPerPixel)) {
            imageMsg->pixel_bit_size = curFrame->as<VideoFrame>()->getPixelAvailableBitSize();
        }

        imageMsg->data.clear();
        imageMsg->encoding = convertFormatToString(curFrame->getFormat());
        imageMsg->metadata.insert(imageMsg->metadata.begin(), curFrame->getMetadata(), curFrame->getMetadata() + curFrame->getMetadataSize());
        imageMsg->data.insert(imageMsg->data.begin(), curFrame->getData(), curFrame->getData() + curFrame->getDataSize());

        writeMcapMessage<sensor_msgs::Image>(imageTopic, curFrame->getTimeStampUsec(), *imageMsg);
    }
    catch(const std::exception &e) {
        LOG_WARN("Write frame data exception! Message: {}", e.what());
    }
}

void McapWriter::writeImuFrame(const OBSensorType &sensorType, std::shared_ptr<const Frame> curFrame) {
    if(startTime_ == 0) {
        startTime_ = curFrame->getTimeStampUsec();
    }
    auto                                      imuTopic = RosTopic::imuDataTopic((uint8_t)sensorType, (uint8_t)curFrame->getType());
    std::chrono::duration<double, std::micro> timestampUs(curFrame->getTimeStampUsec());
    sensor_msgs::ImuPtr                       imuMsg(new sensor_msgs::Imu());
    imuMsg->header.stamp = orbbecRosbag::Time(std::chrono::duration<double>(timestampUs).count());
    streamProfileMap_.insert({ sensorType, curFrame->getStreamProfile() });
    if(sensorType == OB_SENSOR_ACCEL) {
        auto accelFrame               = curFrame->as<AccelFrame>();
        imuMsg->linear_acceleration.x = static_cast<double>(accelFrame->value().x);
        imuMsg->linear_acceleration.y = static_cast<double>(accelFrame->value().y);
        imuMsg->linear_acceleration.z = static_cast<double>(accelFrame->value().z);
        imuMsg->data.insert(imuMsg->data.begin(), accelFrame->getData(), accelFrame->getData() + accelFrame->getDataSize());
        imuMsg->datasize             = (uint32_t)accelFrame->getDataSize();
        imuMsg->number               = accelFrame->getNumber();
        imuMsg->temperature          = accelFrame->temperature();
        imuMsg->timestamp_usec       = accelFrame->getTimeStampUsec();
        imuMsg->timestamp_systemusec = accelFrame->getSystemTimeStampUsec();
        imuMsg->timestamp_globalusec = accelFrame->getGlobalTimeStampUsec();
        writeMcapMessage<sensor_msgs::Imu>(imuTopic, curFrame->getTimeStampUsec(), *imuMsg);
    }
    else {
        auto gyroFrame             = curFrame->as<GyroFrame>();
        imuMsg->angular_velocity.x = static_cast<double>(gyroFrame->value().x);
        imuMsg->angular_velocity.y = static_cast<double>(gyroFrame->value().y);
        imuMsg->angular_velocity.z = static_cast<double>(gyroFrame->value().z);
        imuMsg->data.insert(imuMsg->data.begin(), gyroFrame->getData(), gyroFrame->getData() + gyroFrame->getDataSize());
        imuMsg->datasize             = static_cast<uint32_t>(gyroFrame->getDataSize());
        imuMsg->number               = gyroFrame->getNumber();
        imuMsg->temperature          = gyroFrame->temperature();
        imuMsg->timestamp_usec       = gyroFrame->getTimeStampUsec();
        imuMsg->timestamp_systemusec = gyroFrame->getSystemTimeStampUsec();
        imuMsg->timestamp_globalusec = gyroFrame->getGlobalTimeStampUsec();
        writeMcapMessage<sensor_msgs::Imu>(imuTopic, curFrame->getTimeStampUsec(), *imuMsg);
    }
}

void McapWriter::writeLiDARFrame(std::shared_ptr<const Frame> curFrame) {
    if(startTime_ == 0) {
        startTime_ = curFrame->getTimeStampUsec();
    }
    auto                                      sensorType = OB_SENSOR_LIDAR;
    std::chrono::duration<double, std::micro> timestampUs(curFrame->getTimeStampUsec());
    auto                                      topic = RosTopic::frameDataTopic((uint8_t)sensorType, (uint8_t)curFrame->getType());
    streamProfileMap_.insert({ sensorType, curFrame->getStreamProfile() });

    auto makeFiled = [](sensor_msgs::PointCloud2::_fields_type &fields, const std::string &name, uint32_t offset, uint8_t dataType, uint32_t dataSize) {
        sensor_msgs::PointField field;
        field.name     = name;
        field.offset   = offset;
        field.datatype = dataType;
        field.count    = 1;
        fields.emplace_back(field);
        return dataSize + offset;
    };

    try {
        sensor_msgs::PointCloud2Ptr frameMsg(new sensor_msgs::PointCloud2());
        frameMsg->header.stamp = orbbecRosbag::Time(std::chrono::duration<double>(timestampUs).count());
        frameMsg->header.frame_id = "camera_link";

        auto     dataSize  = curFrame->getDataSize();
        auto     format    = curFrame->getFormat();
        auto     formatStr = convertFormatToString(format);
        uint32_t pointSize = 0;
        switch(format) {
        case OB_FORMAT_LIDAR_POINT: {
            pointSize       = sizeof(OBLiDARPoint);
            uint32_t offset = 0;
            frameMsg->fields.reserve(5);
            offset = makeFiled(frameMsg->fields, "x", offset, sensor_msgs::PointField::FLOAT32, 4);
            offset = makeFiled(frameMsg->fields, "y", offset, sensor_msgs::PointField::FLOAT32, 4);
            offset = makeFiled(frameMsg->fields, "z", offset, sensor_msgs::PointField::FLOAT32, 4);
            offset = makeFiled(frameMsg->fields, "intensity", offset, sensor_msgs::PointField::UINT8, 1);
            offset = makeFiled(frameMsg->fields, "tag", offset, sensor_msgs::PointField::UINT8, 1);
        } break;
        case OB_FORMAT_LIDAR_SPHERE_POINT: {
            pointSize       = sizeof(OBLiDARSpherePoint);
            uint32_t offset = 0;
            frameMsg->fields.reserve(5);
            offset = makeFiled(frameMsg->fields, "distance", offset, sensor_msgs::PointField::FLOAT32, 4);
            offset = makeFiled(frameMsg->fields, "theta", offset, sensor_msgs::PointField::FLOAT32, 4);
            offset = makeFiled(frameMsg->fields, "phi", offset, sensor_msgs::PointField::FLOAT32, 4);
            offset = makeFiled(frameMsg->fields, "intensity", offset, sensor_msgs::PointField::UINT8, 1);
            offset = makeFiled(frameMsg->fields, "tag", offset, sensor_msgs::PointField::UINT8, 1);
        } break;
        case OB_FORMAT_LIDAR_SCAN: {
            pointSize       = sizeof(OBLiDARScanPoint);
            uint32_t offset = 0;
            frameMsg->fields.reserve(3);
            offset = makeFiled(frameMsg->fields, "angle", offset, sensor_msgs::PointField::FLOAT32, 4);
            offset = makeFiled(frameMsg->fields, "distance", offset, sensor_msgs::PointField::FLOAT32, 4);
            offset = makeFiled(frameMsg->fields, "intensity", offset, sensor_msgs::PointField::UINT16, 2);
        } break;
        default:
            LOG_ERROR("Write LiDAR frame data error! Unsupported format: {}", formatStr.c_str());
            return;
        }

        frameMsg->height       = 1;
        frameMsg->width        = static_cast<uint32_t>(dataSize / pointSize);
        frameMsg->is_bigendian = false;
        frameMsg->point_step   = pointSize;
        frameMsg->row_step     = static_cast<uint32_t>(dataSize);
        frameMsg->data.resize(dataSize);
        std::copy(curFrame->getData(), curFrame->getData() + dataSize, frameMsg->data.begin());
        frameMsg->is_dense = false;
        frameMsg->format               = formatStr;
        frameMsg->number               = curFrame->getNumber();
        frameMsg->timestamp_usec       = curFrame->getTimeStampUsec();
        frameMsg->timestamp_systemusec = curFrame->getSystemTimeStampUsec();
        frameMsg->timestamp_globalusec = curFrame->getGlobalTimeStampUsec();
        frameMsg->metadata.resize(curFrame->getMetadataSize());
        std::copy(curFrame->getMetadata(), curFrame->getMetadata() + curFrame->getMetadataSize(), frameMsg->metadata.begin());

        writeMcapMessage<sensor_msgs::PointCloud2>(topic, curFrame->getTimeStampUsec(), *frameMsg);
    }
    catch(const std::exception &e) {
        LOG_WARN("Write LiDAR frame data exception! Message: {}", e.what());
    }
}

void McapWriter::writeProperty(uint32_t propertyID, const uint8_t *data, const uint32_t datasize) {
    std::lock_guard<std::mutex> lock(writeMutex_);
    auto                        now            = std::chrono::system_clock::now();
    auto                        timestamp_usec = std::chrono::duration_cast<std::chrono::microseconds>(now.time_since_epoch()).count();
    auto                        propertyTopic  = RosTopic::propertyTopic();
    custom_msg::propertyPtr     propertyMsg(new custom_msg::property());
    uint32_t                    sec  = timestamp_usec / 1000000;
    uint32_t                    nsec = (timestamp_usec % 1000000) * 1000;
    propertyMsg->header.stamp = orbbecRosbag::Time(sec, nsec);
    propertyMsg->propertyId   = propertyID;
    propertyMsg->data.assign(data, data + datasize);
    propertyMsg->datasize = datasize;

    writeMcapMessage<custom_msg::property>(propertyTopic, timestamp_usec, *propertyMsg);
}

void McapWriter::writeDeviceInfo(const std::shared_ptr<const DeviceInfo> &deviceInfo) {
    std::lock_guard<std::mutex> lock(writeMutex_);
    if(startTime_ == 0) {
        return;
    }
    custom_msg::DeviceInfoPtr                 deviceInfoMsg(new custom_msg::DeviceInfo());
    std::chrono::duration<double, std::micro> timestampUs(startTime_);
    deviceInfoMsg->header.stamp        = orbbecRosbag::Time(std::chrono::duration<double>(timestampUs).count());
    auto deviceInfoTopic               = RosTopic::deviceTopic();
    deviceInfoMsg->name                = deviceInfo->name_;
    deviceInfoMsg->fullName            = deviceInfo->fullName_;
    deviceInfoMsg->asicName            = deviceInfo->asicName_;
    deviceInfoMsg->vid                 = deviceInfo->vid_;
    deviceInfoMsg->pid                 = deviceInfo->pid_;
    deviceInfoMsg->uid                 = deviceInfo->uid_;
    deviceInfoMsg->sn                  = deviceInfo->deviceSn_;
    deviceInfoMsg->fwVersion           = deviceInfo->fwVersion_;
    deviceInfoMsg->hwVersion           = deviceInfo->hwVersion_;
    deviceInfoMsg->supportedSdkVersion = deviceInfo->supportedSdkVersion_;
    deviceInfoMsg->connectionType      = deviceInfo->connectionType_;
    deviceInfoMsg->type                = deviceInfo->type_;
    deviceInfoMsg->backendType         = deviceInfo->backendType_;

    if(deviceInfo->connectionType_ == "Ethernet") {
        auto netDeviceInfo       = std::static_pointer_cast<const NetDeviceInfo>(deviceInfo);
        deviceInfoMsg->ipAddress = netDeviceInfo->ipAddress_;
        deviceInfoMsg->localMac  = netDeviceInfo->localMac_;
    }

    writeMcapMessage<custom_msg::DeviceInfo>(deviceInfoTopic, startTime_, *deviceInfoMsg);
}

void McapWriter::writeStreamProfiles() {
    std::lock_guard<std::mutex> lock(writeMutex_);
    for(auto &it: streamProfileMap_) {
        if(it.first == OB_SENSOR_GYRO) {
            writeGyroStreamProfile(it.second);
        }
        else if(it.first == OB_SENSOR_ACCEL) {
            writeAccelStreamProfile(it.second);
        }
        else if(it.first == OB_SENSOR_LIDAR) {
            writeLiDARStreamProfile(it.second);
        }
        else {
            writeVideoStreamProfile(it.first, it.second);
        }
    }
}

void McapWriter::writeDisparityParam(std::shared_ptr<const DisparityBasedStreamProfile> disparityParam) {
    custom_msg::DisparityParamPtr             disparityParamMsg(new custom_msg::DisparityParam());
    std::chrono::duration<double, std::micro> timestampUs(startTime_);
    disparityParamMsg->header.stamp = orbbecRosbag::Time(std::chrono::duration<double>(timestampUs).count());
    auto disparityParamTopic        = RosTopic::disparityParmTopic();
    auto tmpDisparityParam          = disparityParam->getDisparityParam();
    disparityParamMsg->zpd          = tmpDisparityParam.zpd;
    disparityParamMsg->zpps         = tmpDisparityParam.zpps;
    disparityParamMsg->baseline     = tmpDisparityParam.baseline;
    disparityParamMsg->fx           = tmpDisparityParam.fx;
    disparityParamMsg->bitSize      = tmpDisparityParam.bitSize;
    disparityParamMsg->unit         = tmpDisparityParam.unit;
    disparityParamMsg->minDisparity = tmpDisparityParam.minDisparity;
    disparityParamMsg->packMode     = tmpDisparityParam.packMode;
    disparityParamMsg->dispOffset   = tmpDisparityParam.dispOffset;
    disparityParamMsg->invalidDisp  = tmpDisparityParam.invalidDisp;
    disparityParamMsg->dispIntPlace = tmpDisparityParam.dispIntPlace;
    disparityParamMsg->isDualCamera = tmpDisparityParam.isDualCamera;

    writeMcapMessage<custom_msg::DisparityParam>(disparityParamTopic, startTime_, *disparityParamMsg);
}

void McapWriter::writeVideoStreamProfile(const OBSensorType sensorType, const std::shared_ptr<const StreamProfile> &streamProfile) {
    custom_msg::StreamProfileInfoPtr          streamInfoMsg(new custom_msg::StreamProfileInfo());
    std::chrono::duration<double, std::micro> timestampUs(startTime_);
    streamInfoMsg->header.stamp = orbbecRosbag::Time(std::chrono::duration<double>(timestampUs).count());
    auto strStreamProfile       = RosTopic::streamProfileTopic((uint8_t)streamProfile->getType());
    auto videoStreamProfile     = streamProfile->as<VideoStreamProfile>();
    if(videoStreamProfile != nullptr) {
        if(videoStreamProfile->is<const DisparityBasedStreamProfile>()) {
            writeDisparityParam(videoStreamProfile->as<const DisparityBasedStreamProfile>());
        }

        auto                 spDistortion = videoStreamProfile->getDistortion();
        std::array<float, 8> distortion   = { spDistortion.k1, spDistortion.k2, spDistortion.k3, spDistortion.k4,
                                              spDistortion.k5, spDistortion.k6, spDistortion.p1, spDistortion.p2 };
        streamInfoMsg->cameraDistortion = distortion;
        streamInfoMsg->distortionModel  = static_cast<uint8_t>(spDistortion.model);

        auto spIntrinsic                  = videoStreamProfile->getIntrinsic();
        streamInfoMsg->cameraIntrinsic[0] = spIntrinsic.fx;
        streamInfoMsg->cameraIntrinsic[1] = spIntrinsic.fy;
        streamInfoMsg->cameraIntrinsic[2] = spIntrinsic.cx;
        streamInfoMsg->cameraIntrinsic[3] = spIntrinsic.cy;
        streamInfoMsg->width              = videoStreamProfile->getWidth();
        streamInfoMsg->height             = videoStreamProfile->getHeight();
        streamInfoMsg->fps                = videoStreamProfile->getFps();
        streamInfoMsg->streamType         = static_cast<uint8_t>(videoStreamProfile->getType());
        streamInfoMsg->format             = static_cast<uint8_t>(videoStreamProfile->getFormat());

        if(sensorType == OB_SENSOR_DEPTH && streamProfileMap_.count(OB_SENSOR_COLOR) && streamProfileMap_.count(OB_SENSOR_DEPTH)) {
            auto extrinsic = streamProfile->getExtrinsicTo(streamProfileMap_.at(OB_SENSOR_COLOR));
            for(int i = 0; i < 9; i++) {
                streamInfoMsg->rotationMatrix[i] = extrinsic.rot[i];
            }
            for(int i = 0; i < 3; i++) {
                streamInfoMsg->translationMatrix[i] = extrinsic.trans[i];
            }
        }

        writeMcapMessage<custom_msg::StreamProfileInfo>(strStreamProfile, startTime_, *streamInfoMsg);
    }
}

void McapWriter::writeAccelStreamProfile(const std::shared_ptr<const StreamProfile> &streamProfile) {
    custom_msg::ImuStreamProfileInfoPtr       accelStreamInfoMsg(new custom_msg::ImuStreamProfileInfo());
    std::chrono::duration<double, std::micro> timestampUs(startTime_);
    accelStreamInfoMsg->header.stamp = orbbecRosbag::Time(std::chrono::duration<double>(timestampUs).count());
    auto strStreamProfile            = RosTopic::streamProfileTopic((uint8_t)streamProfile->getType());
    auto accelStreamProfile          = streamProfile->as<AccelStreamProfile>();

    accelStreamInfoMsg->format              = static_cast<OBFormat>(accelStreamProfile->getFormat());
    accelStreamInfoMsg->streamType          = static_cast<OBStreamType>(accelStreamProfile->getType());
    accelStreamInfoMsg->accelFullScaleRange = static_cast<OBAccelFullScaleRange>(accelStreamProfile->getFullScaleRange());
    accelStreamInfoMsg->accelSampleRate     = static_cast<OBAccelSampleRate>(accelStreamProfile->getSampleRate());
    accelStreamInfoMsg->noiseDensity        = accelStreamProfile->getIntrinsic().noiseDensity;
    accelStreamInfoMsg->referenceTemp       = accelStreamProfile->getIntrinsic().randomWalk;
    accelStreamInfoMsg->referenceTemp       = accelStreamProfile->getIntrinsic().referenceTemp;
    for(int i = 0; i < 3; i++) {
        accelStreamInfoMsg->bias[i] = accelStreamProfile->getIntrinsic().bias[i];
    }
    for(int i = 0; i < 3; i++) {
        accelStreamInfoMsg->gravity[i] = accelStreamProfile->getIntrinsic().gravity[i];
    }
    for(int i = 0; i < 9; i++) {
        accelStreamInfoMsg->scaleMisalignment[i] = accelStreamProfile->getIntrinsic().scaleMisalignment[i];
    }
    for(int i = 0; i < 9; i++) {
        accelStreamInfoMsg->tempSlope[i] = accelStreamProfile->getIntrinsic().tempSlope[i];
    }
    OBExtrinsic extrinsic;
    if(streamProfileMap_.count(OB_SENSOR_DEPTH)) {
        extrinsic = streamProfile->getExtrinsicTo(streamProfileMap_.at(OB_SENSOR_DEPTH));
        for(int i = 0; i < 9; i++) {
            accelStreamInfoMsg->rotationMatrix[i] = extrinsic.rot[i];
        }
        for(int i = 0; i < 3; i++) {
            accelStreamInfoMsg->translationMatrix[i] = extrinsic.trans[i];
        }
    }
    writeMcapMessage<custom_msg::ImuStreamProfileInfo>(strStreamProfile, startTime_, *accelStreamInfoMsg);
}

void McapWriter::writeGyroStreamProfile(const std::shared_ptr<const StreamProfile> &streamProfile) {
    custom_msg::ImuStreamProfileInfoPtr       gyroStreamInfoMsg(new custom_msg::ImuStreamProfileInfo());
    std::chrono::duration<double, std::micro> timestampUs(startTime_);
    gyroStreamInfoMsg->header.stamp = orbbecRosbag::Time(std::chrono::duration<double>(timestampUs).count());
    auto strStreamProfile           = RosTopic::streamProfileTopic((uint8_t)streamProfile->getType());
    auto gyroStreamProfile          = streamProfile->as<GyroStreamProfile>();

    gyroStreamInfoMsg->format             = static_cast<OBFormat>(gyroStreamProfile->getFormat());
    gyroStreamInfoMsg->streamType         = static_cast<OBStreamType>(gyroStreamProfile->getType());
    gyroStreamInfoMsg->gyroFullScaleRange = static_cast<OBGyroFullScaleRange>(gyroStreamProfile->getFullScaleRange());
    gyroStreamInfoMsg->gyroSampleRate     = static_cast<OBGyroSampleRate>(gyroStreamProfile->getSampleRate());
    gyroStreamInfoMsg->noiseDensity       = gyroStreamProfile->getIntrinsic().noiseDensity;
    gyroStreamInfoMsg->referenceTemp      = gyroStreamProfile->getIntrinsic().randomWalk;
    gyroStreamInfoMsg->referenceTemp      = gyroStreamProfile->getIntrinsic().referenceTemp;
    for(int i = 0; i < 3; i++) {
        gyroStreamInfoMsg->bias[i] = gyroStreamProfile->getIntrinsic().bias[i];
    }
    for(int i = 0; i < 9; i++) {
        gyroStreamInfoMsg->scaleMisalignment[i] = gyroStreamProfile->getIntrinsic().scaleMisalignment[i];
    }
    for(int i = 0; i < 9; i++) {
        gyroStreamInfoMsg->tempSlope[i] = gyroStreamProfile->getIntrinsic().tempSlope[i];
    }
    writeMcapMessage<custom_msg::ImuStreamProfileInfo>(strStreamProfile, startTime_, *gyroStreamInfoMsg);
}

void McapWriter::writeLiDARStreamProfile(const std::shared_ptr<const StreamProfile> &streamProfile) {
    custom_msg::LiDARStreamProfileInfoPtr     streamInfoMsg(new custom_msg::LiDARStreamProfileInfo());
    std::chrono::duration<double, std::micro> timestampUs(startTime_);
    streamInfoMsg->header.stamp = orbbecRosbag::Time(std::chrono::duration<double>(timestampUs).count());
    auto strStreamProfile       = RosTopic::streamProfileTopic((uint8_t)streamProfile->getType());
    auto lidarProfile           = streamProfile->as<LiDARStreamProfile>();

    streamInfoMsg->format     = static_cast<OBFormat>(lidarProfile->getFormat());
    streamInfoMsg->streamType = static_cast<OBStreamType>(lidarProfile->getType());
    streamInfoMsg->scanRate   = static_cast<OBLiDARScanRate>(lidarProfile->getScanRate());
    writeMcapMessage<custom_msg::LiDARStreamProfileInfo>(strStreamProfile, startTime_, *streamInfoMsg);
}

}  // namespace libobsensor
