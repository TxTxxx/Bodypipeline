"""Controlled CLI for fault/batch tests. Never a real model or user dataset.

Copy beside control.json as an executable in a test-owned directory. This
fixture does not claim to prove vision quality or real Codex availability.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path


def append(path, value):
    with path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(value) + "\n")
        f.flush()


def main():
    home = Path(sys.argv[0]).resolve().parent
    control = json.loads((home / "control.json").read_text())
    if "--version" in sys.argv:
        print("codex-cli 0.167.2")
        return
    if "login" in sys.argv:
        print("Logged in using test fixture")
        return
    prompt = sys.stdin.read()
    match = re.search(r'"episode_id"\s*:\s*"([^"]+)"', prompt)
    episode = match[1] if match else "transport-test"
    mode = control.get("episodes", {}).get(episode, control.get("mode", "valid"))
    append(
        home / "invocations.jsonl",
        {
            "pid": os.getpid(),
            "episode": episode,
            "phase": "start",
            "at": time.time(),
            "mode": mode,
            "argv": sys.argv[1:],
            "env_names": sorted(os.environ),
        },
    )
    print(
        json.dumps({"type": "thread.started", "thread_id": f"fixture-{os.getpid()}"}),
        flush=True,
    )
    time.sleep(control.get("delay", 0))
    if mode in {"authentication", "quota", "rate_limit", "unknown", "configuration"}:
        messages = {
            "authentication": "401 Authentication required",
            "quota": "insufficient quota",
            "rate_limit": "429 rate limit",
            "unknown": "connection reset after dispatch",
            "configuration": "unsupported configuration",
        }
        print(
            json.dumps({"type": "turn.failed", "error": {"message": messages[mode]}}),
            flush=True,
        )
        raise SystemExit(1)
    if mode == "tool":
        print(
            json.dumps(
                {"type": "item.completed", "item": {"type": "command_execution"}}
            ),
            flush=True,
        )
    if "BODYPIPELINE BOUNDARY REFINEMENT ONLY" in prompt:
        request = json.JSONDecoder().raw_decode(
            prompt.split("BODYPIPELINE BOUNDARY REFINEMENT ONLY", 1)[1].lstrip()
        )[0]
        answer = {
            "template_signature": request["template_signature"],
            "boundaries": [
                {
                    "id": g["id"],
                    "time_s": g["coarse_s"],
                    "supported": False,
                    "evidence": "Controlled CLI fixture; no vision quality claim.",
                }
                for g in request["candidate_boundaries"]
            ],
        }
    elif "BODYPIPELINE TASK TEMPLATE CONTRACT" in prompt:
        request = json.JSONDecoder().raw_decode(
            prompt.split("BODYPIPELINE TASK TEMPLATE CONTRACT", 1)[1].lstrip()
        )[0]
        answer = {
            "bodypipeline_task": {
                "version": 2,
                "template_id": request["template_id"],
                "template_signature": request["template_signature"],
                "values": {},
                "attempts": [
                    {
                        "id": "fixture-attempt",
                        "subtask_id": request["template"]["subtasks"][0]["id"],
                        "outcome": "unknown",
                        "values": {},
                        "continues_before": False,
                        "continues_after": False,
                    }
                ],
                "segments": [
                    {
                        "id": "fixture-segment",
                        "label": "SUBTASK",
                        "subtask_id": request["template"]["subtasks"][0]["id"],
                        "attempt_id": "fixture-attempt",
                        "start_s": 0.0,
                        "end_s": request["frames"] / request["fps"],
                        "note": "Controlled CLI test fixture; no recognition claim.",
                        "description": "",
                        "values": {},
                    }
                ],
                "frames": [],
            }
        }
    else:
        answer = {"observation": "Controlled CLI test fixture"}
    reply = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
    reply.write_text(
        "not-json"
        if mode == "invalid"
        else json.dumps({"result_json": json.dumps(answer)})
    )
    print(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 30,
                },
            }
        ),
        flush=True,
    )
    append(
        home / "invocations.jsonl",
        {"pid": os.getpid(), "episode": episode, "phase": "finish", "at": time.time()},
    )


if __name__ == "__main__":
    main()
