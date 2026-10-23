"use client";

import { useEffect, useMemo, useState } from "react";
import { useFlaggedEpisodes } from "@/context/flagged-episodes-context";
import { useEpisodeScores } from "@/components/episode-scores";
import {
  outsideScoreRange,
  scoreValue,
  SCORE_METRICS,
  type ScoreMetric,
} from "@/utils/episode-score-filter-model";

interface RecycleState {
  entries: { episode: number }[];
  total_episodes: number;
  operation?: { state: string };
}

export function EpisodeScoreFilter({ repoId }: { repoId: string }) {
  const { result, retry } = useEpisodeScores(repoId);
  const { addMany } = useFlaggedEpisodes();
  const [metric, setMetric] = useState<ScoreMetric>("overall_score");
  const [rangeMin, setRangeMin] = useState(0);
  const [rangeMax, setRangeMax] = useState(100);
  const [recycle, setRecycle] = useState<RecycleState | null>(null);
  const [recycleError, setRecycleError] = useState("");

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const load = async () => {
      try {
        const response = await fetch(
          `/api/recycle/${encodeURIComponent(repoId.split("/")[1])}`,
          {
            cache: "no-store",
            signal: controller.signal,
          },
        );
        const payload = await response.json();
        if (!response.ok)
          throw new Error(payload.detail || "无法读取回收站状态");
        if (active) {
          setRecycle(payload);
          setRecycleError("");
        }
      } catch (error) {
        if (active)
          setRecycleError(
            error instanceof Error ? error.message : "无法读取回收站状态",
          );
      }
    };
    void load();
    const timer = setInterval(load, 5000);
    window.addEventListener("focus", load);
    return () => {
      active = false;
      controller.abort();
      clearInterval(timer);
      window.removeEventListener("focus", load);
    };
  }, [repoId]);

  const excluded = useMemo(
    () => new Set(recycle?.entries.map((row) => row.episode) || []),
    [recycle],
  );
  const cleaning = ["pending", "running"].includes(
    recycle?.operation?.state || "",
  );
  const ready =
    result.state === "COMPLETED" &&
    !!recycle &&
    !recycleError &&
    !cleaning &&
    result.report?.total_episodes === recycle.total_episodes;
  const outsideIds = useMemo(
    () =>
      ready
        ? outsideScoreRange(
            result.report?.episodes || [],
            metric,
            rangeMin,
            rangeMax,
            excluded,
          )
        : [],
    [ready, result.report, metric, rangeMin, rangeMax, excluded],
  );
  const unavailable = ready
    ? result.report!.episodes.filter(
        (row) =>
          !excluded.has(row.episode_index) && scoreValue(row, metric) === null,
      ).length
    : 0;
  const rangeChanged = rangeMin > 0 || rangeMax < 100;

  return (
    <section
      className="score-filter bg-[var(--surface-1)]/60 rounded-lg p-5 border border-white/10 space-y-4"
      aria-labelledby="score-filter-title"
    >
      <div className="score-filter-heading">
        <h3
          id="score-filter-title"
          className="text-sm font-semibold text-slate-200"
        >
          Episode 评分筛选
        </h3>
        <label className="score-filter-metric">
          评分项目
          <select
            value={metric}
            onChange={(e) => {
              setMetric(e.target.value as ScoreMetric);
              setRangeMin(0);
              setRangeMax(100);
            }}
          >
            {SCORE_METRICS.map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {result.state === "FAILED" ? (
        <p className="text-xs text-red-300" role="alert">
          {result.error || "评分失败"}{" "}
          <button
            type="button"
            className="text-cyan-300 underline"
            onClick={retry}
          >
            重试评分
          </button>
        </p>
      ) : !ready ? (
        <p className="text-xs text-slate-400" role="status">
          {recycleError
            ? `${recycleError}，正在重试…`
            : cleaning
              ? "正在永久清理数据，完成后更新评分。"
              : "正在加载 Episode 评分…"}
        </p>
      ) : (
        <>
          <div className="space-y-2" role="group" aria-label="评分范围">
            <div className="flex items-center justify-between text-xs text-slate-400">
              <span className="tabular-nums">{rangeMin.toFixed(1)} 分</span>
              <span className="tabular-nums">{rangeMax.toFixed(1)} 分</span>
            </div>
            <div className="score-filter-range-track">
              <div className="score-filter-range-rail" />
              <div
                className="score-filter-range-selection"
                style={{ left: `${rangeMin}%`, right: `${100 - rangeMax}%` }}
              />
              <input
                type="range"
                min="0"
                max="100"
                step="0.1"
                value={rangeMin}
                aria-label="最低评分滑块"
                aria-valuetext={`${rangeMin.toFixed(1)} 分`}
                aria-valuemax={rangeMax}
                style={{ zIndex: rangeMin > 50 ? 3 : 2 }}
                onChange={(e) =>
                  setRangeMin(Math.min(Number(e.target.value), rangeMax))
                }
              />
              <input
                type="range"
                min="0"
                max="100"
                step="0.1"
                value={rangeMax}
                aria-label="最高评分滑块"
                aria-valuetext={`${rangeMax.toFixed(1)} 分`}
                aria-valuemin={rangeMin}
                onChange={(e) =>
                  setRangeMax(Math.max(Number(e.target.value), rangeMin))
                }
              />
            </div>
          </div>
          {rangeChanged && (
            <div className="flex items-center justify-between gap-3">
              <span className="text-xs text-slate-400" role="status">
                {outsideIds.length} 个 Episode 在评分范围外
              </span>
              {outsideIds.length > 0 && (
                <button
                  type="button"
                  onClick={() => addMany(outsideIds)}
                  className="text-xs bg-cyan-400/15 text-cyan-300 border border-cyan-400/40 rounded px-2 py-1 hover:bg-cyan-400/20 transition-colors"
                >
                  标记 {outsideIds.length} 个范围外 Episode
                </button>
              )}
            </div>
          )}
          {unavailable > 0 && (
            <p className="text-xs text-slate-500">
              {unavailable} 个 Episode 无此项评分，不参与范围判断。
            </p>
          )}
        </>
      )}
    </section>
  );
}
