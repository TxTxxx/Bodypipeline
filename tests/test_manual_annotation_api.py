import json

import pytest
from fastapi.testclient import TestClient

from pipeline.server.app import create_app


def test_scheme_routes_validate_and_publish(settings_factory):
    app = create_app(settings_factory("server"), background=False)
    client = TestClient(
        app, headers={"Authorization": "Bearer " + app.state.runner.settings.token}
    )
    assert TestClient(app).get("/api/annotation-schemes").status_code == 401
    body = {
        "name": "Review",
        "types": [
            {
                "id": "result",
                "label": "Result",
                "scope": "episode",
                "fields": [{"id": "ok", "label": "Success", "dtype": "bool"}],
            }
        ],
    }
    response = client.post("/api/annotation-schemes", json=body)
    assert response.status_code == 201, response.text
    scheme = response.json()
    assert client.get("/api/annotation-schemes/" + scheme["id"]).status_code == 200
    assert (
        client.post("/api/annotation-schemes", json={**body, "types": []}).status_code
        == 422
    )


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_reader_preserves_actual_frame_timestamps(tmp_path, version):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from pipeline.server.subtask_review_worker import read_subtasks

    (tmp_path / "meta").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "meta/info.json").write_text(
        json.dumps(
            {
                "fps": 10,
                "codebase_version": version,
                "total_episodes": 1,
                "total_frames": 3,
                "features": {"observation.images.cam": {"dtype": "video"}},
            }
        )
    )
    pq.write_table(
        pa.table(
            {
                "episode_index": [0, 0, 0],
                "frame_index": [0, 1, 2],
                "timestamp": [0.0, 0.08, 0.23],
            }
        ),
        tmp_path / "data/file.parquet",
    )
    result = read_subtasks(tmp_path)
    assert result["episodes"][0]["frame_timestamps"] == [0.0, 0.08, 0.23]
    assert result["cameras"] == ["observation.images.cam"]
