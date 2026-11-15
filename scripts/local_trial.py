"""Manage a loopback-only local trial using existing tools and a user systemd unit.

Run `.venv/bin/python scripts/local_trial.py start` from this checkout. No tools
are installed/reset, no dataset is imported, and no model request is submitted.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO / ".runtime/body2-local-trial.json"
SAFE_ENV = (
    "HOME",
    "USER",
    "LOGNAME",
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CODEX_HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def read_config(path):
    if path.is_symlink():
        raise RuntimeError("试用配置不能是符号链接")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError("请先提供有效的本地试用 JSON 配置") from exc
    if not isinstance(value, dict) or not value.get("root"):
        raise RuntimeError("试用配置必须保留明确的 root")
    if value.get("trusted_network") is not True:
        raise RuntimeError("此本机入口要求 trusted_network=true，并始终只监听 loopback")
    return value


def absolute(value, repo=REPO):
    # Never resolve bin/python symlinks: that would lose venv site-packages.
    path = Path(value).expanduser()
    return path if path.is_absolute() else repo / path


def configure(path, repo=REPO):
    raw = read_config(path)
    defaults = {
        "argus_src": str(repo / ".runtime/vendor/argus"),
        "argus_python": str(repo / ".runtime/argus-venv/bin/python"),
        "letools_bin": str(repo / ".runtime/letools-venv/bin/letools"),
    }
    changed = False
    for key, value in defaults.items():
        if not raw.get(key):
            raw[key], changed = value, True
    if "argus_annotation_concurrency" not in raw:
        raw["argus_annotation_concurrency"], changed = 1, True
    if changed:
        # Preserve every existing field (including root and user budgets), and
        # the original permissions. Do not print or duplicate credential values.
        mode = path.stat().st_mode & 0o777
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            try:
                os.chmod(temporary, mode)
                json.dump(raw, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        temporary.replace(path)
    return raw


def tool_environment(repo=REPO, inherited=None):
    inherited = os.environ if inherited is None else inherited
    env = {key: inherited[key] for key in SAFE_ENV if key in inherited}
    # No provider/API credentials are inherited from an agent, shell or manager.
    tool = repo / ".runtime/toolchain/usr"
    binary = tool / "bin"
    env["PATH"] = os.pathsep.join(
        [str(binary), str(repo / ".venv/bin"), env.get("PATH", os.defpath)]
    )
    if (tool / "lib/x86_64-linux-gnu").is_dir():
        env["LD_LIBRARY_PATH"] = str(tool / "lib/x86_64-linux-gnu")
    for name in ("ffmpeg", "ffprobe"):
        found = (
            str(binary / name)
            if (binary / name).is_file()
            else shutil.which(name, path=env["PATH"])
        )
        if not found:
            raise RuntimeError(f"缺少 CPU {name}，请准备现有工具链；此入口不会自动安装")
        env[name.upper()] = found
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["PYTHONUNBUFFERED"] = "1"
    env["NEXT_TELEMETRY_DISABLED"] = "1"
    return env


def preflight(raw, env, repo=REPO):
    from types import SimpleNamespace

    sys.path.insert(0, str(repo))
    from pipeline.server import argus_annotation

    settings = SimpleNamespace(
        argus_src=str(absolute(raw["argus_src"], repo)),
        argus_python=str(absolute(raw["argus_python"], repo)),
        argus_annotation_unlimited=bool(raw.get("argus_annotation_unlimited")),
    )
    ok, reason = argus_annotation.available(settings)
    if not ok:
        raise RuntimeError(f"{reason}；未停止现有服务")
    if not argus_annotation.template_available(
        settings
    ) or not argus_annotation.execution_available(settings):
        raise RuntimeError(
            "现有 Argus 缺少可用的模板/执行契约 v2；保留工具原状，请先核对安装"
        )
    source = settings.argus_src
    dirty = subprocess.check_output(
        ["git", "-C", source, "status", "--porcelain"], env=env
    )
    if dirty:
        raise RuntimeError("Argus 有未提交修改，此入口不会重置或覆盖它")
    imports = "import av,label.harness,label.episode,label.pipeline_template,label.pipeline_runtime,prepare.formats,review.__main__;assert label.pipeline_template.CONTRACT_VERSION==2"
    for args in (
        [settings.argus_python, "-c", imports],
        [settings.argus_python, "-m", "review", "--help"],
    ):
        result = subprocess.run(
            args, cwd=source, env=env, capture_output=True, timeout=30
        )
        if result.returncode:
            raise RuntimeError("Argus 导入或 CLI 帮助检查失败；未改动现有环境")
        if (
            "--help" in args
            and settings.argus_annotation_unlimited
            and b"--no-cap" not in result.stdout
        ):
            raise RuntimeError(
                "Argus review 未提供 --no-cap；请先更新工具链，现有服务保持运行"
            )
    for tool in ("FFMPEG", "FFPROBE"):
        if subprocess.run(
            [env[tool], "-version"], env=env, capture_output=True, timeout=10
        ).returncode:
            raise RuntimeError(f"{tool} 无法运行，请检查 CPU 工具链的动态库")
    return {
        "available": True,
        "template_available": True,
        "execution_contract_v2": True,
        "argus_commit": subprocess.check_output(
            ["git", "-C", source, "rev-parse", "HEAD"], text=True, env=env
        ).strip(),
        "argus_clean": True,
        "imports_and_help": True,
        "cpu_ffmpeg_and_ffprobe": True,
    }


def unit_name(config):
    identity = hashlib.sha256(str(config).encode()).hexdigest()[:12]
    return f"bodypipeline-local-trial-{identity}.service"


def unit_state(unit):
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "-p",
            "MainPID",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
        ],
        capture_output=True,
        text=True,
    )
    return dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )


def local_json(port, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
        return json.load(response)


def ensure_idle(port, root):
    # Do not log task bodies or provider messages, and never stop a busy run.
    active = {
        "QUEUED",
        "RUNNING",
        "PROCESSING",
        "CONVERTING",
        "TRIMMING",
        "CHECKING",
        "ANALYZING",
    }
    for kind in (
        "job",
        "annotation",
        "analysis",
        "check",
        "stationary_trim",
        "manual_trim",
    ):
        for path in (root / "state" / kind).glob("*.json"):
            if json.loads(path.read_text()).get("state") in active:
                raise RuntimeError(
                    "试用服务仍有任务在运行，请先在页面完成或取消，当前服务保持运行"
                )
    for path in ("/api/annotations", "/api/jobs"):
        try:
            data = local_json(port, path)
        except (OSError, ValueError):
            continue  # A broken listener can still be stopped after the state check.
        rows = data if isinstance(data, list) else data.get("jobs", [])
        if any(row.get("state") in active for row in rows):
            raise RuntimeError(
                "试用服务仍有任务在运行，请先在页面完成或取消，当前服务保持运行"
            )


def stop(unit, port, root):
    state = unit_state(unit)
    if state.get("ActiveState") not in {"active", "activating"}:
        print("本入口管理的试用服务未运行；不会停止其他进程。")
        return
    ensure_idle(port, root)
    subprocess.run(["systemctl", "--user", "stop", unit], check=True)
    print("已停止本入口管理的本机试用服务。")


def prepare(config):
    raw = configure(config)
    env = tool_environment()
    report = preflight(raw, env)
    # Use the same Settings.load and ephemeral local auth as the real service,
    # in a child that cannot mutate the caller's environment or open a server.
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_validate",
            "--config",
            str(config),
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("试用配置校验失败，当前服务未停止；请先检查配置后重试")
    return raw, env, {**report, "settings_valid": True}


def start(config, port, prepared=None):
    raw, env, _ = prepared or prepare(config)
    unit = unit_name(config)
    if unit_state(unit).get("ActiveState") == "active":
        print(
            f"已在运行：http://127.0.0.1:{port}/#annotations；更改配置后请用 restart。"
        )
        return
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(
                f"本机端口 {port} 已被其他进程使用；不会自动停止或接管它"
            )
    root = absolute(raw["root"])
    lock_path = root / "server.lock"
    if lock_path.exists():
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "原服务仍持有状态锁（可能正在关闭），请等待其正常退出后重试"
                ) from exc
    log = root / "reports/local-trial.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "systemd-run",
        "--user",
        "--collect",
        "--quiet",
        "--service-type=exec",
        f"--unit={unit}",
        f"--working-directory={REPO}",
        "--property=Restart=no",
        "--property=KillSignal=SIGINT",
        "--property=TimeoutStopSec=30",
        f"--property=StandardOutput=append:{log}",
        f"--property=StandardError=append:{log}",
    ]
    # NAME alone copies the variable from systemd-run's controlled environment.
    # Proxy URLs may contain credentials: keep their values out of argv/logs.
    argv += [f"--setenv={name}" for name in env]
    argv += [
        "--",
        sys.executable,
        str(Path(__file__).resolve()),
        "_serve",
        "--config",
        str(config),
        "--port",
        str(port),
    ]
    subprocess.run(argv, env=env, check=True)
    for _ in range(100):
        try:
            local_json(port, "/healthz")
            options = local_json(port, "/api/annotations/options")
            if all(
                options.get(k)
                for k in ("available", "template_available", "execution_contract_v2")
            ):
                print(
                    f"已启动：http://127.0.0.1:{port}/#annotations\n日志：{log}\n用户服务：{unit}"
                )
                return
        except (OSError, ValueError):
            pass
        if unit_state(unit).get("ActiveState") not in {"active", "activating"}:
            raise RuntimeError(f"试用服务未能启动，请查看日志：{log}")
        time.sleep(0.2)
    raise RuntimeError(f"试用服务尚未健康，请查看日志：{log}")


def serve(config, port, validate_only=False):
    raw = read_config(config)
    env = tool_environment()
    os.environ.clear()
    os.environ.update(env)
    os.environ["PIPELINE_TRUSTED_NETWORK"] = "1"
    os.environ["PIPELINE_DEVICE_TOKEN"] = secrets.token_urlsafe(32)
    # The random local device key stays in this process, never argv/config/logs.
    sys.path.insert(0, str(REPO))
    os.chdir(REPO)
    import uvicorn

    from pipeline.shared.config import Settings

    settings = Settings.load("server", str(config))
    assert settings.root == absolute(raw["root"]).resolve()
    if validate_only:
        return
    from pipeline.server.app import create_app

    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=port,
        workers=1,
        proxy_headers=False,
        access_log=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("start", "restart", "stop", "status", "check", "_serve", "_validate"),
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, default=18122)
    args = parser.parse_args()
    config = absolute(args.config)
    if not 1024 <= args.port <= 65535:
        parser.error("请使用有效的非特权本机端口")
    try:
        if args.action in {"_serve", "_validate"}:
            serve(config, args.port, validate_only=args.action == "_validate")
        elif args.action == "check":
            print(
                json.dumps(
                    prepare(config)[2],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.action == "status":
            print(
                json.dumps(
                    {"unit": unit_name(config), **unit_state(unit_name(config))},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.action == "stop":
            stop(unit_name(config), args.port, absolute(read_config(config)["root"]))
        else:
            prepared = prepare(config)
            if args.action == "restart":
                stop(unit_name(config), args.port, absolute(prepared[0]["root"]))
            start(config, args.port, prepared)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
