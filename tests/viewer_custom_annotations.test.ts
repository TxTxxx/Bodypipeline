import { expect, test } from "bun:test";
import {
  frameTime,
  nearestFrame,
  snapshotKey,
  fieldDefault,
  filterEpisodes,
  schemeDefaults,
} from "../pipeline/server/viewer_ui/custom-annotation-model";
test("nonuniform timestamps snap by actual frames including exclusive end", () => {
  const r = { frames: 3, fps: 30, frame_timestamps: [0, 0.04, 0.11] };
  expect(nearestFrame(r, 0.09)).toBe(2);
  expect(frameTime(r, 3)).toBeCloseTo(0.18);
});
test("saving an older snapshot cannot clear newer custom edits", () => {
  const a = {
    atoms: [],
    values: { result: { ok: false } },
    records: [],
    status: "draft",
  };
  expect(snapshotKey(a)).not.toBe(
    snapshotKey({ ...a, values: { result: { ok: true } } }),
  );
});
test("defaults keep false and zero while leaving unspecified values unlabelled", () => {
  expect(fieldDefault({ id: "x", dtype: "bool" })).toBeUndefined();
  expect(fieldDefault({ id: "x", dtype: "int", default: 0 })).toBe(0);
  expect(
    schemeDefaults({
      types: [
        {
          id: "result",
          scope: "episode",
          fields: [{ id: "ok", dtype: "bool", default: false }],
        },
      ],
    }),
  ).toEqual({ result: { ok: false } });
});
test("filter applies to all episode identities before pagination", () => {
  expect(
    filterEpisodes(
      [
        { episode_uid: "a", episode_index: 0, status: "draft" },
        { episode_uid: "b", episode_index: 21, status: "completed" },
      ],
      "completed",
    ).map((e) => e.episode_index),
  ).toEqual([21]);
});
import { shortcutValue } from "../pipeline/server/viewer_ui/custom-annotation-model";
test("field shortcut toggles bool and cycles single choices without inventing defaults", () => {
  expect(shortcutValue({ id: "ok", dtype: "bool" }, undefined)).toBe(true);
  expect(shortcutValue({ id: "ok", dtype: "bool" }, true)).toBe(false);
  expect(
    shortcutValue(
      {
        id: "result",
        dtype: "single",
        options: [
          { value: "yes", label: "Yes" },
          { value: "no", label: "No" },
        ],
      },
      "yes",
    ),
  ).toBe("no");
  expect(
    shortcutValue({ id: "note", dtype: "text" }, undefined),
  ).toBeUndefined();
});
