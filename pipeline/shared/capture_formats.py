"""Versioned containers accepted by transport, independent of robot semantics."""

CAPTURE_FILENAME = "episode.hdf5"
ARX_HDF5_FORMAT = "arx_direct_teaching_hdf5_v1"
ARX_RAW_SCHEMA = "bodypipeline.arx.direct_teaching.raw.v1"
ARX_ALIGNED_SCHEMA = "bodypipeline.arx.direct_teaching.aligned.v1"
SINGLE_FILE_HDF5_FORMATS = frozenset({"agilex_joint_camera_hdf5_v1", ARX_HDF5_FORMAT})
