"""Independent ARX message capture, with no image processing on ROS callbacks.

The raw SQLite journal stores the original compressed image bytes, arm values,
integer ROS header times and host monotonic receipt times. A bounded worker
queue makes overload an explicit capture failure instead of silently dropping.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
import uuid
from collections import Counter, deque
from pathlib import Path

import numpy as np

from pipeline.shared.arx import CAMERAS, SOURCES, ArxWriter
from pipeline.shared.capture_formats import CAPTURE_FILENAME
from pipeline.shared.core import DomainError, atomic_json, fsync_dir

RAW_SCHEMA = "arx_independent_raw_v1"
RAW_FILENAME = Path(CAPTURE_FILENAME).with_suffix(".raw").name


def message_record(name, msg, received_ns):
    stamp = msg.header.stamp
    source_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
    if name in CAMERAS:
        payload = bytes(msg.data)
    else:
        payload = {
            "qpos": list(msg.joint_pos),
            "qvel": list(msg.joint_vel),
            "effort": list(msg.joint_cur),
            "eef": [*msg.end_pos, msg.joint_pos[-1]],
        }
    return (name, source_ns, int(received_ns), payload, str(msg.header.frame_id))


class RawStreamTap:
    """Keep a short prebuffer so observing a start key never consumes sensor data.

    Callbacks from the duplicate controller subscription must NOT enter this tap;
    each physical arm stream is recorded through its feedback subscription only.
    """

    def __init__(self, history_frames=256):
        self.lock = threading.Lock()
        self.history_frames = history_frames
        self.history = {name: deque() for name in SOURCES}
        self.evicted_through = {name: -1 for name in SOURCES}
        self.sink = None
        self.last_control = {}

    def observe(self, name, msg, received_ns):
        record = message_record(name, msg, received_ns)
        with self.lock:
            queue = self.history[name]
            if len(queue) >= self.history_frames:
                self.evicted_through[name] = queue.popleft()[2]
            queue.append(record)
            if self.sink is not None:
                self.sink.enqueue(record)

    def begin(self, sink, since):
        boundary = int(since * 1_000_000_000)
        with self.lock:
            if self.sink is not None:
                raise DomainError("已有独立 raw 采集会话")
            if any(at > boundary for at in self.evicted_through.values()):
                raise DomainError("启动前缓冲不足，拒绝静默丢弃按键后的 raw 消息")
            buffered = sorted(
                (r for q in self.history.values() for r in q if r[2] > boundary),
                key=lambda r: r[2],
            )
            for record in buffered:
                sink.enqueue(record)
            sink.check()
            self.sink = sink

    def end(self, sink):
        with self.lock:
            if self.sink is sink:
                self.sink = None

    def control_sample(self):
        # This snapshot controls start/return-to-pose only. It never selects or
        # discards the messages written to the independent raw journal.
        with self.lock:
            if any(not q for q in self.history.values()):
                return None
            rows = {name: q[-1] for name, q in self.history.items()}
            if any(r[2] <= self.last_control.get(name, -1) for name, r in rows.items()):
                return None
            self.last_control = {name: r[2] for name, r in rows.items()}
        arms = [rows[name][3] for name in ("left_arm", "right_arm")]
        return {
            **{
                key: [v for arm in arms for v in arm[key]]
                for key in ("qpos", "qvel", "effort", "eef")
            },
            "timestamp": time.monotonic(),
            "received_monotonic": min(r[2] for r in rows.values()) / 1e9,
            "source_timestamps": {name: r[1] / 1e9 for name, r in rows.items()},
        }


class RawJournal:
    def __init__(
        self,
        path,
        metadata,
        *,
        max_queue_bytes=128 * 1024 * 1024,
        max_queue_records=8192,
    ):
        self.path = Path(path)
        with self.path.open("xb"):
            pass
        self.metadata = dict(metadata, schema=RAW_SCHEMA, complete=False)
        self.condition = threading.Condition()
        self.pending = deque()
        self.pending_bytes = 0
        self.max_queue_bytes = max_queue_bytes
        self.max_queue_records = max_queue_records
        self.counts = Counter()
        self.rejected = 0
        self.error = None
        self.stopping = False
        self.complete = False
        self.ready = threading.Event()
        self.thread = threading.Thread(
            target=self._write, daemon=True, name="arx-raw-writer"
        )
        self.thread.start()
        if not self.ready.wait(10):
            self.close(complete=False)
            raise DomainError("raw 写盘线程启动超时")
        self.check()

    def enqueue(self, record):
        size = len(record[3]) if isinstance(record[3], bytes) else 1024
        with self.condition:
            if self.stopping or self.error:
                self.rejected += 1
                return
            if (
                self.pending_bytes + size > self.max_queue_bytes
                or len(self.pending) >= self.max_queue_records
            ):
                self.error = (
                    "raw 写盘队列溢出：采集失败，已接收的 raw 数据保留；禁止补帧"
                )
                self.rejected += 1
                return
            self.pending.append((record, size))
            self.pending_bytes += size
            self.counts[record[0]] += 1
            self.condition.notify()

    def check(self):
        if self.error:
            raise DomainError(self.error)

    def _write(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE samples (id INTEGER PRIMARY KEY, stream TEXT NOT NULL, source_ns INTEGER NOT NULL, received_ns INTEGER NOT NULL, payload BLOB NOT NULL, frame_id TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO metadata VALUES (?, ?)",
                [(k, json.dumps(v, allow_nan=False)) for k, v in self.metadata.items()],
            )
            connection.commit()
            self.ready.set()
            last_commit = time.monotonic()
            while True:
                with self.condition:
                    if not self.pending and not self.stopping:
                        self.condition.wait(0.1)
                    batch = [
                        self.pending.popleft()
                        for _ in range(min(256, len(self.pending)))
                    ]
                    done = self.stopping and not self.pending
                values = []
                for (name, source_ns, received_ns, payload, frame_id), _ in batch:
                    if not isinstance(payload, bytes):
                        payload = json.dumps(
                            payload, allow_nan=False, separators=(",", ":")
                        ).encode()
                    values.append((name, source_ns, received_ns, payload, frame_id))
                connection.executemany(
                    "INSERT INTO samples (stream, source_ns, received_ns, payload, frame_id) VALUES (?, ?, ?, ?, ?)",
                    values,
                )
                with self.condition:
                    self.pending_bytes -= sum(size for _, size in batch)
                if done or time.monotonic() - last_commit >= 0.25:
                    connection.commit()
                    last_commit = time.monotonic()
                if done:
                    break
            final = dict(
                complete=bool(self.complete and not self.error),
                counts=dict(self.counts),
                error=self.error,
                rejected_messages=self.rejected,
            )
            connection.executemany(
                "INSERT OR REPLACE INTO metadata VALUES (?, ?)",
                [(k, json.dumps(v)) for k, v in final.items()],
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:
            with self.condition:
                self.error = self.error or f"raw 写盘失败：{type(exc).__name__}: {exc}"
        finally:
            if connection is not None:
                connection.close()
            self.ready.set()

    def close(self, *, complete):
        with self.condition:
            self.complete = complete
            self.stopping = True
            self.condition.notify_all()
        self.thread.join(timeout=30)
        if self.thread.is_alive():
            raise DomainError("raw 写盘未完成，文件保持未完成状态")
        with self.path.open("rb") as stream:
            os.fsync(stream.fileno())
        fsync_dir(self.path.parent)
        if complete:
            self.check()


def match_source_indices(indexes, reference):
    """Align each stream to the head acquisition times using nearest messages."""
    chosen = {}
    for name, rows in indexes.items():
        times = rows[:, 1]
        right = np.searchsorted(times, reference[:, 1]).clip(0, len(times) - 1)
        left = (right - 1).clip(0, len(times) - 1)
        chosen[name] = np.where(
            np.abs(times[right] - reference[:, 1])
            < np.abs(times[left] - reference[:, 1]),
            right,
            left,
        )
    return chosen


def convert_raw(raw_path, output, profile, *, max_gap_ms=None):
    """Align available messages by actual ROS source times.

    Payloads stay on disk until selected; only the timestamp index is in memory.
    Camera gaps and temporal offsets are reported without rejecting the episode.
    """
    raw_path, output = Path(raw_path), Path(output)
    report_path = raw_path.parent / "alignment-report.json"
    report = {
        "schema": RAW_SCHEMA,
        "reference": "head",
        "clock": "ROS header.stamp",
        "method": "nearest source message on head acquisition time; source reuse is reported",
        "streams": {},
        "errors": [],
        "warnings": [],
    }
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.aligning")
    writer = None
    connection = None
    try:
        if output.exists():
            raise DomainError("对齐目标已存在，拒绝覆盖 raw 或已有 HDF5")
        connection = sqlite3.connect(f"{raw_path.resolve().as_uri()}?mode=ro", uri=True)
        metadata = {
            k: json.loads(v)
            for k, v in connection.execute("SELECT key, value FROM metadata")
        }
        if (
            metadata.get("schema") != RAW_SCHEMA
            or not metadata.get("complete")
            or metadata.get("error")
        ):
            raise DomainError("raw 采集尚未完整结束或已失败，不得转换为完整 HDF5")
        report["session_id"] = metadata["session_id"]
        report["clock_verified"] = profile.get("clock_verified") is True
        gap_bound = (
            1500 / profile["output_fps"] if max_gap_ms is None else float(max_gap_ms)
        )
        if not math.isfinite(gap_bound) or gap_bound <= 1000 / profile["output_fps"]:
            raise DomainError("raw 帧间隔上限必须大于一个目标帧周期")
        indexes = {}
        for name in SOURCES:
            rows = np.asarray(
                connection.execute(
                    "SELECT id, source_ns, received_ns FROM samples WHERE stream=? ORDER BY id",
                    (name,),
                ).fetchall(),
                dtype=np.int64,
            )
            if rows.ndim != 2 or rows.shape[0] < 2:
                raise DomainError(f"raw 数据不足：{name}")
            delta = np.diff(rows[:, 1])
            receipt_delta = np.diff(rows[:, 2])
            spec = {
                "received": len(rows),
                "duplicate_timestamps": int(np.sum(delta == 0)),
                "backwards_timestamps": int(np.sum(delta < 0)),
                "max_gap_ms": float(delta.max() / 1e6),
                "gaps_over_bound": int(np.sum(delta / 1e6 > gap_bound)),
            }
            report["streams"][name] = spec
            if (
                np.any(rows[:, 1] <= 0)
                or np.any(delta <= 0)
                or np.any(receipt_delta <= 0)
            ):
                raise DomainError(
                    f"raw 时间戳重复或倒退：{name}；原始消息保留，不自动排序掩盖错误"
                )
            if name in CAMERAS and spec["gaps_over_bound"]:
                report["warnings"].append(
                    f"{name} 存在超过 {gap_bound:g} ms 的原始相机帧间隔"
                )
            indexes[name] = rows
        head = indexes["head"]
        reference = head
        chosen = match_source_indices(indexes, reference)
        report["frames"] = len(reference)
        report["boundary_trimmed_head_frames"] = 0
        report["unmatched_head_frames"] = 0
        for name in SOURCES:
            unique = len(np.unique(chosen[name]))
            report["streams"][name].update(
                selected=len(reference),
                selected_unique=unique,
                reused_frames=int(len(reference) - unique),
                unselected=int(len(indexes[name]) - unique),
            )
        report["repeated_frames"] = sum(
            report["streams"][name]["reused_frames"] for name in CAMERAS
        )
        writer = ArxWriter(
            temporary, profile, metadata["session_id"], metadata["instruction"]
        )
        source_ids, source_ns, received_ns = [], [], []
        for out_index in range(len(reference)):
            records = {}
            for name in SOURCES:
                row = indexes[name][chosen[name][out_index]]
                payload = connection.execute(
                    "SELECT payload FROM samples WHERE id=?", (int(row[0]),)
                ).fetchone()[0]
                records[name] = payload if name in CAMERAS else json.loads(payload)
            arms = [records[name] for name in ("left_arm", "right_arm")]
            stamp = int(reference[out_index, 2]) / 1e9
            writer.append(
                {
                    **{
                        key: [v for arm in arms for v in arm[key]]
                        for key in ("qpos", "qvel", "effort", "eef")
                    },
                    "timestamp": stamp,
                    "source_timestamps": {
                        name: int(indexes[name][chosen[name][out_index], 1]) / 1e9
                        for name in SOURCES
                    },
                    "images": {name: records[name] for name in CAMERAS},
                }
            )
            source_ids.append(
                [int(indexes[name][chosen[name][out_index], 0]) for name in SOURCES]
            )
            source_ns.append(
                [int(indexes[name][chosen[name][out_index], 1]) for name in SOURCES]
            )
            received_ns.append(
                [int(indexes[name][chosen[name][out_index], 2]) for name in SOURCES]
            )
        handle = writer.handle
        handle.attrs.update(
            recording_mode="raw_first", alignment_clock="source", raw_schema=RAW_SCHEMA
        )
        handle.create_dataset(
            "timestamps/reference_source_ns", data=reference[:, 1], dtype="int64"
        )
        for key, values in (
            ("source_sample_id", source_ids),
            ("timestamps/source_ns", source_ns),
            ("timestamps/received_monotonic_ns", received_ns),
        ):
            dataset = handle.create_dataset(key, data=values, dtype="int64")
            dataset.attrs["columns"] = list(SOURCES)
        report["max_sensor_skew_ms"] = float(
            np.ptp(np.asarray(source_ns, dtype=np.int64), axis=1).max() / 1e6
        )
        if report["max_sensor_skew_ms"] > profile["max_sensor_skew_ms"]:
            report["warnings"].append(
                "部分帧的传感器时间偏差超过 Profile 参考值，已按最近时间对齐"
            )
        report["complete"] = True
        handle.attrs["raw_alignment_report"] = json.dumps(report, ensure_ascii=False)
        writer.close(complete=True)
        writer = None
        # Atomic publication without replacing an output another conversion
        # might have created while this one was still processing.
        os.link(temporary, output)
        fsync_dir(output.parent)
    except Exception as exc:
        report["complete"] = False
        if str(exc) not in report["errors"]:
            report["errors"].append(str(exc))
        raise
    finally:
        try:
            if writer is not None:
                writer.close(complete=False)
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                temporary.unlink(missing_ok=True)
                atomic_json(report_path, report)
    return report


class RawFirstWriter:
    """Recorder lifecycle adapter: capture raw now, convert after detaching ROS."""

    def __init__(self, path, profile, row, source, capture):
        self.path, self.profile, self.source = Path(path), profile, source
        self.count = 0
        self.closed = False
        self.report = None
        self.max_gap_ms = capture.get("raw_max_gap_ms")
        self.journal = RawJournal(
            self.path.parent / RAW_FILENAME,
            {
                "session_id": row["session_id"],
                "instruction": row["instruction"],
                "profile": profile,
                "start_monotonic": row["start_monotonic"],
            },
            max_queue_bytes=int(capture.get("raw_queue_bytes", 128 * 1024 * 1024)),
        )
        try:
            source.begin_raw(self.journal, row["start_monotonic"])
        except Exception:
            self.journal.close(complete=False)
            raise

    def check(self):
        self.journal.check()

    def append(self, sample):
        self.check()
        with self.journal.condition:
            self.count = self.journal.counts["head"]

    def close_raw(self, *, complete):
        if self.closed:
            return
        self.source.end_raw(self.journal)
        self.journal.close(complete=complete)
        self.closed = True
        self.count = self.journal.counts["head"]

    def close(self, *, complete):
        if self.closed:
            return
        self.close_raw(complete=complete)
        if complete:
            self.report = convert_raw(
                self.journal.path, self.path, self.profile, max_gap_ms=self.max_gap_ms
            )
            self.count = self.report["frames"]
        else:
            self.report = {
                "schema": RAW_SCHEMA,
                "complete": False,
                "errors": [self.journal.error or "采集未完整结束，不执行对齐"],
                "received": dict(self.journal.counts),
                "rejected_messages": self.journal.rejected,
            }
            atomic_json(self.path.parent / "alignment-report.json", self.report)
