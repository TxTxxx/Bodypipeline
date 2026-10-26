"""Start the ARX observation stack used by the collection launcher.

The launcher mirrors the hardware portion of ROS2_AC-one_Play/tools/01_collect.sh:
CAN interfaces are initialized on explicit enable, then the arm controllers, arx_joy and the
three RealSense nodes are started. The legacy collect.py recorder is deliberately
not started; BodyPipeline owns recording after the stack is ready.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

from pipeline.shared.core import DomainError


class _AttachedProcess:
    """Retain ownership of a trusted launcher across a Bridge-only restart."""

    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        try:
            state = (Path("/proc") / str(self.pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]
            return 0 if state == "Z" else None
        except OSError:
            return 0

    def wait(self, timeout=5):
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(["ARX launcher", str(self.pid)], timeout)
            time.sleep(0.05)
        return 0


class ArxObservationLauncher:
    def __init__(self, settings):
        cfg = settings.capture.get("hardware", {})
        self.robot_root = Path(
            cfg.get("robot_root", "/opt/ARXroboticsX/ARX_X5")
        ).expanduser().resolve()
        self.project_root = Path(
            cfg.get("project_root", "/opt/ARXroboticsX/ROS2_AC-one_Play")
        ).expanduser().resolve()
        self.ros_setup = Path(cfg.get("ros_setup", "/opt/ros/jazzy/setup.bash"))
        self.camera_serials = cfg.get(
            "camera_serials",
            {
                "camera_h": "REPLACE_HEAD_SERIAL",
                "camera_l": "REPLACE_LEFT_SERIAL",
                "camera_r": "REPLACE_RIGHT_SERIAL",
            },
        )
        profiles = cfg.get("controller_profiles_path")
        self.controller_profiles = json.loads(Path(profiles).read_text()) if profiles else None
        self.resident_control = bool(cfg.get("resident_control"))
        self.control_mode = cfg.get("control_mode", "capture")
        if self.control_mode not in {"capture", "inference"}:
            raise DomainError("未知 ARX 控制模式")
        self.controller_launch = ("v2_joint_control.launch.py"
                                  if self.control_mode == "inference" else "v2_collect.launch.py")
        self.state_root = settings.root / "hardware"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.mode_file = self.state_root / "control-mode.json"
        if self.mode_file.exists():
            saved = json.loads(self.mode_file.read_text()).get("mode")
            if saved in {"capture", "inference"}:
                self.control_mode = saved
                self.controller_launch = "v2_collect.launch.py" if saved == "capture" else "v2_joint_control.launch.py"
        if self.resident_control:
            self.controller_launch = "v2_workbench.launch.py"
        self.processes: dict[str, subprocess.Popen] = {}
        self.logs: dict[str, object] = {}
        self.lock = threading.RLock()

    @staticmethod
    def _command_line(pid: int) -> str:
        try:
            return (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(
                b"\0", b" "
            ).decode(errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _matching_launchers(marker):
        matches = []
        expected = marker.split()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                args = [part.decode(errors="replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
                # Match real ros2 argv, never text embedded in a shell/script.
                start = next((i for i, arg in enumerate(args) if Path(arg).name == "ros2"), None)
                if start is None:
                    continue
                command = ["ros2", *args[start + 1:]]
                found = (command[:len(expected)] == expected if expected[0] == "ros2"
                         else all(part in command for part in expected))
                if found:
                    matches.append((int(entry.name), entry.stat().st_uid))
            except (OSError, ValueError):
                continue
        return matches

    def _external_running(self, marker: str) -> bool:
        return bool(self._matching_launchers(marker))

    def _adopt_running(self, name, marker):
        for pid, uid in self._matching_launchers(marker):
            try:
                if uid == os.getuid() and os.getpgid(pid) == pid:
                    self.processes[name] = _AttachedProcess(pid)
                    return True
            except ProcessLookupError:
                continue
        return False

    def _resolve_mode_conflict(self, other_launch):
        marker = f"ros2 launch arx_x5_controller {other_launch}"
        for pid, uid in self._matching_launchers(marker):
            try:
                if uid != os.getuid() or os.getpgid(pid) != pid:
                    continue
            except ProcessLookupError:
                continue
            # Only our user's dedicated ARX launcher groups are recoverable.
            name = f"old-mode-{pid}"
            self.processes[name] = _AttachedProcess(pid)
            self._stop_named((name,))
        if self._external_running(marker):
            raise DomainError("另一种控制模式的机械臂驱动由其他会话管理，无法自动接管", 409)

    def _missing_can(self):
        missing = []
        for name in (("can1", "can3") if self.control_mode == "inference" and not self.resident_control else ("can1", "can3", "can6")):
            try:
                output = subprocess.check_output(
                    ["ip", "-o", "link", "show", name],
                    text=True, stderr=subprocess.DEVNULL, timeout=3,
                )
            except (OSError, subprocess.SubprocessError):
                output = ""
            if not re.search(r"<[^>]*\bUP\b", output):
                missing.append(name)
        return missing

    def _stop_named(self, names):
        processes = [self.processes[name] for name in names if name in self.processes]
        for process in processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        for name in names:
            self.processes.pop(name, None)
            log = self.logs.pop(name, None)
            if log:
                log.close()

    def _ensure_can(self):
        missing = self._missing_can()
        if not missing:
            return False
        # Old CAN sockets cannot be reused after an adapter disappears. Stop
        # our old controller processes before restoring the interfaces, so a
        # previous control target is never resumed on a reconnected adapter.
        for name, marker in (("lift", f"ros2 launch arx_x5_controller {self.controller_launch}"),
                             ("joy", "ros2 run arx_joy arx_joy")):
            owned = self.processes.get(name)
            if owned is None or owned.poll() is not None:
                if not self._adopt_running(name, marker) and self._external_running(marker):
                    raise DomainError("CAN 已断开，存在其他程序启动的 ARX 控制器；请先关闭该程序再点击使能", 409)
        self._stop_named(("lift", "joy"))
        for name in missing:
            try:
                result = subprocess.run(
                    ["sudo", "-n", "/usr/local/libexec/bodypipeline-can-init", name],
                    capture_output=True, text=True, timeout=12,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise DomainError(f"{name} 自动初始化失败：{exc}", 503) from exc
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()
                raise DomainError(f"{name} 自动初始化失败：{detail}", 503)
        remaining = self._missing_can()
        if remaining:
            raise DomainError("CAN 自动初始化后仍未就绪：" + ", ".join(remaining), 503)
        return True

    def _spawn(self, name: str, command: str, marker: str):
        process = self.processes.get(name)
        if process and process.poll() is None:
            return
        if self._external_running(marker):
            self._adopt_running(name, marker)
            return
        log_path = self.state_root / f"{name}.log"
        log = log_path.open("ab")
        env = os.environ.copy()
        env.setdefault("PYTHONNOUSERSITE", "1")
        process = subprocess.Popen(
            ["bash", "-lc", command],
            cwd=str(self.project_root),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes[name] = process
        self.logs[name] = log

    def _controller_params(self):
        if self.controller_profiles is None:
            return None
        import copy
        capture = self.controller_profiles["capture"]
        if self.resident_control:
            current = copy.deepcopy(self.controller_profiles["inference"])
            for side in ("l", "r"):
                params = current[f"/arm_slave_{side}"]["ros__parameters"]
                params["arm_control_type"] = "workbench"
                params["initial_control_mode"] = self.control_mode
                params["go_home_position"] = capture[f"/arm_master_{side}"]["ros__parameters"]["go_home_position"]
        else:
            current = copy.deepcopy(self.controller_profiles[self.control_mode])
        for side in ("l", "r"):
            name = f"/arm_{'master' if self.control_mode == 'capture' and not self.resident_control else 'slave'}_{side}"
            params = current[name]["ros__parameters"]
            params["fold_reference_home"] = capture[f"/arm_master_{side}"]["ros__parameters"]["go_home_position"]
            params["fold_reference_file"] = str(self.state_root / f"fold-reference-{side}.txt")
        # ROS parameter YAML accepts JSON; keep the trusted profiles dependency-free.
        path = self.state_root / ("controllers-workbench.yaml" if self.resident_control else f"controllers-{self.control_mode}.yaml")
        from pipeline.shared.core import atomic_json
        atomic_json(path, current)
        return path

    def assert_resident_compatible(self):
        if not self.resident_control:
            raise DomainError("当前驱动不支持连续控制切换", 409)
        for launch in ("v2_collect.launch.py", "v2_joint_control.launch.py"):
            if self._external_running(f"ros2 launch arx_x5_controller {launch}"):
                raise DomainError("旧电机驱动仍在运行，不能通过重启切换模式；请先完成常驻驱动升级", 409)

    def switch_mode(self, mode):
        if mode not in {"capture", "inference"}:
            raise DomainError("未知机械臂控制模式", 400)
        with self.lock:
            self.assert_resident_compatible()
            # Administrative selection only. Never spawn, stop, initialize CAN,
            # repair observations or touch motor output from a mode button.
            from pipeline.shared.core import atomic_json
            atomic_json(self.mode_file, {"mode": mode})
            self.control_mode = mode
            return {"mode": mode}

    def start(self):
        with self.lock:
            required = (
                self.ros_setup,
                self.robot_root / "ROS2/X5_ws/install/setup.bash",
                self.robot_root / "arx_joy/install/setup.bash",
                self.project_root / "realsense/install/setup.bash",
            )
            missing = [str(path) for path in required if not path.is_file()]
            if missing:
                raise DomainError("缺少 ROS 环境文件：" + ", ".join(missing))

            other_launch = ("v2_collect.launch.py" if self.control_mode == "inference"
                            else "v2_joint_control.launch.py")
            if self.resident_control:
                self.assert_resident_compatible()
            else:
                self._resolve_mode_conflict(other_launch)
            can_reinitialized = self._ensure_can()
            ros = str(self.ros_setup)
            x5 = str(self.robot_root / "ROS2/X5_ws/install/setup.bash")
            joy = str(self.robot_root / "arx_joy/install/setup.bash")
            realsense = str(self.project_root / "realsense/install/setup.bash")
            params = self._controller_params()
            # Dedicated older launchers did not receive the reference-file
            # parameters. Replace them only on this explicit startup request.
            marker = f"ros2 launch arx_x5_controller {self.controller_launch}"
            for pid, uid in self._matching_launchers(marker) if params is not None and not self.resident_control else []:
                if uid == os.getuid() and f"params_file:={params}" not in self._command_line(pid).split():
                    if self._adopt_running("lift", marker):
                        self._stop_named(("lift",))
                    break
            params_argument = f" params_file:={shlex.quote(str(params))}" if params is not None else ""
            self._spawn(
                "lift",
                f"source {ros} && source {x5} && exec ros2 launch arx_x5_controller {self.controller_launch}{params_argument}",
                f"ros2 launch arx_x5_controller {self.controller_launch}",
            )
            if self.control_mode == "capture" or self.resident_control:
                self._spawn(
                    "joy",
                    f"source {ros} && source {joy} && exec ros2 run arx_joy arx_joy",
                    "ros2 run arx_joy arx_joy",
                )
            for camera, serial in self.camera_serials.items():
                command = (
                    f"source {ros} && source {realsense} && exec ros2 launch "
                    f"realsense2_camera rs_launch.py camera_name:={camera} "
                    f"depth_module.color_profile:=640x480x30 "
                    f"depth_module.depth_profile:=640x480x30 serial_no:=_{serial}"
                )
                self._spawn(camera, command, f"camera_name:={camera}")
            return {
                "started": True,
                "can_reinitialized": can_reinitialized,
                "owned_processes": [name for name, p in self.processes.items() if p.poll() is None],
                "log_root": str(self.state_root),
            }

    def repair_observation(self, missing):
        """Restart only failed, owned sensor/controller launchers, once per enable."""
        with self.lock:
            names = set()
            camera_names = {"head": "camera_h", "left_wrist": "camera_l", "right_wrist": "camera_r"}
            for stream in missing:
                if stream in {"left_arm", "right_arm"}:
                    names.add("lift")
                elif stream == "buttons" and self.control_mode == "capture":
                    names.add("joy")
                elif stream in camera_names:
                    names.add(camera_names[stream])
            for name in names:
                marker = (f"ros2 launch arx_x5_controller {self.controller_launch}" if name == "lift"
                          else "ros2 run arx_joy arx_joy" if name == "joy" else f"camera_name:={name}")
                process = self.processes.get(name)
                if process is None or process.poll() is not None:
                    if not self._adopt_running(name, marker) and self._external_running(marker):
                        raise DomainError(f"{name} 无新鲜反馈，驱动属于其他用户，无法自动重启", 503)
            self._stop_named(names)
            return self.start()

    def stop(self):
        with self.lock:
            for process in self.processes.values():
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and any(
                process.poll() is None for process in self.processes.values()
            ):
                time.sleep(0.1)
            for process in self.processes.values():
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            for log in self.logs.values():
                log.close()
            self.processes.clear()
            self.logs.clear()
