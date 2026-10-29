import json

import pytest

from pipeline.shared.core import DomainError, Store


def scheme_body():
    return {
        "name": "桌面操作",
        "types": [
            {
                "id": "outcome",
                "label": "结果",
                "scope": "episode",
                "fields": [
                    {
                        "id": "success",
                        "label": "成功",
                        "dtype": "bool",
                        "required": True,
                    },
                    {
                        "id": "reason",
                        "label": "原因",
                        "dtype": "single",
                        "options": [{"value": "slip", "label": "滑落"}],
                    },
                ],
            },
            {
                "id": "contact",
                "label": "接触",
                "scope": "interval",
                "allow_overlap": False,
                "fields": [
                    {
                        "id": "force",
                        "label": "力",
                        "dtype": "float",
                        "min": 0,
                        "max": 10,
                    }
                ],
            },
            {
                "id": "note",
                "label": "备注",
                "scope": "frame",
                "fields": [
                    {"id": "text", "label": "文字", "dtype": "text", "max_length": 50}
                ],
            },
        ],
    }


@pytest.fixture
def service(settings_factory, monkeypatch):
    from pipeline.server.manual_annotations import ManualAnnotations
    from pipeline.server.subtask_review import SubtaskReview

    settings = settings_factory("server")
    store = Store(settings.root / "state")
    source = settings.root / "source"
    (source / "meta").mkdir(parents=True)
    (source / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "fps": 10,
                "total_episodes": 2,
                "total_frames": 6,
            }
        )
    )
    store.put(
        "job",
        "sample",
        {"id": "sample", "state": "CONVERTED", "dataset_path": str(source)},
    )
    monkeypatch.setattr(
        SubtaskReview,
        "_source",
        lambda *a: {
            "fps": 10,
            "cameras": ["observation.images.top"],
            "episodes": [
                {
                    "episode_index": 0,
                    "frames": 3,
                    "frame_timestamps": [0, 0.07, 0.21],
                    "segments": [{"start_frame": 0, "end_frame": 2, "text": "grab"}],
                    "atoms": [],
                },
                {
                    "episode_index": 1,
                    "frames": 3,
                    "frame_timestamps": [0, 0.1, 0.2],
                    "segments": [],
                    "atoms": [],
                },
            ],
        },
    )
    return ManualAnnotations(settings, store), source


def head_and_body(svc):
    summary = svc.summary("sample")
    uid = summary["episodes"][0]["episode_uid"]
    episode = svc.episode("sample", uid)
    return uid, {
        "revision": episode["revision"],
        "source_signature": episode["source_signature"],
    }


def bind(svc):
    definition = svc.schemes.create(scheme_body())
    head = svc.summary("sample")
    svc.bind(
        "sample",
        {
            "scheme_id": definition["id"],
            "version": definition["version"],
            "revision": head["revision"],
            "source_signature": head["source_signature"],
        },
    )


def test_scheme_versions_are_immutable(service):
    svc, _ = service
    first = svc.schemes.create(scheme_body())
    body = scheme_body()
    body["name"] = "Changed"
    second = svc.schemes.version(first["id"], {**body, "revision": first["version"]})
    assert second["version"] == 2
    assert svc.schemes.get(first["id"], 1)["name"] == "桌面操作"
    assert svc.schemes.copy(first["id"], {"name": "copy"})["id"] != first["id"]
    svc.schemes.archive(first["id"])
    assert svc.schemes.get(first["id"])["archived"] is True


@pytest.mark.parametrize(
    "patch",
    [
        {"dtype": "int", "default": True},
        {"dtype": "bool", "widget": "slider"},
        {"dtype": "float", "min": 10, "max": 0},
        {"dtype": "text", "default": 2},
    ],
)
def test_invalid_field_definitions_are_rejected(service, patch):
    svc, _ = service
    body = scheme_body()
    body["types"][0]["fields"][0].update(patch)
    with pytest.raises(DomainError):
        svc.schemes.create(body)


def test_workspace_persists_without_preview_and_preserves_source(service):
    svc, source = service
    before = (source / "meta/info.json").read_bytes()
    bind(svc)
    uid, body = head_and_body(svc)
    saved = svc.save(
        "sample",
        uid,
        {**body, "values": {"outcome": {"success": False}}, "status": "completed"},
    )
    assert saved["status"] == "completed"
    restarted = type(svc)(svc.settings, svc.store)
    assert restarted.episode("sample", uid)["values"] == {"outcome": {"success": False}}
    assert (source / "meta/info.json").read_bytes() == before
    with pytest.raises(DomainError, match="版本"):
        svc.save("sample", uid, {**body, "values": {}})


def test_draft_and_completion_validation(service):
    svc, _ = service
    bind(svc)
    uid, body = head_and_body(svc)
    draft = svc.save("sample", uid, {**body, "status": "draft"})
    assert draft["status"] == "draft"
    with pytest.raises(DomainError):
        svc.save(
            "sample",
            uid,
            {
                "revision": draft["revision"],
                "source_signature": draft["source_signature"],
                "status": "completed",
            },
        )


@pytest.mark.parametrize(
    "values",
    [
        {"unknown": {}},
        {"outcome": {"unknown": 1}},
        {"outcome": {"success": 1}},
        {"outcome": {"reason": "new"}},
    ],
)
def test_values_strictly_follow_bound_scheme(service, values):
    svc, _ = service
    bind(svc)
    uid, body = head_and_body(svc)
    with pytest.raises(DomainError):
        svc.save("sample", uid, {**body, "values": values})


def test_custom_time_ranges_and_actual_frame_timestamps(service):
    svc, _ = service
    bind(svc)
    uid, body = head_and_body(svc)
    records = [
        {
            "type_id": "contact",
            "start_frame": 1,
            "end_frame": 3,
            "values": {"force": 3.5},
        },
        {"type_id": "note", "frame_index": 2, "values": {"text": "done"}},
    ]
    saved = svc.save("sample", uid, {**body, "records": records})
    assert saved["frame_timestamps"] == [0, 0.07, 0.21]
    assert saved["records"][0]["id"]
    assert saved["records"][0]["end_frame"] == 3
    body.update(revision=saved["revision"])
    with pytest.raises(DomainError):
        svc.save(
            "sample",
            uid,
            {**body, "records": [records[0], {**records[0], "start_frame": 2}]},
        )
    with pytest.raises(DomainError):
        svc.save(
            "sample",
            uid,
            {**body, "records": [{"type_id": "note", "frame_index": 3, "values": {}}]},
        )


def test_legacy_save_preserves_custom_and_demotes_completion(service):
    svc, _ = service
    bind(svc)
    uid, body = head_and_body(svc)
    svc.save(
        "sample",
        uid,
        {**body, "values": {"outcome": {"success": True}}, "status": "completed"},
    )
    legacy = svc.legacy_episode("sample", 0)
    assert legacy["segments"] == [{"start_frame": 0, "end_frame": 2, "text": "grab"}]
    svc.legacy_save(
        "sample",
        0,
        {
            "revision": legacy["revision"],
            "source_signature": legacy["source_signature"],
            "segments": [],
        },
    )
    current = svc.episode("sample", uid)
    assert current["segments"] == []
    assert current["values"]["outcome"]["success"] is True
    assert current["status"] == "draft"


def test_remap_is_idempotent_and_preserves_episode_identity(service):
    svc, source = service
    summary = svc.summary("sample")
    survivor = summary["episodes"][1]["episode_uid"]
    svc.remap(source, {"1": 0}, "purge-one")
    svc.remap(source, {"1": 0}, "purge-one")
    head = svc.summary("sample")
    assert head["episodes"] == [
        {"episode_uid": survivor, "episode_index": 0, "status": "not_started"}
    ]


def test_export_freezes_completed_only_and_filters(service):
    svc, _ = service
    bind(svc)
    uid, body = head_and_body(svc)
    svc.save(
        "sample",
        uid,
        {**body, "values": {"outcome": {"success": True}}, "status": "completed"},
    )
    filtered = svc.episodes("sample", {"status": "completed"})
    assert len(filtered["episodes"]) == 1
    snapshot = svc.export_snapshot("sample", {})
    assert len(snapshot["episodes"]) == 1
    assert snapshot["episodes"][0]["values"]["outcome"]["success"] is True
    uid, body = head_and_body(svc)
    svc.save("sample", uid, {**body, "values": {"outcome": {"success": False}}})
    assert snapshot["episodes"][0]["values"]["outcome"]["success"] is True


def test_external_data_change_fails_closed(service):
    svc, source = service
    svc.summary("sample")
    (source / "new-file").write_text("changed")
    with pytest.raises(DomainError):
        svc.summary("sample")


@pytest.mark.parametrize(
    "patch",
    [
        {"segments": 1},
        {"segments": [1]},
        {"atoms": {}},
        {"records": "bad"},
        {"records": [{"type_id": []}]},
        {"status": []},
        {"values": []},
    ],
)
def test_malformed_payloads_fail_validation(service, patch):
    svc, _ = service
    bind(svc)
    uid, body = head_and_body(svc)
    with pytest.raises(DomainError):
        svc.save("sample", uid, {**body, **patch})


def test_required_subtask_fields_checked_even_when_missing(service):
    svc, _ = service
    body = scheme_body()
    body["types"].append(
        {
            "id": "subtask",
            "label": "Steps",
            "scope": "interval",
            "fields": [
                {"id": "object", "label": "Object", "dtype": "text", "required": True}
            ],
        }
    )
    definition = svc.schemes.create(body)
    head = svc.summary("sample")
    svc.bind(
        "sample",
        {
            "scheme_id": definition["id"],
            "version": 1,
            "revision": head["revision"],
            "source_signature": head["source_signature"],
        },
    )
    uid, payload = head_and_body(svc)
    with pytest.raises(DomainError):
        svc.save(
            "sample",
            uid,
            {
                **payload,
                "values": {"outcome": {"success": True}},
                "status": "completed",
            },
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["types"][0].update(scope=[]),
        lambda b: b["types"][0]["fields"][0].update(dtype=[]),
        lambda b: b["types"][0]["fields"][0].update(widget=[]),
    ],
)
def test_malformed_schema_fails_validation(service, mutate):
    svc, _ = service
    body = scheme_body()
    mutate(body)
    with pytest.raises(DomainError):
        svc.schemes.create(body)


def test_typed_filters_and_subtask_presence(service):
    svc, _ = service
    bind(svc)
    uid, body = head_and_body(svc)
    svc.save("sample", uid, {**body, "values": {"outcome": {"success": False}}})
    assert (
        len(
            svc.episodes("sample", {"field_id": "outcome.success", "value": "false"})[
                "episodes"
            ]
        )
        == 1
    )
    assert (
        len(
            svc.episodes("sample", {"field_id": "outcome.success", "value": "true"})[
                "episodes"
            ]
        )
        == 0
    )
    assert len(svc.episodes("sample", {"type_id": "subtask"})["episodes"]) == 1
    with pytest.raises(DomainError):
        svc.episodes("sample", {"field_id": "outcome.unknown", "value": "true"})


@pytest.mark.parametrize(
    "body",
    [
        {"include_drafts": "false"},
        {"filters": {"status": "wrong"}},
        {"filters": {"unknown": "ignored"}},
        {"filters": []},
    ],
)
def test_export_options_are_strict(service, body):
    svc, _ = service
    with pytest.raises(DomainError):
        svc.export_snapshot("sample", body)


def test_scheme_version_requires_integer_revision(service):
    svc, _ = service
    first = svc.schemes.create(scheme_body())
    with pytest.raises(DomainError):
        svc.schemes.version(first["id"], {**scheme_body(), "revision": True})


def test_concurrent_scheme_publish_has_one_winner(service):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    svc, _ = service
    first = svc.schemes.create(scheme_body())
    barrier = Barrier(2)

    def publish():
        barrier.wait()
        try:
            return svc.schemes.version(first["id"], {**scheme_body(), "revision": 1})[
                "version"
            ]
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish(), range(2)))
    assert sorted(results) == [2, 409]
    assert svc.schemes.get(first["id"])["version"] == 2
    assert svc.schemes.get(first["id"], 1)["name"] == "桌面操作"


def test_legacy_save_rechecks_canonical_workspace_inside_lock(service, monkeypatch):
    from pipeline.server.manual_annotations import ManualAnnotations
    from pipeline.server.subtask_review import DraftRequest, SubtaskReview
    from pipeline.server.viewer_data import PreviewRegistry

    workspace, source = service
    head = workspace.summary("sample")
    PreviewRegistry(workspace.store, workspace.settings.viewer_ttl).register(
        "sample", source
    )
    calls = 0
    original = ManualAnnotations.exists

    def concurrent_first_read(self, dataset_id):
        nonlocal calls
        calls += 1
        return False if calls == 1 else original(self, dataset_id)

    monkeypatch.setattr(ManualAnnotations, "exists", concurrent_first_read)
    body = DraftRequest(
        revision=head["revision"],
        source_signature=head["source_signature"],
        segments=[{"start_frame": 0, "end_frame": 2, "text": "new edit"}],
    )
    SubtaskReview(workspace.settings, workspace.store).save("sample", 0, body)
    record = workspace.episode("sample", head["episodes"][0]["episode_uid"])
    assert record["segments"][0]["text"] == "new edit"
