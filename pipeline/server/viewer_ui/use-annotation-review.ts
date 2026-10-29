"use client";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import type { LanguageAtom } from "../types/language.types";
import {
  reviewAtoms,
  reviewPayload,
  reviewRequest,
  reviewUrl,
  type Review,
} from "./annotation-review-client";
import {
  frameTime,
  nearestFrame,
  snapshotKey,
  schemeDefaults,
  type CustomState,
  type Scheme,
  type EpisodeSummary,
} from "./custom-annotation-model";
export interface Workspace extends Review, CustomState {
  episode_uid: string;
  scheme: Scheme | null;
  repo: string;
  episode: number;
}
interface Summary {
  revision: number;
  source_signature: string;
  scheme: Scheme | null;
  episodes: EpisodeSummary[];
}
const empty = (): CustomState => ({
  values: {},
  records: [],
  status: "not_started",
});
const pack = (atoms: LanguageAtom[], custom: CustomState) =>
  snapshotKey({ atoms, ...custom });
const base = (repo: string) =>
  `/api/manual-annotations/${encodeURIComponent(repo.slice(3))}`;
interface Backup {
  atoms: LanguageAtom[];
  custom: CustomState;
  saved: string;
  review: Workspace;
}
export function useAnnotationReview(
  atoms: LanguageAtom[],
  setAtoms: Dispatch<SetStateAction<LanguageAtom[]>>,
  clearSelection: Dispatch<SetStateAction<number | null>>,
) {
  const [review, setReview] = useState<Workspace | null>(null),
    [custom, setCustomState] = useState<CustomState>(empty);
  const [loading, setLoading] = useState(false),
    [saving, setSaving] = useState(false),
    [error, setError] = useState(""),
    [saved, setSaved] = useState("");
  const generation = useRef(0),
    identity = useRef({ repo: "", episode: -1 }),
    cache = useRef(new Map<string, Backup>()),
    pending = useRef(new Map<string, Promise<void>>());
  const exportFilters = useRef<Record<string, string>>({});
  useEffect(() => {
    const receive = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (detail?.repo === identity.current.repo)
        exportFilters.current = detail.filters || {};
    };
    window.addEventListener("bp-annotation-filter", receive);
    return () => window.removeEventListener("bp-annotation-filter", receive);
  }, []);
  const dirty = !!review && pack(atoms, custom) !== saved;
  const current = useRef({ atoms, custom, saved, review });
  current.current = { atoms, custom, saved, review };
  const setCustom = (next: CustomState | ((v: CustomState) => CustomState)) =>
    setCustomState((old) => {
      const value = typeof next === "function" ? next(old) : next;
      return { ...value, status: "draft" };
    });
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  useEffect(() => {
    if (!review) return;
    try {
      const key = `bp-workbench-unsaved:v2:${review.repo}:${review.episode}`;
      if (dirty)
        sessionStorage.setItem(
          key,
          JSON.stringify({
            atoms,
            custom,
            revision: review.revision,
            source_signature: review.source_signature,
          }),
        );
      else sessionStorage.removeItem(key);
    } catch {
      /* Browser backup is optional; server remains authoritative. */
    }
  }, [atoms, custom, review, dirty]);
  useEffect(() => {
    if (
      review &&
      custom.status === "completed" &&
      pack(atoms, custom) !== saved
    )
      setCustomState((old) => ({ ...old, status: "draft" }));
  }, [atoms, custom, review, saved]);
  const load = useCallback(
    (repo: string, episode: number) => {
      const head = current.current,
        previous = identity.current;
      if (head.review && pack(head.atoms, head.custom) !== head.saved)
        cache.current.set(`${previous.repo}:${previous.episode}`, {
          atoms: head.atoms,
          custom: head.custom,
          saved: head.saved,
          review: head.review,
        });
      identity.current = { repo, episode };
      const request = ++generation.current;
      setReview(null);
      setCustomState(empty());
      setError("");
      setSaving(false);
      setSaved("");
      if (!repo.startsWith("bp/")) {
        setLoading(false);
        return;
      }
      setAtoms([]);
      clearSelection(null);
      setLoading(true);
      const key = `${repo}:${episode}`;
      Promise.resolve(pending.current.get(key))
        .then(() => reviewRequest<Summary>(base(repo)))
        .then(async (summary) => {
          const entry = summary.episodes.find(
            (e) => e.episode_index === episode,
          );
          if (!entry)
            throw new Error("Episode 不存在或已删除，请重新打开数据集");
          const remote = await reviewRequest<Workspace>(
            `${base(repo)}/episodes/${encodeURIComponent(entry.episode_uid)}`,
          );
          if (request !== generation.current) return;
          const initial = reviewAtoms(remote),
            defaults = schemeDefaults(remote.scheme ?? summary.scheme);
          for (const [id, values] of Object.entries(remote.values || {}))
            defaults[id] = { ...(defaults[id] || {}), ...values };
          const state: CustomState = {
            values: defaults,
            records: remote.records || [],
            status: remote.status || "not_started",
          };
          let backup = cache.current.get(key);
          try {
            const raw = sessionStorage.getItem(
                `bp-workbench-unsaved:v2:${key}`,
              ),
              local = raw ? JSON.parse(raw) : null;
            if (
              !backup &&
              local?.revision === remote.revision &&
              local?.source_signature === remote.source_signature &&
              Array.isArray(local.atoms) &&
              local.custom &&
              window.confirm("恢复本窗口尚未保存的标注？")
            )
              backup = {
                atoms: local.atoms,
                custom: local.custom,
                saved: pack(initial, state),
                review: remote,
              };
            // Import the old session-only text atoms explicitly; never guess old Subtask boundaries.
            const oldRaw = sessionStorage.getItem(
                `lerobot-annotations:v2:${repo}::${episode}`,
              ),
              old = oldRaw ? JSON.parse(oldRaw) : null;
            const manual = Array.isArray(old)
              ? old.filter((a) => a.style !== "subtask")
              : [];
            if (
              !backup &&
              !remote.atoms?.length &&
              manual.length &&
              window.confirm("导入原编辑器尚未保存的非 Subtask 文字标注？")
            )
              backup = {
                atoms: [...initial, ...manual],
                custom: state,
                saved: pack(initial, state),
                review: remote,
              };
          } catch {
            /* Bad local backups cannot replace server data. */
          }
          if (
            backup &&
            backup.review.revision === remote.revision &&
            backup.review.source_signature === remote.source_signature
          ) {
            setAtoms(backup.atoms);
            setCustomState(backup.custom);
            setSaved(backup.saved);
          } else {
            setAtoms(initial);
            setCustomState(state);
            setSaved(pack(initial, state));
            if (backup) setError("服务端版本已变化，旧窗口草稿未覆盖新版本。");
          }
          setReview({
            ...remote,
            scheme: remote.scheme ?? summary.scheme,
            repo,
            episode,
          });
        })
        .catch((e) => {
          if (request === generation.current)
            setError(e instanceof Error ? e.message : String(e));
        })
        .finally(() => {
          if (request === generation.current) setLoading(false);
        });
    },
    [setAtoms, clearSelection],
  );
  const save = async (status?: string) => {
    if (!review || loading)
      return { ok: false, error: error || "标注尚未加载完成" };
    const key = `${review.repo}:${review.episode}`;
    if (pending.current.has(key))
      return { ok: false, error: "正在保存，请稍候" };
    let release!: () => void;
    pending.current.set(
      key,
      new Promise<void>((resolve) => {
        release = resolve;
      }),
    );
    const request = generation.current,
      sentCustom = { ...custom, status: status || "draft" },
      snapshot = pack(atoms, sentCustom),
      before = pack(atoms, custom);
    setSaving(true);
    setError("");
    try {
      const body = { ...reviewPayload(review, atoms), ...sentCustom };
      const result = await reviewRequest<Workspace>(
        `${base(review.repo)}/episodes/${encodeURIComponent(review.episode_uid)}`,
        body,
      );
      if (request !== generation.current) {
        const backup = cache.current.get(key);
        if (backup) {
          const unchanged = pack(backup.atoms, backup.custom) === before,
            updated = {
              ...backup,
              saved: snapshot,
              custom: unchanged ? sentCustom : backup.custom,
              review: { ...backup.review, revision: result.revision },
            };
          cache.current.set(key, updated);
          try {
            const storageKey = `bp-workbench-unsaved:v2:${key}`;
            if (pack(updated.atoms, updated.custom) !== snapshot)
              sessionStorage.setItem(
                storageKey,
                JSON.stringify({
                  atoms: updated.atoms,
                  custom: updated.custom,
                  revision: result.revision,
                  source_signature: review.source_signature,
                }),
              );
            else sessionStorage.removeItem(storageKey);
          } catch {}
        }
        return { ok: true };
      }
      setReview((old) => (old ? { ...old, revision: result.revision } : old));
      setSaved(snapshot);
      if (pack(current.current.atoms, current.current.custom) === before)
        setCustomState(sentCustom);
      cache.current.delete(key);
      window.dispatchEvent(
        new CustomEvent("bp-annotations-saved", {
          detail: { repo: review.repo },
        }),
      );
      return {
        ok: true,
        path: `Server 标注 v${result.revision}（源数据不变）`,
      };
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      if (request === generation.current) setError(message);
      return { ok: false, error: message };
    } finally {
      pending.current.delete(key);
      release();
      if (request === generation.current) setSaving(false);
    }
  };
  const bind = async (scheme_id: string, version: number) => {
    if (!review) throw new Error("尚未加载");
    if (dirty) throw new Error("请先保存或撤销修改，再应用方案");
    await reviewRequest(`${base(review.repo)}/scheme`, {
      scheme_id,
      version,
      revision: review.revision,
      source_signature: review.source_signature,
    });
    load(review.repo, review.episode);
    window.dispatchEvent(
      new CustomEvent("bp-annotations-saved", {
        detail: { repo: review.repo },
      }),
    );
  };
  const exportPackage = async (include_drafts: boolean) => {
    if (!review || dirty) throw new Error("请先保存 Episode");
    const job = await reviewRequest<{ id: string }>(
      `${base(review.repo)}/exports`,
      {
        revision: review.revision,
        source_signature: review.source_signature,
        include_drafts,
        filters: exportFilters.current,
      },
    );
    for (let i = 0; i < 720; i++) {
      const result = await reviewRequest<{ state: string; error?: string }>(
        `${base(review.repo)}/exports/${job.id}`,
      );
      if (result.state === "COMPLETED") {
        window.location.assign(
          `${base(review.repo)}/exports/${job.id}/download`,
        );
        return;
      }
      if (!["QUEUED", "RUNNING"].includes(result.state))
        throw new Error(result.error || result.state);
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error("导出仍在后台运行");
  };
  const exportDataset = async () => {
    if (!review || dirty) throw new Error("请先保存 Episode");
    const url = reviewUrl(review.repo),
      job = await reviewRequest<{ id: string }>(`${url}/exports`, {
        revision: review.revision,
        source_signature: review.source_signature,
        name: null,
      });
    for (let i = 0; i < 720; i++) {
      const result = await reviewRequest<{
        state: string;
        error?: string;
        dataset_name: string;
      }>(`${url}/exports/${job.id}`);
      if (result.state === "CONVERTED")
        return {
          output_dir: result.dataset_name,
          persistent_rows: atoms.length,
          event_rows: custom.records.length,
        };
      if (!["QUEUED", "RUNNING"].includes(result.state))
        throw new Error(result.error || result.state);
      await new Promise((r) => setTimeout(r, 2500));
    }
    throw new Error("导出仍在后台运行");
  };
  const normalize = (atom: LanguageAtom): LanguageAtom => {
    if (!review || atom.style !== "subtask") return atom;
    const start = nearestFrame(review, atom.timestamp),
      end = Math.max(
        start + 1,
        nearestFrame(
          review,
          atom.end_timestamp ?? frameTime(review, start + 1),
          true,
        ),
      );
    return {
      ...atom,
      timestamp: frameTime(review, start),
      end_timestamp: frameTime(review, Math.min(review.frames, end)),
    };
  };
  return {
    load,
    save,
    bind,
    exportPackage,
    exportDataset,
    normalize,
    loading,
    saving,
    error,
    review,
    custom,
    setCustom,
    ready: !!review && !loading,
    dirty,
    undo: () => {
      if (
        review &&
        window.confirm("撤销本窗口未保存修改，重新读取服务器版本？")
      ) {
        cache.current.delete(`${review.repo}:${review.episode}`);
        try {
          sessionStorage.removeItem(
            `bp-workbench-unsaved:v2:${review.repo}:${review.episode}`,
          );
        } catch {}
        setSaved(pack(atoms, custom));
        current.current = { ...current.current, saved: pack(atoms, custom) };
        load(review.repo, review.episode);
      }
    },
  };
}
