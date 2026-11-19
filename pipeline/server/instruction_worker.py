"""Serialized apply runner for in-place language-instruction edits."""

from __future__ import annotations

import json
import subprocess
import threading
import uuid
from pathlib import Path

from pipeline.shared.core import DomainError, digest, executable_path, now

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

    def enqueue(self, dataset_id, body):
        key, path, _ = self.review.resolve(dataset_id)
        with self.lock, dataset_lock(self.store, key):
            key, path, _, snapshot, draft, assignments = self.review.apply_plan(
                dataset_id, body
            )
            self._active_job_blocker(path)
            fingerprint = digest(
                {
                    "dataset": key,
                    "path": str(path),
                    "source_signature": snapshot["signature"],
                    "revision": draft["revision"],
                    "assignments": assignments,
                }
            )
            for row in self.store.all("instruction_apply"):
                if row.get("dataset_key") != key or row.get("state") not in {
                    "QUEUED",
                    "RUNNING",
                }:
                    continue
                # Revisions are immutable because every save creates a new one.
                # A double-click therefore maps to the same apply job even when
                # source-stat snapshots vary in fields irrelevant to this edit.
                if (
                    row.get("revision") == draft["revision"]
                    and row.get("source_signature")
                    == snapshot["signature"]
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
            if row.get("state") != "RUNNING":
                continue
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
            row.update(
                state="RUNNING",
                stage="检查数据集锁",
                progress="正在获取数据集写锁",
                started_at=now(),
            )
            self.store.put("instruction_apply", row["id"], row)
            try:
                path = Path(row["dataset_path"])
                with dataset_lock(self.store, row["dataset_key"]):
                    self.review.resolve(row["dataset_id"])
                    self._active_job_blocker(path, row["id"])
                    self._worker("recover", str(path))
                    _, _, _, snapshot, _, assignments = self.review.apply_plan(
                        row["dataset_id"], self._body(row)
                    )
                    if snapshot["signature"] != row["source_signature"]:
                        raise DomainError(
                            "排队期间来源数据已变化，请重新加载后再应用", 409
                        )
                    spec = Path(row["spec_path"])
                    expected = [
                        {
                            "episode_index": episode,
                            "instruction": item["instruction"],
                        }
                        for episode, item in assignments.items()
                    ]
                    frozen = json.loads(spec.read_text(encoding="utf-8"))
                    if frozen["assignments"] != expected:
                        raise DomainError(
                            "排队期间草稿已变化，请重新提交应用", 409
                        )
                    row.update(
                        stage="原地写入 LeRobot",
                        progress=f"正在应用 {len(assignments)} 个 Episode",
                    )
                    self.store.put("instruction_apply", row["id"], row)
                    report = self._worker("apply", str(spec))
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
            except Exception as exc:
                row.update(
                    state="FAILED",
                    stage="应用失败",
                    progress="数据集已回滚或保持原状",
                    error=str(exc),
                    finished_at=now(),
                )
            self.store.put("instruction_apply", row["id"], row)
            return row
