import { test, expect } from "bun:test";
import {
  AnnotationNavigationStore,
  filterNavigationRows,
  type EpisodeRow,
  type NavigationActions,
} from "../pipeline/server/viewer_ui/annotation-navigation-model";
const rows: EpisodeRow[] = [
  { episode: 0, revision: 1, frames: 20, complete: false },
  { episode: 1, revision: 2, frames: 30, complete: true },
  { episode: 2, revision: 3, frames: 40, complete: false },
];
test("navigation status, recycle and flag filters describe the same result list", () => {
  expect(
    filterNavigationRows(
      rows,
      { review: "unchecked" },
      { excluded: [2], flagged: null },
    ).map((r) => r.episode),
  ).toEqual([0]);
  expect(
    filterNavigationRows(
      rows,
      { review: "checked" },
      { excluded: [], flagged: [0, 1] },
    ).map((r) => r.episode),
  ).toEqual([1]);
  expect(
    filterNavigationRows(rows, {}, { excluded: [], flagged: [] }).map(
      (r) => r.episode,
    ),
  ).toEqual([]);
  expect(
    filterNavigationRows(rows, {}, { excluded: [], flagged: null }),
  ).toEqual(rows);
});
test("a controller remount cannot disconnect the active navigation owner", () => {
  const store = new AnnotationNavigationStore();
  let oldCalls = 0,
    newCalls = 0;
  const actions = (open: () => void): NavigationActions => ({
    open,
    filter() {},
    select() {},
    selectPage() {},
    selectResults() {},
    clearSelection() {},
    page() {},
    refresh() {},
    visibilityChanged() {},
  });
  const stopOld = store.connect(actions(() => oldCalls++));
  const stopNew = store.connect(actions(() => newCalls++));
  stopOld();
  store.run((a) => a.open(1));
  expect(oldCalls).toBe(0);
  expect(newCalls).toBe(1);
  stopNew();
  store.run((a) => a.open(2));
  expect(newCalls).toBe(1);
});
test("selection is synchronous and failed navigation stays visible", async () => {
  const store = new AnnotationNavigationStore();
  let selected = -1;
  store.connect({
    open() {
      throw Error("Episode 不存在");
    },
    filter() {},
    select(ep, checked) {
      selected = checked ? ep : -1;
    },
    selectPage() {},
    selectResults() {},
    clearSelection() {},
    page() {},
    refresh() {},
    visibilityChanged() {},
  });
  store.run((a) => a.select(2, true));
  expect(selected).toBe(2);
  store.run((a) => a.open(99));
  await Promise.resolve();
  expect(store.getSnapshot().error).toBe("Episode 不存在");
});
