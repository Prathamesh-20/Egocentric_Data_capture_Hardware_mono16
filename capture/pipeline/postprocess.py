"""
Post-processing pipeline:
  1. bag → orbbec_color.mp4 + orbbec_depth.mp4
  2. All 4 feeds → combined 2x2 grid MP4
"""
import cv2, struct, logging
import numpy as np
from pathlib import Path

log = logging.getLogger(__name__)

# Import tile size from config
from capture.config import FPS, TILE_W, TILE_H

FONT = cv2.FONT_HERSHEY_SIMPLEX


# ── ROS image parsing ─────────────────────────────────────────────
def parse_ros_image(data):
    try:
        pos     = 4 + 4 + 4   # seq + sec + nsec
        fid_len = struct.unpack_from('<I', data, pos)[0]; pos += 4 + fid_len
        height  = struct.unpack_from('<I', data, pos)[0]; pos += 4
        width   = struct.unpack_from('<I', data, pos)[0]; pos += 4
        enc_len = struct.unpack_from('<I', data, pos)[0]; pos += 4
        enc     = data[pos:pos+enc_len].decode('utf-8', errors='ignore'); pos += enc_len
        pos    += 1 + 4        # is_bigendian + step
        dlen    = struct.unpack_from('<I', data, pos)[0]; pos += 4
        return width, height, enc, data[pos:pos+dlen]
    except:
        return None, None, None, None


# ── Orbbec RLE depth decoder (fast, numpy-vectorized) ────────────
def decode_orbbec_rle(rle_data: bytes, width: int, height: int) -> bytes:
    """
    Decode Orbbec RLE-compressed 16-bit depth data.
    Format: repeating [uint16 value][uint16 count] pairs.
    Uses numpy vectorized ops — ~20x faster than Python loop.
    """
    total_pixels = width * height

    # Parse all RLE pairs at once using numpy
    # Trim to multiple of 4 bytes
    usable = (len(rle_data) // 4) * 4
    if usable == 0:
        return np.zeros(total_pixels, dtype=np.uint16).tobytes()

    # View as uint16 array: [val0, cnt0, val1, cnt1, ...]
    pairs = np.frombuffer(rle_data[:usable], dtype=np.uint16)
    values = pairs[0::2]   # every even index = value
    counts = pairs[1::2]   # every odd index  = count

    # Use numpy.repeat to expand all runs at once
    output = np.repeat(values, counts)

    # Trim or pad to exact pixel count
    if len(output) >= total_pixels:
        output = output[:total_pixels]
    else:
        padded = np.zeros(total_pixels, dtype=np.uint16)
        padded[:len(output)] = output
        output = padded

    return output.tobytes()


# ── Depth colormap ────────────────────────────────────────────────
def depth_to_colormap(raw_bytes, w, h):
    """Per-frame 2-98th percentile — matches OrbbecViewer exactly."""
    arr   = np.frombuffer(raw_bytes, dtype=np.uint16).reshape((h, w))
    valid = arr[arr > 0]
    if len(valid) == 0:
        return np.zeros((h, w, 3), dtype=np.uint8)
    d_min = int(np.percentile(valid, 2))
    d_max = int(np.percentile(valid, 98))
    if d_max == d_min:
        return np.zeros((h, w, 3), dtype=np.uint8)
    clipped  = np.clip(arr, d_min, d_max).astype(np.float32)
    scaled   = (255 - ((clipped - d_min) / (d_max - d_min) * 255)).astype(np.uint8)
    colormap = cv2.applyColorMap(scaled, cv2.COLORMAP_JET)
    colormap[arr == 0] = 0
    return colormap


# ── Bag → MP4 ─────────────────────────────────────────────────────
def convert_bag(bag_path: str, color_out: str, depth_out: str,
                progress_cb=None) -> bool:
    """
    Convert Orbbec .bag → color MP4 + depth MP4.
    Handles RLE-encoded depth from Orbbec Gemini 2L.
    progress_cb(n_color, n_depth) called every 300 frames.
    """
    try:
        from rosbags.rosbag1 import Reader
    except ImportError:
        log.error("rosbags not installed — run: pip install rosbags --break-system-packages")
        return False

    log.info("Converting bag → MP4...")
    color_topic = depth_topic = None
    color_writer = depth_writer = None
    color_n = depth_n = 0

    try:
        with Reader(str(bag_path)) as reader:
            # Detect topics
            seen = set()
            for conn, ts, data in reader.messages():
                if conn.topic in seen or 'Image' not in conn.msgtype: continue
                seen.add(conn.topic)
                w, h, enc, _ = parse_ros_image(data)
                if enc is None: continue
                if enc.upper() in ('MJPG','MJPEG','RGB8','BGR8') and not color_topic:
                    color_topic = conn.topic
                    log.info(f"  Color : {conn.topic} ({enc})")
                elif enc.lower() in ('mono16','16uc1','y16','rle') and not depth_topic:
                    depth_topic = conn.topic
                    log.info(f"  Depth : {conn.topic} ({enc})")
                if color_topic and depth_topic: break

            if not color_topic and not depth_topic:
                log.error("No image topics found in bag"); return False

            for conn, ts, data in reader.messages():
                if conn.topic == color_topic:
                    w, h, enc, img = parse_ros_image(data)
                    if not img: continue
                    if enc.upper() in ('MJPG','MJPEG'):
                        frame = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
                    elif enc == 'rgb8':
                        frame = cv2.cvtColor(np.frombuffer(img, np.uint8).reshape(h,w,3), cv2.COLOR_RGB2BGR)
                    else:
                        frame = np.frombuffer(img, np.uint8).reshape(h,w,3)
                    if frame is None: continue
                    if color_writer is None:
                        fh, fw = frame.shape[:2]
                        color_writer = cv2.VideoWriter(color_out, cv2.VideoWriter_fourcc(*'mp4v'), FPS, (fw, fh))
                        log.info(f"  Color encoder: {fw}x{fh}")
                    color_writer.write(frame)
                    color_n += 1

                elif conn.topic == depth_topic:
                    w, h, enc, img = parse_ros_image(data)
                    if not img: continue

                    # Decode RLE-compressed depth if needed
                    if enc.lower() == 'rle':
                        raw_depth = decode_orbbec_rle(img, w, h)
                    else:
                        raw_depth = img

                    if len(raw_depth) != w * h * 2:
                        continue

                    colormap = depth_to_colormap(raw_depth, w, h)
                    if depth_writer is None:
                        depth_writer = cv2.VideoWriter(depth_out, cv2.VideoWriter_fourcc(*'mp4v'), FPS, (w, h))
                        log.info(f"  Depth encoder: {w}x{h} (source: {enc})")
                    depth_writer.write(colormap)
                    depth_n += 1

                if (color_n + depth_n) % 300 == 0 and (color_n + depth_n) > 0:
                    log.info(f"  Progress — Color: {color_n}  Depth: {depth_n}")
                    if progress_cb: progress_cb(color_n, depth_n)

    except Exception as e:
        log.error(f"Bag conversion error: {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        if color_writer: color_writer.release()
        if depth_writer: depth_writer.release()

    log.info(f"Bag conversion done — Color: {color_n}  Depth: {depth_n}")
    return color_n > 0 or depth_n > 0


# ── Combined 2x2 grid ─────────────────────────────────────────────
def to_tile(frame, label: str):
    if frame is None:
        tile = np.zeros((TILE_H, TILE_W, 3), dtype=np.uint8)
        cv2.putText(tile, "NO SIGNAL", (TILE_W//2-70, TILE_H//2),
                    FONT, 0.7, (80,80,80), 2)
    else:
        tile = cv2.resize(frame, (TILE_W, TILE_H))
    cv2.putText(tile, label, (5, TILE_H-8),  FONT, 0.45, (0,0,0),       2)
    cv2.putText(tile, label, (4, TILE_H-9),  FONT, 0.45, (200,200,200), 1)
    return tile


def make_combined(orbbec_color: str, orbbec_depth: str,
                  kreo1: str, kreo2: str,
                  out_path: str,
                  progress_cb=None) -> bool:
    """
    2x2 grid:
      TL: Orbbec Color  |  TR: Orbbec Depth
      BL: Kreo 1        |  BR: Kreo 2
    """
    log.info("Building 2x2 combined grid...")
    sources = [
        ('oc', 'Orbbec Color', orbbec_color),
        ('od', 'Orbbec Depth', orbbec_depth),
        ('k1', 'Kreo 1 (L)',   kreo1),
        ('k2', 'Kreo 2 (R)',   kreo2),
    ]

    caps = {}
    for key, label, path in sources:
        if path and Path(path).exists():
            cap = cv2.VideoCapture(path)
            if cap.isOpened():
                n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                caps[key] = (cap, label)
                log.info(f"  {label}: {n} frames")
            else:
                caps[key] = (None, label)
        else:
            caps[key] = (None, label)

    grid_w  = TILE_W * 2
    grid_h  = TILE_H * 2
    writer  = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*'mp4v'),
                              FPS, (grid_w, grid_h))
    frame_n = 0

    while True:
        tiles     = {}
        any_active = False
        for key, (cap, label) in caps.items():
            if cap is not None:
                ret, frame = cap.read()
                tiles[key] = (frame if ret else None, label)
                if ret: any_active = True
            else:
                tiles[key] = (None, label)

        if not any_active: break

        top  = np.hstack([to_tile(tiles['oc'][0], tiles['oc'][1]),
                          to_tile(tiles['od'][0], tiles['od'][1])])
        bot  = np.hstack([to_tile(tiles['k1'][0], tiles['k1'][1]),
                          to_tile(tiles['k2'][0], tiles['k2'][1])])
        grid = np.vstack([top, bot])

        cv2.line(grid, (TILE_W, 0),      (TILE_W, grid_h), (40,40,40), 2)
        cv2.line(grid, (0,      TILE_H), (grid_w, TILE_H), (40,40,40), 2)

        writer.write(grid)
        frame_n += 1
        if frame_n % 300 == 0:
            log.info(f"  Combined: {frame_n} frames written")
            if progress_cb: progress_cb(frame_n)

    for cap, _ in caps.values():
        if cap: cap.release()
    writer.release()
    log.info(f"Combined done — {frame_n} frames -> {out_path}")
    return frame_n > 0


# ── Bag → MCAP ───────────────────────────────────────────────────
import json, base64 as _b64

_COLOR_SCHEMA = json.dumps({"title":"foxglove.CompressedImage","$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","properties":{"timestamp":{"type":"object"},"frame_id":{"type":"string"},"data":{"type":"string","contentEncoding":"base64"},"format":{"type":"string"}}})
_DEPTH_SCHEMA = json.dumps({"title":"foxglove.RawImage","$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","properties":{"timestamp":{"type":"object"},"frame_id":{"type":"string"},"width":{"type":"integer"},"height":{"type":"integer"},"encoding":{"type":"string"},"step":{"type":"integer"},"data":{"type":"string","contentEncoding":"base64"}}})

def convert_bag_to_mcap(bag_path: str, mcap_out: str, progress_cb=None) -> bool:
    try:
        from rosbags.rosbag1 import Reader
        from mcap.writer import Writer as McapWriter
    except ImportError as e:
        log.error(f"Missing dependency: {e}"); return False

    log.info(f"Converting bag → MCAP: {mcap_out}")
    color_topic = depth_topic = None
    color_n = depth_n = 0

    try:
        with Reader(str(bag_path)) as reader, open(mcap_out, "wb") as f:
            writer = McapWriter(f)
            writer.start(profile="", library="egocentric-capture")
            color_schema_id = writer.register_schema(name="foxglove.CompressedImage", encoding="jsonschema", data=_COLOR_SCHEMA.encode())
            depth_schema_id = writer.register_schema(name="foxglove.RawImage", encoding="jsonschema", data=_DEPTH_SCHEMA.encode())
            color_ch_id = writer.register_channel(topic="/orbbec/color", message_encoding="json", schema_id=color_schema_id)
            depth_ch_id = writer.register_channel(topic="/orbbec/depth", message_encoding="json", schema_id=depth_schema_id)

            seen = set()
            for conn, ts, data in reader.messages():
                if conn.topic in seen or "Image" not in conn.msgtype: continue
                seen.add(conn.topic)
                w, h, enc, _ = parse_ros_image(data)
                if enc is None: continue
                if enc.upper() in ("MJPG","MJPEG","RGB8","BGR8") and not color_topic:
                    color_topic = conn.topic; log.info(f"  Color: {conn.topic} ({enc})")
                elif enc.lower() in ("mono16","16uc1","y16","rle") and not depth_topic:
                    depth_topic = conn.topic; log.info(f"  Depth: {conn.topic} ({enc})")
                if color_topic and depth_topic: break

            if not color_topic and not depth_topic:
                log.error("No image topics found in bag"); return False

            for conn, ts_ns, data in reader.messages():
                if conn.topic == color_topic:
                    w, h, enc, img = parse_ros_image(data)
                    if not img: continue
                    if enc.upper() not in ("MJPG","MJPEG"):
                        frame = cv2.cvtColor(np.frombuffer(img, np.uint8).reshape(h,w,3), cv2.COLOR_RGB2BGR)
                        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                        img = buf.tobytes()
                    msg = json.dumps({"timestamp":{"sec":ts_ns//1_000_000_000,"nsec":ts_ns%1_000_000_000},"frame_id":"orbbec_color","format":"jpeg","data":_b64.b64encode(img).decode()}).encode()
                    writer.add_message(channel_id=color_ch_id, log_time=ts_ns, data=msg, publish_time=ts_ns)
                    color_n += 1

                elif conn.topic == depth_topic:
                    w, h, enc, img = parse_ros_image(data)
                    if not img: continue

                    # Decode RLE if needed, then store as raw mono16
                    if enc.lower() == 'rle':
                        raw_depth = decode_orbbec_rle(img, w, h)
                    else:
                        raw_depth = img

                    if len(raw_depth) != w * h * 2:
                        continue

                    msg = json.dumps({"timestamp":{"sec":ts_ns//1_000_000_000,"nsec":ts_ns%1_000_000_000},"frame_id":"orbbec_depth","width":w,"height":h,"encoding":"mono16","step":w*2,"data":_b64.b64encode(raw_depth).decode()}).encode()
                    writer.add_message(channel_id=depth_ch_id, log_time=ts_ns, data=msg, publish_time=ts_ns)
                    depth_n += 1

                total = color_n + depth_n
                if total % 300 == 0 and total > 0:
                    log.info(f"  MCAP progress — Color: {color_n}  Depth: {depth_n}")
                    if progress_cb: progress_cb(color_n, depth_n)

            writer.finish()

    except Exception as e:
        log.error(f"MCAP conversion error: {e}")
        import traceback; traceback.print_exc()
        return False

    log.info(f"Bag → MCAP done — Color: {color_n}  Depth: {depth_n} -> {mcap_out}")
    return color_n > 0 or depth_n > 0
