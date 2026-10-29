"""Authenticated Server API for configurable human annotations."""

from fastapi import Body
from fastapi.responses import FileResponse


def install_manual_annotation_routes(app, settings, store):
    from .annotation_schemes import SchemeService
    from .manual_annotation_exports import ManualAnnotationExports
    from .manual_annotations import ManualAnnotations

    schemes = SchemeService(settings, store)
    workspace = ManualAnnotations(settings, store)
    exports = ManualAnnotationExports(settings, store)
    app.state.manual_annotations = workspace
    app.state.manual_annotation_exports = exports

    @app.get("/api/annotation-schemes")
    def scheme_list():
        return schemes.list()

    @app.post("/api/annotation-schemes", status_code=201)
    def scheme_create(body: dict = Body(...)):
        return schemes.create(body)

    @app.get("/api/annotation-schemes/{scheme_id}")
    def scheme_get(scheme_id: str, version: int | None = None):
        return schemes.get(scheme_id, version)

    @app.post("/api/annotation-schemes/{scheme_id}/versions")
    def scheme_version(scheme_id: str, body: dict = Body(...)):
        return schemes.version(scheme_id, body)

    @app.post("/api/annotation-schemes/{scheme_id}/copy", status_code=201)
    def scheme_copy(scheme_id: str, body: dict = Body(default={})):
        return schemes.copy(scheme_id, body)

    @app.post("/api/annotation-schemes/{scheme_id}/archive")
    def scheme_archive(scheme_id: str):
        return schemes.archive(scheme_id)

    @app.get("/api/manual-annotations/{dataset_id}")
    def manual_summary(dataset_id: str):
        return workspace.summary(dataset_id)

    @app.post("/api/manual-annotations/{dataset_id}/scheme")
    def manual_bind(dataset_id: str, body: dict = Body(...)):
        return workspace.bind(dataset_id, body)

    @app.get("/api/manual-annotations/{dataset_id}/episodes")
    def manual_episodes(
        dataset_id: str,
        status: str | None = None,
        type_id: str | None = None,
        field_id: str | None = None,
        field: str | None = None,
        value: str | None = None,
    ):
        return workspace.episodes(
            dataset_id,
            {
                k: v
                for k, v in {
                    "status": status,
                    "type_id": type_id,
                    "field_id": field_id,
                    "field": field,
                    "value": value,
                }.items()
                if v is not None
            },
        )

    @app.get("/api/manual-annotations/{dataset_id}/episodes/{episode_uid}")
    def manual_episode(dataset_id: str, episode_uid: str):
        return workspace.episode(dataset_id, episode_uid)

    @app.post("/api/manual-annotations/{dataset_id}/episodes/{episode_uid}")
    def manual_save(dataset_id: str, episode_uid: str, body: dict = Body(...)):
        return workspace.save(dataset_id, episode_uid, body)

    @app.post("/api/manual-annotations/{dataset_id}/exports", status_code=202)
    def manual_export(dataset_id: str, body: dict = Body(...)):
        return exports.enqueue(dataset_id, body)

    @app.get("/api/manual-annotations/{dataset_id}/exports/{export_id}")
    def manual_export_status(dataset_id: str, export_id: str):
        return exports.status(dataset_id, export_id)

    @app.get("/api/manual-annotations/{dataset_id}/exports/{export_id}/download")
    def manual_export_download(dataset_id: str, export_id: str):
        return FileResponse(
            exports.download(dataset_id, export_id),
            media_type="application/zip",
            filename=f"annotations-{export_id}.zip",
        )
