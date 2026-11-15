"""Argus-local scope selection and bounded boundary refinement.

Copied verbatim into label/pipeline_runtime.py. It uses Argus's preparer, frame
reader, model transport, pricing, receipts and global cap; no parallel pipeline.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import math
import os
import shutil
from pathlib import Path

UNLIMITED_BUDGET_VERSION = 1


def select_scope(job, eps, report, receipt, overrides, policy, checkpoint):
    raw = os.environ.get("PIPELINE_ARGUS_EPISODES")
    if raw is None:
        return report, receipt
    chosen = json.loads(raw)
    if chosen is not None and (
        not isinstance(chosen, list)
        or not chosen
        or any(type(v) is not int or v < 0 for v in chosen)
    ):
        raise ValueError("explicit Episode selection is invalid")
    from label.atomic import write_atomic

    all_file = job / "pipeline-all-prepared.json"
    full = (
        json.loads(all_file.read_text()) if all_file.exists() else copy.deepcopy(report)
    )
    if not all_file.exists():
        write_atomic(all_file, full)
    inactive = job / "episodes_unselected"
    inactive.mkdir(exist_ok=True)
    indices = {}
    for entry in full["episodes"]:
        name = entry["episode_id"]
        folder = eps / name if (eps / name).is_dir() else inactive / name
        context = json.loads((folder / "context.json").read_text())
        index = context.get("episode_index")
        if type(index) is not int:
            raise ValueError("prepared Episode lacks its recorded source index")
        indices[name] = index
    if chosen is not None and not set(chosen) <= set(indices.values()):
        raise ValueError("selected Episode is missing; never fall back to all")
    keep = {
        name for name, index in indices.items() if chosen is None or index in chosen
    }
    for name in indices:
        if name in keep and (inactive / name).exists():
            shutil.move(inactive / name, eps / name)
        elif name not in keep and (eps / name).exists():
            shutil.move(eps / name, inactive / name)
    result = {
        **full,
        "episodes": [e for e in full["episodes"] if e["episode_id"] in keep],
    }
    write_atomic(job / "report.json", result)
    receipt["policies"] = {
        name: policy.resolve(
            json.loads((eps / name / "context.json").read_text()), **overrides
        )
        for name in sorted(keep)
    }
    receipt["bodypipeline_selection"] = chosen
    checkpoint(job, receipt)
    return result, receipt


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def boundary_groups(task, duration):
    points = {}
    for segment in task["segments"]:
        for edge in ("start_s", "end_s"):
            t = segment[edge]
            if t <= 0 or t >= duration:
                continue
            points.setdefault(round(t, 9), []).append([segment["id"], edge])
    return [
        {
            "id": "boundary-" + _fingerprint([point, members])[:20],
            "coarse_s": point,
            "members": members,
        }
        for point, members in sorted(points.items())
    ]


def dense_frames(groups, fps, count, window, target):
    stride = max(1, math.floor(fps * target / 2))
    frames = set()
    for g in groups:
        center = math.ceil(g["coarse_s"] * fps - 1e-6)
        lo = max(0, math.floor((g["coarse_s"] - window) * fps))
        hi = min(count - 1, math.ceil((g["coarse_s"] + window) * fps))
        frames.update(range(lo, hi + 1, stride))
        frames.update(
            (max(0, min(count - 1, center - 1)), max(0, min(count - 1, center)))
        )
    return sorted(frames)


def refine_prompt(spec, groups, samples):
    return (
        "BODYPIPELINE BOUNDARY REFINEMENT ONLY\n"
        + json.dumps(
            {
                "template_id": spec["template"]["id"],
                "template_signature": spec["signature"],
                "template": spec["template"],
                "target_error_s": spec.get("boundary_target_s", 0.2),
                "candidate_boundaries": groups,
                "sampled_data_frames": samples,
            },
            ensure_ascii=False,
        )
        + """
Compare the decoded neighboring frames with the frozen template's observable start/end criteria.
Return ONLY {"template_signature":"supplied signature","boundaries":[{"id":"exact supplied boundary ID","time_s":0.0,"supported":true,"evidence":"short observable basis"}]}.
Each boundary is one atomic change for all supplied member edges. Use request-local seconds and stay inside the supplied evidence window. If evidence is insufficient set supported=false and keep the coarse time. Do not regenerate a second timeline, alter labels/attempt identities/results, fill gaps, or invent precision. Model confidence does not replace human review.
"""
    )


def apply_boundaries(task, reply, groups, spec, context, window):
    from label.pipeline_template import validate_task_reply

    if (
        not isinstance(reply, dict)
        or reply.get("template_signature") != spec["signature"]
    ):
        raise ValueError("refinement template signature mismatch")
    rows = reply.get("boundaries")
    expected = {g["id"]: g for g in groups}
    if (
        not isinstance(rows, list)
        or len(rows) != len(groups)
        or {r.get("id") for r in rows} != set(expected)
    ):
        raise ValueError("refinement must cover exactly the requested boundary IDs")
    result = copy.deepcopy(task)
    evidence = []
    for row in rows:
        g = expected[row["id"]]
        time = row.get("time_s")
        if (
            type(time) not in (int, float)
            or not math.isfinite(time)
            or abs(time - g["coarse_s"]) > window + 1e-6
            or type(row.get("supported")) is not bool
            or not isinstance(row.get("evidence"), str)
        ):
            raise ValueError("invalid refinement boundary/evidence")
        if row["supported"]:
            if not row["evidence"].strip():
                raise ValueError("supported boundary needs observable evidence")
            for sid, edge in g["members"]:
                next(s for s in result["segments"] if s["id"] == sid)[edge] = time
        evidence.append(
            {**copy.deepcopy(row), "coarse_s": g["coarse_s"], "members": g["members"]}
        )
    validate_task_reply(result, spec, context)
    return result, evidence


def _evidence_content(me, ep, indices, folder):
    from PIL import Image, ImageDraw

    parts = []
    receipts = []
    total_bytes = 0
    for view in me.views(ep):
        images = me._decode_view(
            ep, view, indices, widths=[224], detail_ks=set(indices)
        )
        if any(k not in images for k in indices):
            raise ValueError("boundary frames could not all be decoded")
        for block in range(0, len(indices), 8):
            chosen = indices[block : block + 8]
            height = round(images[chosen[0]].height * 224 / images[chosen[0]].width)
            grid = Image.new(
                "RGB", (224 * 4, (height + 24) * math.ceil(len(chosen) / 4)), "#142035"
            )
            draw = ImageDraw.Draw(grid)
            samples = []
            for i, k in enumerate(chosen):
                x = (i % 4) * 224
                y = (i // 4) * (height + 24)
                image = images[k].resize((224, height))
                grid.paste(image, (x, y + 24))
                time = me.frame_time(ep, k)
                draw.text((x + 3, y + 4), f"frame {k} / {time:.4f}s", fill="white")
                samples.append({"frame": k, "time_s": time, "camera": view})
            buf = io.BytesIO()
            grid.save(buf, format="JPEG", quality=88)
            raw = buf.getvalue()
            name = (
                _fingerprint([view, chosen, hashlib.sha256(raw).hexdigest()]) + ".jpg"
            )
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(raw)
            parts.extend(
                [
                    {
                        "type": "text",
                        "text": f"Decoded boundary evidence: {view}; {json.dumps(samples)}",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,"
                            + base64.b64encode(raw).decode(),
                            "detail": "high",
                        },
                    },
                ]
            )
            receipts.append(
                {
                    "path": str(folder / name),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "samples": samples,
                }
            )
            total_bytes += len(raw)
    return parts, receipts, total_bytes


def refine_record(
    record,
    out_path,
    ep_dir,
    spec,
    *,
    call,
    bound,
    price,
    remaining,
    verify=lambda: None,
):
    """Each fine request gets <= 4 total sends and a durable pre-dispatch reserve.

    The global remaining() reads Argus's receipts, including conservative unpaid
    or unverified claims written here, before EVERY send. Successful coarse/fine
    results are retained on budget or retry exhaustion and on explicit resume.
    """
    from label import episode as me
    from label.atomic import write_atomic
    from label.pipeline_template import validate_task_reply

    task = (record.get("labels") or {}).get("bodypipeline_task")
    if (
        not isinstance(task, dict)
        or task.get("version") != 2
        or record.get("parse_ok") is not True
    ):
        return record
    context = json.loads((Path(ep_dir) / "context.json").read_text())
    fps = context["fps"]
    count = context["n_state_frames"]
    target = float(spec.get("boundary_target_s", 0.2))
    window = float(spec.get("boundary_window_s", max(1.0, target * 4)))
    metadata = record.setdefault(
        "pipeline_boundary",
        {
            "version": 1,
            "target_s": target,
            "window_s": window,
            "requests": {},
            "pending": [],
        },
    )
    metadata["pending"] = []
    # Keep the original coarse layout as the identity of refinement requests.
    coarse = metadata.setdefault("coarse_task", copy.deepcopy(task))
    groups = boundary_groups(coarse, count / fps)
    for begin in range(0, len(groups), 4):
        batch = groups[begin : begin + 4]
        indices = dense_frames(batch, fps, count, window, target)
        key = _fingerprint(
            [spec["signature"], target, window, batch, record.get("input_identity")]
        )
        entry = metadata["requests"].setdefault(
            key, {"groups": batch, "frames": indices, "attempts": [], "complete": False}
        )
        if entry["complete"]:
            continue
        try:
            ep = me.load(Path(ep_dir))
            parts, images, image_bytes = _evidence_content(
                me, ep, indices, Path(out_path).parent / ".boundary-evidence" / key
            )
            samples = [{"frame": k, "time_s": me.frame_time(ep, k)} for k in indices]
            prompt = refine_prompt(spec, batch, samples)
            content = [{"type": "text", "text": prompt}, *parts]
            entry.update(
                prompt_text=prompt,
                images=images,
                samples=samples,
                model=record.get("model"),
                input_identity=record.get("input_identity"),
            )
            reserve = bound(content)
            while len(entry["attempts"]) < 4:
                # A crashed CLI call with an unknown billing outcome is never
                # resent by the boundary loop merely because the server resumed.
                if entry.get("pause_reason") == "codex_unknown" or (
                    entry.get("pause_reason") == "codex_configuration"
                    and entry.get("resume_index", 1)
                    >= int(os.environ.get("PIPELINE_CODEX_RESUME_INDEX", "1"))
                ):
                    break
                verify()
                if reserve > remaining() + 1e-10:
                    entry["pause_reason"] = "budget"
                    break
                invoice = {
                    "kind": "bodypipeline_boundary",
                    "request_id": key,
                    "cost_usd": reserve,
                    "reserved_usd": reserve,
                    "cost_is_conservative_estimate": True,
                    "dispatch_outcome": "claimed",
                }
                record.setdefault("auxiliary_cost_history", []).append(invoice)
                attempt = {
                    "index": len(entry["attempts"]) + 1,
                    "reserved_usd": reserve,
                    "status": "claimed",
                }
                entry["attempts"].append(attempt)
                write_atomic(
                    out_path, record
                )  # a crash cannot erase an unsettled reserve
                try:
                    response = None
                    response = call(content)
                    cost, cost_source = price(response.get("usage") or {})
                    invoice.update(
                        cost_usd=reserve if cost is None else cost,
                        dispatch_outcome="unverified" if cost is None else "received",
                        cost_is_conservative_estimate=cost is None,
                        usage=response.get("usage"),
                        cost_source=cost_source,
                    )
                    attempt.update(
                        response=response,
                        status="received",
                        cost_usd=invoice["cost_usd"],
                    )
                    choice = (response.get("choices") or [{}])[0]
                    text = (choice.get("message") or {}).get("content") or ""
                    if choice.get("finish_reason") == "length":
                        raise ValueError("refinement response truncated")
                    reply = json.loads(text)
                    updated, evidence = apply_boundaries(
                        task, reply, batch, spec, context, window
                    )
                    if os.environ.get("PIPELINE_ANNOTATION_BACKEND") == "codex":
                        from label.pipeline_codex import acknowledge

                        acknowledge(response, True)
                    task = updated
                    record["labels"]["bodypipeline_task"] = task
                    entry.update(complete=True, evidence=evidence, pause_reason=None)
                    attempt["status"] = "valid"
                    write_atomic(out_path, record)
                    break
                except Exception as error:
                    if os.environ.get("PIPELINE_ANNOTATION_BACKEND") == "codex":
                        from label.pipeline_codex import CLIError, acknowledge

                        if response is not None:
                            acknowledge(response, False)
                        if isinstance(error, CLIError) and not error.retryable:
                            entry["pause_reason"] = (
                                "codex_configuration"
                                if error.kind
                                in {
                                    "authentication",
                                    "quota",
                                    "unavailable",
                                    "configuration",
                                }
                                else "codex_unknown"
                            )
                            entry["resume_index"] = int(
                                os.environ.get("PIPELINE_CODEX_RESUME_INDEX", "1")
                            )
                            attempt.update(status="failed", error=str(error))
                            write_atomic(out_path, record)
                            break
                    # Verified unpaid rejections release their reserve; uncertain
                    # outcomes retain it even when the request will be retried.
                    if getattr(error, "verified_unpaid", False):
                        invoice.update(
                            cost_usd=0.0,
                            dispatch_outcome="not dispatched",
                            cost_is_conservative_estimate=False,
                        )
                    attempt.update(
                        status="failed", error=f"{type(error).__name__}: {error}"[:1000]
                    )
                    permanent = getattr(error, "http_status", None) in (
                        400,
                        401,
                        402,
                        403,
                        404,
                        405,
                        422,
                    ) or isinstance(error, (KeyError, TypeError))
                    write_atomic(out_path, record)
                    if permanent:
                        entry["pause_reason"] = "configuration"
                        break
            if not entry["complete"]:
                metadata["pending"].extend(g["id"] for g in batch)
                entry.setdefault("pause_reason", "retry_limit")
        except Exception as error:
            entry["evidence_error"] = f"{type(error).__name__}: {error}"[:1000]
            metadata["pending"].extend(g["id"] for g in batch)
        write_atomic(out_path, record)
    validate_task_reply(task, spec, context)
    return record


def refine_outputs(job, args):
    from label import harness as h
    from label.atomic import write_atomic
    from label.dictionary_stage import remaining_label_budget
    from label.pipeline_template import template_spec

    spec = template_spec()
    if spec is None:
        return
    keys = h.get_keys()
    if not keys:
        return
    print("== boundary (template coarse-to-fine)", flush=True)
    out = job / "run/out"
    for ep in sorted((job / "units").glob("episode_*")):
        path = out / f"{ep.name}.json"
        if not path.is_file():
            continue
        record = json.loads(path.read_text())
        model = record.get("model") or args.model
        reasoning = record.get("reasoning_effort") or args.reasoning
        tokens = args.max_tokens or 12000
        quote = (record.get("config") or {}).get("pricing_quote")
        pricing = {"pricing_quote": quote} if quote is not None else {}

        def call(parts):
            return h.call_model_once(
                parts,
                model,
                reasoning,
                keys[0],
                tokens,
                args.timeout or 6060,
                **pricing,
            )

        def bound(parts):
            return h.final_cost_bound(
                parts,
                tokens,
                model=model,
                require_known_pricing=args.cap is not None,
                **pricing,
            )

        def verify():
            problem = h.saved_input_problem(record, ep)
            if problem:
                raise ValueError(problem)

        refine_record(
            record,
            path,
            ep,
            spec,
            call=call,
            bound=bound,
            price=lambda usage: h._verified_final_cost(usage, model),
            remaining=lambda: remaining_label_budget(job, args.cap),
            verify=verify,
        )
        write_atomic(path, record)
