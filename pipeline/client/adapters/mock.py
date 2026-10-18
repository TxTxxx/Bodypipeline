from __future__ import annotations

import io
import json
import math
import threading
import time

from PIL import Image, ImageDraw

from pipeline.shared.core import DomainError


class MockAdapter:
    """Synthetic teleoperation data. It never connects to ROS or controls hardware."""

    def __init__(self, profile):
        self.profile = profile
        self.session = None
        self.stop_event = threading.Event()
        self.thread = None
        self.count = 0
        self.error = None

    def frame(self, camera_id):
        colors = {"left": (37, 73, 98), "front": (35, 84, 76), "right": (72, 57, 97)}
        im = Image.new("RGB", (640, 360), colors.get(camera_id, (40, 50, 60)))
        draw = ImageDraw.Draw(im)
        for x in range(0, 640, 40):
            draw.line((x, 0, x, 360), fill=(70, 88, 100))
        t = time.time()
        x = int(200 + 100 * math.sin(t))
        draw.rectangle((x, 140, x + 100, 240), fill=(207, 163, 91))
        draw.line((60, 300, x, 190), fill=(222, 231, 236), width=13)
        draw.text((20, 20), f"MOCK / {camera_id.upper()} / NO ROBOT", fill="white")
        draw.text(
            (20, 42),
            f"Frames: {self.count}    {time.strftime('%H:%M:%S')}",
            fill="white",
        )
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=75)
        return out.getvalue()

    def health(self):
        return {"ready": True, "mode": "mock", "detail": "模拟模式，不连接机器人"}

    def start(self, session_id, raw_root, instruction):
        if self.session:
            raise DomainError("已有模拟采集进行中")
        episode = raw_root / "episode0"
        if episode.exists():
            raise DomainError("拒绝覆盖已有 episode")
        for c in self.profile["cameras"]:
            (episode / "camera/color" / c["id"]).mkdir(parents=True)
        roles = self.profile["state_streams"] + self.profile["action_streams"]
        for role in roles:
            (episode / "arm/jointState" / role).mkdir(parents=True)
        (episode / "instructions.json").write_text(
            json.dumps({"full-instructions": [instruction]})
        )
        self.session, self.count, self.error = session_id, 0, None
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._record, args=(episode, roles), daemon=True
        )
        self.thread.start()
        return {"started": True}

    def _record(self, episode, roles):
        start = time.time()
        fps = self.profile["output_fps"]
        try:
            while not self.stop_event.is_set():
                stamp = start + self.count / fps
                for i, role in enumerate(roles):
                    data = {
                        "position": [
                            round(0.3 * math.sin(self.count / 12 + j + i * 0.05), 6)
                            for j in range(7)
                        ],
                        "velocity": [],
                        "effort": [],
                    }
                    (
                        episode / "arm/jointState" / role / f"{stamp:.6f}.json"
                    ).write_text(json.dumps(data))
                for c in self.profile["cameras"]:
                    (
                        episode / "camera/color" / c["id"] / f"{stamp:.6f}.jpg"
                    ).write_bytes(self.frame(c["id"]))
                self.count += 1
                self.stop_event.wait(max(0, start + self.count / fps - time.time()))
        except Exception as exc:
            self.error = str(exc)

    def stop(self, session_id):
        if self.session != session_id:
            raise DomainError("模拟采集 owner 不匹配，无法证明已停止")
        self.stop_event.set()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise DomainError("停止超时，禁止清理目录")
        self.session = None
        return {"stopped": True, "fail": bool(self.error), "error": self.error}

    def status(self):
        return {
            "session_id": self.session,
            "frames": self.count,
            "fail": bool(self.error),
            "error": self.error,
        }

    def close(self):
        if self.session:
            self.stop(self.session)
