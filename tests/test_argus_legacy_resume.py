"""An old template cache retains its paid identity and cannot silently upgrade."""

import subprocess
from pathlib import Path

import pytest
from test_annotations import service as service

from pipeline.server import argus_annotation
from pipeline.server.task_annotations import TaskTemplate
from pipeline.shared.core import DomainError, atomic_json, digest


def test_actual_v1_identity_and_native_success_receipt_require_explicit_upgrade(
    service, tmp_path
):
    runner, _, _, _ = service
    root = Path(__file__).resolve().parents[1]
    argus = root / ".runtime/vendor/argus"
    python = root / ".runtime/argus-venv/bin/python"
    if not python.is_file() or not argus.is_dir():
        pytest.skip("requires optional pinned Argus")
    old = tmp_path / "old-0002"
    (old / "label").mkdir(parents=True)
    (old / "label/__init__.py").write_text(
        f"__path__.append({str(argus / 'label')!r})\n"
    )
    # Execute the actual pinned files with only patch 0002, not v2 identity code.
    for name in ("label/harness.py", "label/episode.py"):
        data = subprocess.check_output(["git", "show", "383ca11:" + name], cwd=argus)
        (old / name).write_bytes(data)
    subprocess.run(
        [str(python), str(root / "patches/argus/0002-task-template.py"), str(old)],
        check=True,
    )
    helper = subprocess.check_output(
        ["git", "show", "49aff8e:pipeline/server/argus_template_contract.py"], cwd=root
    )
    (old / "label/pipeline_template.py").write_bytes(helper)
    template = TaskTemplate.model_validate(
        {
            "id": "old-cup",
            "name": "Legacy cup",
            "subtasks": [{"id": "grab", "name": "Grab"}],
            "fields": [],
        }
    ).model_dump(exclude_none=True)
    spec = {"version": 1, "template": template, "signature": digest(template)}
    row = runner.enqueue("job-1")
    row = runner.save(
        row,
        state="PARTIAL",
        task_template=template,
        template_signature=spec["signature"],
        cost_usd=0.001,
    )
    work = runner.work(row)
    atomic_json(work / "task-template.json", spec)
    ep = work / "argus/episodes/episode_000000"
    atomic_json(
        ep / "context.json",
        {
            "episode_id": "episode_000000",
            "episode_index": 0,
            "rig": "teleop_arms",
            "state_kind": "none",
            "fps": 10,
            "n_state_frames": 10,
            "duration_s": 1,
            "cameras": {},
            "instruction": "synthetic fixture",
        },
    )
    atomic_json(ep / "sources.json", {})
    code = r"""
import json,sys,os
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
os.environ['PIPELINE_ARGUS_TASK_TEMPLATE']=sys.argv[3]
from label import harness as h
work=Path(sys.argv[3]).parent; ep=Path(sys.argv[4]); spec=json.loads(Path(sys.argv[3]).read_text())
identity=h.input_identity(ep,model='mock-model',reasoning='none',max_tokens=2048,cell_w=224,example_dir=None,inspection='off')
assert identity['settings']['bodypipeline_task_template']==spec['signature']
assert 'bodypipeline_business_contract' not in identity['settings']
reply={'task_summary':'Move synthetic cup','timeline':[{'start_s':0,'end_s':1,'arm':'both','action':'grasp','contribution':'advancing','progress':1}], 'completion':{'task_completed':'success'},'tasks':[{'start_s':0,'end_s':1,'task':'Move cup','judged_against':'instruction','outcome':'success'}], 'bodypipeline_task':{'template_id':spec['template']['id'],'template_signature':spec['signature'],'values':{},'attempts':[{'subtask_id':'grab','start_s':0,'end_s':1,'values':{}}],'null_segments':[],'frames':[]}}
request={'label_schema':h.LABEL_SCHEMA,'given_prompt':'synthetic fixture','requested_outcome':'completion.task_completed','config':{'timesteps_s':[0,.5,.9]}}
labels,ok=h.read_reply(json.dumps(reply),request)
assert ok,labels
record={**request,'parse_ok':ok,'labels':labels,'response_text':json.dumps(reply),'input_identity':identity,'usage':{'est_cost_usd':.001},'final_dispatch_outcome':'received','final_attempt_index':1,'test_fixture':'native v1 parser and identity; synthetic billing receipt, no paid call'}
path=work/'argus/run/out/episode_000000.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(record))
print(json.dumps({'actual_v1_identity':True,'actual_v1_parser_success':ok,'synthetic_billing_fixture':True}))
"""
    result = subprocess.run(
        [
            str(python),
            "-c",
            code,
            str(old),
            str(argus),
            str(work / "task-template.json"),
            str(ep),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    receipt = work / "argus/run/out/episode_000000.json"
    before = receipt.read_bytes()
    assert argus_annotation.legacy_template_cache(work)
    with pytest.raises(DomainError, match="普通续跑不会自动升级重算"):
        runner.resume(row["id"])
    assert receipt.read_bytes() == before
    assert runner.episodes(row["id"])[0]["labels_available"]
    assert runner.export(row["id"])["mode"] == "template"
