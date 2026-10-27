"""Resolve inspectable datasets from server-owned job records."""

from pathlib import Path

from pipeline.shared.core import DomainError, confined

from .storage import storage_path

TARGET_FORMATS = ("v3.0", "v2.1")
DATA_READY_STATES = {"CONVERTED", "SYNTHETIC_READY", "QUARANTINED", "COMPLETED"}


def _has_metadata(path: Path) -> bool:
    info = path / "meta/info.json"
    return (
        path.is_dir()
        and not path.is_symlink()
        and info.is_file()
        and not info.is_symlink()
    )


def _dataset_roots(settings):
    roots = [settings.root]
    if settings.data_root is not None:
        roots.append(settings.data_root)
    roots.extend(settings.import_roots)
    return roots


def _recorded_dataset(settings, job):
    """Read an existing result path recorded by an older/imported job, without moving data."""
    recorded = job.get("dataset_path")
    if not isinstance(recorded, str):
        return None
    candidate = Path(recorded)
    if not candidate.is_absolute():
        return None
    resolved = candidate.resolve()
    for root in _dataset_roots(settings):
        root = root.resolve()
        try:
            path = (
                resolved
                if resolved == root
                else confined(root, resolved.relative_to(root).as_posix())
            )
        except (ValueError, DomainError):
            continue
        if _has_metadata(path):
            return path
    return None


def dataset_for_job(settings, job):
    state = job["state"]
    if state in {"DELETING", "DELETE_FAILED", "DELETED"}:
        raise DomainError("数据集正在删除或已删除，不能继续使用", 409)
    if state not in DATA_READY_STATES and job.get("dataset_ready") is not True:
        raise DomainError("尚无完成转换和结构验证的数据集；请先完成转换")
    data = storage_path(settings, job, "work", job["id"]) / "dataset"
    if _has_metadata(data):
        return data
    recorded = _recorded_dataset(settings, job)
    if recorded is not None:
        return recorded
    raise DomainError("转换结果不存在或存储不可用，请检查数据目录")
