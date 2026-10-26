"""Authenticated ARX recorder service. HTTP lifecycle controls recording only."""

from __future__ import annotations

import hmac
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from pipeline.shared.arx import CAMERAS, ArxWriter, validate_profile
from pipeline.shared.capture_formats import CAPTURE_FILENAME
from pipeline.shared.core import DomainError, ProcessLock, atomic_json, digest

from .bridge import StartRequest, StopRequest


class ArxRecorder:
    def __init__(self, settings, source):
        self.settings = settings
        self.profile = settings.profile()
        validate_profile(self.profile)
        self.source = source
        self.recording_mode = settings.capture.get("recording_mode", "snapshot")
        if self.recording_mode not in {"snapshot", "raw_first"}:
            raise DomainError("未知 ARX recording_mode")
        self.mutex = threading.RLock()
        self.state_file = settings.root / "arx-recorder.json"
        self.sessions = (
            json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        )
        self.row = None
        self.writer = None
        self.finalizer = None
        self.finalizing_writer = None
        self.before_recording = None
        self.validate_before_recording = None
        self.closed = threading.Event()
        self.first_frame = threading.Event()
        self.last_sample = time.monotonic()
        self.start_timeout = float(settings.capture.get("start_timeout", 5))
        self.stale_timeout = float(settings.capture.get("stale_timeout", 2))
        self.start_trigger = settings.capture.get("start_trigger", "arx_buttons")
        self.auto_stop = bool(settings.capture.get("auto_stop", True))
        self.return_pose_threshold = float(
            settings.capture.get("return_pose_threshold", 0.1)
        )
        self.return_pose_min_frames = int(
            settings.capture.get("return_pose_min_frames", 100)
        )
        self.initial_qpos = None
        if self.start_trigger not in {"arx_buttons", "immediate"}:
            raise DomainError("未知 ARX start_trigger")
        if not 0 < self.start_timeout < 40 or not 0 < self.stale_timeout <= 30:
            raise DomainError("ARX start_timeout / stale_timeout 无效")
        for row in self.sessions.values():
            if row.get("conversion_state") == "RUNNING":
                row["conversion_state"] = "QUEUED"
            if row["phase"] in {
                "STARTING",
                "PREPARING",
                "RECORDING",
                "STOPPING",
                "RECOVERY_REQUIRED",
            }:
                if self._empty_button_wait(row):
                    # Continuous capture preallocates the next episode. A restart
                    # cancels an unused wait; it does not interrupt a recording.
                    row.update(phase="STOPPED", cancelled=True, fail=False,
                               error=None, message=None, stopped_at=time.time())
                    if self.row is None or self.row["phase"] == "STOPPED":
                        self.row = row
                else:
                    row.update(
                        phase="RECOVERY_REQUIRED",
                        fail=True,
                        error="Bridge 重启，录制被中断；该 episode 不可封存",
                    )
                    self.row = row
        if self.sessions:
            self._save()
        self.conversion_process = None
        self.conversion_worker = threading.Thread(
            target=self._convert_pending, name="arx-conversion-queue", daemon=True
        )
        self.conversion_worker.start()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _empty_button_wait(self, row):
        if (self.start_trigger != "arx_buttons"
                or row["phase"] not in {"PREPARING", "STARTING", "RECOVERY_REQUIRED"}
                or row.get("frames", 0) != 0
                or row.get("recording_started_at") is not None
                or not row.get("raw_root")):
            return False
        raw = Path(row["raw_root"])
        try:
            roots = [Path(p).expanduser().absolute()
                     for p in self.settings.local_roots.values()]
            if (not raw.is_absolute() or raw.resolve() != raw
                    or not any(raw.is_relative_to(p) and p.resolve() == p for p in roots)):
                return False
            # Frame counters can lag behind disk writes. Any file, including an
            # incomplete raw file, requires recovery and must be preserved.
            return (not raw.exists() or raw.is_dir()) and not any(
                p.is_symlink() or not p.is_dir() for p in raw.rglob("*")
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def _save(self):
        atomic_json(self.state_file, self.sessions)

    def health(self):
        with self.mutex:
            source = self.source.health()
            return {
                **source,
                "ready": source.get("ready", False)
                and not self.closed.is_set()
                and (self.row is None or self.row["phase"] == "STOPPED"),
                "phase": self.row["phase"] if self.row else "IDLE",
                "profile_hash": digest(self.profile),
            }

    def status(self):
        with self.mutex:
            result = dict(self.row or {"phase": "IDLE", "fail": False, "frames": 0})
            # Include earlier groups too: a folder switch must finish/import
            # the old group's saved episodes while the new group is active.
            result["continuous_sessions"] = [
                dict(row) for row in self.sessions.values() if row.get("continuous")
            ]
            result["conversion_sessions"] = [
                {key: row.get(key) for key in (
                    "session_id", "raw_root", "conversion_state", "conversion_error",
                    "conversion_finished_at", "raw_alignment_report",
                )}
                for row in self.sessions.values() if row.get("conversion_state")
            ]
            return result

    def retry_conversion(self, session_id):
        with self.mutex:
            row = self.sessions.get(session_id)
            if row is None:
                raise DomainError("未找到该片段的转换记录", 404)
            if row.get("conversion_state") in {"QUEUED", "RUNNING"}:
                return {"conversion_state": row["conversion_state"]}
            if row.get("conversion_state") != "FAILED":
                raise DomainError("该片段没有失败的转换任务")
            row.update(conversion_state="QUEUED", conversion_error=None,
                       conversion_retry_at=time.time())
            self._save()
            return {"conversion_state": "QUEUED"}

    def _convert_pending(self):
        # One separate process at a time: image conversion cannot hold the
        # recorder's Python GIL, and queued jobs survive Bridge restarts.
        while not self.closed.is_set():
            with self.mutex:
                row = next((r for r in sorted(self.sessions.values(), key=lambda r: r["created_at"])
                            if r.get("conversion_state") == "QUEUED"), None)
                if row is not None:
                    row.update(conversion_state="RUNNING", conversion_error=None)
                    self._save()
            if row is None:
                self.closed.wait(0.5)
                continue
            try:
                raw = Path(row["conversion_raw_path"])
                output = Path(row["conversion_output_path"])
                report_path = raw.parent / "alignment-report.json"
                if not output.exists():
                    command = [sys.executable, "-m", "scripts.convert_arx_raw", str(raw),
                               "--output", str(output), "--background"]
                    if row.get("conversion_max_gap_ms") is not None:
                        command += ["--max-gap-ms", str(row["conversion_max_gap_ms"])]
                    env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
                               MKL_NUM_THREADS="1")
                    with (raw.parent / "conversion.log").open("w") as log:
                        with self.mutex:
                            if self.closed.is_set():
                                row["conversion_state"] = "QUEUED"
                                self._save()
                                return
                            process = subprocess.Popen(command, stdout=log, stderr=log, env=env)
                            self.conversion_process = process
                        code = process.wait()
                    if code:
                        raise RuntimeError((raw.parent / "conversion.log").read_text()[-2000:])
                report = json.loads(report_path.read_text())
                if not report.get("complete") or report.get("session_id") != row["session_id"]:
                    raise DomainError("HDF5 转换未完成，原始数据已保留")
                with self.mutex:
                    row.update(conversion_state="DONE", conversion_error=None,
                               conversion_finished_at=time.time(), raw_alignment_report=report)
                    self._save()
            except Exception as exc:
                with self.mutex:
                    row.update(conversion_state="QUEUED" if self.closed.is_set() else "FAILED",
                               conversion_error=None if self.closed.is_set() else str(exc))
                    self._save()
            finally:
                with self.mutex:
                    self.conversion_process = None

    def control_state(self):
        return self.source.control_state()

    def set_control_mode(self, mode):
        with self.mutex:
            if self.row and self.row["phase"] not in {"STOPPED", "PREPARING"}:
                raise DomainError("请先保存当前 Episode，再切换控制模式", 409)
            if self.row and self.row["phase"] == "PREPARING":
                self.stop(self.row["session_id"])
            result = self.source.set_control_mode(mode)
            self.start_trigger = "arx_buttons" if mode == "capture" else "immediate"
            return result

    def remember_fold(self):
        with self.mutex:
            return {"remembered": True, "references": self.source.remember_fold()}

    def enable(self):
        with self.mutex:
            if self.row and self.row["phase"] not in {"PREPARING", "STOPPED"}:
                raise DomainError("请先结束当前录制或恢复异常会话，不能重复使能")
            pending = self.row and self.row["phase"] == "PREPARING"
            if pending:
                self.source.disarm_recording()
            prepare_control = getattr(self.source, "prepare_control", None)
            if callable(prepare_control):
                prepare_control()
            else:
                # Compatibility adapters still use the old all-sensor startup
                # contract; ARX Ros2Source separates this from motor enable.
                self.source.ensure_observation()
            result = self.source.enable_arm()
            if callable(prepare_control):
                try:
                    self.source.ensure_observation(timeout=5)
                    result["observations_ready"] = True
                except DomainError as exc:
                    result["observations_ready"] = False
                    result["observation_error"] = exc.message
                    result["message"] = "双臂控制器已确认使能，但相机观测未就绪：" + exc.message
            if pending:
                self.row["start_monotonic"] = time.monotonic()
                self.source.arm_recording(self.row["start_monotonic"])
                self.row["message"] = self.source.preparation()["message"]
                self._save()
            return result

    def reset(self):
        with self.mutex:
            if self.validate_before_recording is not None:
                self.validate_before_recording()
            # Check both references before stopping capture or issuing motion.
            self.source.check_reset()
            if self.row and self.row["phase"] != "STOPPED":
                self.stop(self.row["session_id"])
            return self.source.reset_arm()

    @staticmethod
    def _start_result(row):
        return {
            "started": row["phase"] == "RECORDING",
            "pending": row["phase"] in {"PREPARING", "STARTING"},
            "message": row.get("message"),
            "recording_started_at": row.get("recording_started_at"),
        }

    def _open_writer(self, row):
        episode = Path(row["raw_root"]) / "episode0"
        episode.mkdir(parents=True, exist_ok=False)
        if self.recording_mode == "raw_first":
            from .capture_sources.arx_raw import RawFirstWriter

            self.writer = RawFirstWriter(
                episode / CAPTURE_FILENAME,
                self.profile,
                row,
                self.source,
                self.settings.capture,
            )
            row["recording_mode"] = "raw_first"
            return
        self.writer = ArxWriter(
            episode / CAPTURE_FILENAME,
            self.profile,
            row["session_id"],
            row["instruction"],
        )

    def start(
        self,
        session_id,
        raw_root,
        instruction,
        *,
        continuous=False,
        previous_session_id=None,
    ):
        raw = Path(raw_root).absolute()
        if continuous and self.start_trigger != "arx_buttons":
            raise DomainError("连续采集需要 ARX 实体按键触发")
        if continuous and (
            raw.name != "raw"
            or raw.parent.name != session_id
            or raw.parent.parent.name != "sessions"
        ):
            raise DomainError("连续采集必须使用独立 sessions/<session_id>/raw 目录")
        signature = digest(
            {"raw_root": str(raw), "instruction": instruction, "continuous": continuous}
        )
        with self.mutex:
            if session_id in self.sessions:
                row = self.sessions[session_id]
                if row["request_hash"] != signature:
                    raise DomainError("同一 session_id 的启动参数不同")
                if row["phase"] == "RECORDING" and not row["fail"]:
                    return self._start_result(row)
                if (
                    self.start_trigger == "arx_buttons"
                    and row["phase"] in {"PREPARING", "STARTING"}
                    and not row["fail"]
                ):
                    return self._start_result(row)
                if row["phase"] != "STARTING":
                    raise DomainError("会话已结束或需要恢复，不能重新启动")
            else:
                if not self.health()["ready"]:
                    raise DomainError("ARX 观测未就绪或已有采集会话")
                roots = [
                    Path(p).expanduser().absolute()
                    for p in self.settings.local_roots.values()
                ]
                if raw.resolve() != raw or not any(
                    raw.is_relative_to(p) and p.resolve() == p for p in roots
                ):
                    raise DomainError("不在 Bridge 允许的采集根目录内", 400)
                episode = raw / "episode0"
                if episode.exists():
                    raise DomainError("拒绝覆盖已有 episode")
                if self.validate_before_recording is not None:
                    self.validate_before_recording()
                row = {
                    "session_id": session_id,
                    "request_hash": signature,
                    "raw_root": str(raw),
                    "phase": (
                        "PREPARING"
                        if self.start_trigger == "arx_buttons"
                        else "STARTING"
                    ),
                    "fail": False,
                    "error": None,
                    "frames": 0,
                    "start_monotonic": time.monotonic(),
                    "instruction": instruction,
                    "created_at": time.time(),
                    "continuous": continuous,
                }
                if continuous:
                    previous = self.sessions.get(previous_session_id, {})
                    row["continuous_group_id"] = previous.get(
                        "continuous_group_id", session_id
                    )
                    row["previous_session_id"] = previous_session_id
                self.row = row
                self.sessions[session_id] = row
                self.first_frame.clear()
                self.last_sample = time.monotonic()
                self.initial_qpos = None
                self._save()
                try:
                    if self.before_recording is not None:
                        self.before_recording()
                    if self.start_trigger == "arx_buttons":
                        self.source.arm_recording(row["start_monotonic"])
                        row["message"] = self.source.preparation()["message"]
                        self._save()
                        return self._start_result(row)
                    self._open_writer(row)
                except Exception as exc:
                    self._fault(str(exc))
                    raise
        if not self.first_frame.wait(self.start_timeout):
            with self.mutex:
                self._fault("启动超时：未收到首个完整观测；必须停止并检查")
            raise DomainError("ARX 首帧超时，禁止盲目重发 start", 504)
        with self.mutex:
            if row["phase"] != "RECORDING" or row["fail"]:
                raise DomainError(row.get("error") or "ARX 未进入录制状态")
        return self._start_result(row)

    def _returned_to_start(self, sample):
        if (
            not self.auto_stop
            or self.writer is None
            or self.writer.count <= self.return_pose_min_frames
        ):
            return False
        if self.start_trigger == "arx_buttons":
            requested_at = self.row.get("save_requested_at")
            if (
                requested_at is None
                or sample.get("received_monotonic", 0) <= requested_at
            ):
                return False
        qpos = sample.get("qpos") if isinstance(sample, dict) else None
        if qpos is None:
            return False
        values = [float(value) for value in qpos]
        if self.initial_qpos is None:
            return False
        return len(values) == len(self.initial_qpos) and all(
            abs(value - initial) <= self.return_pose_threshold
            for value, initial in zip(values, self.initial_qpos)
        )

    def _finish_recording(self, reason):
        if not self.row or self.row["phase"] not in {"STARTING", "RECORDING"}:
            return
        if self.recording_mode == "raw_first" and self.writer is not None:
            self._begin_raw_finalize(reason, continuous=True)
            return
        if self.writer:
            self.row.update(phase="STOPPING", message="采集结束，正在保存并对齐数据")
            self._save()
            self.writer.close(complete=not self.row.get("fail"))
            self.row["frames"] = self.writer.count
            if getattr(self.writer, "report", None) is not None:
                self.row["raw_alignment_report"] = self.writer.report
            self.writer = None
        self.row.update(
            phase="STOPPED", stop_reason=reason, stopped_at=time.time(), message=None
        )
        if self.start_trigger == "arx_buttons":
            self.source.disarm_recording()
        self._save()
        self.first_frame.set()
        completed = self.row
        self._continue_recording(completed)

    def select_next(self, session_id, raw_root, instruction, *, continuous=False):
        with self.mutex:
            current = self.row
            if not current or current["phase"] in {"STOPPED", "RECOVERY_REQUIRED"}:
                raise DomainError("当前没有可切换的连续采集会话，请重新进入采集准备")
            if not current.get("continuous") or not continuous:
                raise DomainError("当前采集模式不支持录制中切换下一段目录")
            raw = Path(raw_root).absolute()
            roots = [Path(p).expanduser().absolute() for p in self.settings.local_roots.values()]
            if raw.resolve() != raw or not any(raw.is_relative_to(root) for root in roots):
                raise DomainError("不在 Bridge 允许的采集根目录内", 400)
            if session_id == current["session_id"]:
                current.pop("next_capture", None)
                self._save()
                return {"applied": "current"}
            target = {"session_id": session_id, "raw_root": str(raw),
                      "instruction": instruction, "continuous": continuous}
            if current["phase"] in {"PREPARING", "STARTING"} and self.writer is None:
                # Stop/rearm under the same mutex as start-key handling. Never
                # stop a recording that began between a Client status and here.
                self.stop(current["session_id"])
                self.start(session_id, raw, instruction, continuous=continuous)
                return {"applied": "waiting"}
            current["next_capture"] = target
            if current["phase"] == "STOPPING":
                current["continue_after_save"] = True
            self._save()
            return {"applied": "next"}

    def _continue_recording(self, completed):
        if completed.get("continuous") and not completed.get("fail"):
            # Rearm here, before any Client poll, so closing the browser or
            # delaying a status request cannot prevent the next physical press.
            target = completed.get("next_capture")
            next_id = target["session_id"] if target else str(uuid.uuid4())
            next_raw = (Path(target["raw_root"]) if target else
                        Path(completed["raw_root"]).parent.parent / next_id / "raw")
            try:
                if (
                    shutil.disk_usage(next_raw.parent.parent).free
                    < self.settings.min_free_bytes
                ):
                    raise DomainError("磁盘低水位，连续采集已停止")
                self.start(
                    next_id,
                    next_raw,
                    target["instruction"] if target else completed["instruction"],
                    continuous=True,
                    previous_session_id=None if target else completed["session_id"],
                )
            except Exception as exc:
                completed["continuation_error"] = str(exc)
                self._save()

    def _begin_raw_finalize(self, reason, *, continuous):
        # Called under the recorder mutex. Detach first, so all subsequently
        # arriving messages belong to the next episode's prebuffer only.
        row, writer = self.row, self.writer
        self.source.end_raw(writer.journal)
        row.update(
            phase="STOPPING",
            message="正在保存原始数据",
            stop_reason=reason,
            continue_after_save=continuous,
        )
        if self.start_trigger == "arx_buttons":
            self.source.disarm_recording()
        self._save()

        def finalize():
            try:
                writer.close_raw(complete=not row.get("fail"))
                with self.mutex:
                    self.writer = None
                    row.update(frames=writer.count)
                    if self.closed.is_set() or row.get("fail"):
                        self._save()
                        return
                    row.update(phase="STOPPED", stopped_at=time.time(), message=None,
                               conversion_state="QUEUED", conversion_error=None,
                               conversion_raw_path=str(writer.journal.path),
                               conversion_output_path=str(writer.path),
                               conversion_max_gap_ms=writer.max_gap_ms)
                    self._save()
                    self.first_frame.set()
                    if row.get("continue_after_save"):
                        self._continue_recording(row)
            except Exception as exc:
                with self.mutex:
                    self._fault(str(exc))
            finally:
                with self.mutex:
                    if self.finalizing_writer is writer:
                        self.finalizing_writer = None

        self.finalizing_writer = writer
        self.finalizer = threading.Thread(
            target=finalize, name="arx-raw-converter", daemon=True
        )
        self.finalizer.start()

    def _fault(self, error):
        if self.row and self.row["phase"] not in {"STOPPED", "RECOVERY_REQUIRED"}:
            self.row.update(
                fail=True,
                error=self.row.get("error") or error,
                phase="RECOVERY_REQUIRED",
            )
            finalizing = self.finalizer is not None and self.finalizer.is_alive()
            if (
                self.recording_mode == "raw_first"
                and self.writer is not None
                and (not finalizing or threading.current_thread() is self.finalizer)
            ):
                try:
                    self.writer.close(complete=False)
                except Exception as exc:
                    self.row["raw_close_error"] = str(exc)
            self._save()
        self.first_frame.set()

    def stop(self, session_id):
        with self.mutex:
            row = self.sessions.get(session_id)
            if row is None:
                raise DomainError("未知会话，不能确认停止")
            if row["phase"] == "STOPPED":
                result = {"stopped": True, "fail": row["fail"], "error": row["error"],
                          **{key: row[key] for key in ("conversion_state", "conversion_error") if key in row}}
                if row.get("cancelled"):
                    result["cancelled"] = True
                if (
                    self.start_trigger == "arx_buttons"
                    and row.get("recording_started_at") is not None
                ):
                    result["recording_started_at"] = row["recording_started_at"]
                return result
            if row is not self.row:
                raise DomainError("采集会话 owner 不匹配")
            if (
                self.recording_mode == "raw_first"
                and self.writer is not None
                and self.finalizing_writer is self.writer
            ):
                row["continue_after_save"] = False
                self._save()
                return {
                    "stopped": False,
                    "pending": True,
                    "message": row.get("message"),
                }
            if (
                self.recording_mode == "raw_first"
                and not row["fail"]
                and self.writer is not None
            ):
                if row["phase"] != "STOPPING":
                    self._begin_raw_finalize("操作员结束录制", continuous=False)
                return {
                    "stopped": False,
                    "pending": True,
                    "message": row.get("message"),
                }
            pending = self.writer is None and row["frames"] == 0
            row["phase"] = "STOPPING"
            self._save()
            try:
                if self.writer:
                    self.writer.close(complete=not row["fail"])
                    row["frames"] = self.writer.count
                    if getattr(self.writer, "report", None) is not None:
                        row["raw_alignment_report"] = self.writer.report
                    self.writer = None
                elif pending:
                    row["cancelled"] = True
                elif not row["fail"]:
                    raise DomainError("无法证明 writer 已完成")
            except Exception as exc:
                self._fault(str(exc))
                raise
            row.update(phase="STOPPED", stopped_at=time.time())
            if self.start_trigger == "arx_buttons":
                self.source.disarm_recording()
            self._save()
            self.first_frame.set()
            return self.stop(session_id)

    def _loop(self):
        period = 1 / self.profile["output_fps"]
        while not self.closed.is_set():
            begun = time.monotonic()
            try:
                writer = self.writer
                if (
                    writer is not None
                    and self.recording_mode == "raw_first"
                    and self.row["phase"] in {"STARTING", "RECORDING"}
                ):
                    writer.check()
                sample = self.source.read()
                with self.mutex:
                    if (
                        self.row
                        and self.start_trigger == "arx_buttons"
                        and self.row["phase"] in {"PREPARING", "STARTING"}
                    ):
                        preparation = self.source.preparation()
                        if preparation["triggered_at"] is not None and (
                            self.row["phase"] == "PREPARING"
                            or self.row["start_monotonic"]
                            != preparation["triggered_at"]
                        ):
                            self.row.update(
                                phase="STARTING",
                                start_monotonic=preparation["triggered_at"],
                                message="已收到重力补偿按键，等待新的完整观测",
                            )
                            self._save()
                        elif preparation["triggered_at"] is None and (
                            self.row["phase"] != "PREPARING"
                            or self.row.get("message") != preparation["message"]
                        ):
                            self.row.update(
                                phase="PREPARING", message=preparation["message"]
                            )
                            self._save()
                    if (
                        self.row
                        and self.start_trigger == "arx_buttons"
                        and self.row["phase"] == "RECORDING"
                        and self.row.get("save_requested_at") is None
                    ):
                        requested_at = self.source.preparation().get(
                            "save_requested_at"
                        )
                        if requested_at is not None:
                            self.row.update(
                                save_requested_at=requested_at,
                                message="已收到回位键 data[1]，等待回到起始姿态后自动保存",
                            )
                            self._save()
                    if self.row and self.row["phase"] in {"STARTING", "RECORDING"}:
                        if (
                            sample is not None
                            and sample["timestamp"] >= self.row["start_monotonic"]
                            and (
                                self.start_trigger == "immediate"
                                or sample["received_monotonic"]
                                > self.row["start_monotonic"]
                            )
                        ):
                            if self.writer is None:
                                self._open_writer(self.row)
                            self.writer.append(sample)
                            self.last_sample = time.monotonic()
                            first = self.row["phase"] == "STARTING"
                            if (
                                self.initial_qpos is None
                                and sample.get("qpos") is not None
                            ):
                                self.initial_qpos = [
                                    float(value) for value in sample["qpos"]
                                ]
                            self.row.update(phase="RECORDING", frames=self.writer.count)
                            if first:
                                self.row["recording_started_at"] = time.time()
                                self.row["message"] = None
                                if self.start_trigger == "arx_buttons":
                                    self.source.recording_started(
                                        self.row["start_monotonic"]
                                    )
                                self._save()
                                self.first_frame.set()
                            if self._returned_to_start(sample):
                                self._finish_recording(
                                    "data[1] 回位完成，自动保存"
                                    if self.start_trigger == "arx_buttons"
                                    else "机械臂回到起始姿态"
                                )
                        elif (
                            self.row["phase"] == "STARTING"
                            and self.start_trigger == "arx_buttons"
                            and time.monotonic() - self.row["start_monotonic"]
                            > self.start_timeout
                        ):
                            self._fault("按键后未收到新的完整观测，录制没有开始")
                        elif (
                            self.row["phase"] == "RECORDING"
                            and time.monotonic() - self.last_sample > self.stale_timeout
                        ):
                            self._fault("ARX 完整观测流中断")
            except Exception as exc:
                with self.mutex:
                    self._fault(str(exc))
            self.closed.wait(max(0, period - (time.monotonic() - begun)))

    def close(self):
        self.closed.set()
        self.thread.join(timeout=5)
        if self.finalizer is not None:
            self.finalizer.join(timeout=30)
        with self.mutex:
            if self.row and self.row["phase"] in {
                "PREPARING",
                "STARTING",
                "RECORDING",
                "STOPPING",
            }:
                self._fault("Bridge 关闭，中断的采集不能封存")
            if self.writer and not (
                self.finalizer is not None and self.finalizer.is_alive()
            ):
                self.writer.close(complete=False)
                self.writer = None
        with self.mutex:
            process = self.conversion_process
            if process is not None and process.poll() is None:
                process.terminate()
        self.conversion_worker.join(timeout=5)
        self.source.close()


def create_bridge(settings, source_factory=None, replay_factory=None):
    if source_factory is None:
        from .capture_sources.arx_ros2 import ArxRos2Source

        source_factory = ArxRos2Source
    lock = ProcessLock(settings.root / "bridge.lock")

    @asynccontextmanager
    async def lifespan(app):
        lock.acquire()
        recorder = None
        source = None
        replay = None
        try:
            source = source_factory(settings)
            recorder = ArxRecorder(settings, source)
            app.state.recorder = recorder
            if (
                settings.replay.get("enabled")
                and settings.replay.get("backend") == "capture_bridge"
            ):
                if replay_factory is None:
                    from .arx_replay_bridge import create_replay_control

                    factory = create_replay_control
                else:
                    factory = replay_factory
                replay = factory(settings, recorder, source)
                recorder.before_recording = replay.release_for_capture
                recorder.validate_before_recording = replay.check_capture_allowed
            app.state.replay_control = replay
            yield
        finally:
            try:
                try:
                    if replay:
                        replay.close()
                finally:
                    if recorder:
                        recorder.close()
                    elif source:
                        source.close()
            finally:
                lock.release()

    app = FastAPI(title="ARX capture bridge", lifespan=lifespan)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if not hmac.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + settings.bridge_token
        ):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(DomainError)
    async def error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.code)

    @app.get("/health")
    def health():
        return app.state.recorder.health()

    @app.get("/status")
    def status():
        return app.state.recorder.status()

    @app.get("/control-mode")
    def control_state():
        return app.state.recorder.control_state()

    @app.post("/control-mode")
    def control_mode(body: dict):
        return app.state.recorder.set_control_mode(body.get("mode"))

    @app.post("/remember-fold")
    def remember_fold():
        return app.state.recorder.remember_fold()

    @app.post("/enable")
    def enable():
        return app.state.recorder.enable()

    @app.post("/reset")
    def reset():
        return app.state.recorder.reset()

    @app.post("/start")
    def start(body: StartRequest):
        return app.state.recorder.start(
            body.session_id, body.raw_root, body.instruction, continuous=body.continuous
        )

    @app.post("/conversions/{session_id}/retry")
    def retry_conversion(session_id: str):
        return app.state.recorder.retry_conversion(session_id)

    @app.post("/next")
    def select_next(body: StartRequest):
        return app.state.recorder.select_next(
            body.session_id, body.raw_root, body.instruction, continuous=body.continuous
        )

    @app.post("/stop")
    def stop(body: StopRequest):
        return app.state.recorder.stop(body.session_id)

    @app.get("/joints")
    def joints():
        return app.state.recorder.source.joints()

    @app.get("/frames/{camera_id}")
    def frame(camera_id: str):
        if camera_id not in CAMERAS:
            raise DomainError("未知相机", 404)
        data = app.state.recorder.source.frame(camera_id)
        if data is None:
            raise DomainError("没有新鲜预览帧", 503)
        return Response(data, media_type="image/jpeg")

    def replay_control():
        control = app.state.replay_control
        if control is None:
            raise DomainError("当前 Bridge 未启用共用采集驱动的回放后端", 503)
        return control

    @app.get("/replay/health")
    def replay_health():
        return replay_control().health()

    @app.get("/replay/status")
    def replay_status():
        return replay_control().status()

    @app.post("/replay/start")
    def replay_start(body: dict):
        return replay_control().start(body)

    @app.post("/replay/heartbeat")
    def replay_heartbeat(body: dict):
        return replay_control().heartbeat(body.get("owner"))

    @app.post("/replay/stop")
    def replay_stop(body: dict):
        return replay_control().stop(body.get("owner"))

    return app
