from __future__ import annotations

from pathlib import Path

from pipeline.shared.core import DomainError

# Kinematics is intentionally excluded: robot joint/trajectory limits belong to
# the controller and safety layer, not the dataset conversion pipeline.
DOCTOR_CHECK_IDS = (
    "metadata",
    "temporal",
    "actions",
    "videos",
    "statistics",
    "episodes",
    "consistency",
    "training",
    "anomalies",
    "portability",
    "per_episode",
)
DOCTOR_REQUIRED_CHECKS = frozenset(
    {
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
    }
)


def robot_profile_blockers(profile, *, expected_dimensions=None):
    """Explain incomplete real-robot policy without approving or changing it."""
    requirements = {
        "semantics_approved": "动作语义尚未核实并审批（如主臂 action 与从臂 state 的对应关系）",
        "clock_verified": "相机、状态与动作的时间戳来源及同步方式尚未确认",
        "joint_names": "缺少按数据维度排列的关节名称",
        "joint_units": "缺少逐维单位（包括夹爪单位）",
    }
    blockers = [
        {"field": field, "message": message}
        for field, message in requirements.items()
        if (
            profile.get(field) is not True
            if field in {"semantics_approved", "clock_verified"}
            else not profile.get(field)
        )
    ]
    if all(profile.get(f) for f in ("joint_names", "joint_units")):
        try:
            validate_robot_policy(profile, expected_dimensions=expected_dimensions)
        except (DomainError, TypeError, ValueError) as exc:
            blockers.append(
                {"field": "robot_policy", "message": "机器人配置无效：" + str(exc)}
            )
    return blockers


def validate_robot_policy(profile, *, expected_dimensions=None):
    """Validate the provider-declared joint schema.

    ``expected_dimensions`` is part of a Server Provider/Profile contract. The
    generic quality module intentionally has no 14-dimensional assumption, so a
    new robot can use a different action/state schema without changing jobs.py.
    """
    if not isinstance(profile, dict):
        raise DomainError("机器人 Profile 必须是对象")
    names, units = profile.get("joint_names"), profile.get("joint_units")
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(x, str) and x for x in names)
        or len(set(names)) != len(names)
    ):
        raise DomainError("机器人 Profile 需要非空且唯一的 joint_names")
    if (
        not isinstance(units, list)
        or len(units) != len(names)
        or not all(x in {"rad", "m", "normalized"} for x in units)
    ):
        raise DomainError(
            "需要与 joint_names 等长的逐维 joint_units（rad / m / normalized）"
        )
    if expected_dimensions is not None:
        if type(expected_dimensions) is not int or expected_dimensions < 1:
            raise DomainError("Provider 的 joint 维度声明无效")
        if len(names) != expected_dimensions:
            raise DomainError(
                f"该 Provider 需要 {expected_dimensions} 维 joint_names，当前为 {len(names)} 维"
            )
    # state_limits/action_limits are optional metadata only. They are not
    # required or enforced here; controller-side safety owns those boundaries.


def robot_quality(
    _aligned: Path,
    profile: dict,
    synthetic=False,
    *,
    expected_dimensions=None,
):
    """Record conversion metadata without applying robot joint-limit gates."""
    if synthetic:
        return {
            "status": "SYNTHETIC",
            "joint_limits_enforced": False,
            "reason": "模拟数据不能证明硬件物理有效性",
        }
    validate_robot_policy(profile, expected_dimensions=expected_dimensions)
    return {
        "status": "PASS",
        "joint_limits_enforced": False,
        "scope": "转换流程不检查关节/动作软限位；限位属于控制器与安全层",
        "units": profile["joint_units"],
    }


def doctor_gate(report, info, returncode):
    if returncode not in (0, 1):
        raise DomainError(f"Doctor 程序退出异常：{returncode}")
    if report.get("total_episodes") != info.get("total_episodes") or report.get(
        "total_frames"
    ) != info.get("total_frames"):
        raise DomainError("Doctor 覆盖数量与数据集元信息不一致")
    checks = report.get("checks")
    present = (
        {c.get("name") for c in checks if isinstance(c, dict)}
        if isinstance(checks, list)
        else set()
    )
    if not DOCTOR_REQUIRED_CHECKS.issubset(present):
        raise DomainError("Doctor 报告缺少预期检查项或 schema 已变化")
    skips = []
    for c in checks:
        skipped = [
            m["message"]
            for m in c.get("messages", [])
            if "skip" in str(m.get("message", "")).lower()
        ]
        if skipped:
            skips.append({"check": c["name"], "reasons": skipped})
            # Cross-episode checks are inapplicable for singleton captures.
            # Older reports may still include the explicitly disabled kinematics check.
            permitted = c["name"] == "Kinematic Feasibility" or (
                c["name"] == "Feature Consistency" and info.get("total_episodes") == 1
            )
            if not permitted:
                raise DomainError(f"必需 Doctor 检查被跳过：{c['name']}")
    severity = report.get("overall_severity")
    if severity not in {"PASS", "WARN", "FAIL"}:
        raise DomainError("Doctor severity 无效")
    if returncode == 1 and severity == "PASS":
        raise DomainError("Doctor 返回码与报告冲突")
    return {
        "passed": returncode == 0 and severity == "PASS",
        "severity": severity,
        "skipped": skips,
        "coverage": {
            "episodes": report["total_episodes"],
            "frames": report["total_frames"],
        },
        "policy": "Doctor 仅生成质量报告；URDF 运动学和机器人关节/动作软限位不参与转换判定",
    }
