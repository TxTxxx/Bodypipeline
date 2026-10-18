import copy
import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_ingest import manifest
from test_processing import captured, upload_sample

from pipeline.client.app import create_app as client_app
from pipeline.client.service import CaptureService
from pipeline.server.app import create_app
from pipeline.server.ingest import IngestService
from pipeline.server.jobs import JobRunner
from pipeline.shared.collections import collection_metadata
from pipeline.shared.core import DomainError, digest


def seed(ingest, settings, session, name="fold cloth", complete=True, **changes):
    m, data = manifest(settings)
    m.update(session_id=session, dataset_name=name, instruction="", **changes)
    row = ingest.create(m, "default", "")
    if complete:
        ingest.chunk(row["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)
        row = ingest.complete(row["id"])
    return row


def test_client_dataset_append_and_legacy_read_without_rewrite(settings_factory):
    s = settings_factory("client")
    app = client_app(s, background=False)
    with TestClient(app) as http:
        headers = {"Authorization": "Bearer " + s.token}
        first = http.post(
            "/api/captures",
            headers=headers,
            json={"dataset_name": "  折叠　布料  ", "idempotency_key": "first-capture"},
        ).json()
        second = http.post(
            "/api/captures",
            headers=headers,
            json={"dataset_name": "折叠 布料", "idempotency_key": "second-capture"},
        ).json()
        assert first["collection_id"] == second["collection_id"]
        assert first["instruction"] == second["instruction"] == ""
        assert Path(first["raw_root"]).parents[2] == Path(second["raw_root"]).parents[2]
        assert first["raw_root"] != second["raw_root"]
        assert (
            http.post(
                "/api/captures",
                headers=headers,
                json={"dataset_name": "  ", "idempotency_key": "bad-capture"},
            ).status_code
            == 400
        )
        legacy = {
            k: v for k, v in first.items() if k not in {"dataset_name", "collection_id"}
        }
        legacy["instruction"] = "折叠 布料"
        legacy["raw_root"] = str(s.root / "old-session/raw")
        app.state.service.store.put("capture", first["id"], legacy)
        rows = http.get("/api/captures", headers=headers).json()
        assert len({r["collection_id"] for r in rows}) == 1
        assert app.state.service.store.get("capture", first["id"]) == legacy


def test_named_capture_seals_uploads_without_instruction(settings_factory):
    s = settings_factory("client")
    service = CaptureService(s)
    row = service.create(
        {
            "dataset_name": "new task",
            "local_root": "default",
            "remote_target": "default",
            "idempotency_key": "capture-one",
        }
    )
    service.start(row["id"])
    time.sleep(0.3)
    service.stop(row["id"])
    row = service.keep(row["id"])
    assert row["state"] == "SEALED"
    raw = Path(row["raw_root"])
    m = json.loads((raw.parent / "manifest.json").read_text())
    ingest = IngestService(settings_factory("server"))
    uid = upload_sample(ingest, raw, m)
    upload = ingest.store.get("upload", uid)
    assert upload["collection_id"] == row["collection_id"]
    assert f"collections/{row['collection_id']}/episodes/{uid}" in upload["raw_path"]
    with pytest.raises(DomainError, match="instruction"):
        JobRunner(ingest.settings, ingest).enqueue(uid)


def test_batch_api_snapshot_idempotency_and_scope(settings_factory):
    s = settings_factory("server")
    app = create_app(s, background=False)
    ing = app.state.ingest
    first, second = seed(ing, s, "one"), seed(ing, s, "two")
    partial = seed(ing, s, "partial", complete=False)
    other = seed(ing, s, "other", name="other task")
    real = seed(ing, s, "real", capture_mode="ros2")
    assert real["collection_id"] != first["collection_id"]
    path = f"/api/collections/{first['collection_id']}/process"
    payload = {
        "instruction": "  Fold the cloth.  ",
        "target_format": "v2.1",
        "run_doctor": False,
    }
    with TestClient(app) as http:
        headers = {"Authorization": "Bearer " + s.token}
        assert http.post(path, json=payload).status_code == 401
        assert (
            http.post(
                path,
                json=payload,
                headers={"Authorization": "Bearer " + s.device_token},
            ).status_code
            == 401
        )
        groups = http.get("/api/collections", headers=headers).json()
        group = next(g for g in groups if g["id"] == first["collection_id"])
        assert (group["episode_count"], group["ready_count"]) == (3, 2)
        original = http.post(path, json=payload, headers=headers)
        assert original.status_code == 202
        job = original.json()
        assert job["upload_ids"] == [first["id"], second["id"]]
        assert job["instruction"] == "Fold the cloth."
        assert job["source_count"] == 2
        assert (
            http.post(
                path,
                json={**payload, "upload_ids": list(reversed(job["upload_ids"]))},
                headers=headers,
            ).json()["id"]
            == job["id"]
        )
        assert (
            http.post(
                path, json={**payload, "instruction": "Different."}, headers=headers
            ).json()["id"]
            != job["id"]
        )
        for ids in (
            [first["id"], other["id"]],
            [first["id"], real["id"]],
            [first["id"], first["id"]],
            [partial["id"]],
        ):
            assert http.post(
                path, json={**payload, "upload_ids": ids}, headers=headers
            ).status_code in (400, 409)
        assert http.post(path, json={}, headers=headers).status_code == 422
        assert (
            http.post(
                path, json={**payload, "instruction": "  "}, headers=headers
            ).status_code
            == 400
        )
        assert (
            http.post(
                path, json={**payload, "upload_ids": []}, headers=headers
            ).status_code
            == 422
        )
        third = seed(ing, s, "three")
        assert ing.store.get("job", job["id"])["upload_ids"] == [
            first["id"],
            second["id"],
        ]
        next_job = http.post(path, json=payload, headers=headers).json()
        assert next_job["source_count"] == 3 and third["id"] in next_job["upload_ids"]
        assert next_job["id"] != job["id"]


def test_legacy_upload_grouping_preserves_paths_and_hashes(settings_factory):
    s = settings_factory("server")
    app = create_app(s, background=False)
    m, data = manifest(s)
    row = app.state.ingest.create(m, "default", "")
    row = {
        k: v
        for k, v in row.items()
        if k not in {"collection_id", "dataset_name", "collection_layout"}
    }
    app.state.ingest.store.put("upload", row["id"], row)
    app.state.ingest.chunk(row["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)
    row = app.state.ingest.complete(row["id"])
    expected = s.root / "raw/default" / row["id"] / "data"
    assert row["raw_path"] == str(expected)
    with TestClient(app) as http:
        group = http.get(
            "/api/collections", headers={"Authorization": "Bearer " + s.token}
        ).json()[0]
        assert group["dataset_name"] == m["instruction"]
        assert group["uploads"][0]["raw_path"] == str(expected)
        assert app.state.ingest.store.get("upload", row["id"]) == row
        assert app.state.ingest.manifest(row) == m


def test_collection_identity_separates_profiles_projects_targets(settings_factory):
    m, _ = manifest(settings_factory("server"))
    original = collection_metadata(m, "default", "")
    for field, value in [("project_id", "other"), ("capture_mode", "ros2")]:
        assert collection_metadata({**m, field: value}, "default", "") != original
    other = copy.deepcopy(m)
    other["profile"]["robot_type"] = "other"
    other["profile_hash"] = digest(other["profile"])
    assert collection_metadata(other, "default", "") != original
    assert collection_metadata(m, "other", "") != original
    assert collection_metadata(m, "default", "other") != original


@pytest.mark.parametrize("target_format", ["v2.1", "v3.0"])
def test_real_multi_episode_conversion_instruction_and_doctor(
    settings_factory, letools_binary, target_format
):
    raw, m = captured(settings_factory)
    s = settings_factory("server", processing_mode="real", letools_bin=letools_binary)
    ingest = IngestService(s)
    first = upload_sample(ingest, raw, m)
    m2 = {**m, "session_id": "second-episode"}
    second = upload_sample(ingest, raw, m2)
    cid = ingest.store.get("upload", first)["collection_id"]
    runner = JobRunner(s, ingest)
    instruction = 'Fold cloth gently. "quotes"; $(not-a-command)'
    job = runner.enqueue_collection(cid, instruction, target_format, False)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "CONVERTED", result
    data = Path(result["dataset_path"])
    info = json.loads((data / "meta/info.json").read_text())
    assert info["total_episodes"] == 2
    assert info["total_frames"] == result["alignment_report"]["frames"]
    lineage = json.loads((data.parent / "lineage.json").read_text())
    assert [x["upload_id"] for x in lineage["sources"]] == [first, second]
    assert lineage["instruction"] == instruction
    probe = subprocess.run(
        [
            str(Path(letools_binary).with_name("python")),
            "-c",
            "import json,sys; from pathlib import Path; import pyarrow.parquet as pq; p=Path(sys.argv[1]); tasks=[json.loads(s)['task'] for s in (p/'meta/tasks.jsonl').read_text().splitlines()] if (p/'meta/tasks.jsonl').exists() else pq.read_table(p/'meta/tasks.parquet').to_pandas().index.tolist(); print(json.dumps(tasks))",
            str(data),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(probe.stdout) == [instruction]
    from pipeline.server.checks import QualityRunner

    checks = QualityRunner(s, ingest.store, runner.command)
    check = checks.enqueue(job["id"])
    checks.tick()
    checked = ingest.store.get("check", check["id"])
    assert checked["state"] == "COMPLETED", checked
    assert checked["doctor_report"]["total_episodes"] == 2
    assert checked["doctor_report"]["total_frames"] == info["total_frames"]
    assert (
        ingest.manifest(ingest.store.get("upload", first))["instruction"]
        == "fold cloth"
    )


def test_batch_corrupt_episode_fails_whole_snapshot(settings_factory):
    raw, m = captured(settings_factory)
    s = settings_factory("server")
    ingest = IngestService(s)
    first = upload_sample(ingest, raw, m)
    second = upload_sample(ingest, raw, {**m, "session_id": "corrupt-source-2"})
    third = upload_sample(ingest, raw, {**m, "session_id": "corrupt-source-3"})
    for uid in (second, third):
        row = ingest.store.get("upload", uid)
        (Path(row["raw_path"]) / m["files"][0]["path"]).write_bytes(b"corrupt")
    runner = JobRunner(s, ingest)
    job = runner.enqueue_collection(
        ingest.store.get("upload", second)["collection_id"], "Fold cloth."
    )
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "FAILED"
    assert second in result["error"] and third in result["error"]
    assert not result.get("dataset_ready")
    assert {first, second, third}.issubset(result["upload_ids"])
    diagnostics = {row["upload_id"]: row for row in result["episode_diagnostics"]}
    assert set(diagnostics) == {first, second, third}
    assert diagnostics[first]["passed"] is True
    assert diagnostics[second]["passed"] is False
    assert diagnostics[third]["passed"] is False
    assert "原始文件摘要变化" in diagnostics[second]["error"]
    assert "原始文件摘要变化" in diagnostics[third]["error"]


def test_batch_staging_migration_verifies_every_source(settings_factory, tmp_path):
    from scripts.migrate_server_data import migrate

    raw, m = captured(settings_factory)
    s = settings_factory("server")
    ingest = IngestService(s)
    one = upload_sample(ingest, raw, m)
    upload_sample(ingest, raw, {**m, "session_id": "second-for-migration"})
    runner = JobRunner(s, ingest)
    job = runner.enqueue_collection(
        ingest.store.get("upload", one)["collection_id"], "Fold cloth."
    )
    runner.tick()
    assert ingest.store.get("job", job["id"])["state"] == "DEMO_READY"
    s.data_root = tmp_path / "shared-data"
    s.data_root.mkdir()
    result = migrate(s)
    assert result["uploads"] == 2 and result["jobs"] == 1
    updated = ingest.store.get("job", job["id"])
    aligned = Path(updated["work_path"]) / "aligned"
    assert len(list(aligned.glob("episode*"))) == 2
    assert not any(p.is_symlink() for p in aligned.rglob("*"))
