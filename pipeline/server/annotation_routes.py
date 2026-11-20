"""Shared robot-independent workspace routes for Server and Viewer gateway."""

from __future__ import annotations

from fastapi import Body, Request
from fastapi.responses import FileResponse, Response

from pipeline.shared.core import DomainError, digest
from pipeline.shared.security import authorization_headers
from pipeline.shared.web import ASSETS

from .annotation_media import AnnotationMedia
from .annotation_workbench import (
    AnnotationWorkbench,
    ApplyCandidate,
    BatchConfirm,
    ImportBundle,
    SaveAnnotation,
    Selection,
)
from .merges import source_snapshot
from .viewer_data import serve_dataset_file


def mount_workbench(app, settings, store, runner=None, annotations=None):
    workbench = AnnotationWorkbench(settings, store)
    app.state.annotation_workbench = workbench
    decoder = app.state.annotation_media = AnnotationMedia(settings)

    @app.get("/workbench")
    def page():
        from fastapi.responses import HTMLResponse

        text = (ASSETS / "workbench.html").read_text()
        if settings.role == "viewer":
            from html import escape

            origin = escape(settings.server_url.rstrip("/"), quote=True)
            text = text.replace(
                'href="/" class="brand"',
                f'href="{origin}/" target="_top" class="brand"',
            ).replace(
                'href="/#annotations"', f'href="{origin}/#annotations" target="_top"'
            )
        return HTMLResponse(
            text.replace(
                'data-gateway="false"',
                f'data-gateway="{str(settings.role == "viewer").lower()}"',
            )
        )

    @app.get("/workbench-assets/{name}")
    def asset(name: str):
        if name not in {
            "workbench.js",
            "workbench.css",
            "template-example.json",
            "screw-alignment-en.json",
            "screw-workflow-en.json",
        }:
            raise DomainError("文件不存在", 404)
        return FileResponse(ASSETS / name)

    async def media_call(request, dataset, episode, mode, *, cache=None, **args):
        key, path, _ = workbench.legacy.resolve(dataset)
        snapshot = source_snapshot(path)
        result = await decoder.read(
            request,
            {
                "root": str(path),
                "signature": snapshot["signature"],
                "episode": episode,
                "mode": mode,
                **args,
            },
            cache,
        )
        if source_snapshot(path) != snapshot:
            raise DomainError("读取画面期间来源已变化", 409)
        return result

    @app.get("/api/annotation-workbench/{dataset}/episodes/{episode}/media")
    async def media(dataset: str, episode: int, request: Request):
        _, path, _ = workbench.legacy.resolve(dataset)
        signature = source_snapshot(path)["signature"]
        result = await media_call(request, dataset, episode, "describe")
        return {**result, "source_signature": signature}

    @app.get("/api/annotation-workbench/{dataset}/episodes/{episode}/frame/{frame}")
    async def frame(
        dataset: str,
        episode: int,
        frame: int,
        camera: str,
        signature: str,
        request: Request,
    ):
        _, path, _ = workbench.legacy.resolve(dataset)
        current = source_snapshot(path)["signature"]
        if current != signature:
            raise DomainError("来源已变化，停止使用旧帧号", 409)
        cache = settings.root / "annotation-frame-cache"
        cache.mkdir(parents=True, exist_ok=True)
        file = cache / (digest([dataset, current, episode, frame, camera]) + ".png")
        info = await media_call(
            request, dataset, episode, "frame", cache=file, frame=frame, camera=camera
        )
        return FileResponse(
            file,
            media_type="image/png",
            headers={
                "X-Data-Frame": str(info["frame"]),
                "X-Media-Pts": str(info["media_pts"]),
                "Cache-Control": "private, max-age=86400",
            },
        )

    @app.get("/api/annotation-workbench/{dataset}/media/{relative:path}")
    def video(
        dataset: str, relative: str, request: Request, signature: str | None = None
    ):
        _, path, _ = workbench.legacy.resolve(dataset)
        if signature is not None and source_snapshot(path)["signature"] != signature:
            raise DomainError("来源已变化，停止使用旧视频定位", 409)
        return serve_dataset_file(
            request, workbench.legacy.registry, f"bp/{dataset}/resolve/main/{relative}"
        )

    if settings.role == "viewer":

        @app.api_route(
            "/api/annotation-workbench/{route:path}", methods=["GET", "POST"]
        )
        async def proxy(route: str, request: Request):
            import re

            import httpx

            if not re.fullmatch(r"[A-Za-z0-9_/.-]+", route) or ".." in route:
                raise DomainError("工作区请求无效", 400)
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.request(
                        request.method,
                        settings.server_url.rstrip("/")
                        + "/api/annotation-workbench/"
                        + route,
                        params=request.query_params,
                        json=await request.json() if request.method == "POST" else None,
                        headers=authorization_headers(settings.token),
                        timeout=settings.command_timeout,
                    )
            except httpx.HTTPError as exc:
                raise DomainError("Server 暂不可用，本地草稿保留", 503) from exc
            return Response(
                response.content,
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/json"),
            )

        return

    @app.post("/api/annotation-workbench/templates/validate")
    def validate_templates(body: dict | list = Body(...)):
        from .task_annotations import parse_template_file

        return {"templates": parse_template_file(body)}

    @app.get("/api/annotation-workbench/datasets")
    def datasets():
        from .datasets import DATA_READY_STATES, dataset_for_job

        result = []
        for job in store.all("job"):
            if job.get("state") not in DATA_READY_STATES and not job.get(
                "dataset_ready"
            ):
                continue
            try:
                path = dataset_for_job(settings, job)
                if not (path / "meta/info.json").is_file():
                    continue
                workbench.legacy.registry.register(job["id"], path)
                result.append(
                    {"id": job["id"], "name": job.get("dataset_name") or job["id"]}
                )
            except DomainError:
                continue
        return {"datasets": result}

    @app.get("/api/annotation-workbench/{dataset}/episodes")
    def listing(
        dataset: str,
        subtask: str | None = None,
        count: int | None = None,
        recorded: bool = False,
        template_signature: str | None = None,
    ):
        if count is not None and count < 0:
            raise DomainError("次数不能为负数", 422)
        if annotations is not None:
            annotations.refresh_workbench(dataset)
        return workbench.listing(dataset, subtask, count, recorded, template_signature)

    @app.get("/api/annotation-workbench/{dataset}/episodes/{episode}")
    def episode(dataset: str, episode: int):
        if annotations is not None:
            annotations.refresh_workbench(dataset)
        return workbench.episode(dataset, episode)

    @app.post("/api/annotation-workbench/{dataset}/episodes/{episode}")
    def save(dataset: str, episode: int, body: SaveAnnotation):
        if runner is None:
            raise DomainError("请通过 Server 同步工作区", 503)
        return workbench.save(dataset, episode, body)

    @app.get("/api/annotation-workbench/{dataset}/episodes/{episode}/history")
    def history(dataset: str, episode: int):
        return {"history": workbench.history(dataset, episode)}

    @app.get("/api/annotation-workbench/{dataset}/episodes/{episode}/candidates")
    def candidates(dataset: str, episode: int):
        return {"candidates": workbench.candidates(dataset, episode)}

    @app.post("/api/annotation-workbench/{dataset}/episodes/{episode}/apply")
    def apply(dataset: str, episode: int, body: ApplyCandidate):
        if runner is None:
            raise DomainError("请通过 Server 同步工作区", 503)
        return workbench.apply(dataset, episode, body)

    @app.post("/api/annotation-workbench/{dataset}/confirm")
    def confirm(dataset: str, body: BatchConfirm):
        if runner is None:
            raise DomainError("请通过 Server 同步工作区", 503)
        return workbench.batch_confirm(dataset, body)

    @app.post("/api/annotation-workbench/{dataset}/import")
    def import_bundle(dataset: str, body: ImportBundle):
        bundle = body.bundle
        if (
            bundle.get("format") != "bodypipeline-annotations"
            or bundle.get("version") != 2
            or bundle.get("mode") != "template"
        ):
            raise DomainError("不是可读取的模板标注包；自由分析使用其原生导出", 422)
        source = bundle.get("source") or {}
        if source.get("dataset") != dataset:
            raise DomainError("标注包来源数据集身份不同，禁止套用", 409)
        prepared = []
        for entry in bundle.get("episodes", []):
            ep = entry.get("source_episode", entry.get("episode"))
            if type(ep) is not int:
                raise DomainError("标注包 Episode 身份无效", 422)
            head = workbench.episode(dataset, ep)
            if head["source_signature"] != source.get("signature") or head[
                "frames"
            ] != entry.get("frames"):
                raise DomainError(
                    "标注包来源版本或数据帧数不同，保留原包与当前草稿", 409
                )
            from .annotation_contract import validate_annotation

            document = validate_annotation(entry["document"], head["frames"])
            prepared.append((ep, entry, document))
        if not prepared:
            raise DomainError("标注包为空", 422)
        candidates = [
            workbench.add_candidate(
                dataset,
                ep,
                document,
                run="标注包导入",
                source_signature=source["signature"],
                base_revision=entry["revision"],
                provenance={
                    "importedBundle": {
                        "source": source,
                        "selection": bundle.get("selection"),
                    }
                },
            )
            for ep, entry, document in prepared
        ]
        return {"candidates": [c["id"] for c in candidates]}

    @app.post("/api/annotation-workbench/{dataset}/bundle")
    def bundle(dataset: str, body: Selection):
        return workbench.bundle(dataset, body)

    @app.post("/api/annotation-workbench/{dataset}/exports", status_code=202)
    def export(dataset: str, body: Selection):
        if runner is None:
            raise DomainError("请通过 Server 提交导出", 503)
        return runner.subtask_exports.enqueue_selection(dataset, body)

    @app.get("/api/annotation-workbench/{dataset}/exports/{export_id}")
    def export_status(dataset: str, export_id: str):
        workbench.legacy.resolve(dataset)
        job = store.get("job", export_id)
        if (
            job.get("source_kind") != "lerobot_annotation_subset"
            or job.get("source_job_id") != dataset
        ):
            raise DomainError("导出任务不存在", 404)
        result = {
            k: job.get(k)
            for k in (
                "id",
                "state",
                "stage",
                "dataset_name",
                "dataset_path",
                "episode_count",
                "error",
                "subtask_report",
            )
        }
        if job.get("state") == "CONVERTED":
            from .datasets import dataset_for_job

            row = workbench.legacy.registry.register(
                export_id, dataset_for_job(settings, job)
            )
            result["viewer_url"] = (
                settings.viewer_origin.rstrip("/")
                + row["page_path"]
                + "?tab=annotations"
            )
            from urllib.parse import urlencode

            result["data_url"] = (
                settings.server_url.rstrip("/")
                + "/?"
                + urlencode({"dataset": export_id})
                + "#results"
            )
        return result
