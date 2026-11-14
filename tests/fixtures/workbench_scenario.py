"""Numbered, two-camera, multi-Episode LeRobot fixture; no real recordings."""

import io
import sys
from pathlib import Path

import h5py
import numpy as np
from letools import (
    ConversionConfig,
    HDF5Mapping,
    HDF5NumericField,
    HDF5Source,
    HDF5VideoField,
    convert,
)
from PIL import Image, ImageDraw, ImageFont


def create(root, version="v3.0", count=12):
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    keys = ("observation.images.front", "observation.images.wrist")
    mapping = HDF5Mapping(
        fps=12,
        numeric_fields=(
            HDF5NumericField("state", "observation.state"),
            HDF5NumericField("action", "action"),
        ),
        video_fields=tuple(
            HDF5VideoField(f"images/{i}", k, width=320, height=180)
            for i, k in enumerate(keys)
        ),
        task_key="task",
        robot_type="synthetic_arm",
    )
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    try:
        font = ImageFont.truetype(font_path, 34)
    except OSError:
        font = ImageFont.load_default(size=34)
    for ep in range(count):
        with h5py.File(raw / f"episode_{ep:06d}.hdf5", "w") as f:
            state = np.arange(48 * 3, dtype=np.float32).reshape(48, 3) / 100 + ep
            f.create_dataset("state", data=state)
            f.create_dataset("action", data=state + 0.05)
            f.create_dataset(
                "task", data="synthetic numbered grasp trial", dtype=h5py.string_dtype()
            )
            for camera in range(2):
                images = f.create_dataset(
                    f"images/{camera}",
                    shape=(48,),
                    dtype=h5py.vlen_dtype(np.dtype("uint8")),
                )
                for frame in range(48):
                    image = Image.new(
                        "RGB",
                        (320, 180),
                        (25 + camera * 35, 45 + ep * 4, 70 + frame * 2),
                    )
                    draw = ImageDraw.Draw(image)
                    draw.text(
                        (15, 12),
                        f"EP {ep:02d}  CAM {camera}",
                        font=ImageFont.load_default(size=18),
                        fill="white",
                    )
                    draw.text((15, 58), f"FRAME {frame:02d}", font=font, fill="white")
                    draw.rectangle(
                        (15, 125, 15 + frame * 6, 145), fill=(110 + frame * 2, 180, 160)
                    )
                    draw.text(
                        (15, 155), "SYNTHETIC / CPU FIXTURE", fill=(160, 190, 220)
                    )
                    data = io.BytesIO()
                    image.save(data, format="JPEG", quality=95)
                    images[frame] = np.frombuffer(data.getvalue(), dtype=np.uint8)
    destination = root / "dataset"
    convert(
        HDF5Source(raw, mapping),
        destination,
        version,
        config=ConversionConfig(workers=1, video_workers=1),
    )
    return destination


if __name__ == "__main__":
    create(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "v3.0")
