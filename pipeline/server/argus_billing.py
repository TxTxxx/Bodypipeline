"""Read pinned Argus's cumulative billing rules without importing credentials.

Parity tests execute the pinned native dictionary_stage reader over the same
receipts. Reservations include inspection-only caches and unknown dispatches.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

UNKNOWN_RESERVE = 1e300


def read(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("invalid receipt")
    return value


def amount(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise ValueError("unverified cost")
    return float(value)


def final(invoice):
    if (
        invoice.get("dry_run")
        or invoice.get("final_dispatch_outcome") == "not dispatched"
    ):
        return 0.0, True
    pending = invoice.get("final_dispatch_outcome") in {"claimed", "unverified"}
    return amount(
        invoice.get("final_reserved_usd")
        if pending
        else (invoice.get("usage") or {}).get("est_cost_usd")
    ), not pending


def auxiliary(invoice, routing=False):
    cost = amount(invoice.get("cost_usd", 0))
    previous = (
        cost
        if routing and invoice.get("previously_billed") is True
        else amount(invoice.get("previously_billed_usd", 0))
    )
    if previous > cost:
        raise ValueError("invalid cumulative cost")
    return cost - previous, not invoice.get("cost_is_conservative_estimate", False)


def unpaid(record):
    def attempt(row):
        return (
            isinstance(row, dict)
            and row.get("state") == "rejected unpaid"
            and (row.get("http_status") is None or type(row["http_status"]) is int)
            and type(row.get("http_attempt")) is int
            and row["http_attempt"] == 1
            and type(row.get("cost_usd")) in (float, int)
            and row["cost_usd"] == 0
            and row.get("verified_unpaid") is True
        )

    history = record.get("rejected_attempts", [])
    return (
        attempt(record)
        and isinstance(history, list)
        and all(
            attempt(prior) and "rejected_attempts" not in prior for prior in history
        )
    )


def inspection_cost(doc):
    cost = amount(doc.get("cost_usd"))
    if not isinstance(doc.get("rounds"), list) or not isinstance(
        doc.get("limitations"), list
    ):
        raise ValueError("invalid inspection")
    analysis = doc.get("sensor_analysis")
    if analysis is not None and not isinstance(analysis, dict):
        raise ValueError("invalid sensor receipt")
    complete = not doc.get("cost_is_conservative_estimate", False)
    for row in [*doc["rounds"], *([analysis] if analysis is not None else [])]:
        if not isinstance(row, dict):
            raise ValueError("invalid round")
        dispatched = row.get("state") == "dispatched" or (
            row.get("state") == "rejected unpaid" and not unpaid(row)
        )
        if not dispatched:
            continue
        reserve = row.get("reserved_cost_usd")
        valid = type(reserve) in (int, float) and math.isfinite(reserve) and reserve > 0
        if row is analysis and not valid:
            raise ValueError("invalid sensor reservation")
        if row.get("reservation_accounted") is not True:
            cost += float(reserve) if valid else 1.0
        complete = False
    return cost, complete


def episode_spend(out, name):
    candidates = []
    for prefix in ("", "failed_", "noreply_"):
        path = out / (prefix + name)
        if path.exists():
            record = read(path)
            index = record.get("final_attempt_index", 0)
            if type(index) is not int or index < 0:
                raise ValueError("invalid attempt index")
            candidates.append(record)
    # Native billing_record keeps primary, failed, no-reply precedence on ties.
    record = max(candidates, key=lambda r: r.get("final_attempt_index", 0), default={})
    invoices = []
    history = record.get("auxiliary_cost_history", [])
    if candidates:
        invoices = [
            final(record),
            *[final(i) for i in record.get("final_cost_history", [])],
            *[auxiliary(i) for i in history],
        ]
        invoices.append(
            auxiliary(
                (record.get("config") or {}).get("resolution_route") or {}, routing=True
            )
        )
        invoices.append(auxiliary(record.get("evidence_inspection") or {}))
    cache = out / ".evidence" / (name + ".inspection.json")
    if cache.exists():
        cached = read(cache)
        digest = cached.get("digest")
        represented = sum(
            auxiliary(i)[0]
            for i in history
            if digest and i.get("kind") == "inspection" and i.get("digest") == digest
        )
        active = record.get("evidence_inspection") or {}
        if digest and active.get("digest") == digest:
            represented += auxiliary(active)[0]
        cost, verified = inspection_cost(cached)
        invoices.append((max(0, cost - represented), verified))
    return sum(cost for cost, _ in invoices), all(verified for _, verified in invoices)


def receipt_spend(argus: Path) -> dict:
    total, complete = 0.0, True
    seen = set()
    for out in (argus / "run/out", argus / "run_final/out"):
        names = {
            p.name.removeprefix(prefix)
            for prefix in ("", "failed_", "noreply_")
            for p in out.glob(prefix + "episode_*.json")
        }
        names.update(
            p.name.removesuffix(".inspection.json")
            for p in (out / ".evidence").glob("episode_*.json.inspection.json")
        )
        for name in sorted(names - seen):
            seen.add(name)
            try:
                cost, verified = episode_spend(out, name)
                total += cost
                complete = complete and verified
            except (OSError, ValueError, TypeError, AttributeError, OverflowError):
                total += UNKNOWN_RESERVE
                complete = False
    dictionary = argus / "dictionary.json"
    if dictionary.exists() or (argus / "dictionary.claim").exists():
        try:
            record = read(dictionary)
            cost = record.get("cost_usd")
            if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                total += float(cost)
            elif not (
                record.get("attempted") is False and record.get("status") != "pending"
            ):
                raise ValueError("unsettled dictionary")
        except (OSError, ValueError, TypeError):
            try:
                bound = amount(
                    read(argus / "dictionary_status.json").get("projected_upper_usd", 3)
                )
                if bound <= 0:
                    bound = 3.0
            except (OSError, ValueError, TypeError):
                bound = 3.0
            total += bound
            complete = False
    return {
        "cost_usd": round(total, 6) if complete else None,
        "reserved_cost_usd": round(total, 6),
        "cost_complete": complete,
    }
