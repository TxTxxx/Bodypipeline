"""Argus ``review`` execution and output adaptation for automatic annotation.

Argus is a separate Python 3.11 toolchain.  BodyPipeline only builds its
command line, supervises the process group, and reads the files it writes.  No
Argus module is imported into the Server API environment: the two toolchains
have different dependencies and are deliberately isolated.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from pipeline.shared.core import DomainError, executable_path

STAGE_TITLES = {
    "download": "下载数据",
    "convert": "准备 Episode",
    "dictionary": "解释字段",
    "checks": "确定性检查",
    "checks_jumps": "确定性检查",
    "checks_grippers": "确定性检查",
    "checks_capture": "采集质量检查",
    "checks_sensors": "传感器检查",
    "clips": "生成预览视频",
    "dry_run": "构造模型请求",
    "label": "大模型标注",
    "board": "拼接标注结果",
}

STAGE_ORDER = (
    "download",
    "convert",
    "dictionary",
    "checks",
    "clips",
    "dry_run",
    "label",
    "board",
)

_MARKER = re.compile(r"^== ([A-Za-z0-9_]+)\b", re.MULTILINE)
_EPISODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _python(settings) -> str | None:
    """The configured interpreter, never symlink-resolved.

    Resolving a virtualenv's ``bin/python`` drops its site-packages, which
    surfaces as a ModuleNotFoundError on the first Argus import.
    """

    candidate = settings.argus_python or str(
        Path(settings.argus_src).expanduser() / ".venv/bin/python"
    )
    path = Path(candidate).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return executable_path(candidate)


def available(settings) -> tuple[bool, str]:
    if not settings.argus_src:
        return False, "未配置 argus_src"
    source = Path(settings.argus_src).expanduser()
    if not (source / "review").is_dir() or not (source / "label").is_dir():
        return False, f"Argus review 源码不存在：{source}"
    if _python(settings) is None:
        return False, "未找到 Argus Python 解释器"
    return True, ""


def source_path(settings) -> Path:
    return Path(settings.argus_src).expanduser()


MAX_BASE_URL = 2048
MAX_API_KEY = 4096


def normalize_base_url(value) -> str:
    """Validate a per-task gateway URL without ever echoing the secret fields."""

    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > MAX_BASE_URL:
        raise DomainError("模型 baseURL 过长", 400)
    if any(char.isspace() or ord(char) < 32 for char in text):
        raise DomainError("模型 baseURL 不能包含空白或控制字符", 400)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        raise DomainError("模型 baseURL 必须使用 http:// 或 https://", 400)
    if not parsed.hostname:
        raise DomainError("模型 baseURL 缺少主机名", 400)
    if parsed.username or parsed.password:
        raise DomainError("模型 baseURL 不能包含用户名或密码", 400)
    if parsed.query or parsed.fragment:
        raise DomainError("模型 baseURL 不能包含查询参数或片段", 400)
    return text.rstrip("/")


def normalize_api_key(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > MAX_API_KEY:
        raise DomainError("模型 API Key 过长", 400)
    if any(
        char.isspace()
        or not char.isascii()
        or ord(char) < 32
        or ord(char) == 127
        for char in text
    ):
        raise DomainError("模型 API Key 不能包含空白、控制字符或非 ASCII 字符", 400)
    return text


def resolve_credentials(
    settings, base_url=None, api_key=None
) -> dict:
    """Credentials for one task: the page input wins over server defaults.

    The key is returned to the caller only.  It must never be copied into the
    annotation record, the work context, a log or an export.
    """

    task_base_url = normalize_base_url(base_url)
    server_base_url = normalize_base_url(settings.argus_base_url)
    key = normalize_api_key(api_key) or normalize_api_key(
        os.environ.get(settings.argus_api_key_env, "")
    )
    if not key:
        raise DomainError(
            "未提供模型 API Key，且服务器未配置默认密钥；请在页面填写本次任务的 API Key",
            400,
        )
    return {
        "base_url": task_base_url or server_base_url,
        "api_key": key,
        "task_base_url": task_base_url,
    }


def redact(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "[redacted]")


def environment(
    settings,
    annotation: dict,
    provider_context: dict,
    credentials: dict | None = None,
) -> dict:
    """Environment for the Argus subprocess.

    ``tool_environment`` strips every ``PIPELINE_*`` variable, which is the
    right default for letools.  Argus needs the gateway/key/camera variables
    back, and they are added explicitly here rather than by loosening it.
    """

    source = source_path(settings)
    env = settings.tool_environment()
    env["PYTHONPATH"] = str(source) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["PATH"] = os.pathsep.join(
        [str(source / "tools/bin"), env.get("PATH", "")]
    )
    env.setdefault("MALLOC_ARENA_MAX", "2")

    cameras = provider_context.get("cameras") or {}
    if cameras:
        env["PIPELINE_ARGUS_CAMERA_MOUNTING"] = json.dumps(
            cameras, ensure_ascii=False
        )
    if settings.argus_base_url:
        env["PIPELINE_ARGUS_BASE_URL"] = settings.argus_base_url
        env["PIPELINE_ARGUS_GENERIC_GATEWAY"] = "1"
    key = os.environ.get(settings.argus_api_key_env, "")
    if key:
        env["PIPELINE_ARGUS_API_KEY"] = key
    if credentials:
        base_url = str(credentials.get("base_url") or "")
        task_key = str(credentials.get("api_key") or "")
        if base_url:
            env["PIPELINE_ARGUS_BASE_URL"] = base_url
            env["PIPELINE_ARGUS_GENERIC_GATEWAY"] = "1"
        if task_key:
            env["PIPELINE_ARGUS_API_KEY"] = task_key
    jobs = max(1, int(annotation.get("concurrency") or 2))
    env["RDA_DECODE_CONCURRENCY"] = str(max(2, 2 * jobs))
    return env


def command(settings, annotation: dict, data: Path, work: Path) -> list[str]:
    python = _python(settings)
    if python is None:
        raise DomainError("未找到 Argus Python 解释器", 503)
    args = [
        "-m",
        "review",
        "--data",
        str(data),
        "--rig",
        str(annotation.get("rig") or "teleop_arms"),
        "--out",
        str(work / "argus"),
        "--dataset",
        str(annotation.get("dataset_name") or annotation.get("job_id") or "dataset"),
        "--cap",
        f"{float(annotation.get('cap_usd') or 0.0):.4f}",
        "--concurrency",
        str(max(1, int(annotation.get("concurrency") or 8))),
    ]
    note = str(annotation.get("note") or "").strip()
    if note:
        args += ["--dataset-note", note]
    model = str(annotation.get("model") or "").strip()
    if model:
        args += ["--model", model]
    reasoning = str(annotation.get("reasoning") or "").strip()
    if reasoning:
        args += ["--reasoning", reasoning]
    sample_fps = annotation.get("sample_fps")
    if sample_fps:
        args += ["--sample-fps", f"{float(sample_fps):.4f}"]
    inspection = annotation.get("inspection")
    if inspection in {"auto", "all", "off"}:
        args += ["--inspection", inspection]
    if annotation.get("field_dictionary") is True:
        args += ["--field-dictionary", "on"]
    elif annotation.get("field_dictionary") is False:
        args += ["--field-dictionary", "off"]
    max_tokens = annotation.get("max_tokens")
    if max_tokens:
        args += ["--max-tokens", str(int(max_tokens))]
    return [python, *args]


def stage_from_log(text: str, current: str = "") -> str:
    """The latest stage Argus announced, or the current title when none."""

    stage = current
    for match in _MARKER.finditer(text):
        marker = match.group(1)
        key = marker if marker in STAGE_TITLES else next(
            (prefix for prefix in STAGE_ORDER if marker.startswith(prefix)), ""
        )
        if key:
            stage = STAGE_TITLES[key]
    if "review finished" in text or "== board" in text:
        stage = stage or STAGE_TITLES["board"]
    return stage


def read_run(work: Path) -> dict:
    argus = Path(work) / "argus"
    for relative in ("run_final/run.json", "run/run.json"):
        path = argus / relative
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def read_report(work: Path) -> dict:
    try:
        value = json.loads((Path(work) / "argus/report.json").read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _qa_files(argus: Path) -> list[Path]:
    qa = argus / "qa"
    if not qa.is_dir():
        return []
    return sorted(qa.glob("*.json"))


def _raw_files(argus: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for directory in (argus / "run/out", argus / "run_final/out"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("episode_*.json")):
            files.setdefault(path.stem, path)
        for path in sorted(directory.glob("failed_episode_*.json")):
            files.setdefault(path.stem.removeprefix("failed_"), path)
        for path in sorted(directory.glob("noreply_episode_*.json")):
            files.setdefault(path.stem.removeprefix("noreply_"), path)
    return files


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _summary(name: str, data: dict, source: str) -> dict:
    labels = data.get("labels") if isinstance(data.get("labels"), dict) else {}
    completion = data.get("completion")
    if not isinstance(completion, dict):
        completion = labels.get("completion") if isinstance(labels, dict) else {}
    completion = completion if isinstance(completion, dict) else {}
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    failed = data.get("_label_failed")
    if not failed and data.get("parse_ok") is False:
        failed = {
            "status": "failed",
            "why": data.get("no_reply") or data.get("unreadable") or "模型未返回可用标注",
        }
    timeline = data.get("event_labels") or labels.get("timeline") or []
    key_events = data.get("key_events") or labels.get("key_events") or []
    prompt = data.get("episode_prompt") or labels.get("task_summary") or ""
    duration = data.get("duration_s")
    if duration is None and isinstance(data.get("config"), dict):
        times = data["config"].get("timesteps_s") or []
        if times:
            try:
                duration = round(float(times[-1]) - float(times[0]), 3)
            except (TypeError, ValueError):
                duration = None
    returns = data.get("state_changes") or labels.get("state_changes") or []
    outcome = completion.get("task_completed")
    if isinstance(outcome, bool):
        # Older adapters wrote a boolean; Argus itself uses the word list.
        outcome = "success" if outcome else "failure"
    return {
        "id": name,
        "episode_id": meta.get("episode_id") or name,
        "run_episode": meta.get("run_episode") or name,
        "prompt": prompt,
        "duration_s": duration,
        "completed": outcome == "success",
        "outcome": outcome,
        "failure_kind": completion.get("failure_kind"),
        "completed_at_s": completion.get("completed_at_s"),
        "events": len(timeline) if isinstance(timeline, list) else 0,
        "key_events": len(key_events) if isinstance(key_events, list) else 0,
        "state_changes": len(returns) if isinstance(returns, list) else 0,
        "labels_available": not bool(failed),
        "label_failure": failed,
        "model": meta.get("model") or data.get("model"),
        "usage": data.get("_usage") or data.get("usage"),
        "source": source,
    }


def outputs(work: Path) -> list[dict]:
    """Every episode the run produced, newest output layout first."""

    argus = Path(work) / "argus"
    rows: list[dict] = []
    seen: set[str] = set()
    for path in _qa_files(argus):
        data = _read_json(path)
        if not data:
            continue
        name = path.stem
        rows.append(_summary(name, data, "board"))
        seen.add(name)
    for name, path in _raw_files(argus).items():
        if name in seen:
            continue
        data = _read_json(path)
        if not data or data.get("dry_run"):
            continue
        episode = Path(str(data.get("episode_dir") or name)).name or name
        rows.append(_summary(episode, data, "run"))
    rows.sort(key=lambda row: row["id"])
    return rows


def episode(work: Path, episode_id: str) -> dict | None:
    if not isinstance(episode_id, str) or not _EPISODE_ID.fullmatch(episode_id):
        return None
    argus = Path(work) / "argus"
    for path in _qa_files(argus):
        data = _read_json(path)
        if not data:
            continue
        meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
        ids = {
            path.stem,
            str(meta.get("episode_id") or ""),
            str(meta.get("run_episode") or ""),
        }
        if episode_id in ids:
            return {"id": path.stem, "source": "board", **data}
    path = _raw_files(argus).get(episode_id)
    if path is None:
        for name, candidate in _raw_files(argus).items():
            data = _read_json(candidate)
            if Path(str(data.get("episode_dir") or "")).name == episode_id:
                path = candidate
                break
    if path is not None:
        data = _read_json(path)
        if data:
            return {
                "id": Path(str(data.get("episode_dir") or path.stem)).name,
                "source": "run",
                **data,
            }
    return None


def cost(work: Path) -> dict:
    info = read_run(work)
    value = {
        "status": info.get("status"),
        "cost_usd": info.get("cost_usd"),
        "reserved_cost_usd": info.get("reserved_cost_usd"),
        "cost_complete": info.get("cost_complete"),
        "cap_usd": info.get("cap_usd"),
    }
    if value["cost_usd"] is None:
        billed = 0.0
        reserved = 0.0
        complete = True
        for path in _raw_files(Path(work) / "argus").values():
            data = _read_json(path)
            if data.get("dry_run"):
                continue
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            estimate = usage.get("est_cost_usd")
            if isinstance(estimate, (int, float)) and not isinstance(estimate, bool):
                billed += float(estimate)
            else:
                complete = False
            bound = data.get("final_reserved_usd")
            if isinstance(bound, (int, float)) and not isinstance(bound, bool):
                reserved += float(bound)
        value["cost_usd"] = round(billed, 6) if complete else None
        value["reserved_cost_usd"] = round(reserved, 6)
        value["cost_complete"] = complete
    return value


def progress(work: Path) -> dict:
    argus = Path(work) / "argus"
    units = len(list((argus / "units").glob("episode_*"))) if (argus / "units").is_dir() else 0
    # A resumed run may label into run_final while run/out is being rebuilt.
    labelled: set[str] = set()
    for directory in (argus / "run/out", argus / "run_final/out"):
        if directory.is_dir():
            labelled.update(path.stem for path in directory.glob("episode_*.json"))
    return {
        "units": units,
        "labelled": len(labelled),
        "episodes": max(len(labelled), len(_raw_files(argus))),
    }


def log_names(work: Path) -> list[str]:
    names = ["run"]
    logs = Path(work) / "argus/logs"
    if logs.is_dir():
        names.extend(
            sorted(path.stem for path in logs.glob("*.log") if path.is_file())
        )
    return names


def read_log(work: Path, name: str) -> str:
    if name not in log_names(work):
        raise DomainError("日志不存在", 404)
    if name == "run":
        path = Path(work) / "logs/run.log"
    else:
        path = Path(work) / "argus/logs" / f"{name}.log"
    if not path.is_file() or path.is_symlink():
        raise DomainError("日志不存在", 404)
    return _tail_text(path, 64000)


def _tail_text(path: Path, limit: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - limit))
            return handle.read().decode(errors="replace")
    except OSError:
        return ""


__all__ = [
    "STAGE_ORDER",
    "STAGE_TITLES",
    "available",
    "command",
    "cost",
    "environment",
    "episode",
    "log_names",
    "outputs",
    "progress",
    "read_log",
    "read_report",
    "read_run",
    "source_path",
    "stage_from_log",
]
