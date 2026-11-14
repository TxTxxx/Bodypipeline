"""Local startup preserves user state and cannot stop an unrelated/busy service."""

import fcntl
import importlib.util
import json
import socket
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/local_trial.py"
SPEC = importlib.util.spec_from_file_location("local_trial", SOURCE)
trial = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trial)


def config(tmp_path, **extra):
    path = tmp_path / "trial.json"
    path.write_text(
        json.dumps(
            {
                "root": str(tmp_path / "retained"),
                "trusted_network": True,
                "import_roots": [str(tmp_path / "source")],
                **extra,
            }
        )
    )
    return path


def test_configure_only_fills_missing_tools_and_keeps_permissions_and_user_settings(
    tmp_path,
):
    path = config(
        tmp_path,
        argus_python="/custom/venv/bin/python",
        argus_model="user-model",
        argus_prices={"user-model": [[1e-6, 1e-7, 2e-6]]},
        argus_task_budget_usd=3,
        argus_annotation_concurrency=2,
    )
    path.chmod(0o600)
    before = json.loads(path.read_text())
    after = trial.configure(path, repo=tmp_path)
    assert all(after[key] == value for key, value in before.items())
    assert after["argus_src"] == str(tmp_path / ".runtime/vendor/argus")
    assert path.stat().st_mode & 0o777 == 0o600
    data, modified = path.read_bytes(), path.stat().st_mtime_ns
    trial.configure(path, repo=tmp_path)
    assert path.read_bytes() == data and path.stat().st_mtime_ns == modified
    assert not (tmp_path / "retained").exists()  # no state reset/import/setup


def test_python_symlink_is_not_resolved_out_of_its_venv(tmp_path):
    python = tmp_path / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(trial.sys.executable))
    assert str(trial.absolute(python)) == str(python)


def test_tool_environment_adds_cpu_tools_and_drops_inherited_credentials(tmp_path):
    tool = tmp_path / ".runtime/toolchain/usr"
    (tool / "bin").mkdir(parents=True)
    (tool / "lib/x86_64-linux-gnu").mkdir(parents=True)
    for name in ("ffmpeg", "ffprobe"):
        (tool / "bin" / name).touch()
    env = trial.tool_environment(
        tmp_path,
        inherited={
            "PATH": "/usr/bin",
            "HOME": str(tmp_path),
            "OPENAI_API_KEY": "public-test-placeholder",
            "PIPELINE_ARGUS_API_KEY": "public-test-placeholder",
            "PIPELINE_TOKEN": "public-test-placeholder",
            "HF_TOKEN": "public-test-placeholder",
        },
    )
    assert env["FFMPEG"] == str(tool / "bin/ffmpeg")
    assert env["FFPROBE"] == str(tool / "bin/ffprobe")
    assert env["LD_LIBRARY_PATH"] == str(tool / "lib/x86_64-linux-gnu")
    assert env["CUDA_VISIBLE_DEVICES"] == ""
    assert not any("KEY" in key or "TOKEN" in key for key in env)


def test_start_never_takes_over_an_unmanaged_listener(tmp_path, monkeypatch):
    path = config(tmp_path)
    raw = json.loads(path.read_text())
    monkeypatch.setattr(trial, "unit_state", lambda _: {})
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(RuntimeError, match="不会自动停止"):
            trial.start(path, listener.getsockname()[1], prepared=(raw, {}, {}))


def test_stop_refuses_active_analysis_even_when_http_is_unavailable(
    tmp_path, monkeypatch
):
    root = tmp_path / "retained"
    directory = root / "state/analysis"
    directory.mkdir(parents=True)
    (directory / "running.json").write_text('{"state":"RUNNING"}')
    monkeypatch.setattr(trial, "unit_state", lambda _: {"ActiveState": "active"})
    calls = []
    monkeypatch.setattr(trial.subprocess, "run", lambda *a, **kw: calls.append(a))
    with pytest.raises(RuntimeError, match="仍有任务"):
        trial.stop("owned-trial.service", 18122, root)
    assert calls == []


def test_restart_preflights_before_stopping_an_existing_service(tmp_path, monkeypatch):
    path = config(tmp_path)
    calls = []

    def fail(_):
        calls.append("prepare")
        raise RuntimeError("bad configuration")

    monkeypatch.setattr(trial, "prepare", fail)
    monkeypatch.setattr(trial, "stop", lambda *a: calls.append("stop"))
    monkeypatch.setattr(
        trial.sys, "argv", [str(SOURCE), "restart", "--config", str(path)]
    )
    with pytest.raises(SystemExit) as result:
        trial.main()
    assert result.value.code == 1
    assert calls == ["prepare"]


def test_repeated_start_leaves_running_service_alone(tmp_path, monkeypatch):
    path = config(tmp_path)
    monkeypatch.setattr(trial, "unit_state", lambda _: {"ActiveState": "active"})
    calls = []
    monkeypatch.setattr(trial.subprocess, "run", lambda *a, **kw: calls.append(a))
    trial.start(path, 18122, prepared=({}, {}, {}))
    assert calls == []


def test_start_refuses_a_retained_state_lock_even_after_listener_closed(
    tmp_path, monkeypatch
):
    path = config(tmp_path)
    raw = json.loads(path.read_text())
    root = Path(raw["root"])
    root.mkdir()
    monkeypatch.setattr(trial, "unit_state", lambda _: {})
    with socket.socket() as free_port, (root / "server.lock").open("a") as lock:
        free_port.bind(("127.0.0.1", 0))
        port = free_port.getsockname()[1]
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="状态锁"):
            trial.start(path, port, prepared=(raw, {}, {}))
