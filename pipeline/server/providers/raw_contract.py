"""Upload container rules supplied by trusted providers, with legacy defaults."""

from dataclasses import dataclass
from pathlib import Path

from pipeline.shared.core import DomainError
from pipeline.shared.hdf5 import (
    AGILEX_HDF5_FILENAME,
    AGILEX_HDF5_FORMAT,
    ALOHA_HDF5_FORMAT,
    is_hdf5_path,
)


@dataclass(frozen=True)
class RawFileContract:
    single_file: bool = False
    filename: str | None = None

    def validate_path(self, path: str) -> None:
        if self.single_file:
            if self.filename is not None and path != self.filename:
                raise DomainError(f"采集包文件必须为 {self.filename}", 400)
            if not is_hdf5_path(Path(path)):
                raise DomainError("HDF5 episode 文件必须是 .hdf5 或 .h5", 400)
        elif not path.startswith("episode0/"):
            raise DomainError("文件必须位于 episode0 且不能重复", 400)


def raw_file_contract(raw_format: str, provider=None) -> RawFileContract:
    declare = getattr(provider, "raw_file_contract", None)
    if declare is not None:
        contract = declare(raw_format)
        if not isinstance(contract, RawFileContract):
            raise DomainError("Provider 的原始文件契约无效", 503)
        return contract
    if raw_format == AGILEX_HDF5_FORMAT:
        return RawFileContract(single_file=True, filename=AGILEX_HDF5_FILENAME)
    if raw_format == ALOHA_HDF5_FORMAT:
        return RawFileContract(single_file=True)
    return RawFileContract()
