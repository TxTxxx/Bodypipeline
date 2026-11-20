"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  appliedInstructionOutcome,
  mergeSavedEpisodeDraft,
  renderInstruction,
  selectionsForTemplate,
  type InstructionTemplate,
  type SavedEpisodeDraft,
} from "@/utils/instruction-model";

interface InstructionHead {
  revision: number;
  source_signature: string;
  template: InstructionTemplate;
  warnings: unknown[];
}

interface EpisodeInstruction {
  episode_index: number;
  frames: number;
  current_instruction: string | null;
  revision: number;
  source_signature: string;
  has_draft: boolean;
  selections: Record<string, string>;
  rendered: string | null;
  valid: boolean;
  error: string | null;
}

interface ApplyStatus {
  id: string;
  state: "QUEUED" | "RUNNING" | "CONVERTED" | "FAILED";
  stage: string;
  progress: string;
  report?: {
    updated_episodes: number[];
    noop: boolean;
  };
  error?: string;
}

interface Props {
  repoId: string;
  episodeId: number;
  currentInstruction?: string | null;
}

async function request<T>(
  url: string,
  method: "GET" | "PUT" | "POST" = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(url, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(
      typeof payload.detail === "string"
        ? payload.detail
        : `请求失败 (${response.status})`,
    );
  }
  return payload as T;
}

export function InlineInstructionEditor({
  repoId,
  episodeId,
  currentInstruction,
}: Props) {
  const datasetId = repoId.startsWith("bp/") ? repoId.slice(3) : "";
  const base = `/api/instructions/${encodeURIComponent(datasetId)}`;
  const [head, setHead] = useState<InstructionHead | null>(null);
  const [episode, setEpisode] = useState<EpisodeInstruction | null>(null);
  const [selections, setSelections] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(!!datasetId);
  const [saving, setSaving] = useState(false);
  const [applyStatus, setApplyStatus] = useState<ApplyStatus | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refreshRequired, setRefreshRequired] = useState(false);

  const load = useCallback(async (): Promise<boolean> => {
    if (!datasetId) {
      setLoading(false);
      return false;
    }
    setLoading(true);
    setError("");
    try {
      const [nextHead, nextEpisode] = await Promise.all([
        request<InstructionHead>(base),
        request<EpisodeInstruction>(`${base}/episodes/${episodeId}`),
      ]);
      const normalized = selectionsForTemplate(
        nextHead.template,
        nextEpisode.selections,
      );
      setHead(nextHead);
      setEpisode(nextEpisode);
      setSelections(normalized);
      setEditing(false);
      setApplyStatus(null);
      setRefreshRequired(false);
      return true;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      return false;
    } finally {
      setLoading(false);
    }
  }, [base, datasetId, episodeId]);

  useEffect(() => {
    void load();
  }, [load]);

  const preview = useMemo(
    () => (head ? renderInstruction(head.template, selections) : ""),
    [head, selections],
  );

  const beginEdit = () => {
    if (!episode || refreshRequired) return;
    setSelections(
      episode.selections
        ? selectionsForTemplate(
            head?.template ?? { parts: [], variables: {} },
            episode.selections,
          )
        : {},
    );
    setEditing(true);
    setError("");
    setNotice("");
    setApplyStatus(null);
    setRefreshRequired(false);
  };

  const cancelEdit = () => {
    if (!head || !episode) return;
    const normalized = selectionsForTemplate(head.template, episode.selections);
    setSelections(normalized);
    setEditing(false);
    setError("");
    setApplyStatus(null);
  };

  const saveAndApply = async () => {
    if (!head || !episode) return;
    setSaving(true);
    setError("");
    setNotice("");
    setRefreshRequired(false);
    setApplyStatus({
      id: "",
      state: "QUEUED",
      stage: "保存 Episode 指令",
      progress: "正在更新当前 Episode 的模板选择",
    });
    try {
      const normalized = selectionsForTemplate(head.template, selections);
      const saved = await request<SavedEpisodeDraft>(
        `${base}/episodes/${episodeId}`,
        "PUT",
        {
        revision: head.revision,
        source_signature: head.source_signature,
        selections: normalized,
        },
      );
      const merged = mergeSavedEpisodeDraft(head, episode, saved);
      setHead(merged.head);
      setEpisode(merged.episode);
      setSelections(saved.selections);

      let job = await request<ApplyStatus>(
        `${base}/episodes/${episodeId}/apply`,
        "POST",
        {
          revision: saved.revision,
          source_signature: saved.source_signature,
        },
      );
      setApplyStatus(job);
      for (let attempt = 0; attempt < 600; attempt += 1) {
        if (job.state === "CONVERTED") {
          const refreshed = await load();
          const outcome = appliedInstructionOutcome(
            refreshed,
            job.report?.noop
              ? `Episode ${episodeId} 的指令已是最新状态`
              : `Episode ${episodeId} 的指令已写入原数据集`,
          );
          setNotice(outcome.notice);
          setRefreshRequired(outcome.refreshRequired);
          if (outcome.refreshRequired) {
            setError((current) =>
              current
                ? `${current}；指令已写入，但页面刷新失败，请刷新后继续`
                : "指令已写入，但页面刷新失败，请刷新后继续",
            );
          }
          setSaving(false);
          return;
        }
        if (job.state === "FAILED") {
          throw new Error(job.error || "修改 Episode 指令失败");
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
        job = await request<ApplyStatus>(`${base}/apply/${job.id}`);
        setApplyStatus(job);
      }
      throw new Error("修改任务仍在后台执行，请稍后重新打开检查");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setSaving(false);
    }
  };

  const displayInstruction =
    episode?.current_instruction || currentInstruction || "无";

  if (!datasetId) {
    return (
      <div className="viewer-instruction instruction-inline-readonly">
        <p className="instruction-inline-eyebrow">语言指令</p>
        <p>{displayInstruction}</p>
      </div>
    );
  }

  return (
    <section
      className="viewer-instruction instruction-inline-editor"
      aria-busy={loading || saving}
    >
      <div className="instruction-inline-header">
        <div>
          <p className="instruction-inline-eyebrow">语言指令</p>
          {!editing && (
            <div className="instruction-inline-current">
              {loading ? "正在加载语言模板…" : displayInstruction}
            </div>
          )}
        </div>
        {!loading && !editing && episode && (
          <button
            type="button"
            className="instruction-inline-button"
            onClick={beginEdit}
            disabled={refreshRequired}
          >
            {refreshRequired ? "请刷新页面" : "编辑"}
          </button>
        )}
      </div>

      {error && (
        <div className="instruction-inline-message error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="instruction-inline-message success" role="status">
          {notice}
        </div>
      )}

      {editing && head && episode && (
        <>
          <div className="instruction-inline-fields">
            {Object.entries(head.template.variables).map(
              ([key, definition]) => {
                const current = selections[key] ?? "";
                const removed =
                  current !== "" && !definition.options.includes(current);
                return (
                  <label key={key}>
                    <span>{definition.label}</span>
                    <select
                      value={current}
                      disabled={saving}
                      onChange={(event) =>
                        setSelections((value) => ({
                          ...value,
                          [key]: event.target.value,
                        }))
                      }
                    >
                      {removed && (
                        <option value={current}>
                          {current}（已从模板移除）
                        </option>
                      )}
                      {definition.options.map((option, index) => (
                        <option key={`${option}:${index}`} value={option}>
                          {option || "（空选项）"}
                        </option>
                      ))}
                    </select>
                  </label>
                );
              },
            )}
          </div>
          <div className="instruction-inline-preview">
            <span>将写入数据集的指令</span>
            <strong>{preview || "请完善变量选项"}</strong>
          </div>
          {applyStatus && saving && (
            <div className="instruction-inline-progress" role="status">
              <strong>{applyStatus.stage}</strong>
              <span>{applyStatus.progress}</span>
            </div>
          )}
          <div className="instruction-inline-actions">
            <button
              type="button"
              className="instruction-inline-button"
              onClick={cancelEdit}
              disabled={saving}
            >
              取消
            </button>
            <button
              type="button"
              className="instruction-inline-button primary"
              onClick={() => void saveAndApply()}
              disabled={saving || !preview || refreshRequired}
            >
              {saving
                ? "正在写入原数据集…"
                : refreshRequired
                  ? "请刷新页面"
                  : "保存并修改数据集"}
            </button>
          </div>
        </>
      )}
    </section>
  );
}
