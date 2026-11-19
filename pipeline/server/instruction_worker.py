"""Serialized apply runner for in-place language-instruction edits."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from pipeline.shared.core import (
    DomainError,
    atomic_json,
    digest,
    executable_path,
    now,
)

from .instruction_review import ApplyInstructionsRequest, InstructionReview
from .recycle import dataset_lock


class InstructionApplyRunner:
    def __init__(self, settings, store, review: InstructionReview):
        self.settings, self.store, self.review = settings, store, review
        self.lock = threading.RLock()

    @staticmethod
    def _public(row):
        return {
            key: row.get(key)
            for key in (
                "id",
                "dataset_id",
                "state",
                "stage",
                "progress",
                "created_at",
                "started_at",
                "finished_at",
                "report",
                "error",
            )
        }

    @staticmethod
    def _body(row):
        return ApplyInstructionsRequest(
            revision=row["revision"],
            source_signature=row["source_signature"],
        )

    @staticmethod
    def _episode_scope(episode_indices):
        return sorted(episode_indices) if episode_indices is not None else None

    @staticmethod
    def _frozen_assignments(row):
        spec = json.loads(Path(row["spec_path"]).read_text(encoding="utf-8"))
        return {
            int(item["episode_index"]): str(item["instruction"])
            for item in spec["assignments"]
        }

    @staticmethod
    def _receipt_path(row):
        operation_id = str(row.get("id", ""))
        if not operation_id or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in operation_id
        ):
            raise DomainError("语言指令任务编号无效", 500)
        return (
            Path(row["dataset_path"])
            / "meta"
            / f".bodypipeline-instruction-journal-{operation_id}.json"
        )

    def _committed_receipt(self, row):
        path = self._receipt_path(row)
        report = None
        if path.is_file():
            try:
                journal = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DomainError(
                    "语言指令提交凭据不可读，等待恢复", 500
                ) from exc
            if journal.get("state") == "committed":
                report = journal.get("report")
                if not isinstance(report, dict):
                    raise DomainError(
                        "语言指令提交凭据缺少结果", 500
                    )
                operation = journal.get("operation_id")
                if operation != row["id"]:
                    raise DomainError(
                        "语言指令提交凭据任务编号不匹配", 500
                    )

        spec_path = Path(row["spec_path"])
        if not spec_path.is_file() and report is None:
            return None
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DomainError("语言指令任务文件不可读", 500) from exc
        external = spec.get("committed_report")
        if report is not None:
            atomic_json(spec_path, {**spec, "committed_report": report})
        elif isinstance(external, dict):
            report = external
        if report is None:
            return None

        if path.exists():
            path.unlink()
        operation_root = Path(row["dataset_path"]) / "meta"
        for directory in (
            operation_root
            / f".bodypipeline-instruction-stage-{row['id']}",
            operation_root
            / f".bodypipeline-instruction-backup-{row['id']}",
        ):
            shutil.rmtree(directory, ignore_errors=True)
        return {**report, "operation_id": row["id"]}

    def _remove_receipt(self, row):
        try:
            spec_path = Path(row["spec_path"])
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            if "committed_report" in spec:
                spec.pop("committed_report")
                atomic_json(spec_path, spec)
        except (OSError, ValueError):
            # The job is already terminal; startup cleanup can retry later.
            pass

    def _complete_committed(self, row, report):
        self.review.finalize_apply(
            row["dataset_key"], row["source_signature"], report
        )
        row.update(
            state="CONVERTED",
            stage="已完成",
            progress="语言指令已原地写入并通过 LeTools 校验",
            report=report,
            finished_at=now(),
            error=None,
        )
        self.store.put("instruction_apply", row["id"], row)
        self._remove_receipt(row)
        return row

    def _active_job_blocker(self, path: Path, apply_id: str | None = None):
        target = path.resolve()
        for job in self.store.all("job"):
            if apply_id and job.get("id") == apply_id:
                continue
            if job.get("state") not in {"QUEUED", "RUNNING"}:
                continue
            raw = job.get("dataset_path")
            if not raw:
                continue
            try:
                candidate = Path(raw).resolve()
            except OSError:
                continue
            if candidate == target:
                raise DomainError(
                    "该数据集仍有运行中的工作台任务，请等待任务完成后再应用语言指令",
                    409,
                )

    def enqueue(self, dataset_id, body, episode_indices: set[int] | None = None):
        key, path, _ = self.review.resolve(dataset_id)
        with self.lock, dataset_lock(self.store, key):
            key, path, _, snapshot, draft, assignments = self.review.apply_plan(
                dataset_id, body, episode_indices
            )
            self._active_job_blocker(path)
            episode_scope = (
                self._episode_scope(episode_indices)
            )
            fingerprint = digest(
                {
                    "dataset": key,
                    "path": str(path),
                    "source_signature": snapshot["signature"],
                    "episode_indices": episode_scope,
                    "assignments": assignments,
                }
            )
            for row in self.store.all("instruction_apply"):
                if row.get("dataset_key") != key or row.get("state") not in {
                    "QUEUED",
                    "RUNNING",
                }:
                    continue
                if (
                    row.get("source_signature") == snapshot["signature"]
                    and row.get("episode_indices") == episode_scope
                    and (
                        row.get("fingerprint") == fingerprint
                        or (
                            row.get("fingerprint") is None
                            and row.get("revision") == draft["revision"]
                        )
                    )
                ):
                    return self._public(row)
                raise DomainError(
                    "该数据集已有另一批语言指令正在应用，请等待完成",
                    409,
                )
            apply_id = uuid.uuid4().hex
            spec_path = (
                self.settings.root
                / "reports"
                / f"instruction-apply-{apply_id}.json"
            )
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(
                json.dumps(
                    {
                        "operation_id": apply_id,
                        "dataset_path": str(path),
                        "assignments": [
                            {
                                "episode_index": episode,
                                "instruction": item["instruction"],
                            }
                            for episode, item in assignments.items()
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            row = {
                "id": apply_id,
                "dataset_id": dataset_id,
                "dataset_key": key,
                "dataset_path": str(path),
                "source_signature": snapshot["signature"],
                "revision": draft["revision"],
                "episode_indices": episode_scope,
                "fingerprint": fingerprint,
                "spec_path": str(spec_path),
                "state": "QUEUED",
                "stage": "等待应用",
                "progress": "已冻结 Episode 指令与来源版本",
                "created_at": now(),
            }
            self.store.put("instruction_apply", apply_id, row)
            return self._public(row)

    def status(self, dataset_id, apply_id):
        self.review.resolve(dataset_id)
        row = self.store.get("instruction_apply", apply_id)
        if row.get("dataset_id") != dataset_id:
            raise DomainError("语言指令任务不存在", 404)
        return self._public(row)

    def recover(self):
        for row in self.store.all("instruction_apply"):
            if row.get("state") == "CONVERTED":
                self._remove_receipt(row)
                continue
            receipt = self._committed_receipt(row)
            if receipt is not None:
                try:
                    with self.lock, dataset_lock(
                        self.store, row["dataset_key"]
                    ):
                        self.review.resolve(row["dataset_id"])
                        self._complete_committed(row, receipt)
                    continue
                except Exception as exc:
                    row.update(
                        state="RUNNING",
                        stage="等待提交对账",
                        progress="数据集已提交，正在等待草稿与任务状态同步",
                        error=str(exc),
                    )
                    self.store.put(
                        "instruction_apply", row["id"], row
                    )
                    continue
            if row.get("state") == "RUNNING":
                row.update(
                    state="QUEUED",
                    stage="恢复中断任务",
                    progress="进程重启，将先回滚未完成事务再继续应用",
                )
                self.store.put("instruction_apply", row["id"], row)

    def tick(self):
        queued = [
            row
            for row in self.store.all("instruction_apply")
            if row.get("state") == "QUEUED"
        ]
        if not queued:
            return False
        self.run(queued[-1])
        return True

    def _worker(self, *args):
        tool = executable_path(self.settings.letools_bin)
        python = Path(tool).with_name("python") if tool else None
        if python is None or not python.is_file():
            raise DomainError("应用语言指令需要 LeTools Python 环境", 503)
        try:
            result = subprocess.run(
                [
                    str(python),
                    str(Path(__file__).with_name("instruction_writer_worker.py")),
                    *args,
                ],
                capture_output=True,
                text=True,
                timeout=self.settings.command_timeout,
                env=self.settings.tool_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise DomainError("应用语言指令超时；事务将在下次启动时恢复", 504) from exc
        if result.returncode:
            raise DomainError(
                "应用语言指令失败：" + result.stderr[-1600:], 409
            )
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise DomainError("语言指令写入器未返回有效结果", 502) from exc

    def run(self, row):
        with self.lock:
            path = Path(row["dataset_path"])
            row.update(
                state="RUNNING",
                stage="检查数据集锁",
                progress="正在获取数据集写锁",
                started_at=now(),
            )
            self.store.put("instruction_apply", row["id"], row)
            try:
                with dataset_lock(self.store, row["dataset_key"]):
                    receipt = self._committed_receipt(row)
                    if receipt is not None:
                        self.review.resolve(row["dataset_id"])
                        self._complete_committed(row, receipt)
                        return row
                    self.review.resolve(row["dataset_id"])
                    self._active_job_blocker(path, row["id"])
                    self._worker("recover", str(path))
                    frozen_assignments = self._frozen_assignments(row)
                    _, _, _, snapshot, _, assignments = self.review.apply_plan(
                        row["dataset_id"],
                        self._body(row),
                        set(row["episode_indices"])
                        if row.get("episode_indices") is not None
                        else None,
                        frozen_assignments,
                    )
                    if snapshot["signature"] != row["source_signature"]:
                        raise DomainError(
                            "排队期间来源数据已变化，请重新加载后再应用", 409
                        )
                    row.update(
                        stage="原地写入 LeRobot",
                        progress=f"正在应用 {len(assignments)} 个 Episode",
                    )
                    self.store.put("instruction_apply", row["id"], row)
                    report = {
                        **self._worker("apply", row["spec_path"]),
                        "operation_id": row["id"],
                    }
                    receipt = self._committed_receipt(row)
                    if receipt is not None:
                        report = receipt
                    self._complete_committed(row, report)
                    return row
            except Exception as exc:
                try:
                    with dataset_lock(self.store, row["dataset_key"]):
                        receipt = self._committed_receipt(row)
                        if receipt is not None:
                            self.review.resolve(row["dataset_id"])
                            self._complete_committed(row, receipt)
                            return row
                        self.review.resolve(row["dataset_id"])
                        self._worker("recover", str(path))
                except Exception as recovery_exc:
                    row.update(
                        state="RUNNING",
                        stage="自动回滚失败",
                        progress="等待服务重启后继续恢复数据集事务",
                        error=(
                            f"{exc}; recovery: {recovery_exc}"
                        ),
                    )
                    self.store.put("instruction_apply", row["id"], row)
                    return row
                else:
                    row.update(
                        state="FAILED",
                        stage="应用失败",
                        progress="数据集已回滚或保持原状",
                        error=str(exc),
                        finished_at=now(),
                    )
            self.store.put("instruction_apply", row["id"], row)
            return row
