"use client";

import React, { useEffect, useRef, useState } from "react";
import { useTime } from "../context/time-context";
import { gapAt, merge, ordered, split, validate, type EpisodeDraft, type Segment } from "../utils/subtask-review-model";
import "./subtask-review.css";

interface ExportJob { id: string; state: string; stage: string; dataset_name: string; error?: string }

async function api<T>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `请求失败 (${res.status})`);
  return data as T;
}

/** Lives inside the existing annotation tab, beside its existing video player. */
export function SubtaskReview({ repoId, episodeId }: { repoId: string; episodeId: number }) {
  const base = `/api/subtask-review/${encodeURIComponent(repoId.slice(3))}`;
  const url = `${base}/episodes/${episodeId}`;
  const pendingKey = `bp-subtask-unsaved:v1:${repoId}:${episodeId}`;
  const { currentTime, seek, setIsPlaying } = useTime();
  const [data, setData] = useState<EpisodeDraft | null>(null);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [saved, setSaved] = useState("[]");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [name, setName] = useState("");
  const [job, setJob] = useState<ExportJob | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const alive = useRef(true);
  const track = useRef<HTMLDivElement>(null);
  const dirty = !!data && JSON.stringify(segments) !== saved;
  const invalid = data ? validate(segments, data.frames) : null;
  const frame = data ? Math.max(0, Math.min(data.frames - 1, Math.round(currentTime * data.fps))) : 0;

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  useEffect(() => {
    const controller = { cancelled: false };
    setData(null); setError(""); setNotice(""); setSelected(null);
    api<EpisodeDraft>(url).then(remote => {
      if (controller.cancelled) return;
      setData(remote); setSaved(JSON.stringify(remote.segments));
      let initial = remote.segments;
      try {
        const raw = sessionStorage.getItem(pendingKey);
        const pending = raw ? JSON.parse(raw) : null;
        if (pending && pending.revision === remote.revision && pending.source_signature === remote.source_signature &&
            JSON.stringify(pending.segments) !== JSON.stringify(remote.segments) &&
            Array.isArray(pending.segments) && !validate(pending.segments, remote.frames) &&
            window.confirm("发现本窗口尚未保存的修改，是否恢复？恢复后仍需点击“保存服务端草稿”。")) {
          initial = pending.segments;
        }
        sessionStorage.removeItem(pendingKey);
      } catch { /* Server remains authoritative if local storage is unavailable. */ }
      setSegments(initial);
      try {
        const exportId = sessionStorage.getItem(`bp-subtask-export:${repoId}`);
        if (exportId) api<ExportJob>(`${base}/exports/${encodeURIComponent(exportId)}`)
          .then(result => { if (!controller.cancelled) setJob(result); }).catch(() => {});
      } catch { /* Export polling is optional; published datasets remain registered. */ }
    }).catch(e => { if (!controller.cancelled) setError(String(e.message || e)); });
    return () => { controller.cancelled = true; };
  }, [url, pendingKey, reload, base, repoId]);

  useEffect(() => {
    if (!data) return;
    try {
      if (dirty) sessionStorage.setItem(pendingKey, JSON.stringify({ revision: data.revision, source_signature: data.source_signature, segments }));
      else sessionStorage.removeItem(pendingKey);
    } catch { /* Unsaved backup only; never pretend this is a server save. */ }
  }, [data, dirty, segments, pendingKey]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    if (!job || !["QUEUED", "RUNNING"].includes(job.state)) return;
    const timer = window.setTimeout(() => {
      api<ExportJob>(`${base}/exports/${encodeURIComponent(job.id)}`)
        .then(result => { if (alive.current) { setJob(result); setError(""); } })
        .catch(e => { if (alive.current) { setError(e.message); setJob({ ...job }); } });
    }, 2500);
    return () => window.clearTimeout(timer);
  }, [job, base]);

  async function save() {
    if (!data || invalid || busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api<{ revision: number; segments: Segment[] }>(url, {
        revision: data.revision, source_signature: data.source_signature, segments,
      });
      if (!alive.current) return;
      setData({ ...data, revision: result.revision, has_draft: true, segments: result.segments });
      setSegments(result.segments); setSaved(JSON.stringify(result.segments)); setSelected(null);
      setNotice(`已保存到 Server（草稿版本 ${result.revision}），原数据集未修改。`);
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : String(e)); }
    finally { if (alive.current) setBusy(false); }
  }

  async function exportRevision() {
    if (!data || dirty || busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api<ExportJob>(`${base}/exports`, {
        revision: data.revision, source_signature: data.source_signature, name: name.trim() || null,
      });
      if (alive.current) {
        setJob(result);
        try { sessionStorage.setItem(`bp-subtask-export:${repoId}`, result.id); } catch { /* Optional. */ }
      }
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : String(e)); }
    finally { if (alive.current) setBusy(false); }
  }

  function update(index: number, patch: Partial<Segment>) {
    setNotice(""); setSegments(rows => rows.map((s, i) => i === index ? { ...s, ...patch } : s));
  }

  function jump(index: number) {
    setSelected(index); seek(segments[index].start_frame / data!.fps, "external"); setIsPlaying(false);
  }

  function dragEdge(e: React.PointerEvent<HTMLButtonElement>, index: number, side: "start_frame" | "end_frame") {
    if (!data || busy) return;
    e.preventDefault(); e.stopPropagation(); setSelected(index); setIsPlaying(false);
    const el = e.currentTarget; el.setPointerCapture(e.pointerId);
    const initial = segments[index];
    const others = segments.filter((_, i) => i !== index);
    const lower = Math.max(0, ...others.filter(s => s.end_frame <= initial.start_frame).map(s => s.end_frame));
    const upper = Math.min(data.frames, ...others.filter(s => s.start_frame >= initial.end_frame).map(s => s.start_frame));
    const move = (event: PointerEvent) => {
      const rect = track.current?.getBoundingClientRect();
      if (!rect) return;
      const f = Math.round((event.clientX - rect.left) / rect.width * data.frames);
      const value = side === "start_frame" ? Math.max(lower, Math.min(initial.end_frame - 1, f)) : Math.min(upper, Math.max(initial.start_frame + 1, f));
      update(index, { [side]: value });
    };
    const finish = () => {
      el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", finish); el.removeEventListener("pointercancel", finish);
    };
    el.addEventListener("pointermove", move); el.addEventListener("pointerup", finish); el.addEventListener("pointercancel", finish);
  }

  const coverage = segments.reduce((n, s) => n + s.end_frame - s.start_frame, 0);
  return <section className="bp-subtask" aria-label="Subtask 标注编辑器">
    <header><h2>Subtask 标注</h2><span>{data ? `Episode ${episodeId} · ${data.frames} 帧 · ${data.fps} FPS · 草稿版本 ${data.revision}` : "正在读取逐帧 Subtask…"}</span></header>
    <p>区间按 [开始帧, 结束帧) 编辑。空白处为未标注（-1）；修改仅影响选中区间。保存草稿不会改写原数据集。</p>
    {error && <div role="alert" className="bp-subtask-error">{error}<button disabled={busy} onClick={() => {
      if (!dirty || window.confirm("重新加载将丢弃本窗口未保存的修改，是否继续？")) {
        try { sessionStorage.removeItem(pendingKey); } catch { /* Optional local backup. */ }
        setReload(n => n + 1);
      }
    }}>重新加载服务端版本</button></div>}
    {notice && <div role="status">{notice}</div>}
    {data && <>
      <div className="bp-subtask-summary">{segments.length} 段 · {invalid ? "区间待修正" : `已覆盖 ${coverage}/${data.frames} 帧，未标注 ${data.frames - coverage} 帧`} · 当前帧 {frame} · {dirty ? "有未保存修改" : data.has_draft ? "已保存的服务端草稿" : "原数据集标注"}</div>
      <div className="bp-subtask-track" ref={track} aria-label="Subtask 区间时间线">
        {segments.map((s, i) => <div key={i} title={`${s.text} [${s.start_frame}, ${s.end_frame})`} className={`bp-subtask-span ${selected === i ? "selected" : ""}`} style={{ left: `${100 * s.start_frame / data.frames}%`, width: `${100 * Math.max(0, s.end_frame - s.start_frame) / data.frames}%` }}>
          <button aria-label={`拖动第 ${i + 1} 段开始帧`} className="bp-subtask-edge" disabled={busy || !!invalid} onPointerDown={e => dragEdge(e, i, "start_frame")} />
          <button className="bp-subtask-label" onClick={() => jump(i)}>{s.text}</button>
          <button aria-label={`拖动第 ${i + 1} 段结束帧`} className="bp-subtask-edge" disabled={busy || !!invalid} onPointerDown={e => dragEdge(e, i, "end_frame")} />
        </div>)}
        <div className="bp-subtask-playhead" style={{ left: `${100 * frame / data.frames}%` }} />
      </div>
      <div className="bp-subtask-actions">
        <button disabled={busy} onClick={() => {
          const gap = gapAt(segments, frame, data.frames);
          if (!gap) { setError("当前帧已有标注，请移动到未标注帧；或先拆分、删除区间。"); return; }
          setSegments(ordered([...segments, { start_frame: gap[0], end_frame: gap[1], text: "新子任务" }])); setSelected(null); setError("");
        }}>在当前帧新增区间</button>
        <button disabled={busy || selected === null || !!invalid} onClick={() => {
          const next = split(segments, selected!, frame);
          if (next === segments) setError("请把播放位置移动到选中区间内部，再拆分。");
          else { setSegments(next); setSelected(null); setError(""); }
        }}>在当前帧拆分所选段</button>
        <button disabled={busy || selected === null || !!invalid} onClick={() => {
          const rows = ordered(segments), index = rows.indexOf(segments[selected!]);
          const next = merge(rows, index);
          if (next === rows) setError("只能与紧邻、无间隙且文本相同的下一段合并。可先修改该段文本。");
          else { setSegments(next); setSelected(null); setError(""); }
        }}>与下一段合并</button>
      </div>
      {invalid && <div role="alert" className="bp-subtask-error">{invalid}</div>}
      <fieldset disabled={busy} className="bp-subtask-fields"><legend>逐段编辑（结束帧不包含在区间内）</legend>
        {segments.length === 0 && <p>没有标注区间。保存后整个 Episode 将保持未标注，不会恢复原有区间。</p>}
        {segments.map((s, i) => <div key={i} className={`bp-subtask-row ${selected === i ? "selected" : ""}`}>
          <button onClick={() => jump(i)} aria-label={`跳转第 ${i + 1} 段`}>{i + 1} ▶</button>
          <label>开始帧<input type="number" min="0" max={data.frames - 1} step="1" value={Number.isFinite(s.start_frame) ? s.start_frame : ""} onFocus={() => setSelected(i)} onChange={e => update(i, { start_frame: e.target.value === "" ? NaN : Number(e.target.value) })} /></label>
          <label>结束帧<input type="number" min="1" max={data.frames} step="1" value={Number.isFinite(s.end_frame) ? s.end_frame : ""} onFocus={() => setSelected(i)} onChange={e => update(i, { end_frame: e.target.value === "" ? NaN : Number(e.target.value) })} /></label>
          <label className="bp-subtask-text">文本<textarea rows={2} maxLength={400} value={s.text} onFocus={() => setSelected(i)} onChange={e => update(i, { text: e.target.value })} /></label>
          <span>{(s.start_frame / data.fps).toFixed(3)}–{(s.end_frame / data.fps).toFixed(3)}s</span>
          <button onClick={() => { setSegments(rows => rows.filter((_, j) => j !== i)); setSelected(null); }}>删除</button>
        </div>)}
      </fieldset>
      <div className="bp-subtask-actions">
        <button disabled={busy || !!invalid || !dirty} onClick={save}>{busy ? "处理中…" : "保存服务端草稿"}</button>
        <label>修订数据集名称（可选）<input value={name} maxLength={2000} disabled={busy} onChange={e => setName(e.target.value)} placeholder="默认：原名称-revised" /></label>
        <button disabled={busy || dirty || !!invalid || ["QUEUED", "RUNNING"].includes(job?.state || "")} onClick={exportRevision}>生成独立修订数据集</button>
      </div>
      <p>导出使用所有 Episode 的最新已保存草稿，其他 Episode 保留来源标注。请先保存修改；无需模型或 API Key。</p>
      {job && <div role="status">修订导出：{job.dataset_name} · {job.state} · {job.stage}{job.error && <p className="bp-subtask-error">{job.error}</p>}{job.state === "CONVERTED" && <p>修订数据集已通过深度验证，可从 Server 的数据集管理中打开。原数据集仍保留。</p>}</div>}
    </>}
  </section>;
}
