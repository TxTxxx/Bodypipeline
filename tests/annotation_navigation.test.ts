import { test, expect } from "bun:test";
import {
  AnnotationNavigationStore,
  filterNavigationRows,
  nextUncheckedEpisode,
  nextPrefetchEpisode,
  reconcileNavigationRow,
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
test("review look-ahead skips the same checked rows as the complete button", () => {
  expect(nextUncheckedEpisode(rows, 0)?.episode).toBe(2);
  expect(nextPrefetchEpisode(rows, 0)?.episode).toBe(2);
  const filtered = filterNavigationRows(
    rows,
    {},
    { excluded: [2], flagged: null },
  );
  expect(nextUncheckedEpisode(filtered, 0)).toBeUndefined();
  expect(nextPrefetchEpisode(filtered, 0)?.episode).toBe(1);
  expect(nextPrefetchEpisode(rows, 2)).toBeUndefined();
});
test("saved row updates count membership without rebuilding the full catalog", () => {
  const filter = { subtask: "grasp", count: "2", basis: "complete" };
  const uncertain = {
    ...rows[0],
    revision: 3,
    counts: { recorded_count: 2, complete: false },
  };
  expect(
    reconcileNavigationRow(rows, uncertain, filter).some(
      (r) => r.episode === 0,
    ),
  ).toBe(false);
  const checked = {
    ...uncertain,
    complete: true,
    counts: { recorded_count: 2, complete: true },
  };
  const restored = reconcileNavigationRow(rows.slice(1), checked, filter);
  expect(restored.map((r) => r.episode)).toEqual([0, 1, 2]);
  expect(restored[0].revision).toBe(3);
  expect(
    reconcileNavigationRow(rows, uncertain, {
      ...filter,
      basis: "recorded",
    })[0],
  ).toBe(uncertain);
  expect(
    reconcileNavigationRow(
      rows,
      { ...uncertain, counts: { recorded_count: null, complete: true } },
      filter,
    ).some((r) => r.episode === 0),
  ).toBe(false);
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
test("route handoff retains an already loaded list without showing a fake reload", () => {
  const store = new AnnotationNavigationStore();
  const disconnect = store.connect({
    open() {},
    filter() {},
    select() {},
    selectPage() {},
    selectResults() {},
    clearSelection() {},
    page() {},
    refresh() {},
    visibilityChanged() {},
  });
  store.publish({
    ...store.getSnapshot(),
    rows,
    ready: true,
    loading: false,
    total: 3,
  });
  disconnect();
  expect(store.getSnapshot().loading).toBe(false);
  expect(store.getSnapshot().rows).toBe(rows);
});
test("a remounted browser provider restores its dataset, while SSR stays isolated", () => {
  const originalWindow = globalThis.window;
  try {
    globalThis.window = {} as Window & typeof globalThis;
    const first = new AnnotationNavigationStore("browser-dataset");
    first.publish({
      ...first.getSnapshot(),
      dataset: "browser-dataset",
      rows,
      loading: false,
      ready: true,
    });
    const remount = new AnnotationNavigationStore("browser-dataset");
    expect(remount.getSnapshot().rows).toBe(rows);
    expect(remount.getSnapshot().loading).toBe(false);
    expect(remount.getServerSnapshot().loading).toBe(true);
    expect(
      new AnnotationNavigationStore("other-dataset").getSnapshot().loading,
    ).toBe(true);
    delete (globalThis as any).window;
    expect(
      new AnnotationNavigationStore("browser-dataset").getSnapshot().rows,
    ).toEqual([]);
  } finally {
    if (originalWindow === undefined) delete (globalThis as any).window;
    else globalThis.window = originalWindow;
  }
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
