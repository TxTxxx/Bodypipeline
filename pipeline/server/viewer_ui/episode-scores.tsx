"use client";

import { useEffect, useState } from "react";

export interface EpisodeScore {
  episode_index: number;
  status: "SCORED" | "UNSCORABLE";
  overall_score: number | null;
  smoothness: number | null;
  coverage: number | null;
  consistency: number | null;
  length_score: number | null;
  is_outlier: boolean;
  reasons: string[];
}

interface ScoreState {
  state: "NONE" | "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
  error?: string;
  report?: {
    total_episodes: number;
    scored_episodes: number;
    mean_score: number | null;
    episodes: EpisodeScore[];
  };
}

export function scoreText(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toFixed(1)
    : "—";
}

export function useEpisodeScores(repoId: string) {
  const [entry, setEntry] = useState<{ repoId: string; result: ScoreState }>({
    repoId,
    result: { state: "NONE" },
  });
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    let active = true;
    let retryRequested = generation > 0;
    let timer: ReturnType<typeof setTimeout>;
    const api = `/api/episode-scores/${encodeURIComponent(repoId.split("/")[1])}`;
    setEntry({ repoId, result: { state: "NONE" } });
    const load = async () => {
      try {
        let response = await fetch(api, { cache: "no-store" });
        let payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "无法读取评分");
        if (!active) return;
        if (
          payload.state === "NONE" ||
          (payload.state === "FAILED" && retryRequested)
        ) {
          retryRequested = false;
          response = await fetch(api, { method: "POST" });
          payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || "无法开始评分");
        }
        if (!active) return;
        setEntry({ repoId, result: payload });
        if (payload.state !== "FAILED") {
          timer = setTimeout(
            load,
            payload.state === "COMPLETED" ? 15000 : 2000,
          );
        }
      } catch (error) {
        if (active)
          setEntry({
            repoId,
            result: {
              state: "FAILED",
              error: error instanceof Error ? error.message : "评分不可用",
            },
          });
      }
    };
    void load();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [repoId, generation]);
  // A repo change must never briefly pair the previous dataset's scores with
  // new episode numbers while the effect resets its state.
  const result: ScoreState =
    entry.repoId === repoId ? entry.result : { state: "NONE" };
  return { result, retry: () => setGeneration((v) => v + 1) };
}

export function EpisodeScoreBadge({ score }: { score?: EpisodeScore }) {
  return (
    <span
      className={`episode-score-badge${score?.is_outlier ? " needs-review" : ""}`}
      title={
        score?.status === "UNSCORABLE"
          ? score.reasons.join("；")
          : score
            ? "Doctor 质量分 / 100"
            : "等待评分"
      }
    >
      {scoreText(score?.overall_score)}
    </span>
  );
}

export function EpisodeScorePanel({
  result,
  episodeId,
  retry,
}: {
  result: ScoreState;
  episodeId: number;
  retry: () => void;
}) {
  const report = result.report;
  const score = report?.episodes.find((row) => row.episode_index === episodeId);
  return (
    <section
      className="episode-score-panel"
      aria-label="Episode 质量评分"
      aria-live="polite"
    >
      <div className="episode-score-heading">
        <span>Episode {episodeId} · 质量分</span>
        <strong>
          {scoreText(score?.overall_score)}
          <small> / 100</small>
        </strong>
      </div>
      {result.state === "FAILED" ? (
        <>
          <p role="alert">{result.error || "评分失败"}</p>
          <button type="button" className="episode-score-retry" onClick={retry}>
            重试评分
          </button>
        </>
      ) : result.state !== "COMPLETED" ? (
        <p>正在计算所有 Episode 的评分，可继续播放。</p>
      ) : (
        <>
          {score?.status === "UNSCORABLE" ? (
            <p>该片段无法评分</p>
          ) : (
            <dl className="episode-score-metrics">
              {(
                [
                  ["平滑度", score?.smoothness],
                  ["动作覆盖", score?.coverage],
                  ["一致性", score?.consistency],
                  ["长度", score?.length_score],
                ] as const
              ).map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{scoreText(value)}</dd>
                </div>
              ))}
            </dl>
          )}
          {score?.is_outlier && (
            <p className="episode-score-review">建议人工复查</p>
          )}
          {!!score?.reasons.length && (
            <details>
              <summary>评分说明</summary>
              <ul>
                {score.reasons.map((reason, index) => (
                  <li key={index}>{reason}</li>
                ))}
              </ul>
            </details>
          )}
          <p>
            已评分 {report?.scored_episodes} / {report?.total_episodes} 段 ·
            均分 {scoreText(report?.mean_score)}
          </p>
        </>
      )}
      <p className="episode-score-note">
        Doctor 启发式评分，仅供复查排序，不代表任务成功率；不会自动删除片段。
      </p>
    </section>
  );
}
