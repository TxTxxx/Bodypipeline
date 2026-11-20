"""Duration display rounding must not reject a part that fits its frame budget."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def patched_duration(tmp_path):
    path = Path(__file__).parents[1] / "patches/argus/0006-piece-duration-rounding.py"
    spec = importlib.util.spec_from_file_location("piece_duration_patch", path)
    patch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patch)
    target = tmp_path / "label/episode.py"
    target.parent.mkdir()
    target.write_text(
        "def request_duration(ep):\n"
        "    actual = ep['actual']\n"
        "    stated = ep['context'].get('duration_s')\n"
        "    return max(actual, stated or 0.0)\n"
    )
    patch.apply(tmp_path)
    once = target.read_text()
    patch.apply(tmp_path)
    assert target.read_text() == once
    scope = {}
    exec(compile(once, str(target), "exec"), scope)
    return scope["request_duration"]


def test_exact_frame_limit_survives_rounded_saved_duration(patched_duration):
    limit = 1700 / 30
    ep = {"actual": limit, "context": {"duration_s": 56.667, "piece": {"index": 2}}}
    assert patched_duration(ep) == limit
    assert patched_duration(ep) <= limit


@pytest.mark.parametrize(
    ("actual", "stated", "piece", "expected"),
    [
        (1701 / 30, 56.7, {"index": 2}, 56.7),
        (1700 / 30, 56.8, {"index": 2}, 56.8),
        (1700 / 30, 56.667, None, 56.667),
        (1700 / 30, None, {"index": 2}, 1700 / 30),
        (1700 / 30, 56.666, {"index": 2}, 1700 / 30),
    ],
)
def test_real_overruns_and_non_piece_claims_still_apply(
    patched_duration, actual, stated, piece, expected
):
    ep = {"actual": actual, "context": {"duration_s": stated, "piece": piece}}
    assert patched_duration(ep) == expected
    if expected > 1700 / 30:
        assert patched_duration(ep) > 1700 / 30
