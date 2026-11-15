"""Run the installed Argus parser and dictionary ledger without paid inference."""

import json
import os
import subprocess
from pathlib import Path

import pytest


def test_real_review_parser_dictionary_and_unknown_receipts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    argus = Path(
        os.environ.get("PIPELINE_TEST_ARGUS_ROOT", root / ".runtime/vendor/argus")
    )
    python = Path(
        os.environ.get(
            "PIPELINE_TEST_ARGUS_PYTHON", root / ".runtime/argus-venv/bin/python"
        )
    )
    if not python.is_file() or not (argus / "label/pipeline_runtime.py").is_file():
        pytest.skip("requires installed pinned Argus; see scripts/install_argus.sh")
    program = r"""
import contextlib, io, json, math, sys
from pathlib import Path
from review import __main__ as review
from label import dictionary as dd, dictionary_stage as ds

work = Path(sys.argv[1]); data = work / 'synthetic-input'; data.mkdir()
caps = []
# Real argparse, validation and execution locks; stop at the job boundary.
review.review_job = lambda a, *_: caps.append(a.cap) or 0
for i, flags in enumerate([[], ['--cap', '2'], ['--no-cap']]):
    sys.argv = ['review', '--data', str(data), '--out', str(work / f'parser-{i}'),
                '--rig', 'teleop_arms', *flags]
    assert review.main() == 0
assert caps == [20.0, 2.0, None], caps
for flags in [['--cap', '0'], ['--cap', '2', '--no-cap']]:
    sys.argv = ['review', '--data', str(data), '--out', str(work / 'invalid'),
                '--rig', 'teleop_arms', *flags]
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            review.main()
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError('invalid/mutually exclusive cap accepted')

held = work / 'held'; held.mkdir()
(held / 'dictionary.json').write_text(json.dumps({'cost_usd': 4.25}))
assert ds.remaining_label_budget(held, 20) == 15.75
assert ds.remaining_label_budget(held, 0) == 0
assert math.isinf(ds.remaining_label_budget(held, None))
unknown = work / 'unknown'; unknown.mkdir()
(unknown / 'dictionary.claim').write_text('{}')
assert ds.dictionary_spend(unknown)['cost_usd'] is None
assert not ds.dictionary_spend(unknown)['complete']
assert ds.dictionary_spend(unknown)['reserved_usd'] > 0
assert math.isinf(ds.remaining_label_budget(unknown, None))

# A deliberately expensive synthetic dictionary plan crosses both the native
# $3 dictionary ceiling and $20 review default. Only transport/input inventory
# are controlled; prepare_upload's claim, parse and receipt code are real.
inv = {'digest': 'synthetic-inventory', 'fields': []}
dd.inventory = lambda _: inv
dd.needs_interpretation = lambda _: True
dd.request_plan = lambda _: {'inventory_digest': inv['digest'],
    'content': [{'type': 'text', 'text': 'synthetic dictionary fixture'}],
    'field_ids': [], 'deferred_fields': [], 'max_tokens': 10000000}
calls = []
def model(*args, **kwargs):
    calls.append('synthetic-transport')
    return {'choices': [{'message': {'content': '{"entries":[]}'}}],
            'usage': {'cost': 25.0}}
for name, cap in [('capped', 20), ('uncapped', None)]:
    job = work / name; episode = job / 'episodes/episode_000000'
    episode.mkdir(parents=True); (episode / 'context.json').write_text('{}')
    ds.prepare(job, episode.parent, [episode.name], free=False, cap=cap,
               api_key='synthetic-no-network', call_model=model)
assert calls == ['synthetic-transport'], calls
assert ds.dictionary_spend(work / 'uncapped')['cost_usd'] == 25.0
assert ds.dictionary_spend(work / 'uncapped')['complete']
assert not (work / 'capped/dictionary.claim').exists()
print(json.dumps({'parser_caps': caps, 'capped_dictionary_refused': True,
                  'unlimited_dictionary_cost_usd': 25.0,
                  'unknown_receipt_cost_is_null': True, 'paid_network_calls': 0}))
"""
    result = subprocess.run(
        [str(python), "-c", program, str(tmp_path)],
        cwd=argus,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout.splitlines()[-1])
    assert summary["parser_caps"] == [20, 2, None]
    assert summary["unlimited_dictionary_cost_usd"] == 25
    assert summary["unknown_receipt_cost_is_null"]
