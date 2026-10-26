"""Encode a selected closed episode to stdout; never run during capture saving."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from .adapters.registry import get_capture_backend


def encode(spec):
    os.nice(5)
    backend = get_capture_backend(
        SimpleNamespace(mode=spec["mode"], capture_backend=spec["backend"]),
        spec["profile"],
    )
    source = backend.video_source(Path(spec["source"]), spec["profile"])
    try:
        first = source.frame(0)
        height = min(480, max(image.height for image in first))
        height += height % 2
        widths = [max(2, int(image.width * height / image.height) // 2 * 2) for image in first]
        size = (sum(widths), height + 24)
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "rawvideo", "-pixel_format", "rgb24",
            "-video_size", f"{size[0]}x{size[1]}", "-framerate", str(source.fps),
            "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-threads", "1", "-pix_fmt", "yuv420p",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=sys.stdout.buffer,
                              stderr=sys.stderr.buffer) as encoder:
            try:
                # Preserve wall duration at nominal FPS. Gaps hold the last real
                # image in this viewing copy; source HDF5/raw remain untouched.
                count = max(1, math.ceil(float(source.times[-1]) * source.fps - 1e-7) + 1)
                last_index, pixels = None, None
                for index in range(count):
                    at = index / source.fps
                    selected = max(0, int(np.searchsorted(source.times, at, side="right")) - 1)
                    if selected != last_index:
                        images = first if selected == 0 else source.frame(selected)
                        canvas = Image.new("RGB", size, "black")
                        draw = ImageDraw.Draw(canvas)
                        left = 0
                        for camera, image, width in zip(source.camera_ids, images, widths):
                            fitted = ImageOps.contain(image, (width, height))
                            canvas.paste(fitted, (left + (width - fitted.width) // 2,
                                                  24 + (height - fitted.height) // 2))
                            draw.text((left + 8, 5), camera, fill="white")
                            left += width
                        pixels = canvas.tobytes()
                        last_index = selected
                    encoder.stdin.write(pixels)
                encoder.stdin.close()
                if encoder.wait():
                    raise RuntimeError("视频编码失败")
            finally:
                if encoder.poll() is None:
                    encoder.terminate()
                    encoder.wait(timeout=5)
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    encode(json.loads(args.spec))


if __name__ == "__main__":
    main()
