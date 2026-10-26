"""Shared encoded-image reader; capture bindings declare their own layouts."""

from __future__ import annotations

import io

import h5py
import numpy as np
from PIL import Image

from pipeline.shared.core import DomainError


class Hdf5VideoSource:
    def __init__(self, path, profile, image_path, clocks):
        self.handle = h5py.File(path, "r")
        try:
            if not self.handle.attrs.get("complete"):
                raise DomainError("该片段尚未完整保存")
            self.camera_ids = [camera["id"] for camera in profile["cameras"]]
            self.images = {
                camera: self.handle[image_path.format(camera=camera)]
                for camera in self.camera_ids
            }
            clock, scale = next((key, scale) for key, scale in clocks if key in self.handle)
            values = np.asarray(self.handle[clock])
            # Subtract integer source clocks before conversion to preserve precision.
            self.times = (values - values[0]).astype(np.float64) * scale
            self.fps = float(self.handle.attrs.get("fps", profile["output_fps"]))
            if (not len(self.times) or not np.isfinite(self.times).all()
                    or np.any(np.diff(self.times) <= 0)
                    or not 0 < self.fps <= 240
                    or any(len(images) != len(self.times) for images in self.images.values())):
                raise DomainError("视频图像与时间轴不一致")
        except Exception:
            self.handle.close()
            raise

    def frame(self, index):
        result = []
        for camera in self.camera_ids:
            payload = np.asarray(self.images[camera][index], dtype=np.uint8).tobytes()
            with Image.open(io.BytesIO(payload)) as image:
                result.append(image.convert("RGB"))
        return result

    def close(self):
        self.handle.close()
