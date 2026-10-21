"""Offline AgileX-format simulator; never calls ROS or hardware."""

import sys

import h5py

from .agilex import AgileXReplayProvider
from .base import ReplayCapabilities


class SimulatedReplayProvider:
    """Local development only: print progress, with no ROS or network access."""

    capabilities = ReplayCapabilities(
        id="simulated",
        version="1",
        label="AgileX 格式离线模拟",
        robot_types=("agilex_aloha",),
        file_extensions=(".hdf5", ".h5"),
        input_description="AgileX 平台原始包、对齐 HDF5 和 ALOHA HDF5",
        simulated=True,
    )

    def __init__(self, config, profile):
        self.data = AgileXReplayProvider(config, profile)

    def probe(self, path):
        return self.data.probe(path)

    def prepare(self, path, cache):
        return self.data.prepare(path, cache)

    frame = staticmethod(AgileXReplayProvider.frame)
    error = staticmethod(AgileXReplayProvider.error)

    def command(self, path):
        with h5py.File(path, "r") as handle:
            frames = int(handle["size"][()])
            fps = float(handle.attrs.get("fps", 30))
        return [
            sys.executable,
            "-u",
            "-c",
            "import sys,time; n=int(sys.argv[1]); fps=float(sys.argv[2]); "
            "[(print('frame:', i, flush=True), time.sleep(1/fps)) for i in range(n)]",
            str(frames),
            str(fps),
        ]
