import { test, expect } from "bun:test";
import {
  inclusive,
  resize,
  split,
  replace,
  mergeAttempts,
  mergeSegments,
  recordedCount,
  UndoHistory,
  LocalDrafts,
  EpisodeSelection,
  upgrade,
  type Document,
} from "../pipeline/server/viewer_ui/workbench-model";
const doc = (): Document => ({
  version: 2,
  templateId: "t",
  templateSnapshot: {
    id: "t",
    name: "模板",
    subtasks: [
      { id: "a", name: "A" },
      { id: "b", name: "B" },
    ],
    fields: [],
  },
  values: {},
  attempts: [
    { id: "try-a", subtaskId: "a", values: { score: 1 }, outcome: "failure" },
    { id: "try-b", subtaskId: "b", values: { score: 5 }, outcome: "success" },
  ],
  segments: [
    {
      id: "a",
      start: 0,
      end: 5,
      label: "SUBTASK",
      subtaskId: "a",
      attemptId: "try-a",
      note: "original",
    },
    {
      id: "b",
      start: 5,
      end: 10,
      label: "SUBTASK",
      subtaskId: "b",
      attemptId: "try-b",
    },
  ],
  frames: [],
  history: [],
  countEvidence: {},
  provenance: {},
});
test("inclusive single and last data frame; public boundary updates atomically", () => {
  expect(inclusive(9, 9, 10)).toEqual({ start: 9, end: 10 });
  expect(
    resize(doc(), 10, "a", "end", 7).segments.map((s) => [s.start, s.end]),
  ).toEqual([
    [0, 7],
    [7, 10],
  ]);
  const gap = doc();
  gap.segments[1].start = 7;
  expect(resize(gap, 10, "a", "end", 6).segments[1].start).toBe(7);
});
test("split and local relabel retain identity of both sides and fully undo", () => {
  const d = doc(),
    history = new UndoHistory();
  const cut = split(d, 10, "a", 2);
  expect(recordedCount(cut, "a")).toBe(1);
  const relabelled = replace(
    d,
    10,
    1,
    3,
    {
      id: "new",
      start: 1,
      end: 3,
      label: "SUBTASK",
      subtaskId: "b",
      attemptId: "try-new",
    },
    { id: "try-new", subtaskId: "b", values: {} },
  );
  expect(
    relabelled.segments
      .filter((s) => s.subtaskId === "a")
      .map((s) => s.attemptId),
  ).toEqual(["try-a", "try-a"]);
  history.edit(d, cut);
  history.edit(cut, relabelled);
  expect(history.undo(relabelled)).toEqual(cut);
  expect(history.undo(cut)).toEqual(d);
  expect(history.redo(d)).toEqual(cut);
});
test("same-name retries remain distinct; merging attempts is explicit with retained history", () => {
  const d = doc();
  d.attempts[1].subtaskId = "a";
  d.segments[1].subtaskId = "a";
  expect(recordedCount(d, "a")).toBe(2);
  expect(() => mergeSegments(d, 10, "a", "b")).toThrow();
  const merged = mergeAttempts(d, 10, "try-a", "try-b", {
    outcome: "unknown",
    values: { score: null },
    note: "核查后同一次",
  });
  expect(recordedCount(merged, "a")).toBe(1);
  expect(
    merged.history.some((h) => h.kind === "merged-attempt-originals"),
  ).toBe(true);
  expect(merged.attempts.find((a) => a.id === "try-a")?.outcome).toBe(
    "unknown",
  );
});
test("NULL UNKNOWN OOT and blank are distinct; geometry never counts special segments", () => {
  const d = doc();
  d.segments = [
    { id: "n", label: "NULL", start: 0, end: 1 },
    { id: "u", label: "UNKNOWN", start: 1, end: 2 },
    {
      id: "o",
      label: "OUT_OF_TEMPLATE",
      start: 2,
      end: 3,
      description: "清理桌面",
    },
  ];
  expect(recordedCount(d, "a")).toBe(0);
});
test("offline inputs and undo survive Episode switches; quota errors visible", () => {
  const map = new Map<string, string>();
  const storage = {
    getItem: (k: string) => map.get(k) || null,
    setItem: (k: string, v: string) => {
      map.set(k, v);
    },
    removeItem: (k: string) => map.delete(k),
  };
  const drafts = new LocalDrafts(storage, "source");
  const backup: any = {
    head: { dataset: "source", episode: 0 },
    document: doc(),
    dirty: true,
    input: { first: "-", note: "未完成输入" },
    past: [doc()],
    future: [],
    at: 1,
  };
  drafts.write(0, backup);
  drafts.write(1, { ...backup, head: { dataset: "source", episode: 1 } });
  expect(drafts.read(0)?.input.note).toBe("未完成输入");
  expect(drafts.read(0)?.past).toHaveLength(1);
  const broken = new LocalDrafts(
    {
      ...storage,
      setItem: () => {
        throw Error("quota");
      },
    },
    "source",
  );
  expect(() => broken.write(0, backup)).toThrow("本地备份失败");
});
test("selection deduplicates manual and filter entries, rejects empty instead of all", () => {
  const s = new EpisodeSelection();
  expect(() => s.payload("s", {})).toThrow();
  s.add(4, 2, "filter");
  s.add(4, 2, "manual");
  s.add(9, 0, "manual");
  expect(s.payload("s", {}).episodes).toEqual([4, 9]);
  s.remove(4);
  expect(s.payload("s", {}).episodes).toEqual([9]);
});
test("same ID with changed meaning requires explicit template resolution", () => {
  const d = doc();
  const t = structuredClone(d.templateSnapshot);
  t.subtasks[0].definition = "changed meaning";
  expect(() => upgrade(d, 10, t, {})).toThrow();
  const next = upgrade(d, 10, t, { a: "a", b: "b" });
  expect(next.templateSnapshot.subtasks[0].definition).toBe("changed meaning");
  expect(d.templateSnapshot.subtasks[0].definition).toBeUndefined();
});
