"""Backend identity, capability isolation and legacy task compatibility."""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_annotations import make_argus, make_dataset, make_job

from pipeline.server import annotation_backends as backends
from pipeline.server import argus_annotation, codex_transport
from pipeline.server.annotations import AnnotationRunner
from pipeline.server.app import AnnotationRequest, create_app
from pipeline.server.task_annotations import TemplateLibrary, TemplateRequest
from pipeline.shared.core import DomainError, Store


@pytest.fixture
def service(settings_factory, tmp_path, monkeypatch):
    source = make_argus(tmp_path)
    for name in ("harness.py", "episode.py", "pipeline_template.py"):
        with (source / "label" / name).open("a") as f:
            f.write(
                "\n# pipeline-argus-task-template:v1\n# pipeline-argus-template-retry:v1\n# pipeline-argus-workbench-contract:v2\n# pipeline-argus-codex:v1\nCONTRACT_VERSION = 2\n"
            )
    with (source / "label/pipeline_runtime.py").open("a") as f:
        f.write("\ndef refine_outputs(): pass\n")
    (source / "label/pipeline_codex.py").write_bytes(
        Path(codex_transport.__file__).read_bytes()
    )
    monkeypatch.setattr(
        backends, "_cli_status", lambda *a: (True, "已登录", "codex-cli 0.167.2")
    )
    monkeypatch.setattr(
        codex_transport,
        "public_configuration",
        lambda: {"model": "model-cli", "model_reasoning_effort": "high"},
    )
    imports = tmp_path / "imports"
    imports.mkdir()
    settings = settings_factory(
        "server",
        argus_src=str(source),
        import_roots=[imports],
        codex_bin="/bin/true",
        argus_annotation_unlimited=True,
        argus_base_url="https://broken.invalid/v1",
    )
    store = Store(settings.root / "state")
    data = make_dataset(imports, episodes=6)
    make_job(store, data)
    TemplateLibrary(store).save(
        TemplateRequest(
            revision=0,
            templates=[
                {
                    "id": "english",
                    "name": "Alignment (English)",
                    "subtasks": [{"id": "align", "name": "Align the screwdriver bit"}],
                    "fields": [],
                }
            ],
        )
    )
    return AnnotationRunner(settings, store), store


def test_cli_selection_never_resolves_or_queries_gateway(service, monkeypatch):
    runner, store = service
    monkeypatch.setattr(
        argus_annotation,
        "resolve_credentials",
        lambda *a: pytest.fail("gateway credentials requested"),
    )
    monkeypatch.setattr(
        argus_annotation, "list_models", lambda *a: pytest.fail("gateway queried")
    )
    row = runner.enqueue(
        "job-1", backend="codex", template_id="english", run_scope="trial", episodes=[2]
    )
    assert row["backend"] == "codex" and row["model"] == "model-cli"
    assert row["concurrency"] == 1 and row["sample_fps"] == 3
    assert row["inspection"] == "off" and row["field_dictionary"] is False
    assert row["max_tokens"] is None and row["cost_usd"] is None
    assert row["cap_usd"] is None and row["source"]["signature"]
    assert row["task_template"]["name"] == "Alignment (English)"
    assert store.get("annotation", row["id"])["backend_config"] == row["backend_config"]
    env = argus_annotation.environment(runner.settings, dict(row, attempts=1), {})
    assert env["PIPELINE_ANNOTATION_BACKEND"] == "codex"
    assert "PIPELINE_ARGUS_BASE_URL" not in env and "PIPELINE_ARGUS_API_KEY" not in env
    assert json.loads(env["PIPELINE_CODEX_CONFIG"])["model"] == "model-cli"


@pytest.mark.parametrize(
    "fields",
    [
        {"template_id": None},
        {"base_url": "https://gateway.invalid/v1"},
        {"api_key": "test-only"},
        {"inspection": "auto"},
        {"field_dictionary": True},
        {"max_tokens": 12000},
        {"cap_usd": 2},
        {"reasoning": "none"},
        {"model": "gateway-model"},
    ],
)
def test_unsupported_cli_parameters_are_rejected(service, fields):
    runner, _ = service
    with pytest.raises(DomainError):
        runner.enqueue(
            "job-1", **({"backend": "codex", "template_id": "english"} | fields)
        )


def test_legacy_defaults_and_no_cross_backend_reuse(service, monkeypatch):
    runner, store = service
    monkeypatch.setenv("PIPELINE_ARGUS_API_KEY", "test-key")
    old = runner.enqueue("job-1", template_id="english")
    old.pop("backend")
    store.put("annotation", old["id"], old)
    assert AnnotationRequest(job_id="job-1").backend == "argus"
    assert backends.backend(old) == "argus"
    with pytest.raises(DomainError, match="另一标注后端"):
        runner.enqueue("job-1", backend="codex", template_id="english")
    with pytest.raises(DomainError, match="未知标注后端"):
        runner.enqueue("job-1", backend="missing")


def test_cli_restart_resume_preserves_identity_and_rejects_changed_provider(
    service, monkeypatch
):
    runner, store = service
    row = runner.enqueue(
        "job-1", backend="codex", template_id="english", run_scope="trial", episodes=[1]
    )
    runner.recover()
    assert store.get("annotation", row["id"])["state"] == "INTERRUPTED"
    resumed = runner.resume(row["id"])
    assert resumed["backend_config"] == row["backend_config"]
    assert resumed["selected_episodes"] == [1]
    runner.cancel(row["id"])
    monkeypatch.setattr(
        codex_transport,
        "public_configuration",
        lambda: {"model": "model-cli", "model_provider": "other"},
    )
    with pytest.raises(DomainError, match="部署模型、提供方"):
        runner.resume(row["id"])


def test_cli_not_available_does_not_disable_api(service, monkeypatch):
    runner, _ = service
    monkeypatch.setattr(backends, "_cli_status", lambda *a: (False, "未登录", ""))
    options = runner.options()
    assert options["available"]
    assert not options["backends"]["codex"]["available"]
    with pytest.raises(DomainError, match="未登录"):
        runner.enqueue("job-1", backend="codex", template_id="english")


@pytest.mark.parametrize(
    "reason",
    [
        "Codex CLI 未认证或登录已失效",
        "Codex CLI 额度不足或达到账号使用限制",
        "Codex CLI 结果不符合 JSON 输出契约",
        "Codex CLI 非零退出，费用状态未知",
    ],
)
def test_cli_failure_reaches_task_and_api_without_empty_candidate(service, reason):
    runner, store = service
    source = Path(runner.settings.argus_src)
    runner.settings.argus_python = sys.executable
    (source / "review/__main__.py").write_text(
        """
# pipeline-argus-workbench-contract:v2
# pipeline-argus-unlimited:v1
# "--no-cap"
import json, pathlib, sys
work = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])
ep = work / 'episodes/episode_000002'
ep.mkdir(parents=True)
(ep / 'context.json').write_text(json.dumps({'fps':30,'n_state_frames':30,'episode_index':2}))
out = work / 'run/out'
out.mkdir(parents=True)
(out / 'noreply_episode_000002.json').write_text(json.dumps({
 'parse_ok':False,'no_reply':"""
        + repr(reason)
        + """,
 'final_dispatch_outcome':'unverified','codex_error':{'kind':'authentication','retryable':False}}))
raise SystemExit(1)
"""
    )
    row = runner.enqueue(
        "job-1", backend="codex", template_id="english", run_scope="trial", episodes=[2]
    )
    result = runner.run(row)
    assert result["state"] == "FAILED"
    assert reason in result["error"]
    assert "empty layout" not in result["error"]
    assert result["episodes_done"] == 0 and result["cost_usd"] is None
    assert not any(r.get("task_document") for r in runner.export(row["id"])["episodes"])
    assert not store.all("annotation_candidate")
    runner.settings.trusted_network = True
    http = TestClient(create_app(runner.settings, background=False))
    fetched = http.get(f"/api/annotations/{row['id']}")
    assert fetched.status_code == 200 and reason in fetched.json()["error"]
    episodes = http.get(f"/api/annotations/{row['id']}/episodes").json()["episodes"]
    assert len(episodes) == 1 and reason in episodes[0]["label_failure"]["why"]
    candidates = http.post(f"/api/annotations/{row['id']}/workbench")
    assert candidates.status_code == 200 and candidates.json()["candidates"] == []
