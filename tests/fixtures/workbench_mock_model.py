"""Loopback fixture for manual/browser UI acceptance; never a real model."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def serve(port: int, log: Path):
    def object_after(text, marker):
        return json.JSONDecoder().raw_decode(text.split(marker, 1)[1].lstrip())[0]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, payload):
            raw = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.respond(
                {
                    "object": "list",
                    "data": [{"id": "synthetic-fixture", "object": "model"}],
                }
            )

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
                kind = "template-boundary"
                req = object_after(text, "BODYPIPELINE BOUNDARY REFINEMENT ONLY")
                content = {
                    "template_signature": req["template_signature"],
                    "boundaries": [
                        {
                            "id": b["id"],
                            "time_s": b["coarse_s"],
                            "supported": True,
                            "evidence": "Synthetic fixture only; this is not real model quality evidence.",
                        }
                        for b in req["candidate_boundaries"]
                    ],
                }
            elif "BODYPIPELINE TASK TEMPLATE CONTRACT" in text:
                kind = "template"
                req = object_after(text, "BODYPIPELINE TASK TEMPLATE CONTRACT")
                sid = req["template"]["subtasks"][0]["id"]
                duration = req["frames"] / req["fps"]
                content = {
                    "bodypipeline_task": {
                        "version": 2,
                        "template_id": req["template_id"],
                        "template_signature": req["template_signature"],
                        "values": {},
                        "attempts": [
                            {
                                "id": "execution",
                                "subtask_id": sid,
                                "outcome": "unknown",
                                "values": {},
                            }
                        ],
                        "segments": [
                            {
                                "id": "first",
                                "label": "SUBTASK",
                                "subtask_id": sid,
                                "attempt_id": "execution",
                                "start_s": 0,
                                "end_s": duration / 2,
                            },
                            {
                                "id": "rest",
                                "label": "NULL",
                                "start_s": duration / 2,
                                "end_s": duration,
                            },
                        ],
                        "frames": [],
                    }
                }
            elif '"fine_detail"' in text and '"task_summary"' not in text:
                kind = "routing"
                content = {"fine_detail": False, "why": "Controlled CPU fixture"}
            else:
                kind = "free-native"
                content = {
                    "task_summary": "Synthetic numbered video: move the cup.",
                    "completion": {
                        "task_completed": "success",
                        "success_predicate": "Synthetic cup moved",
                    },
                    "timeline": [
                        {
                            "start_s": 0,
                            "end_s": 2,
                            "arm": "both",
                            "action": "grasp",
                            "object": "synthetic cup",
                            "contribution": "advancing",
                            "progress": 0.5,
                        },
                        {
                            "start_s": 2,
                            "end_s": 4,
                            "arm": "both",
                            "action": "move",
                            "object": "synthetic cup",
                            "contribution": "advancing",
                            "progress": 1,
                        },
                    ],
                    "tasks": [
                        {
                            "start_s": 0,
                            "end_s": 4,
                            "task": "Move synthetic cup",
                            "objects": ["synthetic cup"],
                            "judged_against": "instruction",
                            "subtask": None,
                            "success_predicate": "Synthetic cup moved",
                            "outcome": "success",
                            "completed_at_s": 4,
                            "note": "Controlled fixture",
                        }
                    ],
                    "fixture_note": "Controlled native Argus output; no real model inference.",
                }
            with log.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "kind": kind,
                            "at": time.time(),
                            "actual_prompt": text,
                            "model": body.get("model"),
                            "image_count": sum(
                                p.get("type") == "image_url"
                                for m in body["messages"]
                                if isinstance(m["content"], list)
                                for p in m["content"]
                            ),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            self.respond(
                {
                    "id": "synthetic-fixture",
                    "model": "synthetic-fixture",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(content)},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 50,
                        "completion_tokens": 100,
                        "cost": 0.0001,
                    },
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
