#!/usr/bin/env python3
"""Preflight a BodyPipeline robot deployment and expose its blast radius.

This script never deploys or restarts services. It validates a committed release,
classifies changed paths, checks fleet isolation, and optionally runs local checks.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CLIENT_PREFIXES = (
    "pipeline/client/",
    "configs/client.",
    "deploy/pipeline-client",
)
SHARED_PREFIXES = (
    "pipeline/server/",
    "pipeline/shared/",
    "pipeline/web/",
    "pipeline/cli.py",
    "pipeline/__init__.py",
    "environments/",
    "deploy/Dockerfile",
    "deploy/compose.yaml",
    "deploy/server.",
    "configs/server.",
    "scripts/install_letools.sh",
    "scripts/migrate_server_data.py",
    "pyproject.toml",
    "uv.lock",
    "requirements",
)
NON_RUNTIME_PREFIXES = (
    "docs/",
    "tests/",
    "agents/",
    "patches/",
    ".github/",
)
SERVER_RESOURCE_KEYS = (
    "service",
    "server_port",
    "viewer_port",
    "viewer_ui_port",
    "viewer_data_port",
    "root",
    "data_root",
    "config",
    "environment_file",
    "profile_path",
    "profiles",
)
PORT_KEYS = ("server_port", "viewer_port", "viewer_ui_port", "viewer_data_port")
PATH_KEYS = ("root", "data_root")


class GuardError(Exception):
    pass


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and result.returncode:
        raise GuardError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n{result.stdout}"
        )
    return result


def git(repo: Path, *args: str, check: bool = True) -> str:
    return run(["git", *args], repo, check=check).stdout.strip()


def resolve_commit(repo: Path, ref: str) -> str:
    result = run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], repo, check=False
    )
    if result.returncode:
        raise GuardError(f"Unknown git revision: {ref}")
    return result.stdout.strip()


def changed_files(repo: Path, base: str, head: str) -> list[dict[str, str]]:
    output = git(repo, "diff", "--name-status", f"{base}..{head}")
    rows = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        path = parts[-1]
        rows.append({"status": status, "path": path})
    return rows


def classify(path: str) -> set[str]:
    classes = set()
    if path.startswith(CLIENT_PREFIXES):
        classes.add("client")
    if path.startswith(SHARED_PREFIXES):
        classes.add("shared_server")
    if path.startswith(NON_RUNTIME_PREFIXES):
        classes.add("non_runtime")
    if not classes:
        # Unknown root-level code/config is treated conservatively as shared.
        classes.add("other")
        if path.endswith((".py", ".json", ".toml", ".lock", ".js", ".html", ".css")):
            classes.add("shared_server")
    return classes


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GuardError(f"JSON root must be an object: {path}")
    return value


def load_fleet(path: Path) -> dict:
    data = load_json(path)
    instances = data.get("instances")
    if not isinstance(instances, dict) or not instances:
        raise GuardError("Fleet file must contain a non-empty 'instances' object")
    for name, instance in instances.items():
        if not isinstance(instance, dict):
            raise GuardError(f"Instance {name!r} must be an object")
        if not isinstance(instance.get("host"), str) or not instance["host"].strip():
            raise GuardError(f"Instance {name!r} requires host")
        robots = instance.get("robots")
        if not isinstance(robots, list):
            raise GuardError(f"Instance {name!r} requires robots list")
        ids = []
        for robot in robots:
            if not isinstance(robot, dict) or not robot.get("robot_id"):
                raise GuardError(f"Instance {name!r} has an invalid robot entry")
            ids.append(robot["robot_id"])
        if len(ids) != len(set(ids)):
            raise GuardError(f"Instance {name!r} has duplicate robot_id values")
    return instances


def path_overlap(left: str, right: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str) or not left or not right:
        return False
    a, b = Path(left), Path(right)
    try:
        return a == b or a.is_relative_to(b) or b.is_relative_to(a)
    except (OSError, ValueError):
        return a == b


def profile_paths(instance: dict) -> list[str]:
    paths = []
    legacy = instance.get("profile_path")
    if isinstance(legacy, str) and legacy:
        paths.append(legacy)
    profiles = instance.get("profiles")
    if isinstance(profiles, dict):
        for spec in profiles.values():
            if isinstance(spec, str) and spec:
                paths.append(spec)
            elif isinstance(spec, dict) and isinstance(spec.get("profile_path"), str):
                paths.append(spec["profile_path"])
    return sorted(set(paths))


def validate_instance_isolation(instances: dict, target_name: str) -> list[str]:
    target = instances[target_name]
    problems = []
    for key in ("root", "data_root", "config", "environment_file"):
        if not target.get(key):
            problems.append(f"isolated-server instance requires {key}")
    if not profile_paths(target) and not target.get("profiles"):
        problems.append("isolated-server instance requires profile_path or profiles")
    target_host = target["host"]
    for name, other in instances.items():
        if name == target_name:
            continue
        if other["host"] == target_host:
            if target.get("service") and target.get("service") == other.get("service"):
                problems.append(
                    f"service overlaps with {name}: {target.get('service')}"
                )
            for key in PORT_KEYS:
                if target.get(key) is not None and target.get(key) == other.get(key):
                    problems.append(f"{key} overlaps with {name}: {target.get(key)}")
        for key in ("root", "data_root"):
            if path_overlap(target.get(key, ""), other.get(key, "")):
                problems.append(
                    f"{key} overlaps with {name}: {target.get(key)} <> {other.get(key)}"
                )
        for key in ("config", "environment_file"):
            if target.get(key) and target.get(key) == other.get(key):
                problems.append(f"{key} is shared with {name}: {target.get(key)}")
        shared_profiles = set(profile_paths(target)) & set(profile_paths(other))
        for path in sorted(shared_profiles):
            problems.append(f"Profile path is shared with {name}: {path}")
    return problems


def robot_index(instances: dict) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for name, instance in instances.items():
        for robot in instance["robots"]:
            index.setdefault(robot["robot_id"], []).append(name)
    return index


def validate_robots(instances: dict, target_name: str, targets: list[str]) -> list[str]:
    problems = []
    target = instances[target_name]
    instance_ids = {r["robot_id"] for r in target["robots"]}
    index = robot_index(instances)
    for robot_id in targets:
        if robot_id not in instance_ids:
            problems.append(
                f"target robot {robot_id!r} is not in instance {target_name!r}"
            )
        locations = index.get(robot_id, [])
        if len(locations) > 1:
            problems.append(
                f"target robot {robot_id!r} appears in multiple instances: {locations}"
            )
    return problems


def checks(repo: Path, base: str, head: str, skip: bool) -> list[dict]:
    if skip:
        return [{"name": "all", "status": "SKIPPED", "detail": "--skip-checks"}]
    commands = [
        ("lock", ["uv", "lock", "--check"]),
        (
            "ruff",
            ["uv", "run", "--locked", "ruff", "check", "pipeline", "scripts", "tests"],
        ),
        (
            "format",
            [
                "uv",
                "run",
                "--locked",
                "ruff",
                "format",
                "--check",
                "pipeline",
                "scripts",
                "tests",
            ],
        ),
        ("pytest", ["uv", "run", "--locked", "pytest", "-q"]),
        ("javascript", ["node", "--check", "pipeline/web/app.js"]),
        ("diff", ["git", "diff", "--check", f"{base}..{head}"]),
    ]
    results = []
    for name, cmd in commands:
        if shutil.which(cmd[0]) is None:
            results.append(
                {
                    "name": name,
                    "status": "FAILED",
                    "detail": f"missing executable: {cmd[0]}",
                }
            )
            continue
        result = run(cmd, repo, check=False)
        results.append(
            {
                "name": name,
                "status": "PASSED" if result.returncode == 0 else "FAILED",
                "detail": result.stdout[-4000:],
            }
        )
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Known-good git revision")
    parser.add_argument(
        "--head", default="HEAD", help="Candidate git revision (default: HEAD)"
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("client-only", "isolated-server", "shared-additive"),
    )
    parser.add_argument("--fleet", help="Fleet JSON manifest")
    parser.add_argument("--instance", help="Target Server instance name")
    parser.add_argument(
        "--target-robot-id",
        action="append",
        default=[],
        help="Robot served by this deployment",
    )
    parser.add_argument(
        "--verified-robot-id",
        action="append",
        default=[],
        help="Existing robots verified for shared release",
    )
    parser.add_argument(
        "--acknowledge-shared-impact",
        action="store_true",
        help="Acknowledge shared service restart and fleet-wide blast radius",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Analyze with a dirty worktree; never use for release",
    )
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="Skip local verification; analysis only",
    )
    parser.add_argument("--output", help="Write JSON report to this path")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo = Path.cwd().resolve()
    errors: list[str] = []
    warnings: list[str] = []

    try:
        if git(repo, "rev-parse", "--show-toplevel") != str(repo):
            raise GuardError(f"Run from the repository root: expected {repo}")
        dirty = git(repo, "status", "--porcelain", "--untracked-files=all")
        if dirty and not args.allow_dirty:
            raise GuardError(
                "Worktree is not clean; commit the candidate release or rerun with --allow-dirty for analysis only"
            )
        base_commit = resolve_commit(repo, args.base)
        head_commit = resolve_commit(repo, args.head)
        ancestor = run(
            ["git", "merge-base", "--is-ancestor", base_commit, head_commit],
            repo,
            check=False,
        )
        if ancestor.returncode:
            raise GuardError(
                "--base is not an ancestor of --head; refusing a non-linear deployment range"
            )
        changes = changed_files(repo, base_commit, head_commit)
        for row in changes:
            row["classes"] = sorted(classify(row["path"]))
        shared_changes = [r["path"] for r in changes if "shared_server" in r["classes"]]
        client_changes = [r["path"] for r in changes if "client" in r["classes"]]

        if args.mode == "client-only" and shared_changes:
            errors.append(
                "client-only release contains shared Server/runtime changes: "
                + ", ".join(shared_changes)
            )
        if not args.fleet:
            errors.append(f"{args.mode} requires --fleet")
        if not args.instance:
            errors.append(f"{args.mode} requires --instance")
        if not args.target_robot_id:
            errors.append(f"{args.mode} requires at least one --target-robot-id")

        fleet_summary = {}
        if args.fleet:
            instances = load_fleet(Path(args.fleet).expanduser().resolve())
            if args.instance and args.instance not in instances:
                errors.append(f"Unknown fleet instance: {args.instance}")
            elif args.instance:
                errors.extend(
                    validate_robots(instances, args.instance, args.target_robot_id)
                )
                if args.mode == "isolated-server":
                    errors.extend(validate_instance_isolation(instances, args.instance))
                if args.mode == "shared-additive":
                    if not args.acknowledge_shared_impact:
                        errors.append(
                            "shared-additive requires --acknowledge-shared-impact"
                        )
                    instance_robots = sorted(
                        r["robot_id"] for r in instances[args.instance]["robots"]
                    )
                    missing = sorted(set(instance_robots) - set(args.verified_robot_id))
                    if missing:
                        errors.append(
                            "shared-additive requires verification of all instance robots: "
                            + ", ".join(missing)
                        )
                    if len(instance_robots) > 1:
                        warnings.append(
                            "Shared release restarts one Server serving multiple robots: "
                            + ", ".join(instance_robots)
                        )
                target = instances[args.instance]
                fleet_summary = {
                    "instance": args.instance,
                    "host": target.get("host"),
                    "service": target.get("service"),
                    "robots": [r["robot_id"] for r in target["robots"]],
                    "target_robots": args.target_robot_id,
                    "verified_robots": args.verified_robot_id,
                }
                if args.mode == "isolated-server":
                    fleet_summary["isolation_checked"] = True

        check_results = checks(repo, base_commit, head_commit, args.skip_checks)
        failed_checks = [c for c in check_results if c["status"] == "FAILED"]
        if failed_checks:
            errors.extend(f"check failed: {c['name']}" for c in failed_checks)

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "release": {
                "base": base_commit,
                "head": head_commit,
                "base_ref": args.base,
                "head_ref": args.head,
                "worktree_clean": not bool(dirty),
            },
            "mode": args.mode,
            "impact": {
                "client_paths": client_changes,
                "shared_server_paths": shared_changes,
                "runtime_change_count": len(
                    [
                        r
                        for r in changes
                        if any(c in r["classes"] for c in ("client", "shared_server"))
                    ]
                ),
            },
            "changed_files": changes,
            "fleet": fleet_summary,
            "checks": check_results,
            "warnings": warnings,
            "errors": errors,
            "ok": not errors,
        }
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            output = Path(args.output).expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0 if report["ok"] else 2
    except GuardError as exc:
        print(
            json.dumps(
                {"ok": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
