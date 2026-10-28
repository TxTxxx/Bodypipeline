import { expect, test } from "bun:test";
import { gapAt, merge, ordered, split, validate, type Segment } from "../pipeline/server/viewer_ui/subtask-review-model";

const s = (start_frame: number, end_frame: number, text = "same"): Segment => ({ start_frame, end_frame, text });

test("empty annotations and explicit gaps remain unlabelled", () => {
  expect(validate([], 12)).toBeNull();
  expect(validate([s(0, 2), s(5, 7)], 12)).toBeNull();
  expect(gapAt([s(0, 2), s(5, 7)], 3, 12)).toEqual([3, 5]);
  expect(gapAt([s(0, 2), s(5, 7)], 5, 12)).toBeNull();
  expect(gapAt([s(0, 2), s(5, 7)], 7, 12)).toEqual([7, 12]);
});

test("split preserves coverage, text and unrelated intervals", () => {
  const input = [s(0, 5), s(7, 9)];
  expect(split(input, 0, 2)).toEqual([s(0, 2), s(2, 5), s(7, 9)]);
  expect(split(input, 0, 0)).toBe(input);
  expect(split(input, 0, 5)).toBe(input);
});

test("merge cannot fill gaps or discard a different description", () => {
  const gap = [s(0, 2), s(5, 7)];
  expect(merge(gap, 0)).toBe(gap);
  const different = [s(0, 2), s(2, 7, "other")];
  expect(merge(different, 0)).toBe(different);
  expect(merge([s(0, 2), s(2, 7)], 0)).toEqual([s(0, 7)]);
});

test("text edits never mutate a shared vocabulary or another interval", () => {
  const input = [s(0, 2), s(5, 7)];
  const edited = input.map((segment, i) => i === 0 ? { ...segment, text: "corrected" } : segment);
  expect(edited[1].text).toBe("same");
  expect(input[0].text).toBe("same");
});

test("reject invalid bounds and overlap without inferring continuity", () => {
  for (const input of [[s(-1, 2)], [s(0, 0)], [s(0, 11)], [s(0, 4), s(3, 5)], [s(0.5, 3)], [s(NaN, 3)], [s(0, 2, "  ")]]) {
    expect(validate(input, 10)).not.toBeNull();
  }
  expect(validate(ordered([s(5, 7), s(0, 2)]), 10)).toBeNull();
});
