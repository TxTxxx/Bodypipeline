"""Opt-in real browser acceptance against preview_workbench's synthetic data only.

Run while that preview is up; uses the BODY-2 task's visible browser session.
No model requests, production services, or source data mutations.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path

BROWSER = "/home/txtxx/.local/bin/herdr-browser"
ORIGIN = "http://127.0.0.1:18102"
DATASET = "numbered-fixture"
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/body2"
OUT.mkdir(parents=True, exist_ok=True)


def browser(*args):
    if args[:2] == ("mouse", "move"):
        args = (*args[:2], *[round(float(v)) for v in args[2:]])
    result = subprocess.run(
        [BROWSER, *map(str, args)], capture_output=True, text=True, timeout=50
    )
    if result.returncode:
        raise AssertionError(f"Browser {args}: {result.stderr}{result.stdout}")
    return result.stdout.strip()


def evaluate(code):
    return json.loads(browser("eval", code))


def api(path, body=None):
    req = urllib.request.Request(
        ORIGIN + "/api/annotation-workbench/" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=35) as res:
        return json.load(res)


def head(ep=0):
    return api(f"{DATASET}/episodes/{ep}")


def saved(predicate, ep=0, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = head(ep)
        if predicate(row):
            return row
        time.sleep(0.2)
    row = head(ep)
    raise AssertionError(
        f"Save did not reach expected state: revision={row['revision']} complete={row['complete']} segments={[(s['start'], s['end']) for s in row['document']['segments']]}"
    )


def click(selector):
    browser("wait", selector)
    browser("scrollintoview", selector)
    browser("click", selector)


def fill(selector, text):
    browser("fill", selector, str(text))


def wait(code):
    browser("wait", "--fn", code)


def shot(name):
    browser("screenshot", OUT / name)


def record(name, **facts):
    path = OUT / "browser-results.json"
    rows = json.loads(path.read_text()) if path.exists() else []
    rows.append({"case": name, "at": time.time(), **facts})
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)


def editing():
    browser("set", "offline", "off")
    row = head()
    candidate = api(f"{DATASET}/episodes/0/candidates")["candidates"][0]
    api(
        f"{DATASET}/episodes/0/apply",
        {
            "revision": row["revision"],
            "source_signature": row["source_signature"],
            "candidate": candidate["id"],
            "start": 0,
            "end": 48,
            "acknowledge_current_revision": True,
            "shared_attempt_policy": "keep-existing",
        },
    )
    browser("set", "viewport", 1280, 800)
    browser("open", ORIGIN + f"/workbench?dataset={DATASET}&episode=0")
    wait('document.querySelectorAll("#track [role=button]").length >= 4')
    click("#first")
    wait(
        'document.querySelector("#frame-state").textContent.includes("已对齐数据帧 0")'
    )
    assert evaluate(
        'Array.from(document.querySelectorAll("#preview img")).every(i=>i.complete && i.naturalWidth===320)'
    )
    shot("actual-frame-zero.png")
    click("#last")
    wait(
        'document.querySelector("#frame-state").textContent.includes("已对齐数据帧 47")'
    )
    shot("actual-frame-last.png")
    click('#track [role="button"]:first-child')
    assert evaluate('document.querySelector("#frame").value') == "47"
    fill("#selected-note", "真实浏览器：片段备注输入保护")
    browser("press", "ArrowLeft")
    assert evaluate('document.querySelector("#frame").value') == "47"
    saved(
        lambda h: h["document"]["segments"][0]["note"] == "真实浏览器：片段备注输入保护"
    )
    original = head()["document"]
    fill("#frame", 3)
    click("#browse")
    wait(
        'document.querySelector("#frame-state").textContent.includes("已对齐数据帧 3")'
    )
    click("#split")
    split_doc = saved(
        lambda h: len(h["document"]["segments"]) == len(original["segments"]) + 1
    )["document"]
    assert (
        split_doc["segments"][0]["attemptId"] == split_doc["segments"][1]["attemptId"]
    )
    assert split_doc["attempts"] == original["attempts"]
    fill("#selected-last", 4)
    adjusted = saved(lambda h: h["document"]["segments"][0]["end"] == 5)["document"]
    assert adjusted["segments"][1]["start"] == 5
    click("#undo")
    saved(lambda h: h["document"]["segments"][0]["end"] == 3)
    click("#redo")
    saved(lambda h: h["document"]["segments"][0]["end"] == 5)
    click("#undo")
    saved(lambda h: h["document"]["segments"][0]["end"] == 3)
    click("#undo")
    saved(lambda h: len(h["document"]["segments"]) == len(original["segments"]))
    # Local replacement preserves both original sides and their execution ID.
    fill("#range-first", 2)
    fill("#range-last", 4)
    browser("select", "#new-label", "carry")
    browser("select", "#association", "new")
    browser("select", "#new-outcome", "unknown")
    click("#replace-range")
    local = saved(
        lambda h: any(s.get("subtaskId") == "carry" for s in h["document"]["segments"])
    )["document"]
    assert [(s["start"], s["end"]) for s in local["segments"][:3]] == [
        (0, 2),
        (2, 5),
        (5, 8),
    ]
    assert local["segments"][0]["attemptId"] == local["segments"][2]["attemptId"]
    click("#undo")
    saved(lambda h: len(h["document"]["segments"]) == len(original["segments"]))
    click("#redo")
    saved(lambda h: len(h["document"]["segments"]) == len(local["segments"]))
    click("#undo")
    saved(lambda h: len(h["document"]["segments"]) == len(original["segments"]))
    # Partial confirmation creates geometry, never a second execution.
    fill("#range-first", 2)
    fill("#range-last", 4)
    click("#confirm-range")
    confirmed = saved(
        lambda h: any(
            s["start"] == 2 and s["end"] == 5 and s.get("review")
            for s in h["document"]["segments"]
        )
    )["document"]
    assert confirmed["attempts"] == original["attempts"]
    click("#undo")
    saved(lambda h: len(h["document"]["segments"]) == len(original["segments"]))
    click("#redo")
    restored = saved(
        lambda h: len(h["document"]["segments"]) == len(confirmed["segments"])
    )["document"]
    assert next(s for s in restored["segments"] if s["start"] == 2)["review"]
    shot("editing-partial-review.png")
    record(
        "editing",
        actual_frames=[0, 47, 3],
        cameras=2,
        split_attempt_identity=True,
        shared_boundary=True,
        range_relabel_sides=True,
        partial_confirmation=True,
        undo_redo_review_restored=True,
        input_shortcut_protection=True,
    )


def offline():
    import os

    browser("set", "offline", "off")
    browser("open", ORIGIN + f"/workbench?dataset={DATASET}&episode=0")
    wait('document.querySelectorAll("#track [role=button]").length >= 4')
    click('[data-open="1"]')
    wait('document.querySelector("#episode-title").textContent.includes("001")')
    click("#last")
    wait(
        'document.querySelector("#frame-state").textContent.includes("已对齐数据帧 47")'
    )
    click('[data-open="0"]')
    wait('document.querySelector("#episode-title").textContent.includes("000")')
    click('#track [role="button"]:first-child')
    browser("set", "offline", "on")
    offline_note = "离线本地修改：跨 Episode 保留"
    fill("#selected-note", offline_note)
    fill('[data-field="quality"]', 11)
    fill("#range-first", "-")
    fill("#new-note", "未完成的新片段输入")
    click('[data-open="1"]')
    wait('document.querySelector("#episode-title").textContent.includes("001")')
    if not head(1)["document"]["segments"]:
        fill("#range-first", 47)
        fill("#range-last", 47)
        browser("select", "#new-label", "NULL")
        fill("#new-note", "离线末帧单帧标注")
        click("#create")
    click('[data-open="0"]')
    wait('document.querySelector("#episode-title").textContent.includes("000")')
    assert evaluate('document.querySelector("#range-first").value') == "-"
    assert evaluate('document.querySelector("#new-note").value') == "未完成的新片段输入"
    assert evaluate('document.querySelector("[data-field=quality]").value') == "11"
    shot("offline-cross-episode.png")
    browser("set", "offline", "off")
    saved(lambda h: h["document"]["segments"][0]["note"] == offline_note)
    saved(
        lambda h: any(
            s["start"] == 47 and s["end"] == 48 for s in h["document"]["segments"]
        ),
        ep=1,
    )
    browser("reload")
    wait('document.querySelector("#selected-note") !== null')
    assert evaluate('document.querySelector("[data-field=quality]").value') == "11"
    assert evaluate('document.querySelector("#range-first").value') == "-"
    # Correct the pending rating; server must never have accepted 11/10.
    assert head()["document"]["attempts"][0]["values"]["quality"] != 11
    fill('[data-field="quality"]', 7)
    saved(lambda h: h["document"]["attempts"][0]["values"]["quality"] == 7)
    row = head()
    rival = json.loads(json.dumps(row["document"]))
    rival["values"]["episode-note"] = "另一个窗口的已保存内容"
    api(
        f"{DATASET}/episodes/0",
        {
            "revision": row["revision"],
            "source_signature": row["source_signature"],
            "document": rival,
        },
    )
    fill("#selected-note", "本窗口的未同步内容")
    wait('document.querySelector("#save-status").textContent.includes("版本冲突")')
    assert head()["document"]["segments"][0]["note"] == offline_note
    click("#compare-conflict")
    assert evaluate(
        'document.querySelector("#dialog-body").textContent.includes("本窗口的未同步内容")'
    )
    assert evaluate(
        'document.querySelector("#dialog-body").textContent.includes("另一个窗口的已保存内容")'
    )
    shot("concurrent-both-versions.png")
    click("#use-remote")
    # Temporary source mutation is ONLY this generated fixture; exact bytes,
    # inode and times are restored, leaving the registered source unchanged.
    info = ROOT / ".runtime/body2-preview/dataset/meta/info.json"
    original, stat = info.read_bytes(), info.stat()
    try:
        data = json.loads(original)
        data["synthetic_probe_revision"] = "changed-source"
        info.write_text(json.dumps(data))
        browser("reload")
        wait(
            'document.querySelector("#conflict").textContent.includes("旧来源草稿已保留")'
        )
        assert "离线" not in evaluate('document.querySelector("#conflict").textContent')
        assert "来源" in evaluate('document.querySelector("#save-status").textContent')
        shot("source-version-blocked.png")
    finally:
        info.write_bytes(original)
        os.utime(info, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    click("#refresh")
    wait('!document.querySelector("#save-status").textContent.includes("读取失败")')
    click('#track [role="button"]:first-child')
    browser(
        "network",
        "route",
        f"**/api/annotation-workbench/{DATASET}/episodes/0",
        "--abort",
    )
    fill("#selected-note", "暂时故障后自动重试成功")
    wait('document.querySelector("#save-status").textContent.includes("未同步")')
    shot("transient-save-failure.png")
    browser("network", "unroute")
    saved(
        lambda h: h["document"]["segments"][0]["note"] == "暂时故障后自动重试成功",
        timeout=35,
    )
    record(
        "offline-and-conflict",
        cross_episode_offline=True,
        single_last_frame=True,
        invalid_input_restored=True,
        local_reload_restore=True,
        same_episode_both_versions=True,
        source_409_distinguished=True,
        transient_failure_retried_without_user_action=True,
    )


def server_api(path):
    with urllib.request.urlopen(ORIGIN + path, timeout=35) as res:
        return json.load(res)


def r31():
    import hashlib

    source = ROOT / ".runtime/body2-preview/dataset"

    def source_hashes():
        return {
            str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source.rglob("*")
            if p.is_file()
        }

    before = source_hashes()
    browser("open", ORIGIN + f"/workbench?dataset={DATASET}&episode=1")
    wait('document.querySelector("#episode-title").textContent.includes("001")')
    for ep in (1, 4, 9):
        click(f'[data-open="{ep}"]')
        wait(
            f'document.querySelector("#episode-title").textContent.includes("{ep:03}")'
        )
        if head(ep)["document"]["templateId"] != "cup-fixture":
            click("#templates")
            click("#upgrade-library")
            click("#apply-upgrade")
            saved(lambda h: h["document"]["templateId"] == "cup-fixture", ep=ep)
        fill("#range-first", 0)
        fill("#range-last", 47)
        click("#candidates")
        wait('document.querySelector("#candidate-choice") !== null')
        candidate = next(
            c
            for c in api(f"{DATASET}/episodes/{ep}/candidates")["candidates"]
            if c["run"].startswith("模拟模型")
        )
        browser("select", "#candidate-choice", candidate["id"])
        browser("check", "#candidate-ack")
        browser("check", "#candidate-keep")
        click("#apply-candidate")
        saved(lambda h: len(h["document"]["segments"]) == ep % 3 + 4, ep=ep)
        if ep == 1:
            click("details.card > summary")
            fill("#vqa-question", "杯子在哪？")
            fill("#vqa-answer", "合成画面中心")
            click("#add-vqa")
            saved(lambda h: len(h["document"].get("manualAtoms", [])) >= 2, ep=ep)
        if ep == 4:
            click("#confirm-range")
            saved(lambda h: not h["status"]["pending"], ep=ep)
            click("#complete")
            saved(lambda h: h["complete"], ep=ep)
            click("#count-evidence")
            browser("select", "#evidence-step", "grasp")
            fill(
                "#evidence-reason",
                "人工逐帧核对两次独立抓取；遮挡段不包含新的抓取，其他活动为清理桌面。",
            )
            click("#apply-count-evidence")
            saved(lambda h: bool(h["document"]["countEvidence"]), ep=ep)
    browser("select", "#filter-step", "grasp")
    fill("#filter-count", 2)
    browser("select", "#filter-basis", "complete")
    click("#filter")
    wait('document.querySelectorAll("#episode-list [data-open]").length === 1')
    assert (
        evaluate('document.querySelector("#episode-list [data-open]").dataset.open')
        == "4"
    )
    click("#select-results")
    click("#select-results")
    assert evaluate('document.querySelector("#selection-count").textContent') == "1"
    shot("r31-target-two-attempts.png")
    click("#clear-filter")
    wait('document.querySelectorAll("#episode-list [data-open]").length === 10')
    browser("check", '[data-add="1"]')
    browser("check", '[data-add="9"]')
    click("#delivery")
    wait('document.querySelectorAll("[data-remove-selection]").length === 3')
    click('[data-remove-selection="9"]')
    assert evaluate('document.querySelectorAll("[data-remove-selection]").length') == 2
    click("#dialog-close")
    browser("check", '[data-add="9"]')
    click("#delivery")
    click("#refresh-selection")
    shot("r31-selection-1-4-9.png")
    browser("download", "#export-bundle", OUT / "r31-annotations.json")
    bundle = json.loads((OUT / "r31-annotations.json").read_text())
    assert [e["episode"] for e in bundle["episodes"]] == [1, 4, 9]
    assert bundle["selection"]["origins"] == {
        "1": "manual",
        "4": "filter",
        "9": "manual",
    }
    assert len(bundle["episodes"][0]["document"]["manualAtoms"]) >= 2
    before_jobs = {j["id"] for j in server_api("/api/jobs")}
    click("#export-subset")
    end = time.monotonic() + 30
    job = None
    while time.monotonic() < end:
        jobs = [
            j
            for j in server_api("/api/jobs")
            if j["id"] not in before_jobs
            and j.get("source_kind") == "lerobot_annotation_subset"
        ]
        if jobs:
            job = jobs[0]
            break
        time.sleep(0.2)
    assert job
    # Change the live annotation after queueing: the export must retain the old revision.
    row = head(4)
    doc = json.loads(json.dumps(row["document"]))
    doc["segments"][0]["note"] = "排队之后的修改，不应进入已冻结交付"
    api(
        f"{DATASET}/episodes/4",
        {
            "revision": row["revision"],
            "source_signature": row["source_signature"],
            "document": doc,
        },
    )
    end = time.monotonic() + 90
    while time.monotonic() < end:
        job = server_api("/api/jobs/" + job["id"])
        if job["state"] not in ("QUEUED", "RUNNING"):
            break
        time.sleep(0.5)
    assert job["state"] == "CONVERTED", job.get("error")
    output = Path(job["dataset_path"])
    mapping = json.loads(
        (output / "meta/bodypipeline-episode-mapping.json").read_text()
    )
    assert mapping["source_to_output"] == {"1": 0, "4": 1, "9": 2}
    delivered = json.loads(
        (output / "meta/bodypipeline-annotation-bundle.json").read_text()
    )
    for i, entry in enumerate(delivered["episodes"]):
        assert entry["source_episode"] == [1, 4, 9][i]
        assert entry["document"] == bundle["episodes"][i]["document"]
    hidden = source.with_name("source-hidden-for-read-proof")
    source.rename(hidden)
    try:
        code = """
import json,sys
from pathlib import Path
from letools import open_dataset, validate_dataset
root=Path(sys.argv[1]); source=open_dataset(root); report=validate_dataset(root,deep=True)
assert report.valid,report.errors
assert len(source.episodes)==3
for i,ep in enumerate(source.episodes):
    table=source.read_episode(ep); rows=table.to_pylist(); assert len(rows)==48
    assert rows[0]['observation.state'][0]==[1,4,9][i]
    assert [r['episode_index'] for r in rows]==[i]*48
    assert [r['index'] for r in rows]==list(range(i*48,(i+1)*48))
print(json.dumps({'independent_deep_read':True,'episodes':3,'frames':144,'source_mapping':[1,4,9],'shared_video_offsets_and_rows':True}))
"""
        result = subprocess.run(
            [str(ROOT / ".runtime/letools-venv/bin/python"), "-c", code, str(output)],
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr
        deep = json.loads(result.stdout)
    finally:
        hidden.rename(source)
    assert source_hashes() == before
    (OUT / "r31-subset-job.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2)
    )
    (OUT / "r31-independent-read.json").write_text(
        json.dumps(deep, ensure_ascii=False, indent=2)
    )
    shot("r31-subset-ready.png")
    click("#dialog-close")
    browser("open", ORIGIN + f"/workbench?dataset={job['id']}&episode=0")
    wait('document.querySelector("#episode-title").textContent.includes("000")')
    click("#last")
    wait(
        'document.querySelector("#frame-state").textContent.includes("已对齐数据帧 47")'
    )
    shot("independent-subset-last-frame.png")
    record(
        "r31",
        selection=[1, 4, 9],
        deduplicated=True,
        target_count_filter=True,
        manual_unmatched_allowed=True,
        complete_episode_retained=True,
        source_unchanged=True,
        frozen_revision=True,
        independent_deep_read=deep,
        output_job=job["id"],
    )


def viewer():
    import re

    browser("set", "viewport", 1280, 800)
    browser("open", "http://127.0.0.1:18103/bp/numbered-fixture/episode_000000")
    wait(
        'Array.from(document.querySelectorAll("button")).some(b=>b.textContent.toLowerCase()==="annotations")'
    )
    snapshot = browser("snapshot", "-i")
    line = next(
        line for line in snapshot.splitlines() if 'button "ANNOTATIONS"' in line
    )
    browser("click", "@" + re.search(r"ref=(e[0-9]+)", line).group(1))
    wait(
        'document.querySelector("iframe")?.contentDocument?.querySelector("#episode-title")?.textContent.includes("000")'
    )
    assert evaluate(
        'document.querySelector("iframe").contentDocument.querySelector("#dataset").disabled'
    )
    assert (
        evaluate(
            'getComputedStyle(document.querySelector("iframe").contentDocument.querySelector(".episodes")).display'
        )
        == "none"
    )
    shot("viewer-bound-context.png")
    snapshot = browser("snapshot", "-i")
    line = next(line for line in snapshot.splitlines() if 'button "Episode 1"' in line)
    browser("click", "@" + re.search(r"ref=(e[0-9]+)", line).group(1))
    wait(
        'document.querySelector("iframe")?.contentDocument?.querySelector("#episode-title")?.textContent.includes("001")'
    )
    assert "episode=1" in evaluate('document.querySelector("iframe").src')
    shot("viewer-switch-episode-one.png")
    snapshot = browser("snapshot", "-i")
    line = next(
        line for line in snapshot.splitlines() if 'link "打开完整工作区"' in line
    )
    ref = re.search(r"ref=(e[0-9]+)", line).group(1)
    browser("click", "@" + ref)
    wait(
        'location.pathname === "/workbench" && document.querySelector("#episode-title")?.textContent.includes("001")'
    )
    assert evaluate('document.querySelectorAll("iframe").length') == 0
    subset = json.loads((OUT / "r31-subset-job.json").read_text())["id"]
    browser("select", "#dataset", subset)
    wait(
        f'document.querySelector("#dataset").value === "{subset}" && document.querySelector("#episode-title").textContent.includes("001")'
    )
    click("#last")
    wait(
        'document.querySelector("#frame-state").textContent.includes("已对齐数据帧 47")'
    )
    shot("viewer-open-standalone-subset.png")
    record(
        "viewer-context",
        outer_episode_navigation=True,
        inner_navigation_locked=True,
        explicit_standalone_dataset_switch=True,
        subset=subset,
    )


def manual():
    browser("open", ORIGIN + f"/workbench?dataset={DATASET}&episode=5")
    wait('document.querySelector("#episode-title").textContent.includes("005")')
    if head(5)["document"]["templateId"] != "cup-fixture":
        click("#templates")
        click("#upgrade-library")
        click("#apply-upgrade")
        saved(lambda h: h["document"]["templateId"] == "cup-fixture", ep=5)
    existing = len(head(5)["document"]["segments"])
    pattern = [
        (0, 7, "grasp", "failure"),
        (8, 15, "place", "unknown"),
        (16, 23, "grasp", "success"),
        (24, 47, "carry", "success"),
    ]
    for index, (first, last, label, outcome) in enumerate(pattern):
        if index < existing:
            continue
        fill("#range-first", first)
        fill("#range-last", last)
        browser("select", "#new-label", label)
        browser("select", "#association", "new")
        browser("select", "#new-outcome", outcome)
        fill("#new-note", "人工从零标注 · 合成画面验收")
        click("#create-confirm")
        saved(
            lambda h: (
                len(h["document"]["segments"]) == index + 1
                and not h["status"]["pending"]
            ),
            ep=5,
        )
    click("#complete")
    result = saved(lambda h: h["complete"], ep=5)
    assert [s["subtaskId"] for s in result["document"]["segments"]] == [
        "grasp",
        "place",
        "grasp",
        "carry",
    ]
    assert len(result["document"]["attempts"]) == 4
    browser("select", "#filter-step", "grasp")
    fill("#filter-count", 2)
    click("#filter")
    wait(
        'Array.from(document.querySelectorAll("#episode-list [data-open]")).some(e=>e.dataset.open==="5")'
    )
    shot("manual-arbitrary-order-complete.png")
    click("#clear-filter")
    click('[data-open="6"]')
    wait('document.querySelector("#episode-title").textContent.includes("006")')
    fill("#range-first", 0)
    fill("#range-last", 47)
    browser("select", "#new-label", "NULL")
    if not head(6)["document"]["segments"]:
        click("#create")
    saved(lambda h: len(h["document"]["segments"]) == 1, ep=6)
    click('[data-open="3"]')
    wait('document.querySelector("#episode-title").textContent.includes("003")')
    if head(3)["document"]["templateId"] != "cup-fixture":
        click("#templates")
        click("#upgrade-library")
        click("#apply-upgrade")
        saved(lambda h: h["document"]["templateId"] == "cup-fixture", ep=3)
    fill("#range-first", 0)
    fill("#range-last", 47)
    click("#candidates")
    wait('document.querySelector("#candidate-choice") !== null')
    candidate = next(
        c
        for c in api(f"{DATASET}/episodes/3/candidates")["candidates"]
        if c["provenance"].get("annotation")
    )
    browser("select", "#candidate-choice", candidate["id"])
    browser("check", "#candidate-ack")
    click("#apply-candidate")
    wait('!document.querySelector("#dialog").open')
    ai = saved(lambda h: len(h["document"]["segments"]) == 2, ep=3)
    assert ai["document"]["segments"][0]["evidence"]
    assert not ai["document"]["segments"][0].get("risks")
    shot("actual-model-candidate-in-workspace.png")
    # Use the visible delivery controls to clear old selections and confirm two normal Episodes.
    click("#delivery")
    for ep in (1, 4, 9):
        if evaluate(
            f"document.querySelector('[data-remove-selection=\"{ep}\"]') !== null"
        ):
            click(f'[data-remove-selection="{ep}"]')
    click("#dialog-close")
    browser("check", '[data-add="3"]')
    browser("check", '[data-add="6"]')
    click("#delivery")
    click("#refresh-selection")
    click("#batch-confirm")
    shot("batch-confirm-normal-preview.png")
    click("#apply-batch")
    saved(lambda h: h["complete"], ep=3)
    saved(lambda h: h["complete"], ep=6)
    wait('!document.querySelector("#dialog").open')
    click("#delivery")
    prior = {ep: head(ep)["revision"] for ep in (1, 4, 9)}
    browser("upload", "#import-bundle", OUT / "r31-annotations.json")
    wait('document.querySelector("#notice").textContent.includes("独立候选")')
    assert {ep: head(ep)["revision"] for ep in (1, 4, 9)} == prior
    assert any(
        c["run"] == "标注包导入"
        for c in api(f"{DATASET}/episodes/1/candidates")["candidates"]
    )
    shot("annotation-bundle-reimport-candidates.png")
    click("#dialog-close")
    record(
        "from-zero-and-batch",
        arbitrary_order=["grasp", "place", "grasp", "carry"],
        independent_attempts=4,
        target_grasp_count=2,
        create_and_confirm=True,
        unknown_outcome_keeps_label=True,
        batch_normal_with_evidence=[3, 6],
        actual_model_candidate_applied=True,
        bundle_reimport_keeps_current_heads=True,
    )


def gestures():
    browser("set", "viewport", 1280, 900)
    browser("open", ORIGIN + f"/workbench?dataset={DATASET}&episode=5")
    wait('document.querySelectorAll("#track [role=button]").length===4')
    original = head(5)["document"]
    a = original["segments"][0]

    def choose(sid):
        click(f'[data-segment="{sid}"] strong')

    assert "抓取 第2次" in browser("snapshot", "-i")
    browser("scrollintoview", "#track")
    rect = evaluate(
        '(()=>{const r=document.querySelector("#track").getBoundingClientRect();return {left:r.left,top:r.top,width:r.width,height:r.height}})()'
    )
    x = rect["left"] + rect["width"] * 8 / 48 - 2
    y = rect["top"] + rect["height"] / 2
    browser("mouse", "move", x, y)
    browser("mouse", "down")
    browser("mouse", "move", rect["left"] + rect["width"] * 10 / 48, y)
    browser("mouse", "up")
    adjusted = saved(lambda h: h["document"]["segments"][0]["end"] == 10, ep=5)
    assert adjusted["document"]["segments"][1]["start"] == 10
    shot("actual-pointer-shared-boundary.png")
    click("#undo")
    saved(lambda h: h["document"]["segments"][0]["end"] == 8 and h["complete"], ep=5)
    click("#range-tool")
    browser("scrollintoview", "#track")
    rect = evaluate(
        '(()=>{const r=document.querySelector("#track").getBoundingClientRect();return {left:r.left,top:r.top,width:r.width,height:r.height}})()'
    )
    y = rect["top"] + rect["height"] / 2
    browser("mouse", "move", rect["left"] + rect["width"] * 2 / 48, y)
    browser("mouse", "down")
    browser("mouse", "move", rect["left"] + rect["width"] * 5 / 48, y)
    browser("mouse", "up")
    assert evaluate('document.querySelector("#range-first").value') == "2"
    assert evaluate('document.querySelector("#range-last").value') == "5"
    click("#browse")
    choose(a["id"])
    click("#merge-attempt")
    other = original["segments"][2]["attemptId"]
    browser("select", "#merge-other", other)
    browser("select", "#merge-outcome", "unknown")
    click("#apply-merge-attempt")
    merged = saved(
        lambda h: next(x for x in h["document"]["attempts"] if x["id"] == other).get(
            "archived"
        ),
        ep=5,
    )
    assert (
        merged["document"]["segments"][0]["attemptId"]
        == merged["document"]["segments"][2]["attemptId"]
    )
    click("#undo")
    saved(
        lambda h: (
            h["complete"]
            and not next(x for x in h["document"]["attempts"] if x["id"] == other).get(
                "archived"
            )
        ),
        ep=5,
    )
    fill("#frame", 3)
    click("#browse")
    choose(a["id"])
    click("#split")
    split_doc = saved(lambda h: len(h["document"]["segments"]) == 5, ep=5)["document"]
    fill("#selected-note", "左侧局部信息")
    saved(lambda h: h["document"]["segments"][0]["note"] == "左侧局部信息", ep=5)
    choose(split_doc["segments"][1]["id"])
    fill("#selected-note", "右侧局部信息")
    saved(lambda h: h["document"]["segments"][1]["note"] == "右侧局部信息", ep=5)
    choose(a["id"])
    click("#merge-segment")
    fill("#merged-note", "人工决定保留的局部信息")
    click("#apply-merge-segment")
    saved(
        lambda h: (
            len(h["document"]["segments"]) == 4
            and h["document"]["segments"][0]["note"] == "人工决定保留的局部信息"
        ),
        ep=5,
    )
    for _ in range(12):
        prior = head(5)
        if prior["complete"] and len(prior["document"]["segments"]) == 4:
            break
        click("#undo")
        saved(lambda h: h["revision"] > prior["revision"], ep=5)
    restored = saved(
        lambda h: len(h["document"]["segments"]) == 4 and h["complete"], ep=5
    )
    assert restored["document"]["attempts"] == original["attempts"]
    shot("attempt-numbers-and-restored-review.png")
    record(
        "gestures-and-merges",
        pointer_shared_boundary=True,
        pointer_range=True,
        attempt_conflicts_explicit=True,
        segment_conflicts_explicit=True,
        full_undo_restores_review=True,
        human_ordinals_scope="Episode + subtask",
    )


def archived():
    browser("open", ORIGIN + f"/workbench?dataset={DATASET}&episode=9")
    wait('document.querySelector("#episode-title").textContent.includes("009")')
    original = head(9)
    attempt = original["document"]["attempts"][0]
    span = original["document"]["segments"][0]
    click(f'[data-segment="{span["id"]}"] strong')
    click("#delete-segment")
    saved(
        lambda h: next(
            a for a in h["document"]["attempts"] if a["id"] == attempt["id"]
        )["archived"],
        ep=9,
    )
    click("#templates")
    library = server_api("/api/task-templates")
    if not any(t["name"] == "删除抓取后的历史升级验收" for t in library["templates"]):
        click("#new-template")
        fill("#template-name", "删除抓取后的历史升级验收")
        click("#save-template")
        wait('document.querySelector("#notice").textContent.includes("共享模板已保存")')
        library = server_api("/api/task-templates")
    template = next(
        t for t in library["templates"] if t["name"] == "删除抓取后的历史升级验收"
    )
    wait(
        f'Array.from(document.querySelector("#library-choice").options).some(o=>o.value==="{template["id"]}")'
    )
    browser("select", "#library-choice", template["id"])
    click("#upgrade-library")
    click("#apply-upgrade")
    upgraded = saved(lambda h: h["document"]["templateId"] == template["id"], ep=9)
    archived_attempt = next(
        a for a in upgraded["document"]["attempts"] if a["id"] == attempt["id"]
    )
    assert archived_attempt["values"] == attempt["values"]
    assert (
        archived_attempt["provenance"]["archivedTemplateSnapshot"]
        == original["document"]["templateSnapshot"]
    )
    click("#delivery")
    for ep in evaluate(
        'Array.from(document.querySelectorAll("[data-remove-selection]")).map(e=>e.dataset.removeSelection)'
    ):
        click(f'[data-remove-selection="{ep}"]')
    click("#dialog-close")
    browser("check", '[data-add="9"]')
    click("#delivery")
    click("#refresh-selection")
    browser("download", "#export-bundle", OUT / "archived-template-upgrade.json")
    bundle = json.loads((OUT / "archived-template-upgrade.json").read_text())
    assert (
        bundle["episodes"][0]["document"]["attempts"]
        == upgraded["document"]["attempts"]
    )
    shot("archived-attempt-upgrade-delivery.png")
    click("#dialog-close")
    click("#undo")
    saved(
        lambda h: h["document"]["templateId"] == original["document"]["templateId"],
        ep=9,
    )
    click("#undo")
    restored = saved(
        lambda h: any(s["id"] == span["id"] for s in h["document"]["segments"]), ep=9
    )
    assert restored["document"]["attempts"] == original["document"]["attempts"]
    record(
        "archived-template-upgrade",
        stable_attempt_id=attempt["id"],
        values_preserved=True,
        actual_old_snapshot_preserved=True,
        saved_exported_reread=True,
        undo_restored=True,
    )


def races():
    import os

    control = ROOT / ".runtime/body2-preview/ui-delay-fixture.json"
    subset = json.loads((OUT / "r31-subset-job.json").read_text())["id"]
    browser("open", ORIGIN + f"/workbench?dataset={DATASET}&episode=0")
    wait('document.querySelectorAll("#track [role=button]").length >= 4')
    click('#track [role="button"]:first-child strong')
    try:
        control.write_text(
            json.dumps(
                {
                    "method": "POST",
                    "path": f"/api/annotation-workbench/{DATASET}/episodes/0",
                    "seconds": 2,
                }
            )
        )
        fill("#selected-note", "旧工作区延迟响应仅写回原数据集")
        wait('document.querySelector("#save-status").textContent.includes("正在同步")')
        browser("select", "#dataset", subset)
        wait(
            f'document.querySelector("#dataset").value==="{subset}" && document.querySelectorAll("#episode-list [data-open]").length===3'
        )
        saved(
            lambda h: (
                h["document"]["segments"][0]["note"] == "旧工作区延迟响应仅写回原数据集"
            )
        )
        key = f"bodypipeline:workbench:v2:{subset}:0"
        backup = evaluate(f"JSON.parse(localStorage.getItem({json.dumps(key)}))")
        assert backup["head"]["dataset"] == subset
        assert (
            backup["document"]["segments"][0]["note"]
            != "旧工作区延迟响应仅写回原数据集"
        )
        control.write_text(
            json.dumps(
                {
                    "method": "GET",
                    "path": f"/api/annotation-workbench/{DATASET}/episodes",
                    "seconds": 2,
                }
            )
        )
        browser("select", "#dataset", DATASET)
        browser("select", "#dataset", subset)
        wait(
            f'document.querySelector("#dataset").value==="{subset}" && document.querySelectorAll("#episode-list [data-open]").length===3'
        )
        browser("wait", 2300)
        assert (
            evaluate('document.querySelectorAll("#episode-list [data-open]").length')
            == 3
        )
        shot("dataset-race-isolated.png")
    finally:
        control.unlink(missing_ok=True)
    # A clean cached revision zero also belongs to a concrete source signature.
    browser("open", ORIGIN + f"/workbench?dataset={DATASET}&episode=10")
    wait('document.querySelector("#episode-title").textContent.includes("010")')
    old = head(10)
    assert old["revision"] == 0
    info = ROOT / ".runtime/body2-preview/dataset/meta/info.json"
    original, stat = info.read_bytes(), info.stat()
    try:
        content = json.loads(original)
        content["synthetic_version_probe"] = "different-source-at-revision-zero"
        info.write_text(json.dumps(content))
        browser("reload")
        wait('document.querySelector("#save-status").textContent.includes("版本冲突")')
        assert evaluate(
            'document.querySelector("#conflict").textContent.includes("双方")'
        )
        assert (
            head(10)["revision"] == 0
            and head(10)["source_signature"] != old["source_signature"]
        )
        shot("clean-revision-zero-source-conflict.png")
    finally:
        info.write_bytes(original)
        os.utime(info, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    browser("reload")
    wait('document.querySelector("#episode-title").textContent.includes("010")')
    record(
        "async-isolation",
        delayed_save_dataset_bound=True,
        stale_list_response_ignored=True,
        clean_revision_zero_checks_source=True,
        delays="controlled fixture, actual browser HTTP",
    )


def selection():
    browser("set", "viewport", 1280, 900)
    browser("open", ORIGIN + f"/workbench?dataset={DATASET}&episode=5")
    wait('document.querySelector("#episode-title").textContent.includes("005")')
    click("#zoom-in")
    assert "24 / 48" in evaluate('document.querySelector("#zoom-status").textContent')
    click("#pan-right")
    click("#zoom-all")
    assert "48 / 48" in evaluate('document.querySelector("#zoom-status").textContent')
    click("#delivery")
    for ep in evaluate(
        'Array.from(document.querySelectorAll("[data-remove-selection]")).map(e=>e.dataset.removeSelection)'
    ):
        click(f'[data-remove-selection="{ep}"]')
    click("#dialog-close")
    click("#select-page")
    assert evaluate('document.querySelector("#selection-count").textContent') == "10"
    click("#next-page")
    assert (
        evaluate('document.querySelectorAll("#episode-list [data-open]").length') == 2
    )
    click("#select-page")
    assert evaluate('document.querySelector("#selection-count").textContent') == "12"
    click("#select-results")
    assert evaluate('document.querySelector("#selection-count").textContent') == "12"
    click("#delivery")
    shot("selection-current-page-all-results.png")
    for ep in range(12):
        click(f'[data-remove-selection="{ep}"]')
    before = len(server_api("/api/jobs"))
    click("#export-bundle")
    wait('document.querySelector("#notice").textContent.includes("导出清单为空")')
    click("#export-subset")
    assert len(server_api("/api/jobs")) == before
    shot("empty-selection-no-full-export.png")
    click("#dialog-close")
    record(
        "selection-and-zoom",
        current_page=10,
        all_results=12,
        pagination_preserves_selection=True,
        duplicate_selection_deduplicated=True,
        empty_selection_never_exports_all=True,
        local_zoom_and_pan=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=[
            "editing",
            "offline",
            "r31",
            "viewer",
            "manual",
            "gestures",
            "archived",
            "races",
            "selection",
        ],
    )
    args = parser.parse_args()
    globals()[args.phase]()
