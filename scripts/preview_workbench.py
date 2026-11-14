"""Isolated synthetic BODY-2 preview; never starts/restarts deployed services.

Prepare: .runtime/letools-venv/bin/python tests/fixtures/workbench_scenario.py .runtime/body2-preview
Run: .venv/bin/python scripts/preview_workbench.py (Server 18102, --viewer 18103)
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.server.annotation_contract import validate_annotation
from pipeline.server.app import create_app
from pipeline.server.task_annotations import (
    TaskTemplate,
    TemplateLibrary,
    TemplateRequest,
)
from pipeline.server.viewer_data import PreviewRegistry
from pipeline.shared.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def app_for(viewer=False, model_fixture=False):
    root = ROOT / ".runtime/body2-preview"
    model_options = {}
    if model_fixture and not viewer:
        import os
        import sys

        sys.path.insert(0, str(ROOT / "tests/fixtures"))
        from workbench_mock_model import serve

        serve(18104, root / "mock-model-requests.jsonl")
        # Public dummy credential accepted only by this local fixture.
        os.environ["BODY2_SYNTHETIC_MODEL_KEY"] = "synthetic-local-fixture"
        os.environ["FFMPEG"] = str(ROOT / ".runtime/toolchain/usr/bin/ffmpeg")
        os.environ["FFPROBE"] = str(ROOT / ".runtime/toolchain/usr/bin/ffprobe")
        os.environ["LD_LIBRARY_PATH"] = str(
            ROOT / ".runtime/toolchain/usr/lib/x86_64-linux-gnu"
        )
        model_options = dict(
            argus_src=str(ROOT / ".runtime/vendor/argus"),
            argus_python=str(ROOT / ".runtime/argus-venv/bin/python"),
            argus_base_url="http://127.0.0.1:18104/v1",
            argus_api_key_env="BODY2_SYNTHETIC_MODEL_KEY",
            argus_model="synthetic-fixture",
            argus_models=["synthetic-fixture"],
            argus_prices={"synthetic-fixture": [[0.000001, 0, 0.000001]]},
            argus_max_tokens=2048,
            argus_annotation_concurrency=1,
            argus_annotation_reasoning="none",
            argus_annotation_inspection="off",
        )
    settings = Settings(
        role="viewer" if viewer else "server",
        root=root / "state",
        token="synthetic-fixture-" + "x" * 32,
        trusted_network=True,
        profile_path=str(ROOT / "configs/agilex-aloha.json"),
        import_roots=[root],
        letools_bin=str(ROOT / ".runtime/letools-venv/bin/letools"),
        min_free_bytes=0,
        processing_mode="real",
        poll_seconds=0.1,
        server_url="http://127.0.0.1:18102",
        viewer_origin="http://127.0.0.1:18103",
        viewer_ui_port=13121,
        **model_options,
    )
    if viewer:
        from pipeline.server.viewer import create_gateway

        return create_gateway(settings)
    app = create_app(settings, background=True)

    @app.middleware("http")
    async def controlled_latency(request, call_next):
        # Opt-in browser race fixture, outside the read-only dataset.
        import asyncio
        import json

        policy = root / "ui-delay-fixture.json"
        if policy.is_file():
            value = json.loads(policy.read_text())
            if request.url.path == value.get("path") and request.method == value.get(
                "method"
            ):
                await asyncio.sleep(min(3, max(0, float(value.get("seconds", 0)))))
        return await call_next(request)

    for method in ("recover", "autostart", "poll", "stop"):
        setattr(app.state.viewer, method, lambda: None)
    store = app.state.runner.store
    store.put(
        "viewer",
        "service",
        {"id": "service", "state": "READY", "mode": "synthetic-fixture"},
    )
    source = root / "dataset"
    if not source.is_dir():
        raise SystemExit(
            "Prepare the synthetic fixture first; see this script's docstring"
        )
    store.put(
        "job",
        "numbered-fixture",
        {
            "id": "numbered-fixture",
            "state": "CONVERTED",
            "dataset_ready": True,
            "dataset_path": str(source),
            "dataset_name": "合成多相机 · 帧号验收",
            "created_at": 1,
            "robot_type": "synthetic_arm",
            "capture_mode": "mock",
            "target_format": "v3.0",
        },
    )
    PreviewRegistry(store, settings.viewer_ttl).register("numbered-fixture", source)
    template = {
        "id": "cup-fixture",
        "name": "杯子操作 · 合成验收",
        "subtasks": [
            {
                "id": "grasp",
                "name": "抓取",
                "definition": "一次接近、调整、抓取与提起构成一次执行。",
                "includes": "接近、调整夹爪、持物与任务内等待",
                "excludes": "失败后重新开始为另一次尝试",
                "startCondition": "手臂明确朝杯子接近",
                "endCondition": "提起稳定，或放弃本次执行",
            },
            {
                "id": "carry",
                "name": "搬运",
                "definition": "保持持物向目标移动",
                "startCondition": "开始向目标位移",
                "endCondition": "到达目标上方",
            },
            {
                "id": "place",
                "name": "放置",
                "definition": "将持物释放在目标位置",
                "startCondition": "开始下放物体",
                "endCondition": "物体稳定，夹爪离开",
            },
        ],
        "fields": [
            {
                "id": "quality",
                "label": "执行质量",
                "scope": "attempt",
                "kind": "rating",
                "minimum": 0,
                "maximum": 10,
                "step": 1,
                "description": "根据稳定性与多余调整评价",
                "scale": "0=无法执行；5=明显调整；10=顺畅稳定。证据不足可空。",
            },
            {
                "id": "episode-note",
                "label": "整体备注",
                "scope": "episode",
                "kind": "textarea",
            },
            {
                "id": "frame-note",
                "label": "帧观察",
                "scope": "frame",
                "kind": "textarea",
            },
        ],
    }
    template = TaskTemplate.model_validate(template).model_dump(exclude_none=True)
    lib = TemplateLibrary(store).read()
    if not lib["templates"]:
        TemplateLibrary(store).save(
            TemplateRequest(revision=lib["revision"], templates=[template])
        )
    wb = app.state.annotation_workbench
    for ep in range(12):
        head = wb.episode("numbered-fixture", ep)
        if wb.candidates("numbered-fixture", ep):
            continue
        attempts = []
        segments = []
        tries = ep % 3 + 1
        point = 0
        for n in range(tries):
            aid = f"fixture-{ep}-grasp-{n}"
            attempts.append(
                {
                    "id": aid,
                    "subtaskId": "grasp",
                    "subtaskName": "抓取",
                    "outcome": "success" if n == tries - 1 else "failure",
                    "values": {"quality": 8 if n == tries - 1 else 3},
                }
            )
            segments.append(
                {
                    "id": f"fixture-{ep}-segment-{n}",
                    "start": point,
                    "end": point + 8,
                    "label": "SUBTASK",
                    "subtaskId": "grasp",
                    "attemptId": aid,
                    "note": "合成模型候选，非真实模型推断",
                    "evidence": [
                        {
                            "kind": "synthetic",
                            "boundaryFrames": [point, max(point, point + 7)],
                            "note": "用于验证实际链路，不证明模型质量",
                        }
                    ],
                }
            )
            point += 8
        segments.extend(
            [
                {
                    "id": f"fixture-{ep}-unknown",
                    "start": point,
                    "end": point + 4,
                    "label": "UNKNOWN",
                },
                {
                    "id": f"fixture-{ep}-outside",
                    "start": point + 4,
                    "end": point + 8,
                    "label": "OUT_OF_TEMPLATE",
                    "description": "清理桌面",
                },
                {
                    "id": f"fixture-{ep}-null",
                    "start": point + 8,
                    "end": 48,
                    "label": "NULL",
                },
            ]
        )
        document = {
            "version": 2,
            "templateId": template["id"],
            "templateSnapshot": template,
            "values": {},
            "attempts": attempts,
            "segments": segments,
            "frames": [],
            "history": [],
            "provenance": {"fixture": True, "model": "controlled-synthetic"},
            "countEvidence": {},
        }
        wb.add_candidate(
            "numbered-fixture",
            ep,
            validate_annotation(document, 48),
            run="模拟模型 · 合成帧号验收",
            source_signature=head["source_signature"],
            base_revision=head["revision"],
            provenance={"synthetic": True},
        )
    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--viewer", action="store_true")
    parser.add_argument("--model-fixture", action="store_true")
    args = parser.parse_args()
    uvicorn.run(
        app_for(args.viewer, args.model_fixture),
        host="127.0.0.1",
        port=18103 if args.viewer else 18102,
    )
