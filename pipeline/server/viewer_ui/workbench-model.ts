/** Canonical annotation v2 operations, shared by the browser and CPU regressions. */
export type Value = string | number | boolean | string[] | null;
export interface Field {
  id: string;
  label: string;
  scope: "episode" | "attempt" | "segment" | "frame";
  kind: "rating" | "number" | "text" | "textarea" | "single" | "multi" | "bool";
  options?: string[];
  description?: string;
  minimum?: number;
  maximum?: number;
  step?: number;
  scale?: string;
}
export interface Subtask {
  id: string;
  name: string;
  definition?: string;
  includes?: string;
  excludes?: string;
  startCondition?: string;
  endCondition?: string;
}
export interface Template {
  id: string;
  name: string;
  subtasks: Subtask[];
  fields: Field[];
}
export interface Attempt {
  id: string;
  subtaskId?: string;
  subtaskName?: string;
  outcome?: "success" | "failure" | "unknown";
  values: Record<string, Value>;
  note?: string;
  archived?: boolean;
  provenance?: Record<string, unknown>;
}
export type Label = "SUBTASK" | "NULL" | "UNKNOWN" | "OUT_OF_TEMPLATE";
export interface Segment {
  id: string;
  start: number;
  end: number;
  label: Label;
  subtaskId?: string;
  attemptId?: string;
  note?: string;
  description?: string;
  values?: Record<string, Value>;
  review?: { contentKey: string; actor: string; at: number };
  evidence?: Record<string, unknown>[];
  risks?: string[];
  provenance?: Record<string, unknown>;
}
export interface FrameNote {
  id: string;
  frame: number;
  values: Record<string, Value>;
}
export interface Document {
  version: 2;
  templateId: string;
  templateSnapshot: Template;
  values: Record<string, Value>;
  attempts: Attempt[];
  segments: Segment[];
  frames: FrameNote[];
  manualAtoms?: Record<string, unknown>[];
  countEvidence: Record<string, { reason: string; contentKey: string }>;
  history: Record<string, unknown>[];
  provenance: Record<string, unknown>;
}
export interface Head {
  id: string;
  dataset: string;
  episode: number;
  revision: number;
  source_signature: string;
  document: Document;
  complete: boolean;
  frames: number;
  fps: number;
  status: {
    eligible: boolean;
    missing: string[];
    gaps: number[][];
    pending: string[];
  };
  template_signature: string;
}
export const copy = <T>(x: T): T => structuredClone(x);
export function uid(): string {
  const b = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
export function validate(doc: Document, count: number): void {
  let end = 0;
  const ids = new Set<string>();
  const steps = new Set(doc.templateSnapshot.subtasks.map((s) => s.id));
  const attempts = new Map(doc.attempts.map((a) => [a.id, a]));
  for (const row of [...doc.attempts, ...doc.segments, ...doc.frames]) {
    if (!row.id || ids.has(row.id)) throw Error("记录身份重复");
    ids.add(row.id);
  }
  for (const s of [...doc.segments].sort((a, b) => a.start - b.start)) {
    if (
      !Number.isInteger(s.start) ||
      !Number.isInteger(s.end) ||
      s.start < end ||
      s.start < 0 ||
      s.end <= s.start ||
      s.end > count
    )
      throw Error("片段重叠或超出数据帧范围");
    end = s.end;
    const a = attempts.get(s.attemptId || "");
    if (s.attemptId && (!a || a.archived)) throw Error("关联的尝试不存在");
    if (
      s.label === "SUBTASK" &&
      (!a || a.subtaskId !== s.subtaskId || !steps.has(s.subtaskId || ""))
    )
      throw Error("正式标签需要明确关联同标签的尝试");
    if (s.label !== "SUBTASK" && s.subtaskId)
      throw Error("特殊状态不是正式标签");
    if (s.label === "NULL" && s.attemptId) throw Error("NULL 不能关联尝试");
  }
}
export function clean(doc: Document): Document {
  const active = new Set(doc.segments.map((s) => s.attemptId));
  doc.attempts.forEach((a) => {
    if (!active.has(a.id) && !a.archived) {
      a.provenance = {
        ...a.provenance,
        archivedTemplateSnapshot: copy(doc.templateSnapshot),
      };
    }
    a.archived = !active.has(a.id);
    if ("attempt-outcome" in a.values)
      a.values["attempt-outcome"] = a.outcome
        ? { success: "成功", failure: "失败", unknown: "无法判断" }[a.outcome]
        : null;
  });
  doc.segments.sort((a, b) => a.start - b.start);
  return doc;
}
/** Review stamps are retained in undo snapshots; server checks trusted history. */
export function operation(
  doc: Document,
  count: number,
  name: string,
  change: (d: Document) => void,
): Document {
  const next = copy(doc);
  change(next);
  clean(next);
  validate(next, count);
  const previous = new Map(doc.segments.map((s) => [s.id, s]));
  for (const s of next.segments) {
    const old = previous.get(s.id);
    const oldAttempt = doc.attempts.find((a) => a.id === old?.attemptId);
    const attempt = next.attempts.find((a) => a.id === s.attemptId);
    const withoutReview = (x: Segment | undefined) =>
      x ? { ...x, review: undefined } : undefined;
    if (
      JSON.stringify(withoutReview(s)) !== JSON.stringify(withoutReview(old)) ||
      JSON.stringify(attempt) !== JSON.stringify(oldAttempt) ||
      JSON.stringify(next.values) !== JSON.stringify(doc.values) ||
      JSON.stringify(next.manualAtoms) !== JSON.stringify(doc.manualAtoms) ||
      JSON.stringify(next.templateSnapshot) !==
        JSON.stringify(doc.templateSnapshot) ||
      JSON.stringify(
        next.frames.filter((f) => s.start <= f.frame && f.frame < s.end),
      ) !==
        JSON.stringify(
          doc.frames.filter((f) => s.start <= f.frame && f.frame < s.end),
        )
    )
      delete s.review;
  }
  // History lives durably on the Server as well; short local operation context
  // retains removed identities/values inside a portable document.
  const removed = doc.segments.filter(
    (s) => !next.segments.some((n) => n.id === s.id),
  );
  const archived = doc.attempts.filter(
    (a) => !next.attempts.some((n) => n.id === a.id && !n.archived),
  );
  if (removed.length || archived.length)
    next.history.push({
      kind: name,
      at: Date.now(),
      records: removed,
      attempts: archived,
    });
  return next;
}
export function resize(
  doc: Document,
  count: number,
  id: string,
  side: "start" | "end",
  frame: number,
): Document {
  return operation(doc, count, "boundary", (d) => {
    const s = d.segments.find((s) => s.id === id);
    if (!s) throw Error("未选片段");
    const other = d.segments.find(
      (o) =>
        o.id !== id &&
        (side === "start" ? o.end === s.start : o.start === s.end),
    );
    if (side === "start") {
      if (frame < 0 || frame >= s.end || (other && frame <= other.start))
        throw Error("交界必须保留两侧至少一帧");
      s.start = frame;
      if (other) other.end = frame;
    } else {
      if (frame <= s.start || frame > count || (other && frame >= other.end))
        throw Error("交界必须保留两侧至少一帧");
      s.end = frame;
      if (other) other.start = frame;
    }
  });
}
export function split(
  doc: Document,
  count: number,
  id: string,
  at: number,
): Document {
  return operation(doc, count, "split", (d) => {
    const i = d.segments.findIndex((s) => s.id === id),
      s = d.segments[i];
    if (!s || at <= s.start || at >= s.end) throw Error("请选择片段内的分界帧");
    d.segments.splice(
      i,
      1,
      { ...s, end: at },
      { ...copy(s), id: uid(), start: at },
    );
  });
}
/** The caller explicitly assigns the replacement's attempt; geometry never does. */
export function replace(
  doc: Document,
  count: number,
  start: number,
  end: number,
  segment: Segment,
  attempt?: Attempt,
): Document {
  return operation(doc, count, "range-relabel", (d) => {
    const result: Segment[] = [];
    for (const s of d.segments) {
      if (s.end <= start || s.start >= end) {
        result.push(s);
        continue;
      }
      if (s.start < start) result.push({ ...s, end: start });
      if (s.end > end)
        result.push({
          ...copy(s),
          id: s.start < start ? uid() : s.id,
          start: end,
        });
    }
    if (attempt) {
      const old = d.attempts.find((a) => a.id === attempt.id);
      if (old && JSON.stringify(old) !== JSON.stringify(attempt))
        throw Error("局部操作不能改写已有尝试的共享字段");
      if (!old) d.attempts.push(copy(attempt));
    }
    result.push({ ...segment, start, end });
    d.segments = result;
  });
}
export function move(
  doc: Document,
  count: number,
  id: string,
  start: number,
): Document {
  return operation(doc, count, "move", (d) => {
    const s = d.segments.find((s) => s.id === id);
    if (!s) throw Error("未选片段");
    const length = s.end - s.start;
    s.start = start;
    s.end = start + length;
  });
}
export function mergeSegments(
  doc: Document,
  count: number,
  left: string,
  right: string,
  resolution?: {
    note: string;
    values: Record<string, Value>;
    description?: string;
  },
): Document {
  return operation(doc, count, "merge-segments", (d) => {
    const a = d.segments.find((s) => s.id === left),
      b = d.segments.find((s) => s.id === right);
    if (
      !a ||
      !b ||
      a.end !== b.start ||
      a.attemptId !== b.attemptId ||
      a.label !== b.label ||
      a.subtaskId !== b.subtaskId
    )
      throw Error(
        "片段合并要求相邻、同内容且属于同次尝试；不同尝试请先明确合并尝试",
      );
    if (
      (a.note !== b.note ||
        JSON.stringify(a.values) !== JSON.stringify(b.values) ||
        a.description !== b.description) &&
      !resolution
    )
      throw Error("局部信息冲突，请明确选择保留值");
    d.history.push({
      kind: "merged-segment-originals",
      segments: [copy(a), copy(b)],
    });
    a.end = b.end;
    if (resolution) {
      a.note = resolution.note;
      a.values = resolution.values;
      if (resolution.description !== undefined)
        a.description = resolution.description;
    }
    d.segments = d.segments.filter((s) => s.id !== right);
  });
}
export function mergeAttempts(
  doc: Document,
  count: number,
  keep: string,
  remove: string,
  resolution: {
    outcome: Attempt["outcome"];
    values: Record<string, Value>;
    note: string;
  },
): Document {
  return operation(doc, count, "merge-attempts", (d) => {
    const a = d.attempts.find((a) => a.id === keep),
      b = d.attempts.find((a) => a.id === remove);
    if (!a || !b || a.id === b.id || a.subtaskId !== b.subtaskId)
      throw Error("只能明确合并同标签的不同尝试");
    d.history.push({
      kind: "merged-attempt-originals",
      attempts: [copy(a), copy(b)],
    });
    Object.assign(a, copy(resolution));
    d.segments.forEach((s) => {
      if (s.attemptId === remove) s.attemptId = keep;
    });
    b.archived = true;
  });
}
export function relabelAttempt(
  doc: Document,
  count: number,
  id: string,
  subtaskId: string,
): Document {
  return operation(doc, count, "relabel-whole-attempt", (d) => {
    const a = d.attempts.find((a) => a.id === id),
      step = d.templateSnapshot.subtasks.find((s) => s.id === subtaskId);
    if (!a || !step) throw Error("标签或尝试不存在");
    a.subtaskId = step.id;
    a.subtaskName = step.name;
    d.segments.forEach((s) => {
      if (s.attemptId === id && s.label === "SUBTASK") s.subtaskId = step.id;
    });
  });
}
export class UndoHistory {
  past: Document[] = [];
  future: Document[] = [];
  edit(before: Document, after: Document): Document {
    this.past.push(copy(before));
    this.future = [];
    return after;
  }
  undo(current: Document): Document {
    const before = this.past.pop();
    if (!before) return current;
    this.future.push(copy(current));
    return copy(before);
  }
  redo(current: Document): Document {
    const after = this.future.pop();
    if (!after) return current;
    this.past.push(copy(current));
    return copy(after);
  }
}
export function recordedCount(doc: Document, subtask: string): number {
  const active = new Set(doc.segments.map((s) => s.attemptId));
  return new Set(
    doc.attempts
      .filter((a) => active.has(a.id) && a.subtaskId === subtask && !a.archived)
      .map((a) => a.id),
  ).size;
}
export function inclusive(
  first: number,
  last: number,
  count: number,
): { start: number; end: number } {
  if (
    !Number.isInteger(first) ||
    !Number.isInteger(last) ||
    first < 0 ||
    last < first ||
    last >= count
  )
    throw Error(`首末帧需在 0–${count - 1}，首尾均包含`);
  return { start: first, end: last + 1 };
}
export function gaps(doc: Document, count: number): number[][] {
  const r: number[][] = [];
  let end = 0;
  for (const s of [...doc.segments].sort((a, b) => a.start - b.start)) {
    if (s.start > end) r.push([end, s.start]);
    end = s.end;
  }
  if (end < count) r.push([end, count]);
  return r;
}
export const editableTarget = (target: EventTarget | null) =>
  target instanceof HTMLElement &&
  !!target.closest("input,textarea,select,[contenteditable=true]");
export function templateDiff(
  old: Template,
  next: Template,
): Record<string, unknown>[] {
  const result: Record<string, unknown>[] = [];
  for (const [kind, items, others] of [
    ["subtask", old.subtasks, next.subtasks],
    ["field", old.fields, next.fields],
  ] as const) {
    for (const item of items) {
      const match = others.find((n) => n.id === item.id);
      if (JSON.stringify(item) !== JSON.stringify(match))
        result.push({ kind, id: item.id, before: item, after: match || null });
    }
    for (const item of others)
      if (!items.some((s) => s.id === item.id))
        result.push({ kind, id: item.id, before: null, after: item });
  }
  return result;
}
/** No name/ID match is taken as evidence that semantics remained unchanged. */
export function upgrade(
  doc: Document,
  count: number,
  template: Template,
  mapping: Record<string, string>,
): Document {
  return operation(doc, count, "template-upgrade", (d) => {
    const steps = new Map(template.subtasks.map((s) => [s.id, s]));
    for (const a of d.attempts) {
      if (a.archived) {
        a.provenance ??= {};
        a.provenance.archivedTemplateSnapshot ??= copy(doc.templateSnapshot);
        continue;
      }
      if (!a.subtaskId) continue;
      const mapped = steps.get(mapping[a.subtaskId]);
      if (!mapped)
        throw Error("请明确处理所有已使用标签的映射，未解决时可继续旧快照");
      a.subtaskId = mapped.id;
      a.subtaskName = mapped.name;
    }
    for (const s of d.segments)
      if (s.label === "SUBTASK") {
        const mapped = steps.get(mapping[s.subtaskId || ""]);
        if (!mapped) throw Error("片段映射未解决");
        s.subtaskId = mapped.id;
      }
    d.history.push({
      kind: "template-original",
      snapshot: d.templateSnapshot,
      values: copy(d.values),
      attempts: copy(doc.attempts),
      frames: copy(d.frames),
      countEvidence: copy(d.countEvidence),
    });
    d.countEvidence = {};
    d.templateId = template.id;
    d.templateSnapshot = copy(template);
  });
}
export interface Backup {
  head: Head;
  document: Document;
  dirty: boolean;
  input: Record<string, string>;
  past: Document[];
  future: Document[];
  remote?: Head;
  at: number;
  operation?: string;
  mapping?: Record<string, string>;
}
/** Survives browser close and Episode switches; quota failures are surfaced. */
export class LocalDrafts {
  constructor(
    private storage: Pick<Storage, "getItem" | "setItem" | "removeItem">,
    private dataset: string,
  ) {}
  key(ep: number) {
    return `bodypipeline:workbench:v2:${encodeURIComponent(this.dataset)}:${ep}`;
  }
  read(ep: number): Backup | null {
    const raw = this.storage.getItem(this.key(ep));
    if (!raw) return null;
    const b = JSON.parse(raw) as Backup;
    if (b.head.dataset !== this.dataset || b.head.episode !== ep)
      throw Error("本地备份来源不匹配，原记录保留");
    return b;
  }
  write(ep: number, backup: Backup) {
    try {
      this.storage.setItem(this.key(ep), JSON.stringify(backup));
    } catch {
      throw Error("本地备份失败或空间不足；请下载草稿，勿关闭页面");
    }
  }
}
export class EpisodeSelection {
  entries = new Map<
    number,
    { revision: number; origin: string; counts?: unknown }
  >();
  add(ep: number, revision: number, origin: string, counts?: unknown) {
    if (!this.entries.has(ep))
      this.entries.set(ep, { revision, origin, counts });
  }
  remove(ep: number) {
    this.entries.delete(ep);
  }
  payload(signature: string, filter: unknown) {
    if (!this.entries.size) throw Error("导出清单为空，请明确加入 Episode");
    return {
      source_signature: signature,
      episodes: [...this.entries.keys()].sort((a, b) => a - b),
      revisions: Object.fromEntries(
        [...this.entries].map(([ep, e]) => [String(ep), e.revision]),
      ),
      origins: Object.fromEntries(
        [...this.entries].map(([ep, e]) => [String(ep), e.origin]),
      ),
      filter,
    };
  }
}

/** Human display order is scoped to this Episode and subtask, never an ID. */
export function attemptOrdinals(doc: Document): Map<string, number> {
  const first = new Map<string, number>();
  for (const s of doc.segments)
    if (s.attemptId)
      first.set(
        s.attemptId,
        Math.min(first.get(s.attemptId) ?? Infinity, s.start),
      );
  const counts = new Map<string, number>(),
    result = new Map<string, number>();
  const active = doc.attempts
    .filter((a) => !a.archived && first.has(a.id))
    .sort(
      (a, b) => first.get(a.id)! - first.get(b.id)! || a.id.localeCompare(b.id),
    );
  for (const a of active) {
    const key = a.subtaskId || "OUT_OF_TEMPLATE";
    const ordinal = (counts.get(key) || 0) + 1;
    counts.set(key, ordinal);
    result.set(a.id, ordinal);
  }
  return result;
}
