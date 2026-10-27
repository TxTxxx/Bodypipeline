"""Subscription-only live quad preview. No publishers, service clients or SDK."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path
from urllib.request import urlopen


def has_h264_idr(payload: bytes) -> bool:
    """Return whether an Annex-B access unit contains an IDR NAL."""

    offset = 0
    while True:
        start = payload.find(b"\x00\x00\x01", offset)
        if start < 0:
            return False
        header = start + 3
        if header < len(payload) and payload[header] & 0x1F == 5:
            return True
        offset = header + 1


def preview_interval(max_fps: float) -> float:
    return 1.0 / min(30.0, max(0.5, float(max_fps)))


def main():
    import av
    import cv2
    import rclpy
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import CompressedImage

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--topic", default="/quad_tile/compressed_undistorted")
    parser.add_argument(
        "--status-url", default="http://127.0.0.1:8080/api/v1/record/status"
    )
    parser.add_argument(
        "--pause-during-recording",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--max-preview-fps", type=float, default=2.0)
    parser.add_argument("--max-tile-width", type=int, default=640)
    parser.add_argument("--jpeg-quality", type=int, default=70)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    if args.max_tile_width < 160:
        raise ValueError("--max-tile-width must be at least 160")
    if not 40 <= args.jpeg_quality <= 95:
        raise ValueError("--jpeg-quality must be between 40 and 95")
    pending, stamps = [], OrderedDict()
    lock = threading.Lock()
    stopping = threading.Event()
    paused = threading.Event()
    reset_decoder = threading.Event()
    reset_decoder.set()

    def new_codec():
        value = av.CodecContext.create("h264", "r")
        value.thread_count = 2
        return value

    codec = new_codec()
    rclpy.init()
    node = rclpy.create_node("bodypipeline_tianji_preview")

    def monitor_recording():
        while not stopping.wait(0.5):
            if not args.pause_during_recording:
                continue
            try:
                with urlopen(args.status_url, timeout=1.5) as response:
                    status = json.load(response)["data"]
                occupied = status.get("phase") in {
                    "starting",
                    "recording",
                    "saving",
                    "cancelling",
                }
            except Exception:
                continue
            if occupied and not paused.is_set():
                paused.set()
                reset_decoder.set()
                with lock:
                    pending.clear()
            elif not occupied and paused.is_set():
                paused.clear()
                reset_decoder.set()

    def receive(msg):
        try:
            if "h264" not in msg.format.lower():
                raise ValueError("Expected the verified H.264 quad topic")
            if paused.is_set():
                return
            ns = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
            stamps[ns] = time.time()
            while len(stamps) > 128:
                stamps.popitem(last=False)
            payload = bytes(msg.data)
            if reset_decoder.is_set():
                if not has_h264_idr(payload):
                    return
                nonlocal codec
                codec = new_codec()
                reset_decoder.clear()
            packet = av.Packet(payload)
            packet.pts = packet.dts = ns
            packet.time_base = Fraction(1, 10**9)
            for frame in codec.decode(packet):
                if frame.is_corrupt or frame.pts not in stamps:
                    continue
                with lock:
                    pending[:] = [(frame, stamps[frame.pts], frame.pts / 1e9)]
        except (ValueError, av.error.FFmpegError) as exc:
            node.get_logger().warning(str(exc))
            reset_decoder.set()

    node.create_subscription(
        CompressedImage,
        args.topic,
        receive,
        QoSProfile(
            depth=32,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        ),
    )

    def render():
        sequence = 0
        cameras = (
            ("head_left", 0, 0),
            ("head_right", 0, 1),
            ("left_wrist", 1, 0),
            ("right_wrist", 1, 1),
        )
        while not stopping.wait(preview_interval(args.max_preview_fps)):
            if paused.is_set():
                continue
            with lock:
                value = pending.pop() if pending else None
            if value is None:
                continue
            frame, received, stamp = value
            if not 0 <= time.time() - received <= 0.5:
                continue
            try:
                rgb = frame.to_ndarray(format="rgb24")
                height, width = rgb.shape[:2]
                if rgb.ndim != 3 or rgb.shape[2] != 3 or height % 2 or width % 2:
                    raise ValueError("Unexpected quad dimensions")
                half_h, half_w = height // 2, width // 2
                files = {}
                # Two slots plus atomic metadata prevent partially written JPEGs.
                for name, row, col in cameras:
                    tile = rgb[
                        row * half_h : (row + 1) * half_h,
                        col * half_w : (col + 1) * half_w,
                    ]
                    if tile.shape[1] > args.max_tile_width:
                        scale = args.max_tile_width / tile.shape[1]
                        tile = cv2.resize(
                            tile,
                            (
                                args.max_tile_width,
                                max(2, round(tile.shape[0] * scale)),
                            ),
                            interpolation=cv2.INTER_AREA,
                        )
                    ok, payload = cv2.imencode(
                        ".jpg",
                        cv2.cvtColor(tile, cv2.COLOR_RGB2BGR),
                        [cv2.IMWRITE_JPEG_QUALITY, args.jpeg_quality],
                    )
                    if not ok:
                        raise ValueError("JPEG encoder failed")
                    filename = f"{name}-{sequence % 2}.jpg"
                    temp = args.root / (filename + ".tmp")
                    temp.write_bytes(payload.tobytes())
                    os.replace(temp, args.root / filename)
                    files[name] = filename
                temp = args.root / "latest.tmp"
                temp.write_text(
                    json.dumps(
                        dict(
                            files=files,
                            source_time=stamp,
                            received_time=received,
                            tile_width=half_w,
                            tile_height=half_h,
                        )
                    )
                )
                os.replace(temp, args.root / "latest.json")
                sequence += 1
            except Exception as exc:
                node.get_logger().error(str(exc))

    worker = threading.Thread(target=render, daemon=True)
    worker.start()
    monitor = threading.Thread(target=monitor_recording, daemon=True)
    monitor.start()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        worker.join(timeout=3)
        monitor.join(timeout=2)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
