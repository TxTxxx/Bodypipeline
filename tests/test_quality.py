import pytest

from pipeline.server.quality import (
    DOCTOR_CHECK_IDS,
    doctor_gate,
    robot_profile_blockers,
    robot_quality,
    validate_robot_policy,
)
from pipeline.shared.core import DomainError

NAMES = [
    "Metadata & Format Compliance",
    "Temporal Consistency",
    "Action Quality",
    "Video Integrity",
    "Data Distribution",
    "Episode Health",
    "Feature Consistency",
    "Training Readiness",
    "Anomaly Detection",
    "Portability",
    "Per-Episode Summary",
]


def report():
    return {
        "overall_severity": "PASS",
        "total_episodes": 1,
        "total_frames": 100,
        "checks": [{"name": x, "severity": "PASS", "messages": []} for x in NAMES],
    }


def test_doctor_coverage_and_skips():
    assert "kinematics" not in DOCTOR_CHECK_IDS
    info = {"total_episodes": 1, "total_frames": 100}
    assert doctor_gate(report(), info, 0)["passed"]
    wrong = report()
    wrong["total_frames"] = 20
    with pytest.raises(DomainError):
        doctor_gate(wrong, info, 0)
    wrong = report()
    wrong["checks"][0]["messages"] = [{"message": "Skipped: failed to load"}]
    with pytest.raises(DomainError):
        doctor_gate(wrong, info, 0)
    optional = report()
    optional["checks"].append(
        {
            "name": "Kinematic Feasibility",
            "severity": "PASS",
            "messages": [{"message": "Skipped: no URDF"}],
        }
    )
    gate = doctor_gate(optional, info, 0)
    assert gate["passed"] and gate["skipped"]
    warn = report()
    warn["overall_severity"] = "WARN"
    assert not doctor_gate(warn, info, 1)["passed"]


def test_robot_profile_schema_does_not_require_or_enforce_joint_limits(tmp_path):
    with pytest.raises(DomainError):
        validate_robot_policy({})
    profile = {
        "id": "robot-profile",
        "joint_names": [str(i) for i in range(14)],
        "joint_units": ["rad"] * 14,
    }
    validate_robot_policy(profile)
    assert (
        robot_profile_blockers(
            {
                **profile,
                "semantics_approved": True,
                "clock_verified": True,
            }
        )
        == []
    )

    # Limits are optional metadata; malformed or out-of-range values cannot gate conversion.
    profile["state_limits"] = "invalid"
    profile["action_limits"] = [[1, -1]] * 14
    validate_robot_policy(profile)
    result = robot_quality(tmp_path, profile)
    assert result["status"] == "PASS"
    assert result["joint_limits_enforced"] is False

    profile["joint_units"][-1] = "guess"
    with pytest.raises(DomainError):
        validate_robot_policy(profile)


def test_tool_environment_strips_all_device_credentials(settings_factory, monkeypatch):
    monkeypatch.setenv("ROBOT_SECRET", "secret-value")
    monkeypatch.setenv("PIPELINE_TOKEN", "admin-value")
    monkeypatch.setenv("HF_TOKEN", "hf-value")
    settings = settings_factory(
        "server",
        devices=[
            {"robot_id": "one", "project_id": "project", "token_env": "ROBOT_SECRET"}
        ],
    )
    env = settings.tool_environment()
    assert (
        "ROBOT_SECRET" not in env
        and "PIPELINE_TOKEN" not in env
        and "HF_TOKEN" not in env
    )
