"""Crash receipts are compared with the actual pinned native Argus reader."""

import json
import subprocess
from pathlib import Path

import pytest

from pipeline.server.argus_billing import receipt_spend


@pytest.mark.parametrize(
    "case",
    [
        "cache-only",
        "cache-dispatched",
        "tie",
        "cache-cumulative",
        "dictionary-zero",
        "dictionary-invalid",
    ],
)
def test_receipt_reader_matches_native_argus(tmp_path, case):
    root = Path(__file__).resolve().parents[1]
    argus = root / ".runtime/vendor/argus"
    python = root / ".runtime/argus-venv/bin/python"
    if not python.exists() or not (argus / "label/dictionary_stage.py").exists():
        pytest.skip("optional pinned Argus toolchain")
    out = tmp_path / "run/out"
    cache = out / ".evidence/episode_000000.json.inspection.json"
    cache.parent.mkdir(parents=True)
    inspection = {"cost_usd": 0.25, "rounds": [], "limitations": [], "digest": "proof"}
    if case.startswith("cache"):
        if case == "cache-dispatched":
            inspection.update(
                cost_usd=0, rounds=[{"state": "dispatched", "reserved_cost_usd": 0.6}]
            )
        if case == "cache-cumulative":
            inspection["cost_usd"] = 0.6
            (out / "episode_000000.json").write_text(
                json.dumps(
                    {
                        "usage": {"est_cost_usd": 0.1},
                        "auxiliary_cost_history": [
                            {"kind": "inspection", "digest": "proof", "cost_usd": 0.2}
                        ],
                    }
                )
            )
        cache.write_text(json.dumps(inspection))
    if case == "tie":
        for name, cost, history in (
            ("episode_000000.json", 0.2, 0.3),
            ("failed_episode_000000.json", 5, 9),
            ("noreply_episode_000000.json", 4, 8),
        ):
            (out / name).write_text(
                json.dumps(
                    {
                        "final_attempt_index": 2,
                        "usage": {"est_cost_usd": cost},
                        "final_cost_history": [{"usage": {"est_cost_usd": history}}],
                    }
                )
            )
    if case.startswith("dictionary"):
        (tmp_path / "dictionary.claim").write_text("claimed")
        (tmp_path / "dictionary_status.json").write_text(
            json.dumps({"projected_upper_usd": 0 if case.endswith("zero") else -1})
        )
    code = """
import json,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from label.dictionary_stage import dictionary_spend, label_spend, label_spend_complete
p=Path(sys.argv[2]); d=dictionary_spend(p); c=label_spend_complete(p) and d['complete']; cost=label_spend(p)+d['reserved_usd']
print(json.dumps({'cost_usd':round(cost,6) if c else None,'reserved_cost_usd':round(cost,6),'cost_complete':c}))
"""
    result = subprocess.run(
        [str(python), "-c", code, str(argus), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert receipt_spend(tmp_path) == json.loads(result.stdout)
