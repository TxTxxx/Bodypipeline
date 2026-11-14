"""Opt-in real-browser video acceptance on the BODY-2 task's own browser.

Start workbench_video_server first. Run --case native/real/fallback/races.
Only this fixture server is contacted; real datasets are read, never annotated.
Raw timings and actual screen recordings go to the ignored output directory.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

BROWSER = "/home/txtxx/.local/bin/herdr-browser"
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/body2-video"
ORIGIN = "http://127.0.0.1:18132"


def browser(*args):
    environment = dict(os.environ)
    toolchain = ROOT / ".runtime/toolchain/usr"
    environment["PATH"] = (
        str(toolchain / "bin") + os.pathsep + environment.get("PATH", "")
    )
    environment["LD_LIBRARY_PATH"] = (
        str(toolchain / "lib/x86_64-linux-gnu")
        + os.pathsep
        + environment.get("LD_LIBRARY_PATH", "")
    )
    result = subprocess.run(
        [BROWSER, *map(str, args)],
        capture_output=True,
        text=True,
        timeout=40,
        env=environment,
    )
    if result.returncode:
        raise AssertionError(f"Browser {args[:2]}: {result.stderr}{result.stdout}")
    return result.stdout.strip()


def evaluate(code):
    return json.loads(browser("eval", code))


def api(path, body=None):
    request = urllib.request.Request(
        ORIGIN + "/api/annotation-workbench/" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


JS = r"""
(()=>{
window.videoProbeStop?.();
const cleanup=[];
window.videoProbeStop=()=>cleanup.forEach(fn=>fn());
window.videoProbe = { requests: [], events: [], samples: [], input: [], active: 0, maxActive: 0 };
window.sampleCameras = () => [...document.querySelectorAll('.camera')].map(c => {
  const video = c.querySelector('video'), image = c.querySelector('img');
  const visible = !image.hidden && image.naturalWidth ? image : video;
  const canvas = document.createElement('canvas');
  canvas.width = video.videoWidth || image.naturalWidth;
  canvas.height = video.videoHeight || image.naturalHeight;
  const context = canvas.getContext('2d', { willReadFrequently: true });
  context.drawImage(visible, 0, 0, canvas.width, canvas.height);
  let code = 0;
  if (canvas.width === 480 && canvas.height === 270) for (let b=0;b<12;b++)
    if (context.getImageData(26+b*28,232,1,1).data[0]>128) code |= 1<<b;
  return { frame: Number(c.dataset.displayedFrame), pts: Number(c.dataset.mediaPts),
    clock: c.dataset.clock, code, width: canvas.width, height: canvas.height,
    image: !image.hidden, currentTime: video.currentTime, seeking: video.seeking,
    src: video.src };
});
window.waitVideoAligned = async(target, timeout=8000) => {
  const start=performance.now();
  while (performance.now()-start<timeout) {
    const cameras=[...document.querySelectorAll('.camera')];
    if (cameras.length && cameras.every(c=>Number(c.dataset.displayedFrame)===target)
      && document.querySelector('#frame-state').textContent.includes('已对齐数据帧 '+target+' ·')) {
      await new Promise(requestAnimationFrame);
      return { target, ms: performance.now()-start, cameras: sampleCameras() };
    }
    await new Promise(requestAnimationFrame);
  }
  throw Error('No exact frame '+target+': '+document.querySelector('#frame-state').textContent);
};
window.videoSeek = async(target) => {
  const start=performance.now(), input=document.querySelector('#frame');
  input.value=String(target); input.dispatchEvent(new Event('change',{bubbles:true}));
  const result=await waitVideoAligned(target); result.ms=performance.now()-start; return result;
};
const rawFetch=window.videoProbeRawFetch||(window.videoProbeRawFetch=window.fetch.bind(window));
window.fetch=async(...args)=>{
  const url=String(args[0]);
  if (!url.includes('/frame/')) return rawFetch(...args);
  const r={url,start:performance.now()}; videoProbe.requests.push(r);
  videoProbe.maxActive=Math.max(videoProbe.maxActive,++videoProbe.active);
  try { const response=await rawFetch(...args);r.status=response.status;return response; }
  catch(e){r.error=e.name;throw e;}
  finally{r.end=performance.now();videoProbe.active--;}
};
const scrub=document.querySelector('#scrub');
const onInput=()=>videoProbe.input.push({at:performance.now(),frame:Number(scrub.value)});
scrub.addEventListener('input',onInput);cleanup.push(()=>scrub.removeEventListener('input',onInput));
[...document.querySelectorAll('.camera video')].forEach((v,i)=>{
  for(const event of ['seeking','seeked']) {
    const listener=()=>videoProbe.events.push({at:performance.now(),event,camera:i,time:v.currentTime});
    v.addEventListener(event,listener);cleanup.push(()=>v.removeEventListener(event,listener));
  }
  if(typeof v.requestVideoFrameCallback==='function') {
    let callback,stopped=false;
    const next=()=>{callback=v.requestVideoFrameCallback((now,m)=>{
      if(stopped)return;videoProbe.samples.push({at:now,camera:i,pts:m.mediaTime});next();
    });};next();
    cleanup.push(()=>{stopped=true;v.cancelVideoFrameCallback(callback);});
  }
});
return true;
})()
"""


def start(dataset="video-fixture", episode=1):
    before = api(f"{dataset}/episodes/{episode}")
    begin = time.monotonic()
    browser("open", ORIGIN + f"/workbench?dataset={dataset}&episode={episode}")
    browser("set", "viewport", 1280, 800)
    browser(
        "wait",
        "--fn",
        'document.querySelector("#frame-state").textContent.includes("已对齐数据帧")',
    )
    opened_ms = (time.monotonic() - begin) * 1000
    evaluate(JS)
    return before, opened_ms


def pixels(result, episode):
    assert len(result["cameras"]) == 3
    assert all(
        c["code"] == result["target"] | (episode << 8) for c in result["cameras"]
    ), result
    assert all(c["frame"] == result["target"] for c in result["cameras"])


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * p
    base = int(position)
    return round(
        values[base]
        + (values[min(base + 1, len(values) - 1)] - values[base]) * (position - base),
        2,
    )


def summary(values):
    return {
        "n": len(values),
        "p50_ms": percentile(values, 0.5),
        "p95_ms": percentile(values, 0.95),
        "max_ms": round(max(values), 2),
    }


def all_camera_feedback(probe, media):
    rows = []
    for inp in probe["input"]:
        delays = []
        for camera, spec in enumerate(media["cameras"]):
            pts = spec["start"] + media["timestamps"][inp["frame"]]
            found = next(
                (
                    sample
                    for sample in probe["samples"]
                    if sample["camera"] == camera
                    and sample["at"] >= inp["at"]
                    and abs(sample["pts"] - pts) < 1e-5
                ),
                None,
            )
            delays.append(found["at"] - inp["at"] if found else None)
        rows.append({"frame": inp["frame"], "camera_delays_ms": delays})
    values = [
        max(row["camera_delays_ms"])
        for row in rows
        if all(d is not None for d in row["camera_delays_ms"])
    ]
    return {
        "inputs": len(rows),
        "all_presented": len(values),
        **(summary(values) if values else {}),
        "raw": rows,
    }


def drag():
    rect = evaluate('document.querySelector("#scrub").getBoundingClientRect().toJSON()')
    y = round(rect["y"] + rect["height"] / 2)
    browser("mouse", "move", round(rect["x"] + 10), y)
    browser("mouse", "down")
    for i in range(1, 25):
        browser(
            "mouse", "move", round(rect["x"] + 10 + (rect["width"] - 20) * i / 25), y
        )
        time.sleep(0.025)
    browser("mouse", "up")
    return evaluate('waitVideoAligned(Number(document.querySelector("#frame").value))')


def reference(dataset, episode, media):
    # This is a separate acceptance read, never the interactive preview path.
    # Compare actual paused video pixels with independent PyAV decoded PNGs.
    return evaluate(f"""(async()=>{{
      const media={json.dumps(media)}, target=Number(document.querySelector('#frame').value);
      return await Promise.all([...document.querySelectorAll('.camera')].map(async(c,i)=>{{
        const v=c.querySelector('video'), a=document.createElement('canvas');
        a.width=v.videoWidth;a.height=v.videoHeight;
        const ctx=a.getContext('2d',{{willReadFrequently:true}});ctx.drawImage(v,0,0);
        const actual=ctx.getImageData(0,0,a.width,a.height).data;
        const response=await fetch('/api/annotation-workbench/{dataset}/episodes/{episode}/frame/'+target+
          '?camera='+encodeURIComponent(media.cameras[i].key)+'&signature='+media.source_signature);
        if(!response.ok)throw Error(await response.text());
        const image=await createImageBitmap(await response.blob());ctx.drawImage(image,0,0);
        const expected=ctx.getImageData(0,0,a.width,a.height).data;
        let error=0,count=0;for(let j=0;j<actual.length;j+=4){{
          error+=Math.abs(actual[j]-expected[j])+Math.abs(actual[j+1]-expected[j+1])+Math.abs(actual[j+2]-expected[j+2]);count+=3;
        }}
        return {{camera:i,frame:target,pts:Number(c.dataset.mediaPts),decoderPts:Number(response.headers.get('X-Media-Pts')),meanAbsoluteRgbError:error/count}};
      }}));
    }})()""")


def native(dataset="video-fixture", episode=1):
    before, opened_ms = start(dataset, episode)
    fixture = dataset == "video-fixture"
    name = "synthetic" if fixture else f"real-episode-{episode}"
    samples = []
    for target in [
        0,
        before["frames"] - 1,
        1,
        before["frames"] // 2,
        before["frames"] // 2 + 1,
        3,
        11,
        2,
    ]:
        result = evaluate(f"videoSeek({target})")
        if fixture:
            pixels(result, episode)
        samples.append(result)
    browser("record", "start", OUT / f"{name}-interaction.webm")
    drag_result = drag()
    if fixture:
        pixels(drag_result, episode)
    # Continuous playback from the first frame, followed by a true pause.
    browser("click", "#first")
    evaluate("waitVideoAligned(0)")
    browser("click", "#play")
    time.sleep(0.9)
    running = evaluate(
        '({frame:Number(document.querySelector("#frame").value),cameras:sampleCameras(),button:document.querySelector("#play").textContent})'
    )
    assert running["frame"] >= 10 and running["button"] == "暂停", running
    browser("click", "#play")
    paused = evaluate(
        'waitVideoAligned(Number(document.querySelector("#frame").value))'
    )
    if fixture:
        pixels(paused, episode)
    steps = evaluate("""(async()=>{const rows=[];for(let n=0;n<12;n++){
      const before=Number(document.querySelector('#frame').value),start=performance.now();
      document.querySelector('#forward').click();const result=await waitVideoAligned(before+1);
      result.ms=performance.now()-start;rows.push(result);}return rows;})()""")
    if fixture:
        for row in steps:
            pixels(row, episode)
    browser("click", "#last")
    last = evaluate(f"waitVideoAligned({before['frames'] - 1})")
    if fixture:
        pixels(last, episode)
    browser("screenshot", OUT / f"{name}-last.png")
    browser("record", "stop")
    probe = evaluate("videoProbe")
    assert not probe["requests"], probe["requests"]
    media = api(f"{dataset}/episodes/{episode}/media")
    rgb = reference(dataset, episode, media)
    assert all(
        abs(r["pts"] - r["decoderPts"]) < 1e-5 and r["meanAbsoluteRgbError"] < 4
        for r in rgb
    ), rgb
    assert api(f"{dataset}/episodes/{episode}")["revision"] == before["revision"]
    # Match emitted frame PTS to actual slider inputs; skipped inputs are not
    # reported as fast successes. Mouse/CLI time is excluded from each delay.
    feedback = []
    for inp in probe["input"]:
        target_pts = media["cameras"][0]["start"] + media["timestamps"][inp["frame"]]
        found = next(
            (
                s
                for s in probe["samples"]
                if s["camera"] == 0
                and s["at"] >= inp["at"]
                and abs(s["pts"] - target_pts) < 1e-5
            ),
            None,
        )
        if found:
            feedback.append(found["at"] - inp["at"])
    result = {
        "case": name,
        "all_camera_drag": all_camera_feedback(probe, media),
        "browser_open_until_aligned_ms": round(opened_ms, 2),
        "random_seek": summary([r["ms"] for r in samples]),
        "seeks": samples,
        "step": summary([r["ms"] for r in steps]),
        "steps": steps,
        "drag_feedback": summary(feedback) if feedback else None,
        "drag_inputs": len(probe["input"]),
        "drag_serviced": len(feedback),
        "drag_release": drag_result,
        "running": running,
        "paused": paused,
        "last": last,
        "interactive_png_requests": len(probe["requests"]),
        "probe": probe,
        "pixel_reference": rgb,
        "source_revision_unchanged": True,
    }
    (OUT / f"{name}-results.json").write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "case",
                    "browser_open_until_aligned_ms",
                    "random_seek",
                    "step",
                    "drag_feedback",
                    "drag_inputs",
                    "drag_serviced",
                    "interactive_png_requests",
                    "pixel_reference",
                )
            }
        ),
        flush=True,
    )


def fallback():
    start()
    evaluate(
        "window.savedVFC=HTMLVideoElement.prototype.requestVideoFrameCallback; HTMLVideoElement.prototype.requestVideoFrameCallback=undefined; true"
    )
    browser("click", '[data-open="2"]')
    browser(
        "wait",
        "--fn",
        'document.querySelectorAll(".camera[data-clock=decoded]").length===3',
    )
    evaluate(JS)
    cold = evaluate("videoSeek(67)")
    hot = evaluate("videoSeek(68).then(()=>videoSeek(67))")
    pixels(cold, 2)
    pixels(hot, 2)
    assert all(c["clock"] == "decoded" and c["image"] for c in hot["cameras"])
    # A new preview immediately reveals native video, rather than the old PNG.
    evaluate(
        "const input=document.querySelector('#scrub');input.value='80';input.dispatchEvent(new Event('input',{bubbles:true}));true"
    )
    assert evaluate(
        "[...document.querySelectorAll('.camera img')].every(i=>i.hidden && getComputedStyle(i).display==='none')"
    )
    evaluate(
        "document.querySelector('#scrub').dispatchEvent(new Event('change',{bubbles:true}));true"
    )
    aligned = evaluate("waitVideoAligned(80)")
    pixels(aligned, 2)
    result = {
        "case": "fallback-without-frame-callback",
        "cold": cold,
        "hot": hot,
        "new_preview_hides_png": True,
        "aligned": aligned,
        "probe": evaluate("videoProbe"),
    }
    (OUT / "fallback-results.json").write_text(json.dumps(result, indent=2))
    evaluate(
        "HTMLVideoElement.prototype.requestVideoFrameCallback=window.savedVFC;true"
    )
    print(
        json.dumps(
            {
                "case": result["case"],
                "cold_ms": cold["ms"],
                "hot_ms": hot["ms"],
                "requests": len(result["probe"]["requests"]),
            }
        ),
        flush=True,
    )


def races():
    start()
    result = evaluate("""(async()=>{
      for(let i=0;i<12;i++){
        document.querySelector('#play').click();
        const input=document.querySelector('#frame');input.value=String(5+i);input.dispatchEvent(new Event('change',{bubbles:true}));
        await new Promise(r=>setTimeout(r,12));
      }
      const landed=await videoSeek(45);
      const select=document.querySelector('#dataset');
      select.value='local-screw-04';select.dispatchEvent(new Event('change',{bubbles:true}));
      await new Promise(r=>setTimeout(r,15));
      select.value='video-fixture';select.dispatchEvent(new Event('change',{bubbles:true}));
      await new Promise(r=>setTimeout(r,600));
      const final=await videoSeek(23);
      return {landed,final,selected:select.value,title:document.querySelector('#episode-title').textContent};
    })()""")
    assert result["selected"] == "video-fixture"
    assert all("/video-fixture/media/" in c["src"] for c in result["final"]["cameras"])
    pixels(result["final"], 1)
    (OUT / "races-results.json").write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                "case": "rapid-play-seek-and-dataset-switch",
                "latest_frame": result["final"]["target"],
                "selected": result["selected"],
            }
        ),
        flush=True,
    )


def editing():
    # Seed only this disposable synthetic source's annotation head.
    dataset = "video-fixture"
    head = api(dataset + "/episodes/0")
    document = head["document"]
    document["templateSnapshot"]["subtasks"] = [
        {"id": "view", "name": "动作", "definition": "仅合成视频回归"}
    ]
    document["attempts"] = [
        {
            "id": key,
            "subtaskId": "view",
            "subtaskName": "动作",
            "outcome": outcome,
            "values": {},
        }
        for key, outcome in (("one", "failure"), ("two", "success"))
    ]
    document["segments"] = [
        {
            "id": key,
            "start": a,
            "end": b,
            "label": "SUBTASK",
            "subtaskId": "view",
            "attemptId": attempt,
        }
        for key, a, b, attempt in (
            ("a", 0, 20, "one"),
            ("b", 20, 40, "one"),
            ("c", 40, 80, "two"),
        )
    ]
    document["frames"] = []
    api(
        dataset + "/episodes/0",
        {
            "revision": head["revision"],
            "source_signature": head["source_signature"],
            "document": document,
            "template_mapping": {},
            "operation": "synthetic-video-editing-fixture",
        },
    )
    start(dataset, 0)

    def saved(predicate):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            current = api(dataset + "/episodes/0")
            if predicate(current):
                return current
            time.sleep(0.1)
        raise AssertionError("Synthetic annotation did not save")

    browser("click", '[data-segment="a"]')
    browser("fill", "#selected-note", "视频修订：输入快捷键保护")
    position = evaluate('document.querySelector("#frame").value')
    browser("press", "ArrowRight")
    assert evaluate('document.querySelector("#frame").value') == position
    saved(
        lambda h: h["document"]["segments"][0].get("note") == "视频修订：输入快捷键保护"
    )
    evaluate(
        "document.querySelector('#timeline').scrollIntoView({block:'center'});true"
    )
    track = evaluate(
        'document.querySelector("#track").getBoundingClientRect().toJSON()'
    )
    edge = evaluate(
        'document.querySelector("[data-segment=a] .edge.end").getBoundingClientRect().toJSON()'
    )
    browser(
        "mouse",
        "move",
        round(edge["x"] + edge["width"] / 2),
        round(edge["y"] + edge["height"] / 2),
    )
    browser("mouse", "down")
    browser(
        "mouse",
        "move",
        round(track["x"] + track["width"] * 24 / 120),
        round(edge["y"] + edge["height"] / 2),
    )
    browser("mouse", "up")
    adjusted = saved(
        lambda h: (
            h["document"]["segments"][0]["end"] == 24
            and h["document"]["segments"][1]["start"] == 24
        )
    )
    assert len(adjusted["document"]["attempts"]) == 2
    browser("click", "#undo")
    saved(lambda h: h["document"]["segments"][0]["end"] == 20)
    browser("click", "#redo")
    saved(lambda h: h["document"]["segments"][0]["end"] == 24)
    browser("click", "#undo")
    before = saved(lambda h: h["document"]["segments"][0]["end"] == 20)
    browser("click", "#range-tool")
    evaluate(
        "document.querySelector('#timeline').scrollIntoView({block:'center'});true"
    )
    track = evaluate(
        'document.querySelector("#track").getBoundingClientRect().toJSON()'
    )
    y = round(track["y"] + track["height"] / 2)
    browser("mouse", "move", round(track["x"] + track["width"] * 90 / 120), y)
    browser("mouse", "down")
    browser("mouse", "move", round(track["x"] + track["width"] * 100 / 120), y)
    browser("mouse", "up")
    assert evaluate('document.querySelector("#range-first").value') == "90"
    assert evaluate('document.querySelector("#range-last").value') == "100"
    ruler = evaluate(
        'document.querySelector("#ruler").getBoundingClientRect().toJSON()'
    )
    y = round(ruler["y"] + ruler["height"] / 2)
    browser("mouse", "move", round(ruler["x"] + ruler["width"] * 30 / 120), y)
    browser("mouse", "down")
    browser("mouse", "move", round(ruler["x"] + ruler["width"] * 70 / 120), y)
    browser("mouse", "up")
    pixels(evaluate("waitVideoAligned(70)"), 0)
    assert api(dataset + "/episodes/0")["document"] == before["document"]
    browser("set", "offline", "on")
    try:
        browser("fill", "#selected-note", "离线视频回归：草稿保留")
        browser("click", '[data-open="1"]')
        browser(
            "wait",
            "--fn",
            'document.querySelector("#episode-title").textContent.includes("001")',
        )
        browser("click", '[data-open="0"]')
        browser(
            "wait",
            "--fn",
            'document.querySelector("#episode-title").textContent.includes("000")',
        )
        assert (
            evaluate('document.querySelector("#selected-note").value')
            == "离线视频回归：草稿保留"
        )
    finally:
        browser("set", "offline", "off")
    saved(
        lambda h: h["document"]["segments"][0].get("note") == "离线视频回归：草稿保留"
    )
    result = {
        "case": "editing-preserved",
        "input_shortcuts": True,
        "shared_boundary": True,
        "undo_redo": True,
        "range_and_ruler_gestures": True,
        "browsing_does_not_modify_annotation": True,
        "offline_cross_episode_and_resync": True,
        "attempt_ids": [a["id"] for a in adjusted["document"]["attempts"]],
    }
    (OUT / "editing-results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


def showcase():
    for dataset, episode, name in (
        ("video-fixture", 1, "synthetic"),
        ("local-screw-04", 0, "real-episode-0"),
        ("local-screw-04", 1, "real-episode-1"),
    ):
        before, _ = start(dataset, episode)
        browser("click", "#first")
        evaluate("waitVideoAligned(0)")
        browser("record", "start", OUT / f"{name}-clean.webm")
        browser("click", "#play")
        time.sleep(1.1)
        browser("click", "#play")
        evaluate('waitVideoAligned(Number(document.querySelector("#frame").value))')
        drag()
        for _ in range(4):
            previous = evaluate('Number(document.querySelector("#frame").value)')
            browser("click", "#back")
            evaluate(f"waitVideoAligned({previous - 1})")
        browser("click", "#last")
        last = evaluate(f"waitVideoAligned({before['frames'] - 1})")
        if dataset == "video-fixture":
            pixels(last, episode)
        time.sleep(0.4)
        browser("record", "stop")
        browser("screenshot", OUT / f"{name}-clean.png")
        print(
            json.dumps(
                {
                    "case": "clean-recording",
                    "source": name,
                    "cameras": len(last["cameras"]),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case",
        choices=["native", "real", "fallback", "races", "showcase", "editing"],
        required=True,
    )
    parser.add_argument("--origin", default=ORIGIN)
    args = parser.parse_args()
    ORIGIN = args.origin.rstrip("/")
    OUT.mkdir(parents=True, exist_ok=True)
    if args.case == "native":
        native()
    elif args.case == "real":
        native("local-screw-04", 0)
        native("local-screw-04", 1)
    elif args.case == "fallback":
        fallback()
    elif args.case == "races":
        races()
    elif args.case == "editing":
        editing()
    else:
        showcase()
