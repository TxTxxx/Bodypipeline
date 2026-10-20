"""Queued Argus analyses over converted datasets.

Argus is a separate toolchain (its own Python 3.11 environment and pinned
checkout). This module owns everything around it: the queue, the trust boundary
between a Server job and that toolchain, spend accounting, and the on-disk
record of what ran.

Design boundaries, matching the rest of the pipeline:

* The converted dataset is read-only input. Analyses write into their own
  ``analyses/<analysis_id>/`` directory and never modify the dataset.
* Doctor stays the data-quality gate. An analysis is an independent report; its
  failure, partial result or budget refusal never changes a job's state.
* Nothing is inferred. Facts the analysis needs but the dataset does not carry
  (camera mounting, state semantics) come from the Provider's
  ``analysis_context``; when absent, Argus reports the affected check as not
  assessed rather than guessing.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from pipeline.shared.core import DomainError, executable_path, now

from .datasets import dataset_for_job
from .storage import storage_path

# Execution states, separate from the analysis verdict.
QUEUED = "QUEUED"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
PARTIAL = "PARTIAL"
FAILED = "FAILED"
INTERRUPTED = "INTERRUPTED"
UNAVAILABLE = "UNAVAILABLE"

# Analysis verdicts.
VERDICT_CLEAN = "CLEAN"  # every check ran and none fired
VERDICT_REVIEW = "REVIEW"  # at least one check fired
VERDICT_INCOMPLETE = "INCOMPLETE"  # some checks could not be assessed

MODES = ("local", "full")


class BudgetExceeded(DomainError):
    def __init__(self, message: str):
        super().__init__(message, 429)


class AnalysisRunner:
    """Run Argus locally (free) and optionally label (paid), under a spend cap."""

    def __init__(self, settings, store, command):
        self.settings, self.store, self.command = settings, store, command
        self.lock = threading.RLock()
        self.shutdown_requested = threading.Event()

    # ------------------------------------------------------------- persistence
    def save(self, row, **fields):
        row.update(**fields, updated_at=now())
        self.store.put("analysis", row["id"], row)
        return row

    def recover(self):
        """A restart cannot resume a subprocess; keep the record honest."""

        for row in self.store.all("analysis"):
            if row["state"] in {QUEUED, RUNNING}:
                self.save(
                    row,
                    state=INTERRUPTED,
                    stage="分析已中断",
                    error="Server 重启；可重新提交分析",
                    finished_at=now(),
                )

    # ---------------------------------------------------------------- settings
    def available(self) -> tuple[bool, str]:
        """Whether the toolchain is usable, and why not when it is not."""

        if not self.settings.argus_src:
            return False, "未配置 argus_src"
        source = Path(self.settings.argus_src).expanduser()
        if not (source / "label").is_dir():
            return False, f"argus 源码不存在：{source}"
        python = self._python()
        if python is None:
            return False, "未找到 Argus Python 解释器"
        return True, ""

    def _python(self) -> str | None:
        """The interpreter to run Argus with, never symlink-resolved.

        ``executable_path`` resolves symlinks, which turns a virtualenv's
        ``bin/python`` into the base interpreter and silently drops the venv's
        site-packages. That fails as "ModuleNotFoundError" on the first import,
        so a venv path is returned as configured.
        """

        candidate = self.settings.argus_python or str(
            Path(self.settings.argus_src).expanduser() / ".venv/bin/python"
        )
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return executable_path(candidate)

    def _env(self, job: dict, provider_context: dict) -> dict:
        """Environment for the Argus subprocess.

        ``tool_environment`` strips every ``PIPELINE_*`` variable, which is the
        right default for letools. Argus needs three of them back, and they are
        added explicitly here rather than by loosening that filter.
        """

        env = self.settings.tool_environment()
        env["PYTHONPATH"] = str(Path(self.settings.argus_src).expanduser())
        env["PATH"] = os.pathsep.join(
            [
                str(Path(self.settings.argus_src).expanduser() / "tools/bin"),
                env.get("PATH", ""),
            ]
        )
        cameras = provider_context.get("cameras") or {}
        if cameras:
            env["PIPELINE_ARGUS_CAMERA_MOUNTING"] = json.dumps(
                cameras, ensure_ascii=False
            )
        if self.settings.argus_base_url:
            env["PIPELINE_ARGUS_BASE_URL"] = self.settings.argus_base_url
            env["PIPELINE_ARGUS_GENERIC_GATEWAY"] = "1"
        key = os.environ.get(self.settings.argus_api_key_env, "")
        if key:
            env["PIPELINE_ARGUS_API_KEY"] = key
        env["RDA_DECODE_CONCURRENCY"] = "4"
        return env

    # ------------------------------------------------------------------ budget
    def _task_spend(self, jid: str) -> float:
        total = 0.0
        for row in self.store.all("analysis"):
            if row.get("job_id") == jid:
                total += float(row.get("cost_usd") or 0.0)
        return total

    def _daily_spend(self) -> float:
        """Spend since local midnight; the ledger never resets on restart."""

        midnight = time.time() - (time.time() % 86400)
        total = 0.0
        for row in self.store.all("analysis"):
            if float(row.get("cost_usd") or 0.0) <= 0:
                continue
            if float(row.get("finished_at") or row.get("created_at") or 0) >= midnight:
                total += float(row["cost_usd"])
        return total

    def budget(self, jid: str, episodes: int, mode: str) -> dict:
        task_cap = float(self.settings.argus_task_budget_usd)
        daily_cap = float(self.settings.argus_daily_budget_usd)
        per_episode = float(self.settings.argus_per_episode_budget_usd)
        # A batch is one job with many episodes, so the task allowance scales with
        # the batch instead of being a fixed figure that fits six episodes.
        batch_cap = per_episode * max(1, episodes)
        task_limit = min(task_cap, batch_cap)
        task_used = self._task_spend(jid)
        daily_used = self._daily_spend()
        if mode == "local":
            return {
                "mode": mode,
                "task_cap_usd": task_limit,
                "task_used_usd": round(task_used, 4),
                "daily_cap_usd": daily_cap,
                "daily_used_usd": round(daily_used, 4),
                "available_usd": 0.0,
            }
        available = max(0.0, min(task_limit - task_used, daily_cap - daily_used))
        return {
            "mode": mode,
            "task_cap_usd": round(task_limit, 4),
            "task_used_usd": round(task_used, 4),
            "daily_cap_usd": daily_cap,
            "daily_used_usd": round(daily_used, 4),
            "available_usd": round(available, 4),
        }

    # ------------------------------------------------------------------ enqueue
    def enqueue(self, jid: str, mode: str = "local", episodes: list[int] | None = None):
        if mode not in MODES:
            raise DomainError("分析模式必须为 local 或 full", 400)
        with self.lock:
            job = self.store.get("job", jid)
            data = dataset_for_job(self.settings, job)
            ok, why = self.available()
            if not ok:
                raise DomainError(f"Argus 不可用：{why}", 503)
            for row in self.store.all("analysis"):
                if row["job_id"] == jid and row["state"] in {QUEUED, RUNNING}:
                    return row
            info = json.loads((data / "meta/info.json").read_text())
            count = int(info.get("total_episodes") or 1)
            budget = self.budget(jid, count, mode)
            if mode == "full" and budget["available_usd"] <= 0:
                raise BudgetExceeded(
                    "今日或本任务的分析预算已用尽；仍可运行 local 分析"
                )
            row = {
                "id": str(uuid.uuid4()),
                "job_id": jid,
                "state": QUEUED,
                "stage": "等待分析",
                "mode": mode,
                "episodes": episodes,
                "episode_count": count,
                "budget": budget,
                "cost_usd": 0.0,
                "created_at": now(),
                "error": None,
                "dataset_path": str(data),
                "robot_type": job.get("robot_type"),
            }
            return self.save(row)

    def cancel(self, aid: str):
        row = self.store.get("analysis", aid)
        if row["state"] not in {QUEUED, RUNNING}:
            return row
        return self.save(row, state=INTERRUPTED, stage="已取消", finished_at=now())

    def tick(self):
        queued = [r for r in self.store.all("analysis") if r["state"] == QUEUED]
        if queued:
            self.run(queued[-1])
            return True
        return False

    # --------------------------------------------------------------------- run
    def run(self, row):
        self.save(row, state=RUNNING, stage="准备数据", started_at=now())
        work = storage_path(self.settings, row, "analyses", row["id"])
        try:
            work.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise DomainError("分析目录已存在，拒绝覆盖", 409) from None
        try:
            job = self.store.get("job", row["job_id"])
            provider_context = self._provider_context(job)
            data = Path(row["dataset_path"])
            env = self._env(job, provider_context)

            (work / "context.json").write_text(
                json.dumps(
                    {
                        "job_id": job["id"],
                        "analysis_id": row["id"],
                        "mode": row["mode"],
                        "robot_type": row.get("robot_type"),
                        "dataset_path": str(data),
                        "dataset_info": json.loads(
                            (data / "meta/info.json").read_text()
                        ),
                        "provider_context": provider_context,
                        "argus": {
                            "src": self.settings.argus_src,
                            "model": self.settings.argus_model,
                        },
                        "budget": row["budget"],
                        "created_at": row["created_at"],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )

            code, _, err = self._call(
                [
                    "-m",
                    "prepare",
                    "lerobot",
                    "prepare",
                    "--root",
                    str(data),
                    "--rig",
                    "teleop_arms",
                    "--out",
                    str(work / "episodes"),
                ],
                work,
                env,
                "prepare",
            )
            if code:
                raise DomainError(
                    "Argus 数据准备失败：" + err.read_text(errors="replace")[-1200:]
                )

            self.save(row, stage="确定性检查")
            code, _, err = self._call(
                ["-m", "checks", str(work / "episodes")], work, env, "checks"
            )
            # check modules exit 3 for partial per-episode failures; both are recorded.
            if code not in (0, 3):
                raise DomainError(
                    "Argus 检查失败：" + err.read_text(errors="replace")[-1200:]
                )
            row["checks_exit"] = code

            summary = self._summarize(work / "episodes")
            self.save(
                row,
                stage="检查完成",
                checks=summary["checks"],
                # Keep the reasons: "not applicable here" and "evidence missing"
                # look identical in a count, and only the second is actionable.
                not_assessed=summary["not_assessed"],
            )

            if row["mode"] == "local":
                gate = self._local_gate(self._provider_context(job), row["id"])
                return self._finish(row, work, summary, gate)

            self.save(row, stage="云端标注")
            label = self._label(row, work, env)
            return self._finish(row, work, summary, label)

        except InterruptedError as exc:
            self.save(
                row,
                state=INTERRUPTED,
                stage="分析已中断",
                error=str(exc),
                finished_at=now(),
            )
        except DomainError as exc:
            self.save(
                row,
                state=FAILED,
                stage="分析失败",
                error=exc.message,
                finished_at=now(),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            self.save(
                row,
                state=FAILED,
                stage="分析失败",
                error=f"{type(exc).__name__}: {exc}"[:800],
                finished_at=now(),
            )

    def _provider_context(self, job: dict) -> dict:
        """Facts from the job's Provider that the dataset does not carry.

        The job's Profile snapshot records what the capture was converted with,
        but it can lag Server configuration: captures converted before a camera's
        mounting was documented carry the older profile. Facts the snapshot does
        not state are therefore taken from the trusted Runtime's current Profile,
        which comes from Server configuration — never from the dataset or a
        request.
        """

        profile = job.get("profile") or {}
        provider_id = job.get("provider")
        runtime = None
        if job.get("runtime_id"):
            try:
                runtime = self.settings.runtime_registry().get(job["runtime_id"])
            except (KeyError, AttributeError):
                runtime = None
        if runtime is not None:
            provider_id = provider_id or runtime.provider
        if not profile and runtime is not None:
            profile = runtime.profile
        if not provider_id:
            return {}
        try:
            from .providers import get_provider

            provider = get_provider(provider_id)
            context = provider.analysis_context(profile) or {}
            if runtime is not None and runtime.profile is not profile:
                # Fill only what the snapshot lacks; a stated fact is not overridden.
                trusted = provider.analysis_context(runtime.profile) or {}
                if not context.get("cameras"):
                    context["cameras"] = trusted.get("cameras") or {}
                for key in (
                    "state_streams",
                    "action_streams",
                    "joint_names",
                    "joint_units",
                ):
                    if not context.get(key):
                        context[key] = trusted.get(key) or []
            return context
        except (DomainError, AttributeError, TypeError):
            return {}

    def _call(self, args: list[str], work: Path, env: dict, name: str):
        python = self._python()
        if python is None:
            raise DomainError("未找到 Argus Python 解释器", 503)
        stdout, stderr = work / f"{name}.json", work / f"{name}.stderr.log"
        with stdout.open("wb") as out, stderr.open("wb") as err:
            process = subprocess.Popen(
                [python, *args],
                stdout=out,
                stderr=err,
                start_new_session=True,
                cwd=Path(self.settings.argus_src),
                env=env,
            )
            try:
                deadline = time.monotonic() + self.settings.command_timeout
                while process.poll() is None:
                    if self.shutdown_requested.wait(0.25):
                        raise InterruptedError("Worker 收到停止请求")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Argus 命令执行超时")
                code = process.returncode
            except BaseException:
                os.killpg(process.pid, 9)
                process.wait()
                raise
        return code, stdout, stderr

    def _summarize(self, episodes: Path) -> dict:
        """Fold the per-episode sidecars into a verdict the UI can show."""

        pairing = {
            "assessed": 0,
            "not_assessed": 0,
            "crossed": 0,
            "undefined": 0,
            "skipped": 0,
        }
        jumps = {"flagged": 0, "clean": 0, "not_assessed": 0, "skipped": 0}
        grippers = {"flagged": 0, "clean": 0, "skipped": 0}
        qc_flags = 0
        qc_not_assessed: dict[str, str] = {}
        qc_layout_na: set[str] = set()
        qc_missing_evidence: set[str] = set()
        state_none = 0
        episodes_seen = 0
        for path in sorted(episodes.glob("episode_*/context.json")):
            try:
                ctx = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            episodes_seen += 1
            if ctx.get("state_kind") == "none":
                state_none += 1
            # A check that returned None never ran (no state, no mounted camera,
            # nothing to compare). That is not "clean" and not "assessed".
            sp = ctx.get("stream_pairing")
            if sp is None:
                pairing["skipped"] += 1
            elif isinstance(sp, dict):
                if "not_assessed" in sp:
                    pairing["not_assessed"] += 1
                elif sp.get("crossed") is not None:
                    pairing["assessed"] += 1
                    if sp["crossed"]:
                        pairing["crossed"] += 1
                else:
                    # Correlations exist but are undefined (a still arm gives no
                    # speed to correlate with): measured, not passed.
                    pairing["undefined"] += 1
            rj = ctx.get("recorded_jumps")
            if rj is None:
                jumps["skipped"] += 1
            elif isinstance(rj, dict):
                if "not_assessed" in rj:
                    jumps["not_assessed"] += 1
                elif rj.get("flagged"):
                    jumps["flagged"] += 1
                else:
                    jumps["clean"] += 1
            gc = ctx.get("gripper_channels")
            if gc is None:
                grippers["skipped"] += 1
            elif isinstance(gc, dict) and "flagged" in gc:
                grippers["flagged" if gc["flagged"] else "clean"] += 1
            qc = ctx.get("capture_qc") or {}
            qc_flags += len(qc.get("flags") or [])
            for check, why in (qc.get("not_assessed") or {}).items():
                qc_not_assessed.setdefault(check, why)
                if self._layout_not_applicable(why):
                    qc_layout_na.add(check)
                else:
                    qc_missing_evidence.add(check)
        return {
            "episodes": episodes_seen,
            "state_unavailable": state_none,
            "checks": {
                "stream_pairing": pairing,
                "recorded_jumps": jumps,
                "gripper_channels": grippers,
                "capture_qc_flags": qc_flags,
                "capture_qc_not_assessed": len(qc_not_assessed),
                "capture_qc_layout_not_applicable": len(qc_layout_na),
                "capture_qc_missing_evidence": len(qc_missing_evidence),
            },
            "not_assessed": qc_not_assessed,
        }

    @staticmethod
    def _layout_not_applicable(reason: str) -> bool:
        """Whether a not-assessed reason describes an absent signal rather than missing proof.

        Argus reports a check as not assessed both when the recording has no such
        signal (joint angles carry no end-effector pose) and when it has the signal
        but lacks the evidence to judge it. Only the second is a coverage gap.
        """

        text = str(reason).lower()
        markers = (
            # the recording does not carry the signal the check needs
            "has joint angles but no",
            "no gripper pose",
            "does not have",
            "the pipeline does not use",
            "which the pipeline does not",
            "the pipeline keeps no",
            "no separate full-rate copy",
            # the value is derived, so a structural check cannot fail
            "actions are computed",
            "action times are taken",
            "we compute the actions",
            "tests how the actions are computed",
            "tests upstream",
            "cannot fire",
            "always valid",
            # the check is defined only for another input layer
            "need a separate native-rate",
            "our sidecars do not have",
        )
        return any(marker in text for marker in markers)

    def _local_gate(self, provider_context: dict, aid: str) -> dict:
        """Why the cross-modal checks could not run, when they could not.

        A missing camera mounting is a real limitation, not a pass. It is
        reported with the fix instead of being silently dropped.
        """

        cameras = provider_context.get("cameras") or {}
        mounted = [
            name
            for name, value in cameras.items()
            if str(value).startswith("wrist") or value == "actor"
        ]
        if len(mounted) >= 2:
            return {"cross_modal_ready": True, "reason": None}
        return {
            "cross_modal_ready": False,
            "reason": (
                "Profile 未声明腕部相机安装关系，跨模态检查会被标记为未评估。"
                "在 Profile 的 cameras 中补充 mounting（如 wrist_left / wrist_right）后重新分析。"
            ),
        }

    def _label(self, row, work: Path, env: dict) -> dict:
        """Run the paid harness within the remaining allowance."""

        budget = self.budget(row["job_id"], row["episode_count"], "full")
        cap = budget["available_usd"]
        if cap <= 0:
            return {"state": "SKIPPED", "reason": "可用预算为 0"}
        max_tokens = str(self.settings.argus_max_tokens)
        label_env = dict(env)
        code, _, err = self._call(
            [
                "-m",
                "label",
                "--dataset",
                row["id"],
                "--episodes",
                str(work / "episodes"),
                "--kind",
                "full",
                "--cap",
                f"{cap:.4f}",
                "--runs",
                str(work / "runs"),
                "--",
                "--model",
                self.settings.argus_model,
                "--max-tokens",
                max_tokens,
            ],
            work,
            label_env,
            "label",
        )
        run_json = self._latest_run(work / "runs")
        cost = float((run_json or {}).get("cost_usd") or 0.0)
        if cost:
            self.save(row, cost_usd=round(cost, 6))
        if code not in (0, 3):
            return {
                "state": "FAILED",
                "error": err.read_text(errors="replace")[-800:],
                "cost_usd": cost,
            }
        return {
            "state": (run_json or {}).get("status") or "UNKNOWN",
            "episodes_done": (run_json or {}).get("episodes_done"),
            "episodes_failed": (run_json or {}).get("episodes_failed"),
            "cost_usd": cost,
            "run": str(work / "runs"),
        }

    @staticmethod
    def _latest_run(root: Path) -> dict | None:
        best = None
        for path in sorted(root.glob("*/run.json")):
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if best is None or (value.get("started_at") or "") >= (
                best.get("started_at") or ""
            ):
                best = value
        return best

    def _finish(self, row, work: Path, summary: dict, gate: dict) -> dict:
        checks = summary["checks"]
        fired = (
            checks["gripper_channels"]["flagged"]
            + checks["capture_qc_flags"]
            + checks["recorded_jumps"]["flagged"]
            + checks["stream_pairing"]["crossed"]
        )
        incomplete = (
            checks.get("capture_qc_missing_evidence", 0) > 0
            or checks["stream_pairing"]["not_assessed"] > 0
            or checks["stream_pairing"]["undefined"] > 0
            or checks["recorded_jumps"]["not_assessed"] > 0
        )
        if fired:
            verdict = VERDICT_REVIEW
        elif incomplete:
            verdict = VERDICT_INCOMPLETE
        else:
            verdict = VERDICT_CLEAN
        label = gate
        state = COMPLETED
        if isinstance(label, dict) and label.get("state") == "FAILED":
            state = FAILED
        elif isinstance(label, dict) and label.get("state") == "PARTIAL":
            state = PARTIAL
        self.save(
            row,
            state=state,
            stage="分析完成",
            verdict=verdict,
            checks=checks,
            cross_modal=gate,
            label=label,
            work_path=str(work),
            finished_at=now(),
        )
        return row


__all__ = [
    "AnalysisRunner",
    "BudgetExceeded",
    "COMPLETED",
    "FAILED",
    "INTERRUPTED",
    "PARTIAL",
    "QUEUED",
    "RUNNING",
    "UNAVAILABLE",
    "VERDICT_CLEAN",
    "VERDICT_INCOMPLETE",
    "VERDICT_REVIEW",
]
