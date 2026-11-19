"""Encoded image batch I/O must preserve byte payloads and selection semantics."""

import h5py
import numpy as np
import pytest

from pipeline.server.providers.hdf5_images import (
    IMAGE_BATCH_FRAMES,
    copy_images,
    image_batches,
    inspect_images,
)


@pytest.mark.parametrize("layout", ["fixed", "vlen_equal", "vlen_ragged", "bytes"])
@pytest.mark.parametrize("selection", ["all", "repeated", "sparse", "one", "empty"])
def test_copy_preserves_every_byte(tmp_path, layout, selection):
    count = IMAGE_BATCH_FRAMES * 5 + 1
    payloads = [
        np.arange(10 + (i % 7 if layout == "vlen_ragged" else 0), dtype=np.uint8)
        + i % 128
        for i in range(count)
    ]
    indices = {
        "all": None,
        "repeated": np.repeat(np.arange(3, 45), 3),
        "sparse": np.array([120, 1, 120, 7, 159, 1]),
        "one": np.array([4]),
        "empty": np.array([], dtype=int),
    }[selection]
    expected = np.arange(count) if indices is None else indices
    with h5py.File(tmp_path / "images.hdf5", "w") as handle:
        if layout == "fixed":
            source = handle.create_dataset("source", data=np.stack(payloads))
        elif layout == "bytes":
            # HDF5 vlen byte strings cannot contain NUL.
            payloads = [p + 1 for p in payloads]
            source = handle.create_dataset(
                "source", (count,), dtype=h5py.special_dtype(vlen=bytes)
            )
            source[:] = [p.tobytes() for p in payloads]
        else:
            source = handle.create_dataset(
                "source", (count,), dtype=h5py.vlen_dtype(np.dtype("uint8"))
            )
            for i, payload in enumerate(payloads):
                source[i] = payload
        destination = handle.create_dataset(
            "output", (len(expected),), dtype=h5py.vlen_dtype(np.dtype("uint8"))
        )
        copy_images(source, destination, indices)
        for output_index, source_index in enumerate(expected):
            assert (
                destination[output_index].tobytes() == payloads[source_index].tobytes()
            )
        # The source is never mutated.
        for i, payload in enumerate(payloads):
            actual = source[i]
            assert (
                actual.tobytes() if isinstance(actual, np.ndarray) else actual
            ) == payload.tobytes()


def test_batches_bound_reads_and_restore_repeated_indices():
    class Dataset:
        def __init__(self):
            self.reads = []
            self.rows = np.arange(200 * 4, dtype=np.uint8).reshape(200, 4)

        def __len__(self):
            return len(self.rows)

        def __getitem__(self, key):
            self.reads.append(key)
            return self.rows[key]

    source = Dataset()
    indices = np.repeat(np.arange(50), 3)
    batches = list(image_batches(source, indices))
    assert (
        len(source.reads)
        == (len(indices) + IMAGE_BATCH_FRAMES - 1) // IMAGE_BATCH_FRAMES
    )
    assert all(len(rows) <= IMAGE_BATCH_FRAMES for _, rows in batches)
    assert all(
        isinstance(key, slice) and key.stop - key.start <= IMAGE_BATCH_FRAMES * 2
        for key in source.reads
    )
    np.testing.assert_array_equal(
        np.concatenate([rows for _, rows in batches]), source.rows[indices]
    )


@pytest.mark.parametrize("cpus", [1, 4])
def test_inspection_checks_every_frame_in_order_without_threaded_hdf5(
    monkeypatch, cpus
):
    import threading

    import pipeline.server.providers.hdf5_images as module

    monkeypatch.setattr(
        module.os, "sched_getaffinity", lambda _: set(range(cpus)), raising=False
    )
    caller = threading.get_ident()

    class Dataset:
        def __len__(self):
            return 70

        def __getitem__(self, key):
            assert threading.get_ident() == caller
            return np.arange(70)[key]

    assert list(inspect_images(Dataset(), lambda row: int(row))) == list(range(70))

    def inspect_frame(row):
        if row == 65:
            raise ValueError("late corrupt image")
        return int(row)

    inspected = inspect_images(Dataset(), inspect_frame)
    assert [next(inspected) for _ in range(65)] == list(range(65))
    with pytest.raises(ValueError, match="late corrupt image"):
        next(inspected)


@pytest.mark.parametrize("corruption", ["decode", "size", "encoding"])
def test_late_image_errors_are_not_hidden_by_batching(tmp_path, corruption):
    import io

    from PIL import Image

    from pipeline.server.episode_validation import EpisodeValidationError
    from pipeline.server.providers.arx_kinesthetic import _image_report

    def encoded(size=(32, 24), format="JPEG"):
        buffer = io.BytesIO()
        Image.new("RGB", size).save(buffer, format=format)
        return np.frombuffer(buffer.getvalue(), dtype=np.uint8)

    with h5py.File(tmp_path / "bad.hdf5", "w") as handle:
        images = handle.create_dataset(
            "images", (70,), dtype=h5py.vlen_dtype(np.dtype("uint8"))
        )
        images.attrs["encoded_format"] = "jpeg"
        for i in range(70):
            images[i] = encoded()
        images[65] = {
            "decode": np.array([1, 2, 3], dtype=np.uint8),
            "size": encoded((16, 16)),
            "encoding": encoded(format="PNG"),
        }[corruption]
        with pytest.raises(EpisodeValidationError):
            _image_report(images, "head", 70)
