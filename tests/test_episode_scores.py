import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from pipeline.server.episode_scores import EpisodeScoreCache, dataset_signature
from pipeline.server.viewer import create_gateway
from pipeline.server.viewer_data import PreviewRegistry
from pipeline.shared.core import Store

WORKER = Path(__file__).resolve().parents[1] / "pipeline/server/episode_score_worker.py"


def dataset(root):
    (root / "meta").mkdir(parents=True)
    (root / "meta/info.json").write_text(
        json.dumps({"total_episodes": 1, "total_frames": 3})
    )
    return root


def wait_for_score(cache, preview):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = cache.status(preview)
        if result["state"] not in {"QUEUED", "RUNNING"}:
            return result
        time.sleep(0.01)
    pytest.fail("评分未结束")


def test_score_cache_async_dedup_restart_and_invalidation(
    settings_factory, tmp_path, monkeypatch
):
    settings = settings_factory("viewer", letools_bin=sys.executable)
    store = Store(settings.root / "state")
    root = dataset(tmp_path / "dataset")
    preview = {"dataset_path": str(root)}
    cache = EpisodeScoreCache(settings, store)
    gate = Event()
    calls = []

    def run(_root, key, signature, python):
        calls.append(python)
        gate.wait(5)
        store.put(
            "episode-score",
            key,
            {"state": "COMPLETED", "signature": signature, "report": {"episodes": []}},
        )

    monkeypatch.setattr(cache, "_run", run)
    try:
        assert cache.status(preview) == {"state": "NONE"}
        assert cache.start(preview)["state"] == "QUEUED"
        assert cache.start(preview)["state"] in {"QUEUED", "RUNNING"}
        gate.set()
        assert wait_for_score(cache, preview)["state"] == "COMPLETED"
        assert len(calls) == 1
        assert cache.start(preview)["state"] == "COMPLETED"
        another = EpisodeScoreCache(settings, store)
        try:
            assert another.status(preview)["state"] == "COMPLETED"
            (root / "meta/info.json").write_text('{"total_episodes": 2}')
            assert another.status(preview)["state"] == "NONE"
        finally:
            another.close()
    finally:
        gate.set()
        cache.close()


def test_score_cache_rejects_stale_and_incomplete_output(
    settings_factory, tmp_path, monkeypatch
):
    settings = settings_factory("viewer", letools_bin=sys.executable)
    store = Store(settings.root / "state")
    root = dataset(tmp_path / "dataset")
    cache = EpisodeScoreCache(settings, store)
    report = {"episodes": [{"episode_index": 0}], "total_episodes": 1}

    class Process:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def communicate(self, **kwargs):
            return json.dumps(report), ""

    monkeypatch.setattr(
        "pipeline.server.episode_scores.subprocess.Popen", lambda *a, **k: Process()
    )
    try:
        signature = dataset_signature(root)
        (root / "meta/info.json").write_text('{"changed": true}')
        cache._run(root, "stale", signature, sys.executable)
        assert "发生变化" in store.get("episode-score", "stale")["error"]
        report["episodes"] = []
        cache._run(root, "incomplete", dataset_signature(root), sys.executable)
        assert "未覆盖" in store.get("episode-score", "incomplete")["error"]
    finally:
        cache.close()


def test_gateway_score_endpoints_require_auth_and_registered_preview(
    settings_factory, tmp_path, monkeypatch
):
    settings = settings_factory("viewer")
    store = Store(settings.root / "state")
    PreviewRegistry(store, 300).register("job-a", dataset(tmp_path / "dataset"))
    app = create_gateway(settings)
    cache = app.state.episode_scores
    monkeypatch.setattr(cache, "start", lambda row: {"state": "QUEUED"})
    with TestClient(app) as client:
        assert client.get("/api/episode-scores/job-a").status_code == 401
        assert client.post("/api/episode-scores/job-a").status_code == 401
        client.post("/api/login", json={"token": settings.token})
        client.headers["Origin"] = "http://testserver"
        assert client.get("/api/episode-scores/job-a").json() == {"state": "NONE"}
        assert client.post("/api/episode-scores/job-a").status_code == 202
        assert client.get("/api/episode-scores/not-registered").status_code == 404
        assert client.post("/api/episode-scores/not-registered").status_code == 404
        row = store.get("preview", "job-a")
        row["expires_at"] = 0
        store.put("preview", "job-a", row)
        assert client.get("/api/episode-scores/job-a").status_code == 404


def test_score_signature_tracks_shards_and_rejects_symlinks(tmp_path):
    from pipeline.shared.core import DomainError

    root = dataset(tmp_path / "dataset")
    (root / "data").mkdir()
    shard = root / "data/file.parquet"
    shard.write_bytes(b"before")
    before = dataset_signature(root)
    shard.write_bytes(b"after")
    assert dataset_signature(root) != before
    shard.unlink()
    shard.symlink_to(root / "meta/info.json")
    with pytest.raises(DomainError, match="符号链接"):
        dataset_signature(root)


def test_gateway_runs_real_single_episode_score(
    settings_factory, tmp_path, letools_binary
):
    root = tmp_path / "dataset"
    python = str(Path(letools_binary).with_name("python"))
    subprocess.run(
        [
            python,
            "-c",
            """
import json, sys
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
p=Path(sys.argv[1]); (p/'meta').mkdir(parents=True); (p/'data').mkdir()
(p/'meta/info.json').write_text(json.dumps({'total_episodes':1,'total_frames':4}))
pq.write_table(pa.table({'episode_index':[0]*4,'frame_index':list(range(4)),
'action':[[0.,1.],[.01,1.01],[.02,1.02],[.03,1.03]]}),p/'data/episode_0.parquet')
""",
            str(root),
        ],
        check=True,
    )
    settings = settings_factory("viewer", letools_bin=letools_binary)
    store = Store(settings.root / "state")
    PreviewRegistry(store, 300).register("sample", root)
    with TestClient(create_gateway(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.post("/api/episode-scores/sample").status_code == 202
        deadline = time.monotonic() + 10
        while True:
            result = client.get("/api/episode-scores/sample").json()
            if result["state"] not in {"QUEUED", "RUNNING"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert result["state"] == "COMPLETED", result
        row = result["report"]["episodes"][0]
        assert row["status"] == "SCORED" and 0 <= row["overall_score"] <= 100
        assert row["consistency"] is None
        assert "不足 4" in row["reasons"][0]


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_real_doctor_scores_vector_actions_all_shards_without_mutating(
    tmp_path, letools_binary, version
):
    python = str(Path(letools_binary).with_name("python"))
    script = r"""
import json, sys
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from lerobot_doctor import score as doctor
root = Path(sys.argv[1]); version = sys.argv[2]
(root / 'data/chunk-000').mkdir(parents=True)
(root / 'meta').mkdir()
lengths = [6, 2, 7, 8, 9, 5, 0]
actions = {i: np.stack([np.linspace(0, (i + 1)*.02, n), np.linspace(.1, .3, n)], axis=1) for i,n in enumerate(lengths) if n}
actions[5][2,0] = float('nan')
parts = [[],[]] if version == 'v3.0' else [[] for _ in range(6)]
for ep, a in actions.items():
    for frame, value in enumerate(a):
        part = (0 if frame < 3 else 1) if version == 'v3.0' else ep
        parts[part].append({'episode_index':ep,'frame_index':frame,'action':value.tolist()})
for i, rows in enumerate(parts): pq.write_table(pa.Table.from_pylist(rows),root / f'data/chunk-000/file-{i:03d}.parquet')
(root/'meta/info.json').write_text(json.dumps({'codebase_version':version,'total_episodes':len(lengths),'total_frames':sum(lengths)}))
# Independent expected input to the upstream algorithm: preserve each (T,D).
eligible = {i:{'actions':actions[i]} for i in [0,2,3,4]}
with patch.object(doctor,'_load_episodes',return_value=eligible):
    expected = doctor.score_episodes(root)
print(json.dumps({s.episode_index: {'overall':s.overall_score,'smoothness':s.smoothness,'consistency':s.consistency} for s in expected.scores}))
"""
    root = tmp_path / version
    reference = subprocess.run(
        [python, "-c", script, str(root), version],
        capture_output=True,
        text=True,
        check=True,
    )
    expected = json.loads(reference.stdout)
    before = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }
    result = subprocess.run(
        [python, str(WORKER), str(root)], capture_output=True, text=True, check=True
    )
    report = json.loads(result.stdout)
    assert report["total_episodes"] == 7 and report["scored_episodes"] == 4
    assert [r["episode_index"] for r in report["episodes"]] == list(range(7))
    for ep in (0, 2, 3, 4):
        row = report["episodes"][ep]
        assert row["overall_score"] == pytest.approx(expected[str(ep)]["overall"])
        assert row["smoothness"] == pytest.approx(expected[str(ep)]["smoothness"])
        assert row["consistency"] == pytest.approx(expected[str(ep)]["consistency"])
    assert report["episodes"][0]["frames"] == 6
    for ep in (1, 5, 6):
        assert report["episodes"][ep]["status"] == "UNSCORABLE"
        assert report["episodes"][ep]["overall_score"] is None
        assert report["episodes"][ep]["reasons"]
    after = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }
    assert after == before
