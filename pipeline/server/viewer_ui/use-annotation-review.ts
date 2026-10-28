"use client";

import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import type { LanguageAtom } from "../types/language.types";
import { reviewAtoms, reviewPayload, reviewRequest, reviewUrl, type Review } from "../utils/annotation-review-client";

/** Data bridge only: the original composer, inspector, timeline and VQA stay mounted. */
export function useAnnotationReview(
  atoms: LanguageAtom[], setAtoms: Dispatch<SetStateAction<LanguageAtom[]>>,
  clearSelection: Dispatch<SetStateAction<number | null>>,
) {
  const [review, setReview] = useState<(Review & { repo: string; episode: number }) | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("[]");
  const dirty = !!review && JSON.stringify(atoms) !== saved;
  const generation = useRef(0);
  const pendingSaves = useRef(new Map<string, Promise<void>>());
  const identity = useRef({ repo: "", episode: -1 });
  const cache = useRef(new Map<string, { atoms: LanguageAtom[]; saved: string; review: Review }>());
  const current = useRef({ atoms, saved, review });
  current.current = { atoms, saved, review };
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  useEffect(() => {
    if (!review) return;
    const { repo, episode } = review;
    try {
      const key = `bp-workbench-unsaved:v1:${repo}:${episode}`;
      if (dirty) sessionStorage.setItem(key, JSON.stringify({ atoms, revision: review.revision, source_signature: review.source_signature }));
      else sessionStorage.removeItem(key);
    } catch { /* Server drafts remain authoritative. */ }
  }, [atoms, review, dirty]);

  const load = useCallback((repo: string, episode: number) => {
    const previous = identity.current;
    const head = current.current;
    if (previous.repo && head.review?.repo === previous.repo && head.review?.episode === previous.episode && JSON.stringify(head.atoms) !== head.saved)
      cache.current.set(`${previous.repo}:${previous.episode}`, { atoms: head.atoms, saved: head.saved, review: head.review });
    identity.current = { repo, episode };
    const request = ++generation.current;
    setReview(null); setError(""); setSaving(false); setSaved("[]");
    if (!repo.startsWith("bp/")) { setLoading(false); return; }
    setAtoms([]); clearSelection(null); setLoading(true);
    Promise.resolve(pendingSaves.current.get(`${repo}:${episode}`))
      .then(() => reviewRequest<Review>(reviewUrl(repo, episode))).then(remote => {
      if (request !== generation.current) return;
      const initial = reviewAtoms(remote);
      let pending = cache.current.get(`${repo}:${episode}`);
      try {
        const raw = sessionStorage.getItem(`bp-workbench-unsaved:v1:${repo}:${episode}`);
        const local = raw ? JSON.parse(raw) : null;
        if (!pending && local?.revision === remote.revision && local?.source_signature === remote.source_signature && Array.isArray(local.atoms) &&
            window.confirm("恢复本窗口尚未保存的标注？恢复后仍需点击“保存 Episode”。"))
          pending = { atoms: local.atoms, review: remote, saved: JSON.stringify(initial) };
        // Preserve manual work left by the original session-only editor, without
        // applying its implicit Subtask spans over the AI/server intervals.
        const legacyRaw = sessionStorage.getItem(`lerobot-annotations:v2:${repo}::${episode}`);
        const legacy = legacyRaw ? JSON.parse(legacyRaw) : null;
        const manual = Array.isArray(legacy) ? legacy.filter(a => a.style !== "subtask") : [];
        if (!pending && !remote.atoms?.length && manual.length && window.confirm("发现原编辑器的本窗口手动标注，是否合并非 Subtask 标注？旧 Subtask 备份保留，不会覆盖 AI/服务端区间。"))
          pending = { atoms: [...initial, ...manual], review: remote, saved: JSON.stringify(initial) };
      } catch { /* Invalid or unavailable browser storage is not authoritative. */ }
      if (pending && pending.review.revision === remote.revision && pending.review.source_signature === remote.source_signature) {
        setAtoms(pending.atoms); setSaved(pending.saved);
      } else {
        setAtoms(initial); setSaved(JSON.stringify(initial));
        if (pending) setError("此 Episode 的服务端版本已变化；未保存的旧窗口内容未套用。请核对后再编辑。");
      }
      setReview({ ...remote, repo, episode });
    }).catch(e => { if (request === generation.current) setError(e.message); })
      .finally(() => { if (request === generation.current) setLoading(false); });
  }, [setAtoms, clearSelection]);

  const save = async () => {
    if (!review || loading) return { ok: false, error: error || "标注尚未加载完成" };
    const { repo, episode } = identity.current;
    const key = `${repo}:${episode}`;
    if (pendingSaves.current.has(key)) return { ok: false, error: "正在保存，请稍候" };
    let release!: () => void;
    pendingSaves.current.set(key, new Promise<void>(resolve => { release = resolve; }));
    const request = generation.current;
    const snapshot = JSON.stringify(atoms);
    setSaving(true); setError("");
    try {
      const body = reviewPayload(review, atoms);
      const result = await reviewRequest<{ revision: number }>(reviewUrl(repo, episode), body);
      if (request !== generation.current) {
        const pending = cache.current.get(key);
        if (pending) cache.current.set(key, { ...pending, saved: snapshot, review: { ...review, revision: result.revision } });
        // Reconcile the off-screen backup too, so returning to this Episode
        // neither loses edits made during save nor reapplies an obsolete version.
        try {
          const storageKey = `bp-workbench-unsaved:v1:${key}`;
          if (pending && JSON.stringify(pending.atoms) !== snapshot)
            sessionStorage.setItem(storageKey, JSON.stringify({ atoms: pending.atoms, revision: result.revision, source_signature: review.source_signature }));
          else sessionStorage.removeItem(storageKey);
        } catch { /* Browser backup is optional. */ }
        return { ok: true };
      }
      setReview({ ...review, revision: result.revision }); setSaved(snapshot);
      cache.current.delete(`${identity.current.repo}:${identity.current.episode}`);
      return { ok: true, path: `Server 草稿 v${result.revision}（原数据集不变）` };
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      if (request === generation.current) setError(message);
      return { ok: false, error: message };
    } finally {
      pendingSaves.current.delete(key); release();
      if (request === generation.current) setSaving(false);
    }
  };

  const exportDataset = async () => {
    if (!review || loading) throw new Error(error || "标注尚未加载完成");
    if (JSON.stringify(atoms) !== saved) throw new Error("请先保存 Episode 标注，再生成修订数据集");
    const base = reviewUrl(identity.current.repo);
    const request = generation.current;
    const job = await reviewRequest<{ id: string }>(`${base}/exports`, {
      revision: review.revision, source_signature: review.source_signature, name: null });
    for (let i = 0; i < 720; i++) {
      const status = await reviewRequest<{ state: string; error?: string; dataset_name: string }>(`${base}/exports/${job.id}`);
      if (status.state === "CONVERTED") return { output_dir: status.dataset_name, persistent_rows: atoms.filter(a => a.style !== "vqa" && a.style !== "interjection").length, event_rows: atoms.filter(a => a.style === "vqa" || a.style === "interjection").length };
      if (!["QUEUED", "RUNNING"].includes(status.state)) throw new Error(status.error || `修订导出 ${status.state}`);
      if (request !== generation.current) throw new Error("已切换 Episode；修订导出仍在后台执行，请在工作台查看");
      await new Promise(resolve => setTimeout(resolve, 2500));
    }
    throw new Error("导出仍在后台执行，请在工作台任务列表查看");
  };

  const normalize = (atom: LanguageAtom): LanguageAtom => {
    if (!review || atom.style !== "subtask") return atom;
    const timestamp = Math.max(0, Math.min(review.frames - 1, Math.round(atom.timestamp * review.fps))) / review.fps;
    const next = atoms.filter(a => a.style === "subtask" && a.timestamp > timestamp).sort((a,b) => a.timestamp-b.timestamp)[0];
    const end = atom.end_timestamp ?? next?.timestamp ?? review.frames / review.fps;
    return { ...atom, timestamp, end_timestamp: Math.min(review.frames, Math.max(Math.round(timestamp * review.fps) + 1, Math.round(end * review.fps))) / review.fps };
  };
  return { load, save, exportDataset, normalize, loading, saving, error, review,
    ready: !!review && !loading, dirty };
}
