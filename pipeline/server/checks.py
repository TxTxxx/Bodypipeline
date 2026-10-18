"""Queued, read-only Doctor runs. Reports never alter converted datasets."""

from __future__ import annotations

import json
import threading
import uuid

from pipeline.shared.core import DomainError, executable_path, now

from .datasets import dataset_for_job
from .quality import DOCTOR_CHECK_IDS, doctor_gate
from .storage import storage_path


class QualityRunner:
    def __init__(self, settings, store, command):
        self.settings, self.store, self.command = settings, store, command
        self.lock = threading.RLock()

    def save(self, row, **fields):
        row.update(**fields, updated_at=now())
        self.store.put("check", row["id"], row)
        return row

    def recover(self):
        for row in self.store.all("check"):
            if row["state"] == "RUNNING":
                self.save(
                    row,
                    state="INTERRUPTED",
                    stage="质检已中断",
                    error="Worker 重启；可重新运行 Doctor",
                    finished_at=now(),
                )

    def enqueue(self, jid):
        with self.lock:
            job = self.store.get("job", jid)
            dataset_for_job(self.settings, job)
            if not executable_path(self.settings.letools_bin):
                raise DomainError("未安装 letools / Doctor 依赖", 503)
            for row in self.store.all("check"):
                if row["job_id"] == jid and row["state"] in {"QUEUED", "RUNNING"}:
                    return row
            row = {
                "id": str(uuid.uuid4()),
                "job_id": jid,
                "state": "QUEUED",
                "stage": "等待 Doctor",
                "created_at": now(),
                "error": None,
                "target_format": job.get("target_format", "v3.0"),
            }
            return self.save(row)

    def tick(self):
        queued = [r for r in self.store.all("check") if r["state"] == "QUEUED"]
        if queued:
            self.run(queued[-1])
            return True
        return False

    def run(self, row):
        self.save(row, state="RUNNING", stage="验证数据集结构", started_at=now())
        try:
            job = self.store.get("job", row["job_id"])
            data = dataset_for_job(self.settings, job)
            work = (
                storage_path(self.settings, job, "work", job["id"])
                / "checks"
                / row["id"]
            )
            work.mkdir(parents=True, exist_ok=False)
            self.save(row, work_path=str(work), dataset_path=str(data))
            tool = executable_path(self.settings.letools_bin)
            if not tool:
                raise DomainError("未安装 letools / Doctor 依赖")
            code, _, err = self.command(
                [tool, "validate", str(data), "--deep"], work, "validate"
            )
            if code:
                raise DomainError(
                    "结构验证失败：" + err.read_text(errors="replace")[-2000:]
                )
            self.save(row, stage="Doctor 全量质检")
            code, output, err = self.command(
                [
                    tool,
                    "doctor",
                    "check",
                    str(data),
                    "--checks",
                    ",".join(DOCTOR_CHECK_IDS),
                    "--ci",
                    "--fail-on",
                    "warn",
                ],
                work,
                "doctor",
            )
            try:
                report = json.loads(output.read_text())
                if not isinstance(report, dict) or not report:
                    raise ValueError("empty report")
            except (ValueError, OSError) as exc:
                raise DomainError(
                    "Doctor 未生成有效报告：" + err.read_text(errors="replace")[-1200:]
                ) from exc
            self.save(row, doctor_report=report)
            info = json.loads((data / "meta/info.json").read_text())
            gate = doctor_gate(report, info, code)
            self.save(
                row,
                state="COMPLETED",
                stage="质检完成",
                quality_gate=gate,
                severity=gate["severity"],
                finished_at=now(),
            )
        except InterruptedError as exc:
            self.save(
                row,
                state="INTERRUPTED",
                stage="质检已中断",
                error=str(exc),
                finished_at=now(),
            )
        except Exception as exc:
            self.save(
                row,
                state="FAILED",
                stage="质检执行失败",
                error=str(exc),
                finished_at=now(),
            )
