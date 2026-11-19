"""Bounded HDF5 image I/O, without decoding or changing encoded payloads."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

# Keep only a small window of encoded images in memory, not an entire episode.
IMAGE_BATCH_FRAMES = 32


def inspect_images(dataset, inspect_frame):
    """Inspect every frame in order, decoding outside h5py's global lock.

    Only encoded NumPy rows cross into threads, never HDF5 handles. Limit both
    queued payloads and decoder concurrency; keep errors in source-frame order.
    """
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        cpus = os.cpu_count() or 1
    workers = max(1, min(3, cpus))
    if workers == 1:
        for _, rows in image_batches(dataset):
            yield from map(inspect_frame, rows)
        return
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="image-check"
    ) as pool:
        for _, rows in image_batches(dataset):
            yield from pool.map(inspect_frame, rows)


def image_batches(dataset, indices=None):
    """Yield output offsets and encoded rows, preserving repeats and order.

    HDF5 fancy indexing requires sorted, unique indices. Read each selected
    source row once per batch, then apply the inverse map in NumPy. Nearby
    selections use a hyperslab instead of expensive HDF5 point selections.
    Both fixed-width padded byte arrays and variable-length arrays are valid.
    """
    count = len(dataset) if indices is None else len(indices)
    for start in range(0, count, IMAGE_BATCH_FRAMES):
        stop = min(count, start + IMAGE_BATCH_FRAMES)
        if indices is None:
            yield start, dataset[start:stop]
            continue
        selected, inverse = np.unique(indices[start:stop], return_inverse=True)
        lower, upper = int(selected[0]), int(selected[-1]) + 1
        if upper - lower <= IMAGE_BATCH_FRAMES * 2:
            rows = dataset[lower:upper][selected - lower]
        else:
            rows = dataset[selected.tolist()]
        yield start, rows[inverse]


def copy_images(source, destination, indices=None):
    """Copy exact encoded bytes using bounded batch reads and writes."""
    for start, rows in image_batches(source, indices):
        if rows.ndim == 1:
            converted = np.empty(len(rows), dtype=object)
            for index, row in enumerate(rows):
                converted[index] = (
                    np.asarray(row, dtype=np.uint8)
                    if isinstance(row, np.ndarray)
                    else np.frombuffer(bytes(row), dtype=np.uint8)
                )
            rows = converted
            if len({len(row) for row in rows}) == 1:
                # h5py misinterprets equal-length object arrays as a 2-D object
                # matrix. A uint8 matrix also handles one-row batches.
                rows = np.stack(rows)
        else:
            rows = np.asarray(rows, dtype=np.uint8)
        destination[start : start + len(rows)] = rows
