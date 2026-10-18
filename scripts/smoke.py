#!/usr/bin/env python3
"""Exercise already running LOCAL MOCK services only. Refuses a real robot client."""

import json
import time
import uuid
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
keys = json.loads((root / ".runtime/local-credentials.json").read_text())
headers = {"Authorization": "Bearer " + keys["PIPELINE_TOKEN"]}
with (
    httpx.Client(
        base_url="http://127.0.0.1:8080", headers=headers, timeout=120, trust_env=False
    ) as client,
    httpx.Client(
        base_url="http://127.0.0.1:8081", headers=headers, timeout=120, trust_env=False
    ) as server,
):
    config = client.get("/api/config").raise_for_status().json()
    assert config["mode"] == "mock", "REFUSED: smoke test must never run on real robot"
    real = (
        server.get("/api/config").raise_for_status().json()["processing_mode"] == "real"
    )
    body = {
        "instruction": "Fold the cloth — synthetic integration test",
        "idempotency_key": str(uuid.uuid4()),
        "remote_prefix": "smoke",
    }
    row = client.post("/api/captures", json=body).raise_for_status().json()
    cid = row["id"]
    started = client.post(f"/api/captures/{cid}/start").raise_for_status().json()
    assert started["state"] == "RECORDING", started
    time.sleep(4.2 if real else 2)
    stopped = client.post(f"/api/captures/{cid}/stop").raise_for_status().json()
    assert stopped["state"] == "REVIEW", stopped
    sealed = client.post(f"/api/captures/{cid}/keep").raise_for_status().json()
    deadline = time.time() + 120
    while sealed["state"] == "SEALING" and time.time() < deadline:
        time.sleep(0.2)
        sealed = client.get(f"/api/captures/{cid}").raise_for_status().json()
    assert sealed["state"] == "SEALED", sealed
    deadline = time.time() + 120
    job = None
    while time.time() < deadline:
        row = client.get(f"/api/captures/{cid}").raise_for_status().json()
        jobs = server.get("/api/jobs").raise_for_status().json()
        match = [j for j in jobs if j["upload_id"] == row.get("upload_id")]
        if match:
            job = match[0]
            if job["state"] not in {"QUEUED", "RUNNING"}:
                break
        time.sleep(0.5)
    assert row["upload_state"] == "ACKED", row
    assert job and job["state"] == ("SYNTHETIC_READY" if real else "DEMO_READY"), job
    if real:
        assert (
            job["quality_gate"]["passed"]
            and job["doctor_report"]["overall_severity"] == "PASS"
        )
    # Also prove discard never creates an upload.
    discard = (
        client.post(
            "/api/captures", json={**body, "idempotency_key": str(uuid.uuid4())}
        )
        .raise_for_status()
        .json()
    )
    client.post(f"/api/captures/{discard['id']}/start").raise_for_status()
    time.sleep(0.1)
    result = (
        client.post(f"/api/captures/{discard['id']}/discard").raise_for_status().json()
    )
    assert result["state"] == "DISCARDED" and result["upload_state"] == "NONE"
    summary = {
        "capture_id": cid,
        "upload_id": row["upload_id"],
        "files": row["file_count"],
        "bytes": row["total_bytes"],
        "job_id": job["id"],
        "job_state": job["state"],
        "aligned_frames": job["alignment_report"]["frames"],
        "discard_test": "passed",
    }
    (root / ".runtime/smoke-result.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
