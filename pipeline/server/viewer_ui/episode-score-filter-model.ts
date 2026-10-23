import type { EpisodeScore } from "@/components/episode-scores";

export const SCORE_METRICS = [
  ["overall_score", "总分"],
  ["smoothness", "平滑度"],
  ["coverage", "动作覆盖"],
  ["consistency", "一致性"],
  ["length_score", "长度"],
] as const;

export type ScoreMetric = (typeof SCORE_METRICS)[number][0];

export function scoreValue(row: EpisodeScore, metric: ScoreMetric) {
  const value = row[metric];
  return row.status === "SCORED" &&
    typeof value === "number" &&
    Number.isFinite(value)
    ? value
    : null;
}

export function outsideScoreRange(
  episodes: EpisodeScore[],
  metric: ScoreMetric,
  min: number,
  max: number,
  excluded: ReadonlySet<number>,
) {
  if (
    !Number.isFinite(min) ||
    !Number.isFinite(max) ||
    min < 0 ||
    max > 100 ||
    min > max
  )
    return [];
  return episodes
    .filter((row) => {
      if (excluded.has(row.episode_index)) return false;
      const value = scoreValue(row, metric);
      return value !== null && (value < min || value > max);
    })
    .map((row) => row.episode_index)
    .sort((a, b) => a - b);
}
