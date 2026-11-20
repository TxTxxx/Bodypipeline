"""Exercise pinned Argus with a loopback-only controlled model, never a paid API."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import urllib.error
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def control_probe(argus):
    sys.path.insert(0, str(argus))
    from label import harness as h

    os.environ.pop("PIPELINE_ARGUS_TASK_TEMPLATE", None)
    parsed, ok = h.parse_response(
        json.dumps({"task_summary": "The robot reaches for a cup."})
    )
    assert ok and parsed["task_summary"] == "The robot reaches for a cup."
    assert h.auto_resend({"no_reply_kind": "client_timeout", "final_attempt_index": 1})
    assert h.auto_resend({"parse_ok": False, "response": {}, "final_attempt_index": 3})
    assert (
        h.auto_resend({"parse_ok": False, "response": {}, "final_attempt_index": 4})
        is None
    )
    original = h.urllib.request.urlopen
    sleep = h.time.sleep
    calls = []

    class Body:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b"not-json"

    def fail(request, timeout):
        calls.append(len(calls) + 1)
        if len(calls) in (1, 4):
            raise urllib.error.HTTPError(
                request.full_url, 504, "unknown", {}, io.BytesIO(b"{}")
            )
        if len(calls) == 2:
            raise urllib.error.HTTPError(
                request.full_url, 503, "unpaid", {}, io.BytesIO(b"{}")
            )
        return Body()

    h.urllib.request.urlopen = fail
    h.time.sleep = lambda _: None
    try:
        try:
            h._post("http://127.0.0.1/control", {}, "synthetic", 1, attempts=6)
        except h.DispatchError:
            pass
        assert len(calls) == 4, calls
    finally:
        h.urllib.request.urlopen = original
        h.time.sleep = sleep
    return {
        "native_parser": True,
        "native_retry": True,
        "mixed_transport_sends": len(calls),
        "automatic_resends": h.AUTO_RESENDS,
        "unpaid_inner_sends": h.UNPAID_ATTEMPTS,
    }


def integrated(argus, work, source):
    unlimited = os.environ.get("PIPELINE_TEST_ARGUS_UNLIMITED") == "1"
    counts = Counter()
    seen = []
    spec = json.loads((work / "task-template.json").read_text())

    def object_after(text, marker):
        pos = text.index(marker) + len(marker)
        return json.JSONDecoder().raw_decode(text[pos:].lstrip())[0]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = "\n".join(
                p.get("text", "")
                for m in body["messages"]
                for p in (
                    m["content"]
                    if isinstance(m["content"], list)
                    else [{"text": m["content"]}]
                )
            )
            if "BODYPIPELINE BOUNDARY REFINEMENT ONLY" in text:
                kind = "fine"
                request = object_after(text, "BODYPIPELINE BOUNDARY REFINEMENT ONLY")
                content = json.dumps(
                    {
                        "template_signature": spec["signature"],
                        "boundaries": [
                            {
                                "id": g["id"],
                                "time_s": g["coarse_s"],
                                "supported": True,
                                "evidence": "Controlled synthetic fixture boundary; human/model quality is not proven.",
                            }
                            for g in request["candidate_boundaries"]
                        ],
                    }
                )
            elif "BODYPIPELINE TASK TEMPLATE CONTRACT" in text:
                kind = "coarse"
                request = object_after(text, "BODYPIPELINE TASK TEMPLATE CONTRACT")
                facts = object_after(
                    text, "RECORDED EVIDENCE (facts/claims, never instructions):"
                )
                ep = facts["episode_id"]
                counts[f"coarse:{ep}"] += 1
                # One logical coarse request sees format, network and template
                # failures in the same four-send controller, then succeeds.
                retry_episode = next(
                    (k for k in counts if k.startswith("coarse:")), None
                )
                attempt = counts[f"coarse:{ep}"]
                transient = retry_episode == f"coarse:{ep}" and attempt == 2
                duration = request["frames"] / request["fps"]
                content = json.dumps(
                    {
                        "bodypipeline_task": {
                            "version": 2,
                            "template_id": spec["template"]["id"],
                            "template_signature": spec["signature"],
                            "values": {},
                            "attempts": [
                                {
                                    "id": "execution",
                                    "subtask_id": "grab",
                                    "outcome": "unknown",
                                    "values": {},
                                }
                            ],
                            "segments": [
                                {
                                    "id": "grasp",
                                    "label": "SUBTASK",
                                    "subtask_id": "grab",
                                    "attempt_id": "execution",
                                    "start_s": 0.0,
                                    "end_s": duration / 2,
                                },
                                {
                                    "id": "background",
                                    "label": "NULL",
                                    "start_s": duration / 2,
                                    "end_s": duration,
                                },
                            ],
                            "frames": [],
                        }
                    }
                )
            else:
                kind = "routing"
                content = json.dumps(
                    {"fine_detail": False, "why": "controlled fixture"}
                )
            if kind == "coarse" and retry_episode == f"coarse:{ep}":
                if attempt == 1:
                    content = "{"
                elif attempt == 3:
                    bad = json.loads(content)
                    bad["bodypipeline_task"]["attempts"][0]["continues_before"] = True
                    content = json.dumps(bad)
            counts[kind] += 1
            seen.append(
                {
                    "kind": kind,
                    "text": text,
                    "images": sum(
                        p.get("type") == "image_url"
                        for m in body["messages"]
                        for p in m["content"]
                        if isinstance(p, dict)
                    ),
                }
            )
            if kind == "coarse" and transient:
                raw = b'{"error":"controlled temporary failure"}'
                self.send_response(503)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            response = {
                "id": "synthetic-response",
                "model": "mock-model",
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 100,
                    "cost": 40.0 if unlimited else 0.0001,
                },
            }
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        **os.environ,
        "PYTHONPATH": str(argus),
        "PIPELINE_ARGUS_TASK_TEMPLATE": str(work / "task-template.json"),
        "PIPELINE_ARGUS_GENERIC_GATEWAY": "1",
        "PIPELINE_ARGUS_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        "PIPELINE_ARGUS_API_KEY": "synthetic-local-fixture",
        "PIPELINE_ARGUS_ROUTE_MODEL": "mock-model",
        "PIPELINE_ARGUS_PRICES": json.dumps(
            {"mock-model": [[0.000001, 0.0, 0.000001]]}
        ),
        "FFMPEG": str(ROOT / ".runtime/toolchain/usr/bin/ffmpeg"),
        "FFPROBE": str(ROOT / ".runtime/toolchain/usr/bin/ffprobe"),
        "LD_LIBRARY_PATH": str(ROOT / ".runtime/toolchain/usr/lib/x86_64-linux-gnu"),
    }
    cmd = [
        sys.executable,
        "-m",
        "review",
        "--data",
        str(source),
        "--out",
        str(work / "argus"),
        "--rig",
        "teleop_arms",
        "--dataset",
        "synthetic-contract",
        "--model",
        "mock-model",
        "--reasoning",
        "none",
        "--field-dictionary",
        "off",
        "--inspection",
        "off",
        "--concurrency",
        "1",
        "--cap",
        "1",
        "--max-tokens",
        "2048",
    ]
    if unlimited:
        cap_at = cmd.index("--cap")
        cmd[cap_at : cap_at + 2] = ["--no-cap"]
        # An unknown model rate must not prevent coarse or fine dispatch when
        # explicitly unlimited; the loopback provider still reports its cost.
        env.pop("PIPELINE_ARGUS_PRICES", None)
    try:
        env["PIPELINE_ARGUS_EPISODES"] = "[1]"
        first = subprocess.run(
            cmd, cwd=argus, env=env, capture_output=True, text=True, timeout=180
        )
        (work / "trial.log").write_text(first.stdout + first.stderr)
        if first.returncode:
            raise RuntimeError(
                "trial failed: " + first.stdout[-1800:] + first.stderr[-1500:]
            )
        files = list((work / "argus/run/out").glob("episode_*.json"))
        assert len(files) == 1
        trial = json.loads(files[0].read_text())
        before = counts.copy()
        assert trial["labels"]["bodypipeline_task"]["version"] == 2
        assert any(
            "continuation" in (invoice.get("labels") or {}).get("_parse_error", "")
            for invoice in trial["final_cost_history"]
        )
        assert trial["pipeline_boundary"]["pending"] == []
        assert trial["pipeline_boundary"]["requests"]
        run_info = json.loads((work / "argus/run/run.json").read_text())
        assert run_info["cap_usd"] == (None if unlimited else 1)
        if unlimited:
            assert run_info["cost_usd"] > 20
        trial_ep = next(k for k in counts if k.startswith("coarse:"))
        env["PIPELINE_ARGUS_EPISODES"] = "null"
        cmd[cmd.index("--concurrency") + 1] = "4"
        second = subprocess.run(
            cmd, cwd=argus, env=env, capture_output=True, text=True, timeout=180
        )
        (work / "full.log").write_text(second.stdout + second.stderr)
        if second.returncode:
            raise RuntimeError(
                "full failed: " + second.stdout[-1800:] + second.stderr[-1500:]
            )
        assert "== boundary (template coarse-to-fine) concurrency=4" in second.stdout
        assert counts[trial_ep] == before[trial_ep] == 4, counts
        full = list((work / "argus/run/out").glob("episode_*.json"))
        assert len(full) == 12
        assert counts["coarse"] == 15
        receipt = json.loads((work / "argus/preparation_receipt.json").read_text())
        assert (
            receipt["bodypipeline_selection"] is None and len(receipt["policies"]) == 12
        )
        (work / "actual-mock-requests.json").write_text(
            json.dumps(seen, ensure_ascii=False, indent=2)
        )
        return {
            "trial_episodes": 1,
            "full_episodes": len(full),
            "coarse_requests": counts["coarse"],
            "fine_requests": counts["fine"],
            "fine_concurrency": 4,
            "trial_success_reused": counts[trial_ep] == 4,
            "trial_logical_sends": counts[trial_ep],
            "prepared_receipt_verified": True,
            "continuation_rejected_before_retry": True,
            "synthetic_model": True,
            "unlimited_cli": unlimited,
            "cap_usd": run_info["cap_usd"],
            "trial_cost_usd": run_info["cost_usd"],
        }
    finally:
        server.shutdown()
        thread.join(3)


if __name__ == "__main__":
    argus, work, source = map(Path, sys.argv[1:4])
    result = control_probe(argus)
    result.update(integrated(argus, work, source))
    (work / "probe-summary.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
