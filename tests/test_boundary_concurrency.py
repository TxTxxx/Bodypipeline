"""Boundary workers overlap without sharing Episode writes or overspending caps."""

import copy
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType, SimpleNamespace

import pytest

from pipeline.server import argus_template_runtime as runtime
from pipeline.shared.core import atomic_json


@pytest.fixture
def argus_modules(monkeypatch):
    label = ModuleType("label")
    label.__path__ = []
    modules = {}
    for name in (
        "harness",
        "atomic",
        "dictionary_stage",
        "pipeline_template",
        "episode",
    ):
        module = ModuleType("label." + name)
        monkeypatch.setitem(sys.modules, "label." + name, module)
        setattr(label, name, module)
        modules[name] = module
    monkeypatch.setitem(sys.modules, "label", label)
    modules["atomic"].write_atomic = atomic_json
    modules["harness"].get_keys = lambda: ["synthetic-only"]
    modules["dictionary_stage"].remaining_label_budget = lambda *args: float("inf")
    modules["pipeline_template"].template_spec = lambda: {"signature": "test"}
    modules["pipeline_template"].validate_task_reply = lambda *args: None
    modules["episode"].load = lambda path: {}
    modules["episode"].frame_time = lambda ep, frame: frame / 10
    monkeypatch.delenv("PIPELINE_ANNOTATION_BACKEND", raising=False)
    return modules


def prepare_episode(job, index):
    ep = job / "units" / f"episode_{index:06}"
    atomic_json(ep / "context.json", {"fps": 10, "n_state_frames": 10})
    record = {
        "model": "synthetic-only",
        "reasoning_effort": "none",
        "input_identity": {"episode": ep.name},
        "parse_ok": True,
        "labels": {
            "bodypipeline_task": {
                "version": 2,
                "segments": [{"id": "s", "start_s": 0, "end_s": 0.5}],
            }
        },
    }
    path = job / "run/out" / f"{ep.name}.json"
    atomic_json(path, record)
    return ep, path, record


@pytest.mark.parametrize("concurrency", [1, 2, 4])
def test_episode_workers_respect_concurrency_and_preserve_record_identity(
    tmp_path, monkeypatch, argus_modules, concurrency, capsys
):
    originals = [prepare_episode(tmp_path, i) for i in range(8)]
    barrier = threading.Barrier(concurrency)
    guard = threading.Lock()
    active = peak = 0
    seen = []

    def refine(record, path, ep, spec, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            seen.append(ep.name)
        if int(ep.name.split("_")[-1]) < concurrency:
            barrier.wait(timeout=5)
        record["refined"] = True
        with guard:
            active -= 1

    monkeypatch.setattr(runtime, "refine_record", refine)
    args = SimpleNamespace(concurrency=concurrency, cap=None, max_tokens=None)
    runtime.refine_outputs(tmp_path, args)
    assert peak == concurrency
    assert sorted(seen) == [ep.name for ep, _, _ in originals]
    for _, path, original in originals:
        assert json.loads(path.read_text()) == {**original, "refined": True}
    assert f"concurrency={concurrency}" in capsys.readouterr().out


def test_unexpected_failure_does_not_dispatch_remaining_episodes(
    tmp_path, monkeypatch, argus_modules
):
    for index in range(5):
        prepare_episode(tmp_path, index)
    seen = []

    def fail(record, path, ep, spec, **kwargs):
        seen.append(ep.name)
        raise ValueError("source proof changed")

    monkeypatch.setattr(runtime, "refine_record", fail)
    with pytest.raises(ValueError, match="source proof changed"):
        runtime.refine_outputs(
            tmp_path, SimpleNamespace(concurrency=4, cap=None, max_tokens=None)
        )
    assert 1 <= len(seen) <= 4
    assert "episode_000004" not in seen


def test_parallel_claims_cannot_both_spend_the_last_budget(
    tmp_path, monkeypatch, argus_modules
):
    episodes = [prepare_episode(tmp_path, i) for i in range(2)]
    both_claiming = threading.Event()
    budget_denied = threading.Event()
    counter_lock = threading.Lock()
    claims = calls = 0

    class ObservedLock:
        lock = threading.Lock()

        def __enter__(self):
            nonlocal claims
            with counter_lock:
                claims += 1
                if claims == 2:
                    both_claiming.set()
            self.lock.acquire()

        def __exit__(self, *args):
            self.lock.release()

    reservation_lock = ObservedLock()

    def remaining():
        # The second worker must be waiting for the same reservation lock.
        assert both_claiming.wait(5)
        reserved = sum(
            invoice["cost_usd"]
            for _, path, _ in episodes
            for invoice in json.loads(path.read_text()).get(
                "auxiliary_cost_history", []
            )
        )
        return 1 - reserved

    def call(parts):
        nonlocal calls
        with counter_lock:
            calls += 1
        assert budget_denied.wait(5)
        return {"choices": [{"message": {"content": "{}"}}], "usage": {}}

    monkeypatch.setattr(runtime, "_evidence_content", lambda *args: ([], [], 0))
    monkeypatch.setattr(
        runtime,
        "apply_boundaries",
        lambda task, *args: (copy.deepcopy(task), {}),
    )

    def refine(item):
        ep, path, record = item
        runtime.refine_record(
            record,
            path,
            ep,
            {"signature": "test", "template": {"id": "test"}},
            call=call,
            bound=lambda parts: 0.6,
            price=lambda usage: (0.6, "synthetic"),
            remaining=remaining,
            reservation_lock=reservation_lock,
        )
        entries = record["pipeline_boundary"]["requests"].values()
        if any(e.get("pause_reason") == "budget" for e in entries):
            budget_denied.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(refine, episodes))
    assert calls == 1
    saved = [json.loads(path.read_text()) for _, path, _ in episodes]
    assert (
        sum(
            invoice["cost_usd"]
            for record in saved
            for invoice in record.get("auxiliary_cost_history", [])
        )
        == 0.6
    )
    assert (
        sum(
            entry.get("complete", False)
            for record in saved
            for entry in record["pipeline_boundary"]["requests"].values()
        )
        == 1
    )
