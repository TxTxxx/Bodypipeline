import { expect, test } from "bun:test";
import type { EpisodeScore } from "../pipeline/server/viewer_ui/episode-scores";
import {
  outsideScoreRange,
  scoreValue,
  SCORE_METRICS,
} from "../pipeline/server/viewer_ui/episode-score-filter-model";

function episode(
  id: number,
  score: number | null,
  extra: Partial<EpisodeScore> = {},
): EpisodeScore {
  return {
    episode_index: id,
    status: score === null ? "UNSCORABLE" : "SCORED",
    overall_score: score,
    smoothness: score,
    coverage: score,
    consistency: score,
    length_score: score,
    is_outlier: false,
    reasons: [],
    ...extra,
  };
}

const excluded = new Set<number>();

test("flag targets are outside the retained range; both endpoints are retained", () => {
  const rows = [
    episode(0, 0),
    episode(1, 59.9),
    episode(2, 60),
    episode(3, 80),
    episode(4, 80.1),
    episode(5, 100),
  ];
  expect(outsideScoreRange(rows, "overall_score", 60, 80, excluded)).toEqual([
    0, 1, 4, 5,
  ]);
  expect(outsideScoreRange(rows, "overall_score", 0, 100, excluded)).toEqual(
    [],
  );
  expect(outsideScoreRange(rows, "overall_score", 60, 60, excluded)).toEqual([
    0, 1, 3, 4, 5,
  ]);
});

test("invalid or reversed ranges produce no bulk flag targets", () => {
  for (const [min, max] of [
    [-1, 100],
    [0, 101],
    [80, 60],
    [NaN, 100],
    [0, Infinity],
    [-Infinity, 100],
  ]) {
    expect(
      outsideScoreRange([episode(0, 70)], "overall_score", min, max, excluded),
    ).toEqual([]);
  }
});

test("missing, nonfinite and unscorable scores are excluded rather than treated as zero", () => {
  const rows = [
    episode(0, 0),
    episode(1, null),
    episode(2, NaN),
    episode(3, Infinity),
    episode(4, 80, { status: "UNSCORABLE" }),
    episode(5, 80, { overall_score: null }),
  ];
  expect(outsideScoreRange(rows, "overall_score", 60, 100, excluded)).toEqual([
    0,
  ]);
  for (const row of rows.slice(1))
    expect(scoreValue(row, "overall_score")).toBeNull();
});

test("each subscore is independent; unassessed consistency is skipped", () => {
  const rows = [
    episode(0, 90, { smoothness: 20, consistency: null }),
    episode(1, 50, { smoothness: 95, consistency: 0 }),
  ];
  expect(outsideScoreRange(rows, "overall_score", 80, 100, excluded)).toEqual([
    1,
  ]);
  expect(outsideScoreRange(rows, "smoothness", 80, 100, excluded)).toEqual([0]);
  expect(outsideScoreRange(rows, "consistency", 1, 100, excluded)).toEqual([1]);
  for (const [metric] of SCORE_METRICS) {
    expect(
      outsideScoreRange([episode(2, 10)], metric, 20, 100, excluded),
    ).toEqual([2]);
  }
});

test("batch targets cover the full dataset, exclude recycle entries, sort IDs and leave input unchanged", () => {
  const rows = Array.from({ length: 150 }, (_, id) =>
    episode(id, id < 140 ? 95 : 10),
  ).reverse();
  const original = structuredClone(rows);
  const recycle = new Set([141, 145, 149]);
  expect(outsideScoreRange(rows, "overall_score", 20, 100, recycle)).toEqual([
    140, 142, 143, 144, 146, 147, 148,
  ]);
  expect(rows).toEqual(original);
  expect([...recycle]).toEqual([141, 145, 149]);
  expect(outsideScoreRange([], "overall_score", 20, 100, recycle)).toEqual([]);
});
