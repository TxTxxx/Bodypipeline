"""In-place language-instruction rewrites on real LeRobot datasets."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCENARIO = Path(__file__).parent / "fixtures/merge_scenario.py"
WORKER = (
    Path(__file__).resolve().parents[1]
    / "pipeline/server/instruction_writer_worker.py"
)


def hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def run_worker(python, dataset, operation, assignments):
    spec = dataset.parent / f"{operation}.json"
    spec.write_text(
        json.dumps(
            {
                "operation_id": operation,
                "dataset_path": str(dataset),
                "assignments": [
                    {"episode_index": episode, "instruction": instruction}
                    for episode, instruction in assignments.items()
                ],
            },
            ensure_ascii=False,
        )
    )
    result = subprocess.run(
        [python, str(WORKER), "apply", str(spec)],
        capture_output=True,
        text=True,
    )
    return result


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_real_in_place_rewrite_preserves_paths_and_unassigned_episode(
    tmp_path, letools_binary, version
):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [python, str(SCENARIO), "create", str(fixtures), version], check=True
    )
    dataset = fixtures / "input-0"
    original_path = str(dataset.resolve())
    before = hashes(dataset)

    result = run_worker(
        python, dataset, "real-one", {0: "把蓝色圆柱放入右侧盒子"}
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["updated_episodes"] == [0]
    assert str(dataset.resolve()) == original_path
    journal = json.loads(
        (
            dataset
            / "meta/.bodypipeline-instruction-journal-real-one.json"
        ).read_text()
    )
    assert journal["state"] == "committed"
    assert journal["report"]["updated_episodes"] == [0]

    loaded = subprocess.run(
        [python, str(WORKER), "read", str(dataset)],
        check=True,
        capture_output=True,
        text=True,
    )
    episodes = {
        item["episode_index"]: item for item in json.loads(loaded.stdout)["episodes"]
    }
    assert episodes[0]["instruction"] == "把蓝色圆柱放入右侧盒子"
    assert episodes[1]["task_index"] == 1
    assert episodes[1]["instruction"] == "place"

    after = hashes(dataset)
    if version == "v2.1":
        assert (
            after["data/chunk-000/episode_000001.parquet"]
            == before["data/chunk-000/episode_000001.parquet"]
        )
        assert (
            after["data/chunk-000/episode_000000.parquet"]
            != before["data/chunk-000/episode_000000.parquet"]
        )
    else:
        assert (
            after["data/chunk-000/file-000.parquet"]
            != before["data/chunk-000/file-000.parquet"]
        )
        assert (
            after["meta/episodes/chunk-000/file-000.parquet"]
            != before["meta/episodes/chunk-000/file-000.parquet"]
        )

    validation = subprocess.run(
        [letools_binary, "validate", "--deep", str(dataset)],
        capture_output=True,
        text=True,
    )
    assert validation.returncode == 0, validation.stdout + validation.stderr


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_shared_instruction_is_reused_and_duplicate_apply_is_idempotent(
    tmp_path, letools_binary, version
):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [python, str(SCENARIO), "create", str(fixtures), version], check=True
    )
    dataset = fixtures / "input-0"
    first = run_worker(
        python, dataset, "shared-first", {0: "共享指令", 1: "共享指令"}
    )
    assert first.returncode == 0, first.stderr
    before_duplicate = hashes(dataset)
    duplicate = run_worker(
        python, dataset, "shared-second", {0: "共享指令", 1: "共享指令"}
    )
    assert duplicate.returncode == 0, duplicate.stderr
    assert json.loads(duplicate.stdout)["noop"] is True
    assert hashes(dataset) == before_duplicate
    read = subprocess.run(
        [python, str(WORKER), "read", str(dataset)],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(read.stdout)
    assert {item["task_index"] for item in payload["episodes"]} == {2}
    assert [item["task"] for item in payload["tasks"]].count("共享指令") == 1


def test_validation_failure_rolls_back_every_replaced_file(
    tmp_path, letools_binary
):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [python, str(SCENARIO), "create", str(fixtures), "v3.0"], check=True
    )
    dataset = fixtures / "input-0"
    before = hashes(dataset)
    spec = tmp_path / "rollback.json"
    spec.write_text(
        json.dumps(
            {
                "operation_id": "rollback",
                "dataset_path": str(dataset),
                "assignments": [
                    {"episode_index": 0, "instruction": "rollback me"}
                ],
            }
        )
    )
    code = f"""
import json, sys
from pathlib import Path
import letools
from pipeline.server.instruction_writer_worker import apply_instructions
letools.validate_dataset = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("injected validation failure"))
try:
    apply_instructions(json.loads(Path({str(spec)!r}).read_text()))
except Exception as exc:
    print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
    raise SystemExit(7)
raise SystemExit(0)
"""
    result = subprocess.run(
        [python, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        },
    )
    assert result.returncode == 7, result.stderr
    assert "injected validation failure" in result.stderr
    assert hashes(dataset) == before


def test_restart_recovery_rolls_back_an_applied_uncommitted_journal(
    tmp_path, letools_binary
):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [python, str(SCENARIO), "create", str(fixtures), "v3.0"], check=True
    )
    dataset = fixtures / "input-0"
    relative = "data/chunk-000/file-000.parquet"
    original = dataset / relative
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    backup = dataset / "meta/.bodypipeline-instruction-backup-crash" / relative
    backup.parent.mkdir(parents=True)
    shutil.copy2(original, backup)
    original.write_bytes(original.read_bytes() + b"interrupted")
    journal = (
        dataset
        / "meta/.bodypipeline-instruction-journal-crash.json"
    )
    journal.write_text(
        json.dumps(
            {
                "operation_id": "crash",
                "state": "applied",
                "files": [
                    {
                        "path": relative,
                        "staged": "meta/.bodypipeline-instruction-stage-crash/"
                        + relative,
                        "backup": backup.relative_to(dataset).as_posix(),
                    }
                ],
            }
        )
    )
    result = subprocess.run(
        [python, str(WORKER), "recover", str(dataset)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["recovered"] == ["crash"]
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before
    assert not (
        dataset / "meta/.bodypipeline-instruction-backup-crash"
    ).exists()
