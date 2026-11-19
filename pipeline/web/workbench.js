// pipeline/server/viewer_ui/annotation-navigation-model.ts
function filterNavigationRows(rows, filter, visibility) {
  const excluded = new Set(visibility.excluded), flagged = visibility.flagged === null ? null : new Set(visibility.flagged);
  return rows.filter((row) => !excluded.has(row.episode) && (!flagged || flagged.has(row.episode)) && (filter.review === "checked" ? row.complete : filter.review === "unchecked" ? !row.complete : true));
}

// pipeline/server/viewer_ui/workbench-media.ts
function nearestFrame(times, time) {
  let lo = 0, hi = times.length - 1;
  while (lo < hi) {
    const mid = lo + hi >>> 1;
    if (times[mid] < time)
      lo = mid + 1;
    else
      hi = mid;
  }
  return lo && time - times[lo - 1] <= times[lo] - time ? lo - 1 : lo;
}
function presentedFrame(media, camera, pts) {
  const c = media.cameras[camera], time = pts - c.start;
  if (!Number.isFinite(pts) || pts < c.start - 0.000001 || pts >= c.end - 0.000001)
    return null;
  const n = nearestFrame(media.timestamps, time);
  return Math.abs(media.timestamps[n] - time) <= 0.51 / media.fps ? n : null;
}
function seekTime(media, camera, frame) {
  const c = media.cameras[camera], start = c.start + media.timestamps[frame];
  const width = frame + 1 < media.frames ? media.timestamps[frame + 1] - media.timestamps[frame] : Math.min(1 / media.fps, c.end - start);
  return Math.min(c.end - 0.000001, start + width * 0.25);
}

class LatestSeek {
  target = 0;
  issued = null;
  completed = null;
  request(frame) {
    this.target = frame;
  }
  take() {
    if (this.issued !== null || this.completed === this.target)
      return null;
    return this.issued = this.target;
  }
  finish() {
    this.completed = this.issued;
    this.issued = null;
  }
}

class VideoTransport {
  options;
  cameras;
  alignedFrame = null;
  target = 0;
  playing = false;
  disposed = false;
  browsing = false;
  generation = 0;
  playIntent = 0;
  pumpFrame = 0;
  fallbackTimer;
  fallback;
  fallbackFrame = null;
  objects = [];
  listeners = [];
  published = -1;
  constructor(options) {
    this.options = options;
    this.cameras = options.hosts.map((host) => {
      const video = host.querySelector("video");
      const c = {
        host,
        video,
        image: host.querySelector("img"),
        queue: new LatestSeek,
        shown: null,
        native: typeof video.requestVideoFrameCallback === "function",
        clean: []
      };
      const listen = (event, fn) => {
        video.addEventListener(event, fn);
        c.clean.push(() => video.removeEventListener(event, fn));
      };
      listen("loadedmetadata", () => this.schedulePump());
      listen("seeked", () => {
        c.queue.finish();
        this.publish();
        this.schedulePump();
      });
      listen("error", () => {
        if (!this.disposed)
          this.options.error("浏览器无法读取此视频；暂停精确画面仍可尝试服务端解码。");
      });
      if (!c.native)
        listen("timeupdate", () => {
          if (this.playing)
            this.present(c, video.currentTime, false);
        });
      return c;
    });
    this.cameras.forEach((c) => this.watch(c));
  }
  watch(c) {
    if (!c.native || this.disposed)
      return;
    c.callback = c.video.requestVideoFrameCallback((_now, metadata) => {
      if (this.disposed)
        return;
      this.present(c, metadata.mediaTime, true);
      this.watch(c);
    });
  }
  present(c, pts, exact) {
    if (!c.image.hidden)
      return;
    const i = this.cameras.indexOf(c), media = this.options.media;
    c.pts = pts;
    c.shown = exact ? presentedFrame(media, i, pts) : null;
    c.host.dataset.mediaPts = String(pts);
    c.host.dataset.clock = exact ? "presented" : "preview";
    if (c.shown === null)
      delete c.host.dataset.displayedFrame;
    else
      c.host.dataset.displayedFrame = String(c.shown);
    c.host.querySelector(".frame-tag").textContent = c.shown === null ? "视频预览" : `DATA FRAME ${c.shown}`;
    if (this.playing && i === 0) {
      const time = pts - media.cameras[0].start;
      this.target = Math.max(0, Math.min(media.frames - 1, nearestFrame(media.timestamps, time)));
      if (time >= media.timestamps[media.frames - 1] - 0.000001) {
        this.pause(media.frames - 1);
        return;
      }
      this.cameras.slice(1).forEach((other, j) => {
        if (other.video.seeking)
          return;
        const delta = other.video.currentTime - media.cameras[j + 1].start - (c.video.currentTime - media.cameras[0].start);
        if (Math.abs(delta) > 0.12) {
          other.video.currentTime = Math.min(media.cameras[j + 1].end - 0.000001, media.cameras[j + 1].start + c.video.currentTime - media.cameras[0].start);
        } else
          other.video.playbackRate = Math.abs(delta) > 1 / media.fps ? delta > 0 ? 0.96 : 1.04 : 1;
      });
    }
    this.publish();
  }
  publish() {
    if (this.disposed)
      return;
    const aligned = !this.playing && !this.browsing && this.cameras.length > 0 && this.cameras.every((c) => c.shown === this.target && (!c.video.seeking || c.host.dataset.clock === "decoded"));
    this.alignedFrame = aligned ? this.target : null;
    const shown = this.cameras.map((c) => c.shown === null ? "—" : c.shown).join(" / ");
    this.options.update(this.target, aligned ? `已对齐数据帧 ${this.target} · ${this.cameras.length} 个相机` : this.playing ? `连续播放 · 实际画面 ${shown}` : `预览目标 ${this.target} · 已显示 ${shown}`, aligned, this.playing);
    for (const listener of this.listeners)
      if (listener.frame !== this.target || aligned)
        listener.resolve(aligned && listener.frame === this.target);
    this.listeners = this.listeners.filter((l) => l.frame === this.target && !aligned);
    if (aligned) {
      clearTimeout(this.fallbackTimer);
      this.fallback?.abort();
      if (this.published !== this.target) {
        this.published = this.target;
        this.options.settled();
      }
    }
  }
  schedulePump() {
    if (this.disposed || this.playing || this.pumpFrame)
      return;
    this.pumpFrame = requestAnimationFrame(() => {
      this.pumpFrame = 0;
      if (this.disposed || this.playing)
        return;
      this.cameras.forEach((c, i) => {
        c.queue.request(this.target);
        if (c.video.readyState < 1 || c.video.seeking || c.queue.issued !== null || c.shown === this.target)
          return;
        const next = c.queue.take();
        if (next !== null)
          c.video.currentTime = seekTime(this.options.media, i, next);
      });
      this.publish();
    });
  }
  seek(frame, preview = false) {
    if (this.disposed)
      return;
    const next = Math.max(0, Math.min(this.options.media.frames - 1, Math.round(frame)));
    const changed = this.target !== next || this.playing;
    if (changed) {
      this.playIntent++;
      this.generation++;
      this.fallback?.abort();
      this.published = -1;
      this.objects.forEach(URL.revokeObjectURL);
      this.objects = [];
      this.cameras.forEach((c) => {
        if (!c.image.hidden) {
          c.shown = null;
          delete c.host.dataset.displayedFrame;
          delete c.host.dataset.mediaPts;
          c.host.dataset.clock = "preview";
          c.host.querySelector(".frame-tag").textContent = "视频预览";
        }
        c.image.hidden = true;
      });
    }
    this.target = next;
    this.browsing = preview;
    this.playing = false;
    this.cameras.forEach((c) => {
      c.video.pause();
      c.video.playbackRate = 1;
      c.queue.request(next);
    });
    this.publish();
    this.schedulePump();
    clearTimeout(this.fallbackTimer);
    if (!preview && this.alignedFrame === null) {
      this.fallbackTimer = setTimeout(() => void this.precise(), this.cameras.every((c) => c.native) ? 700 : 80);
    }
  }
  async precise() {
    if (this.disposed || this.playing || this.browsing || this.alignedFrame !== null)
      return;
    if (this.fallback && !this.fallback.signal.aborted && this.fallbackFrame === this.target)
      return;
    this.fallback?.abort();
    const abort = this.fallback = new AbortController, generation = this.generation, target = this.target;
    this.fallbackFrame = target;
    let objects = [];
    try {
      const frames = await this.options.exact(target, abort.signal);
      if (abort.signal.aborted || this.disposed || generation !== this.generation)
        return;
      if (frames.length !== this.cameras.length || frames.some((f, i) => f.frame !== target || presentedFrame(this.options.media, i, f.pts) !== target))
        throw Error("精确画面时间戳与数据帧不一致");
      objects = frames.map((f) => URL.createObjectURL(f.blob));
      const images = await Promise.all(objects.map(async (url) => {
        const image = new Image;
        image.src = url;
        await image.decode();
        return image;
      }));
      if (abort.signal.aborted || this.disposed || generation !== this.generation) {
        objects.forEach(URL.revokeObjectURL);
        return;
      }
      this.objects.forEach(URL.revokeObjectURL);
      this.objects = objects;
      objects = [];
      this.cameras.forEach((c, i) => {
        const image = images[i];
        image.alt = c.image.alt;
        image.dataset.frameImage = c.image.dataset.frameImage;
        c.image.replaceWith(image);
        c.image = image;
        c.shown = target;
        c.host.dataset.displayedFrame = String(target);
        c.host.dataset.mediaPts = String(frames[i].pts);
        c.host.dataset.clock = "decoded";
        c.host.querySelector(".frame-tag").textContent = `DATA FRAME ${target}`;
      });
      this.publish();
    } catch (e) {
      if (!abort.signal.aborted && !this.disposed && generation === this.generation)
        this.options.error(`精确画面暂不可用：${e.message}。目标帧未被标为已对齐。`);
    } finally {
      objects.forEach(URL.revokeObjectURL);
      if (this.fallback === abort)
        this.fallbackFrame = null;
    }
  }
  ready() {
    if (this.alignedFrame === this.target)
      return Promise.resolve(true);
    const target = this.target;
    return new Promise((resolve) => {
      const listener = { frame: target, resolve: (ok) => {
        clearTimeout(timer);
        resolve(ok);
      } };
      const timer = setTimeout(() => {
        this.listeners = this.listeners.filter((l) => l !== listener);
        resolve(false);
      }, 8000);
      this.listeners.push(listener);
    });
  }
  async play() {
    if (this.playing || this.disposed)
      return;
    this.seek(this.target);
    const intent = ++this.playIntent;
    if (!await this.ready() || intent !== this.playIntent || this.disposed)
      return;
    this.fallback?.abort();
    this.generation++;
    this.playing = true;
    this.browsing = false;
    this.cameras.forEach((c) => {
      c.image.hidden = true;
      c.queue.completed = null;
    });
    try {
      await Promise.all(this.cameras.map((c) => c.video.play()));
      if (!this.disposed && intent === this.playIntent)
        this.publish();
    } catch (e) {
      if (this.disposed || intent !== this.playIntent)
        return;
      this.pause();
      this.options.error(e.message);
    }
  }
  pause(at) {
    ++this.playIntent;
    if (!this.playing && at === undefined)
      return;
    const shown = this.cameras[0]?.shown;
    this.seek(at ?? shown ?? this.target);
  }
  dispose() {
    this.disposed = true;
    this.playIntent++;
    this.fallback?.abort();
    clearTimeout(this.fallbackTimer);
    cancelAnimationFrame(this.pumpFrame);
    this.listeners.forEach((l) => l.resolve(false));
    this.listeners = [];
    this.objects.forEach(URL.revokeObjectURL);
    this.cameras.forEach((c) => {
      c.clean.forEach((fn) => fn());
      if (c.callback !== undefined)
        c.video.cancelVideoFrameCallback(c.callback);
      c.video.pause();
      c.video.removeAttribute("src");
      c.video.load();
    });
  }
}

// pipeline/server/viewer_ui/workbench-model.ts
var copy = (x) => structuredClone(x);
function uid() {
  const b = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
function validate(doc, count) {
  let end = 0;
  const ids = new Set;
  const steps = new Set(doc.templateSnapshot.subtasks.map((s) => s.id));
  const attempts = new Map(doc.attempts.map((a) => [a.id, a]));
  for (const row of [...doc.attempts, ...doc.segments, ...doc.frames]) {
    if (!row.id || ids.has(row.id))
      throw Error("记录身份重复");
    ids.add(row.id);
  }
  for (const s of [...doc.segments].sort((a, b) => a.start - b.start)) {
    if (!Number.isInteger(s.start) || !Number.isInteger(s.end) || s.start < end || s.start < 0 || s.end <= s.start || s.end > count)
      throw Error("片段重叠或超出数据帧范围");
    end = s.end;
    const a = attempts.get(s.attemptId || "");
    if (s.attemptId && (!a || a.archived))
      throw Error("关联的尝试不存在");
    if (s.label === "SUBTASK" && (!a || a.subtaskId !== s.subtaskId || !steps.has(s.subtaskId || "")))
      throw Error("正式标签需要明确关联同标签的尝试");
    if (s.label !== "SUBTASK" && s.subtaskId)
      throw Error("特殊状态不是正式标签");
    if (s.label === "NULL" && s.attemptId)
      throw Error("NULL 不能关联尝试");
  }
}
function clean(doc) {
  const active = new Set(doc.segments.map((s) => s.attemptId));
  doc.attempts.forEach((a) => {
    if (!active.has(a.id) && !a.archived) {
      a.provenance = {
        ...a.provenance,
        archivedTemplateSnapshot: copy(doc.templateSnapshot)
      };
    }
    a.archived = !active.has(a.id);
    if ("attempt-outcome" in a.values)
      a.values["attempt-outcome"] = a.outcome ? { success: "成功", failure: "失败", unknown: "无法判断" }[a.outcome] : null;
  });
  doc.segments.sort((a, b) => a.start - b.start);
  return doc;
}
function operation(doc, count, name, change) {
  const next = copy(doc);
  change(next);
  clean(next);
  validate(next, count);
  const previous = new Map(doc.segments.map((s) => [s.id, s]));
  for (const s of next.segments) {
    const old = previous.get(s.id);
    const oldAttempt = doc.attempts.find((a) => a.id === old?.attemptId);
    const attempt = next.attempts.find((a) => a.id === s.attemptId);
    const withoutReview = (x) => x ? { ...x, review: undefined } : undefined;
    if (JSON.stringify(withoutReview(s)) !== JSON.stringify(withoutReview(old)) || JSON.stringify(attempt) !== JSON.stringify(oldAttempt) || JSON.stringify(next.values) !== JSON.stringify(doc.values) || JSON.stringify(next.manualAtoms) !== JSON.stringify(doc.manualAtoms) || JSON.stringify(next.templateSnapshot) !== JSON.stringify(doc.templateSnapshot) || JSON.stringify(next.frames.filter((f) => s.start <= f.frame && f.frame < s.end)) !== JSON.stringify(doc.frames.filter((f) => s.start <= f.frame && f.frame < s.end)))
      delete s.review;
  }
  const removed = doc.segments.filter((s) => !next.segments.some((n) => n.id === s.id));
  const archived = doc.attempts.filter((a) => !next.attempts.some((n) => n.id === a.id && !n.archived));
  if (removed.length || archived.length)
    next.history.push({
      kind: name,
      at: Date.now(),
      records: removed,
      attempts: archived
    });
  return next;
}
function resize(doc, count, id, side, frame) {
  return operation(doc, count, "boundary", (d) => {
    const s = d.segments.find((s) => s.id === id);
    if (!s)
      throw Error("未选片段");
    const other = d.segments.find((o) => o.id !== id && (side === "start" ? o.end === s.start : o.start === s.end));
    if (side === "start") {
      if (frame < 0 || frame >= s.end || other && frame <= other.start)
        throw Error("交界必须保留两侧至少一帧");
      s.start = frame;
      if (other)
        other.end = frame;
    } else {
      if (frame <= s.start || frame > count || other && frame >= other.end)
        throw Error("交界必须保留两侧至少一帧");
      s.end = frame;
      if (other)
        other.start = frame;
    }
  });
}
function replace(doc, count, start, end, segment, attempt) {
  return operation(doc, count, "range-relabel", (d) => {
    const result = [];
    for (const s of d.segments) {
      if (s.end <= start || s.start >= end) {
        result.push(s);
        continue;
      }
      if (s.start < start)
        result.push({ ...s, end: start });
      if (s.end > end)
        result.push({
          ...copy(s),
          id: s.start < start ? uid() : s.id,
          start: end
        });
    }
    if (attempt) {
      const old = d.attempts.find((a) => a.id === attempt.id);
      if (old && JSON.stringify(old) !== JSON.stringify(attempt))
        throw Error("局部操作不能改写已有尝试的共享字段");
      if (!old)
        d.attempts.push(copy(attempt));
    }
    result.push({ ...segment, start, end });
    d.segments = result;
  });
}
function move(doc, count, id, start) {
  return operation(doc, count, "move", (d) => {
    const s = d.segments.find((s) => s.id === id);
    if (!s)
      throw Error("未选片段");
    const length = s.end - s.start;
    s.start = start;
    s.end = start + length;
  });
}
function relabelAttempt(doc, count, id, subtaskId) {
  return operation(doc, count, "relabel-whole-attempt", (d) => {
    const a = d.attempts.find((a) => a.id === id), step = d.templateSnapshot.subtasks.find((s) => s.id === subtaskId);
    if (!a || !step)
      throw Error("标签或尝试不存在");
    a.subtaskId = step.id;
    a.subtaskName = step.name;
    d.segments.forEach((s) => {
      if (s.attemptId === id && s.label === "SUBTASK")
        s.subtaskId = step.id;
    });
  });
}

class UndoHistory {
  past = [];
  future = [];
  edit(before, after) {
    this.past.push(copy(before));
    this.future = [];
    return after;
  }
  undo(current) {
    const before = this.past.pop();
    if (!before)
      return current;
    this.future.push(copy(current));
    return copy(before);
  }
  redo(current) {
    const after = this.future.pop();
    if (!after)
      return current;
    this.past.push(copy(current));
    return copy(after);
  }
}
function inclusive(first, last, count) {
  if (!Number.isInteger(first) || !Number.isInteger(last) || first < 0 || last < first || last >= count)
    throw Error(`首末帧需在 0–${count - 1}，首尾均包含`);
  return { start: first, end: last + 1 };
}
function gaps(doc, count) {
  const r = [];
  let end = 0;
  for (const s of [...doc.segments].sort((a, b) => a.start - b.start)) {
    if (s.start > end)
      r.push([end, s.start]);
    end = s.end;
  }
  if (end < count)
    r.push([end, count]);
  return r;
}
var editableTarget = (target) => target instanceof HTMLElement && !!target.closest("input,textarea,select,[contenteditable=true]");
function templateDiff(old, next) {
  const result = [];
  for (const [kind, items, others] of [
    ["subtask", old.subtasks, next.subtasks],
    ["field", old.fields, next.fields]
  ]) {
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
function upgrade(doc, count, template, mapping) {
  return operation(doc, count, "template-upgrade", (d) => {
    const steps = new Map(template.subtasks.map((s) => [s.id, s]));
    for (const a of d.attempts) {
      if (a.archived) {
        a.provenance ??= {};
        a.provenance.archivedTemplateSnapshot ??= copy(doc.templateSnapshot);
        continue;
      }
      if (!a.subtaskId)
        continue;
      const mapped = steps.get(mapping[a.subtaskId]);
      if (!mapped)
        throw Error("请明确处理所有已使用标签的映射，未解决时可继续旧快照");
      a.subtaskId = mapped.id;
      a.subtaskName = mapped.name;
    }
    for (const s of d.segments)
      if (s.label === "SUBTASK") {
        const mapped = steps.get(mapping[s.subtaskId || ""]);
        if (!mapped)
          throw Error("片段映射未解决");
        s.subtaskId = mapped.id;
      }
    d.history.push({
      kind: "template-original",
      snapshot: d.templateSnapshot,
      values: copy(d.values),
      attempts: copy(doc.attempts),
      frames: copy(d.frames),
      countEvidence: copy(d.countEvidence)
    });
    d.countEvidence = {};
    d.templateId = template.id;
    d.templateSnapshot = copy(template);
  });
}

class LocalDrafts {
  storage;
  dataset;
  constructor(storage, dataset) {
    this.storage = storage;
    this.dataset = dataset;
  }
  key(ep) {
    return `bodypipeline:workbench:v2:${encodeURIComponent(this.dataset)}:${ep}`;
  }
  read(ep) {
    const raw = this.storage.getItem(this.key(ep));
    if (!raw)
      return null;
    const b = JSON.parse(raw);
    if (b.head.dataset !== this.dataset || b.head.episode !== ep)
      throw Error("本地备份来源不匹配，原记录保留");
    return b;
  }
  write(ep, backup) {
    try {
      this.storage.setItem(this.key(ep), JSON.stringify(backup));
    } catch {
      throw Error("本地备份失败或空间不足；请下载草稿，勿关闭页面");
    }
  }
}

class EpisodeSelection {
  entries = new Map;
  add(ep, revision, origin, counts) {
    if (!this.entries.has(ep))
      this.entries.set(ep, { revision, origin, counts });
  }
  remove(ep) {
    this.entries.delete(ep);
  }
  payload(signature, filter) {
    if (!this.entries.size)
      throw Error("导出清单为空，请明确加入 Episode");
    return {
      source_signature: signature,
      episodes: [...this.entries.keys()].sort((a, b) => a - b),
      revisions: Object.fromEntries([...this.entries].map(([ep, e]) => [String(ep), e.revision])),
      origins: Object.fromEntries([...this.entries].map(([ep, e]) => [String(ep), e.origin])),
      filter
    };
  }
}
function attemptOrdinals(doc) {
  const first = new Map;
  for (const s of doc.segments)
    if (s.attemptId)
      first.set(s.attemptId, Math.min(first.get(s.attemptId) ?? Infinity, s.start));
  const counts = new Map, result = new Map;
  const active = doc.attempts.filter((a) => !a.archived && first.has(a.id)).sort((a, b) => first.get(a.id) - first.get(b.id) || a.id.localeCompare(b.id));
  for (const a of active) {
    const key = a.subtaskId || "OUT_OF_TEMPLATE";
    const ordinal = (counts.get(key) || 0) + 1;
    counts.set(key, ordinal);
    result.set(a.id, ordinal);
  }
  return result;
}

// pipeline/server/viewer_ui/workbench.ts
var pendingSaves = new Map;
function mountWorkbench(root, options = {}) {
  const lifecycle = new AbortController;
  let disposed = false;
  let requestedTime = options.initialTime;
  if (options.navigation)
    root.querySelector(".episode-picker")?.remove();
  const listen = (name, run) => window.addEventListener(name, run, { signal: lifecycle.signal });
  const $ = (id) => root.querySelector(`#${id}`);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[c]);
  const value = (id) => $(id);
  const btn = (id, run) => $(id).addEventListener("click", () => safe(run));
  const labels = {
    SUBTASK: "正式标签",
    NULL: "NULL · 已确认背景",
    UNKNOWN: "UNKNOWN · 证据不足",
    OUT_OF_TEMPLATE: "OUT OF TEMPLATE · 模板外活动"
  };
  const outcomes = { success: "成功", failure: "失败", unknown: "无法判断" };
  const apiBase = "/api/annotation-workbench/";
  let dataset = options.dataset ?? new URLSearchParams(location.search).get("dataset") ?? "", episode = options.episode ?? Number(new URLSearchParams(location.search).get("episode") || 0);
  let allRows = [], rows = [], page = 0, filterState = {}, signature = "", library = {
    revision: 0,
    templates: []
  };
  let drafts, selection = new EpisodeSelection, active = null, tool = "browse", frame = 0, zoomStart = 0, zoomEnd = 1, playing = false;
  let navigationLoading = false;
  let navigationInitialized = false;
  let transport;
  let reviewingEpisode;
  const sessions = new Map;
  let loadGeneration = 0, datasetGeneration = 0, listGeneration = 0;
  function note(text = "") {
    if (disposed)
      return;
    $("notice").textContent = text;
  }
  function safe(run) {
    try {
      const result = run();
      if (result instanceof Promise)
        result.catch((e) => note(String(e.message || e)));
    } catch (e) {
      note(e.message || String(e));
    }
  }
  async function request(path, body, finishSave = false) {
    if (disposed)
      throw Error("工作区已关闭，修改已在本机保留");
    const response = await fetch(apiBase + path, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    if (disposed && !finishSave)
      throw Error("工作区已关闭，修改已在本机保留");
    let result;
    try {
      result = await response.json();
    } catch {
      throw Error("服务未返回有效结果，本地草稿保留");
    }
    if (!response.ok) {
      const err = Error(typeof result.detail === "string" ? result.detail : Array.isArray(result.detail) ? result.detail.map((d) => `${(d.loc || []).filter((v) => v !== "body").join(" → ")}: ${d.msg}`).join("；") : "请求失败，请刷新后重试");
      err.status = response.status;
      throw err;
    }
    return result;
  }
  function current() {
    if (!active)
      throw Error("先打开一条数据");
    return active;
  }
  function attemptText(a, doc = current().doc) {
    const number = attemptOrdinals(doc).get(a.id);
    return `${a.subtaskName || "模板外活动"} · ${number ? `第 ${number} 次` : "历史操作"}`;
  }
  function selected(s = current()) {
    return s.doc.segments.find((x) => x.id === s.input.selected);
  }
  function ranges(s = current()) {
    return inclusive(Number(s.input.first), Number(s.input.last), s.head.frames);
  }
  function backup(s) {
    try {
      new LocalDrafts(localStorage, s.head.dataset).write(s.head.episode, {
        head: s.head,
        document: s.doc,
        dirty: s.dirty,
        input: s.input,
        past: s.undo.past,
        future: s.undo.future,
        remote: s.remote,
        mapping: s.mapping,
        operation: s.operation,
        at: Date.now()
      });
      s.localError = "";
    } catch (e) {
      s.localError = e.message;
      note(e.message);
    }
  }
  function rememberInput() {
    if (!active)
      return;
    for (const [id, key] of [
      ["range-first", "first"],
      ["range-last", "last"],
      ["new-label", "label"],
      ["association", "association"],
      ["new-note", "note"],
      ["new-description", "description"],
      ["new-outcome", "outcome"]
    ])
      active.input[key] = value(id).value;
    backup(active);
    $("input-status").textContent = active.localError ? "本地备份失败" : "编辑中输入已在本机保留";
  }
  function change(next, name = "edit", render = true) {
    const s = current();
    s.doc = s.undo.edit(s.doc, next);
    s.dirty = true;
    s.operation = name;
    s.retryAt = 0;
    backup(s);
    if (render) {
      renderTimeline();
      renderSummary();
      renderStatus();
    }
    schedule();
  }
  function edit(name, modify, render = true) {
    const s = current();
    change(operation(s.doc, s.head.frames, name, modify), name, render);
  }
  let saveTimer;
  function schedule() {
    if (disposed)
      return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      for (const s of sessions.values())
        if (s.dirty && !s.remote && !s.readFailure && !s.saving && Date.now() >= s.retryAt)
          safe(() => sync(s));
    }, 500);
  }
  async function sync(s) {
    if (s.saving)
      return s.saving;
    if (!s.dirty || s.remote || s.readFailure)
      return;
    const snapshot = copy(s.doc);
    const op = s.operation;
    const revision = s.head.revision;
    s.error = "";
    const promise = (async () => {
      try {
        const result = await request(`${s.head.dataset}/episodes/${s.head.episode}`, {
          revision,
          source_signature: s.head.source_signature,
          document: snapshot,
          operation: op,
          ...s.mapping ? { template_mapping: s.mapping } : {}
        }, true);
        s.head = { ...s.head, ...result };
        if (JSON.stringify(s.doc) === JSON.stringify(snapshot)) {
          s.doc = copy(result.document);
          s.dirty = false;
          s.mapping = undefined;
        }
        if (!disposed && dataset === s.head.dataset && selection.entries.has(s.head.episode))
          renderSelectionCount();
        backup(s);
        if (!disposed && active === s) {
          renderStatus();
          renderSummary();
          renderTimeline();
        }
        const row = dataset === s.head.dataset ? allRows.find((r) => r.episode === s.head.episode) : undefined;
        if (row && !disposed) {
          const saved = {
            revision: s.head.revision,
            complete: s.head.complete,
            status: result.status
          };
          Object.assign(row, saved);
          const filteredRow = rows.find((row) => row.episode === s.head.episode);
          if (filteredRow)
            Object.assign(filteredRow, saved);
          renderEpisodes();
          if (filterState.subtask)
            safe(() => refresh(false));
        }
      } catch (e) {
        s.error = e.message;
        s.retryAt = Date.now() + 8000;
        if (e.status === 409) {
          try {
            s.remote = await request(`${s.head.dataset}/episodes/${s.head.episode}`);
          } catch {}
        }
        backup(s);
        if (active === s)
          renderStatus();
      } finally {
        s.saving = undefined;
        if (active === s)
          renderStatus();
        if (s.dirty && !s.remote)
          schedule();
      }
    })();
    s.saving = promise;
    const saveKey = `${s.head.dataset}:${s.head.episode}`;
    pendingSaves.set(saveKey, promise);
    promise.finally(() => {
      if (pendingSaves.get(saveKey) === promise)
        pendingSaves.delete(saveKey);
    });
    renderStatus();
    await promise;
  }
  async function direct(extra) {
    const s = current();
    await sync(s);
    if (s.readFailure)
      throw Error(s.readFailure);
    if (s.dirty || s.remote)
      throw Error("请先同步或处理冲突；本地输入保留");
    const original = copy(s.doc);
    const response = await request(`${s.head.dataset}/episodes/${s.head.episode}`, {
      revision: s.head.revision,
      source_signature: s.head.source_signature,
      document: original,
      operation: "human-review",
      ...extra
    });
    s.undo.edit(s.doc, response.document);
    s.head = { ...s.head, ...response };
    if (JSON.stringify(s.doc) === JSON.stringify(original))
      s.doc = copy(response.document);
    else {
      s.dirty = true;
      schedule?.();
    }
    backup(s);
    if (active === s) {
      renderAll(false);
      await refresh(false);
    }
    return response;
  }
  function renderStatus() {
    const s = active;
    if (!s || disposed)
      return;
    $("save-status").innerHTML = s.remote ? '<span class="error">版本冲突 · 双方已保留</span>' : s.saving ? '<span class="saving">正在同步…</span>' : s.error ? `<span class="error">未同步 · ${esc(s.error)}</span>` : s.dirty ? '<span class="saving">草稿已在本机 · 待同步</span>' : '<span class="synced">已同步服务器</span>';
    $("save-status").innerHTML += `<br><span class="hint">${s.head.complete && !s.dirty ? "整个 Episode 已检查" : "整个 Episode 未检查 · 可直接导出"}${s.localError ? " · 本地备份失败" : " · 本地备份可恢复"}</span>`;
    const c = $("conflict");
    c.hidden = !s.remote && !s.readFailure;
    if (s.readFailure) {
      c.innerHTML = `<strong>旧来源草稿已保留</strong><p>${esc(s.readFailure)}</p>`;
    } else if (s.remote) {
      c.innerHTML = '<strong>这个 Episode 已在其他窗口更新</strong><p>本地修改与服务器内容均保留。其他 Episode 可以继续工作。</p><button id="compare-conflict">对照双方并处理</button>';
      btn("compare-conflict", conflictDialog);
    }
    $("undo").toggleAttribute("disabled", !s.undo.past.length);
    $("redo").toggleAttribute("disabled", !s.undo.future.length);
  }
  function renderSummary() {
    if (!active)
      return;
    const s = active, blank = gaps(s.doc, s.head.frames);
    const fromAI = !!(s.doc.provenance?.initialAI || s.doc.provenance?.annotation || s.doc.provenance?.templateSignature || s.doc.provenance?.model || s.doc.segments.some((seg) => seg.provenance?.annotation));
    const origin = fromAI ? s.doc.provenance?.humanEdited ? "AI 标注，经人工修改" : "AI 标注" : s.doc.provenance?.humanEdited ? "人工修改的标注" : "已有标注";
    $("document-summary").innerHTML = `<span>${origin}</span><span>${s.doc.segments.length} 个片段</span><span>${new Set(s.doc.segments.map((x) => x.attemptId).filter(Boolean)).size} 次操作</span><span>${blank.length ? `空白 ${blank.map(([a, b]) => `${a}–${b - 1}`).join("、")}` : "全片已有标注"}</span><span>${s.head.complete && !s.dirty ? "整个 Episode 已检查" : "整个 Episode 未检查"}</span><span>模板：${esc(s.doc.templateSnapshot.name)}</span>`;
    $("review-state").textContent = s.head.complete && !s.dirty ? "整个 Episode 已检查" : "可跳过，直接导出";
    const button = $("complete");
    button.disabled = !!reviewingEpisode;
    button.textContent = reviewingEpisode ? "正在保存整条检查状态…" : "检查完成，下一条";
  }
  async function finishEpisodeReview() {
    const s = current();
    if (reviewingEpisode)
      return;
    reviewingEpisode = s;
    renderSummary();
    try {
      if (!s.head.complete || s.dirty)
        await direct({ confirm: [[0, s.head.frames]], complete: true });
      if (active !== s || disposed)
        return;
      if (s.dirty || !s.head.complete) {
        note("检查期间又有修改，请检查最新标注后再确认。");
        return;
      }
      const next = navigationRows().find((r) => r.episode > s.head.episode && !r.complete);
      if (next)
        await openEpisode(next.episode);
      else
        note("当前 Episode 的标注已检查并保存；后面没有尚未检查的数据。");
    } finally {
      reviewingEpisode = undefined;
      if (active && !disposed)
        renderSummary();
    }
  }
  function navigationRows() {
    return filterNavigationRows(rows, filterState, options.navigation?.getVisibility() || { excluded: [], flagged: null });
  }
  function availableRows() {
    const excluded = new Set(options.navigation?.getVisibility().excluded || []);
    return allRows.filter((row) => !excluded.has(row.episode));
  }
  function persistNavigation() {
    if (!options.navigation || !navigationInitialized || navigationLoading || !allRows.length)
      return;
    try {
      localStorage.setItem(`bp-navigation:${dataset}`, JSON.stringify({
        filter: filterState,
        page,
        episode,
        source_signature: signature
      }));
    } catch {
      note("筛选状态本地备份失败，请保持页面打开。");
    }
  }
  function publishNavigation() {
    if (!options.navigation || disposed)
      return;
    const filtered = navigationRows();
    const pageCount = Math.max(1, Math.ceil(filtered.length / 10));
    if (allRows.length)
      page = Math.max(0, Math.min(page, pageCount - 1));
    options.navigation.publish({
      dataset,
      episode,
      ready: !!active,
      loading: navigationLoading,
      error: "",
      rows: filtered.slice(page * 10, (page + 1) * 10),
      total: availableRows().length,
      resultCount: filtered.length,
      page: Math.min(page, pageCount - 1),
      pageCount,
      selected: [...selection.entries.keys()],
      filter: copy(filterState),
      subtasks: active?.doc.templateSnapshot.subtasks.map((step) => ({
        id: step.id,
        name: step.name
      })) || [],
      currentOutsideFilter: !!active && !filtered.some((row) => row.episode === episode)
    });
    persistNavigation();
  }
  function renderEpisodes() {
    if (options.navigation) {
      publishNavigation();
      return;
    }
    const list = navigationRows().slice(page * 10, (page + 1) * 10);
    $("episode-list").innerHTML = list.map((r) => `<div class="episode-row ${r.episode === episode ? "current" : ""}"><input type="checkbox" aria-label="加入 Episode ${r.episode}" data-add="${r.episode}" ${selection.entries.has(r.episode) ? "checked" : ""}><button data-open="${r.episode}"><strong>Episode ${String(r.episode).padStart(3, "0")}</strong> <span class="pill ${r.complete ? "green" : "amber"}">${r.complete ? "已检查" : "未检查"}</span><small>${r.frames} 帧<br>${r.counts?.recorded_count == null ? "" : `已记录 ${r.counts.recorded_count} 次 · ${r.counts.complete ? "次数确定" : "次数不确定"}`}</small></button></div>`).join("") || '<div class="empty">没有符合筛选条件的数据。可以清除筛选后重新选择。</div>';
    $("page-status").textContent = `${page + 1} / ${Math.max(1, Math.ceil(rows.length / 10))} · ${rows.length} 条`;
    $("episode-list").querySelectorAll("[data-open]").forEach((el) => el.onclick = () => safe(() => openEpisode(Number(el.dataset.open))));
    $("episode-list").querySelectorAll("[data-add]").forEach((el) => el.onchange = () => {
      const r = rows.find((r) => r.episode === Number(el.dataset.add));
      if (el.checked)
        selection.add(r.episode, r.revision, filterState.subtask ? "filter" : "manual", r.counts);
      else
        selection.remove(r.episode);
      persistSelection();
      renderSelectionCount();
    });
  }
  function persistSelection() {
    try {
      localStorage.setItem(`bp-selection:${dataset}`, JSON.stringify([...selection.entries]));
    } catch (e) {
      note("选择清单本地备份失败，请保持页面打开");
    }
  }
  function renderSelectionCount() {
    $("selection-count").textContent = String(selection.entries.size);
    publishNavigation();
  }
  async function refresh(reset = true) {
    const sourceDataset = dataset, generation = ++listGeneration;
    const chosenFilter = copy(filterState);
    navigationLoading = true;
    publishNavigation();
    let query = "";
    if (chosenFilter.subtask)
      query = `?subtask=${encodeURIComponent(chosenFilter.subtask)}${chosenFilter.count !== "" && chosenFilter.count !== undefined ? `&count=${encodeURIComponent(chosenFilter.count)}` : ""}&recorded=${chosenFilter.basis === "recorded"}${chosenFilter.template_signature ? `&template_signature=${encodeURIComponent(chosenFilter.template_signature)}` : ""}`;
    try {
      const result = await request(`${sourceDataset}/episodes${query}`);
      const completeRows = !query ? result.episodes : (await request(`${sourceDataset}/episodes`)).episodes;
      if (sourceDataset !== dataset || generation !== listGeneration)
        return;
      rows = result.episodes;
      allRows = completeRows;
      signature = result.source_signature;
      try {
        localStorage.setItem(`bp-catalog:${sourceDataset}`, JSON.stringify({ rows: allRows, signature }));
      } catch {
        note("列表本地备份失败，服务器读取结果仍保留。请保持页面打开。");
      }
    } catch (e) {
      if (sourceDataset !== dataset || generation !== listGeneration)
        return;
      const cached = localStorage.getItem(`bp-catalog:${sourceDataset}`);
      if (!cached) {
        navigationLoading = false;
        publishNavigation();
        throw e;
      }
      const old = JSON.parse(cached);
      allRows = old.rows;
      rows = allRows;
      signature = old.signature;
      note(`${e.message}。来源列表使用本地备份；未同步修改继续保留，筛选需恢复连接后核实。`);
    }
    if (reset)
      page = 0;
    navigationLoading = false;
    renderEpisodes();
  }
  async function openDataset(id) {
    disposeVideo();
    const generation = ++datasetGeneration;
    ++loadGeneration;
    ++listGeneration;
    for (const s of sessions.values()) {
      backup(s);
      safe(() => sync(s));
    }
    dataset = id;
    navigationInitialized = false;
    sessions.clear();
    active = null;
    selection = new EpisodeSelection;
    drafts = new LocalDrafts(localStorage, dataset);
    try {
      selection.entries = new Map(JSON.parse(localStorage.getItem(`bp-selection:${dataset}`) || "[]"));
    } catch {}
    filterState = {};
    page = 0;
    let previousEpisode = episode;
    signature = "";
    if (options.navigation) {
      try {
        const saved = JSON.parse(localStorage.getItem(`bp-navigation:${dataset}`) || "{}");
        filterState = saved.filter || {};
        previousEpisode = Number.isInteger(saved.episode) ? saved.episode : episode;
        page = Number.isInteger(saved.page) && saved.page >= 0 ? saved.page : 0;
      } catch {}
    }
    for (const ep of options.navigation?.getVisibility().excluded || [])
      selection.remove(ep);
    navigationInitialized = true;
    allRows = [];
    rows = [];
    renderSelectionCount();
    await refresh(false);
    if (generation !== datasetGeneration || dataset !== id)
      return;
    if (options.navigation && previousEpisode !== episode) {
      const index = navigationRows().findIndex((row) => row.episode === episode);
      if (index >= 0) {
        page = Math.floor(index / 10);
        renderEpisodes();
      }
    }
    if (!allRows.length) {
      note("来源没有可用 Episode");
      return;
    }
    await openEpisode(allRows.some((r) => r.episode === episode) ? episode : allRows[0].episode);
    if (options.native)
      return;
    const prefetchDataset = dataset, prefetchDrafts = drafts, prefetchRows = [...allRows];
    (async () => {
      for (const row of prefetchRows) {
        if (disposed || dataset !== prefetchDataset)
          break;
        if (row.episode === episode)
          continue;
        try {
          if (prefetchDrafts.read(row.episode))
            continue;
          const head = await request(`${prefetchDataset}/episodes/${row.episode}`);
          prefetchDrafts.write(row.episode, {
            head,
            document: head.document,
            dirty: false,
            input: {},
            past: [],
            future: [],
            at: Date.now()
          });
        } catch {
          break;
        }
      }
    })();
  }
  async function openEpisode(ep) {
    if (disposed)
      return;
    if (options.native && active && ep !== options.episode && options.onEpisode) {
      await sync(active);
      if (active.dirty || active.remote)
        throw Error("当前修改尚未保存，请先处理保存问题");
      options.onEpisode(ep);
      return;
    }
    const load = ++loadGeneration, sourceDataset = dataset;
    if (active)
      backup(active);
    disposeVideo();
    episode = ep;
    await pendingSaves.get(`${dataset}:${ep}`);
    if (disposed || load !== loadGeneration || dataset !== sourceDataset)
      return;
    const existing = sessions.get(ep);
    if (existing)
      active = existing;
    else {
      const saved = drafts.read(ep);
      let head;
      let headError = "", readFailure;
      try {
        head = await request(`${dataset}/episodes/${ep}`);
      } catch (e) {
        const network = e instanceof TypeError;
        headError = network ? "网络不可用，正在使用本地备份" : `服务器读取失败：${e.message}`;
        if (!saved)
          throw Error(`${headError}；这个 Episode 尚未缓存`);
        head = saved.head;
        if ([401, 403, 409, 404, 422].includes(e.status))
          readFailure = `${headError}。保留旧来源草稿，仅在本地编辑；刷新核对前禁止同步到服务器。`;
        note(readFailure || headError);
      }
      const sameSource = !saved || saved.head.source_signature === head.source_signature;
      if (disposed || load !== loadGeneration || dataset !== sourceDataset)
        return;
      const use = saved && sameSource && (saved.dirty || saved.head.revision === head.revision);
      const s = {
        head,
        doc: use ? copy(saved.document) : copy(head.document),
        undo: new UndoHistory,
        input: use ? copy(saved.input) : {},
        dirty: !!use && !!saved.dirty,
        operation: saved?.operation || "edit",
        mapping: use ? saved.mapping : undefined,
        retryAt: 0,
        error: headError,
        readFailure
      };
      if (use) {
        s.undo.past = saved.past || [];
        s.undo.future = saved.future || [];
      }
      if (saved && (!sameSource || saved.dirty && saved.head.revision !== head.revision)) {
        s.remote = copy(head);
        s.head = saved.head;
        s.doc = copy(saved.document);
        s.error = "服务器修订或来源与本地草稿不同";
      }
      sessions.set(ep, s);
      active = s;
    }
    if (disposed || load !== loadGeneration || dataset !== sourceDataset)
      return;
    const s = current();
    s.input = {
      first: "0",
      last: "0",
      label: s.doc.templateSnapshot.subtasks[0]?.id || "NULL",
      association: "new",
      note: "",
      description: "",
      outcome: "",
      ...s.input
    };
    frame = Math.max(0, Math.min(s.head.frames - 1, options.native && requestedTime != null ? Math.round(requestedTime * s.head.fps) : Number(s.input.frame || 0)));
    zoomStart = 0;
    zoomEnd = s.head.frames;
    try {
      if (s.readFailure)
        throw Error(s.readFailure);
      if (s.remote && s.remote.source_signature !== s.head.source_signature)
        throw Error("来源版本改变，旧草稿与新来源隔离，请先处理冲突");
      s.media = await request(`${sourceDataset}/episodes/${ep}/media`);
      if (s.media.source_signature && s.media.source_signature !== s.head.source_signature) {
        s.media = undefined;
        throw Error("来源媒体版本不同，禁止混用新画面和旧帧标注");
      }
      localStorage.setItem(`bp-media:${sourceDataset}:${s.head.source_signature}:${ep}`, JSON.stringify(s.media));
    } catch (e) {
      const old = localStorage.getItem(`bp-media:${sourceDataset}:${s.head.source_signature}:${ep}`);
      if (old)
        s.media = JSON.parse(old);
      else
        note(e.message);
    }
    if (disposed || load !== loadGeneration || dataset !== sourceDataset)
      return;
    if (!options.native) {
      const url = new URL(location.href);
      url.searchParams.set("dataset", dataset);
      url.searchParams.set("episode", String(ep));
      history.replaceState(null, "", url);
    }
    renderAll();
    await seek(frame);
    if (s.dirty)
      schedule();
  }
  function renderAll(preview = true) {
    if (!active)
      return;
    $("episode-title").textContent = `Episode ${String(episode).padStart(3, "0")}`;
    value("scrub").max = String(active.head.frames - 1);
    value("frame").max = String(active.head.frames - 1);
    $("total").textContent = `/ ${active.head.frames - 1} · 共 ${active.head.frames} 帧`;
    if (preview)
      renderPreview();
    renderNewForm();
    renderEditor();
    renderTimeline();
    renderFields();
    renderSummary();
    renderStatus();
    renderEpisodes();
    populateFilter();
  }
  function populateFilter() {
    if (!active)
      return;
    if (options.navigation) {
      publishNavigation();
      return;
    }
    const select = $("filter-step");
    const old = select.value;
    select.innerHTML = '<option value="">全部 Episode</option>' + active.doc.templateSnapshot.subtasks.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
    select.value = old;
  }
  function disposeVideo() {
    transport?.dispose();
    transport = undefined;
    playing = false;
    $("play").textContent = "播放";
  }
  function renderPreview() {
    disposeVideo();
    const s = current();
    if (!s.media?.cameras.length) {
      $("preview").innerHTML = '<div class="empty">此来源没有可用视频。标注数据帧仍保留，不能把帧号当作画面验证。</div>';
      return;
    }
    $("preview").innerHTML = s.media.cameras.map((c, i) => `<div class="camera" data-camera="${esc(c.key)}"><span class="camera-label">${esc(c.key)}</span><video preload="auto" playsinline muted data-video="${i}" src="${apiBase}${encodeURIComponent(s.head.dataset)}/media/${c.path.split("/").map(encodeURIComponent).join("/")}?signature=${s.head.source_signature}"></video><img hidden alt="${esc(c.key)} 当前数据帧画面" data-frame-image="${i}"><span class="frame-tag">视频预览</span></div>`).join("");
    const controller = new VideoTransport({
      media: s.media,
      hosts: Array.from($("preview").querySelectorAll(".camera")),
      exact: (chosen, signal) => exactFrames(s, chosen, signal),
      update: (chosen, state, aligned, running) => {
        if (disposed || active !== s || transport !== controller)
          return;
        frame = chosen;
        playing = running;
        options.onTime?.(s.media?.timestamps[chosen] ?? chosen / s.head.fps, running);
        $("play").textContent = running ? "暂停" : "播放";
        updateFrameNumber(state);
        renderPlayhead();
        frameEditing(aligned);
      },
      settled: () => {
        if (disposed || active !== s || transport !== controller)
          return;
        renderFrameFields();
        frameEditing(true);
        backup(s);
      },
      error: (message) => {
        if (active === s && transport === controller)
          $("frame-state").textContent = message;
      }
    });
    transport = controller;
  }
  function updateFrameNumber(state) {
    value("frame").value = String(frame);
    value("scrub").value = String(frame);
    if (active)
      active.input.frame = String(frame);
    if (state)
      $("frame-state").textContent = state;
  }
  function frameEditing(aligned) {
    for (const id of ["mark-start", "mark-end", "split", "add-vqa"]) {
      const button = root.querySelector(`#${id}`);
      if (button)
        button.disabled = !aligned;
    }
    $("frame-fields").querySelectorAll("input,select,textarea").forEach((el) => el.disabled = !aligned);
  }
  function annotationFrame() {
    if (current().media?.cameras.length && transport?.alignedFrame !== frame)
      throw Error("请先暂停并等待实际画面对齐，再记录当前帧。");
    return frame;
  }
  async function exactFrames(s, chosen, signal) {
    const media = s.media;
    return Promise.all(media.cameras.map(async (c) => {
      const url = `${apiBase}${encodeURIComponent(s.head.dataset)}/episodes/${s.head.episode}/frame/${chosen}?camera=${encodeURIComponent(c.key)}&signature=${s.head.source_signature}`;
      let response;
      try {
        response = await fetch(url, { signal });
        if (!response.ok)
          throw Error(`画面读取失败 (${response.status})`);
        if ("caches" in window) {
          const cache = await caches.open("bodypipeline-workbench-frames-v2");
          cache.put(url, response.clone()).catch(() => {});
        }
      } catch (e) {
        if (signal.aborted || !("caches" in window))
          throw e;
        const old = await (await caches.open("bodypipeline-workbench-frames-v2")).match(url);
        if (!old)
          throw Error("此帧画面未缓存");
        response = old;
      }
      return {
        frame: Number(response.headers.get("X-Data-Frame") ?? chosen),
        pts: Number(response.headers.get("X-Media-Pts") ?? c.start + media.timestamps[chosen]),
        blob: await response.blob()
      };
    }));
  }
  function seek(target, preview = false) {
    const s = current();
    if (transport)
      transport.seek(target, preview);
    else {
      frame = Math.max(0, Math.min(s.head.frames - 1, Math.round(target)));
      updateFrameNumber("没有可用画面，当前仅为数据帧位置");
      renderPlayhead();
      renderFrameFields();
      options.onTime?.(frame / s.head.fps, false);
    }
  }
  function pause() {
    transport?.pause();
  }
  function play() {
    const s = current();
    if (s.readFailure)
      throw Error(s.readFailure);
    if (s.remote && s.remote.source_signature !== s.head.source_signature)
      throw Error("来源变化后禁止播放新来源覆盖旧草稿画面");
    return transport?.play();
  }
  function visiblePercent(f) {
    return (f - zoomStart) / (zoomEnd - zoomStart) * 100;
  }
  function renderTimeline() {
    if (!active)
      return;
    const s = active, lo = zoomStart, hi = zoomEnd;
    $("ruler").innerHTML = Array.from({ length: 7 }, (_, i) => `<span class="tick" style="left:${i / 6 * 100}%">${Math.round(lo + (hi - lo) * i / 6)}</span>`).join("");
    const segments = s.doc.segments.filter((x) => x.start < hi && x.end > lo);
    $("track").innerHTML = segments.map((x) => {
      const name = x.label === "SUBTASK" ? s.doc.templateSnapshot.subtasks.find((t) => t.id === x.subtaskId)?.name : labels[x.label];
      const a = s.doc.attempts.find((a) => a.id === x.attemptId);
      return `<div class="segment ${x.label} ${s.input.selected === x.id ? "selected" : ""}" data-segment="${esc(x.id)}" style="left:${Math.max(0, visiblePercent(x.start))}%;width:${(Math.min(hi, x.end) - Math.max(lo, x.start)) / (hi - lo) * 100}%" tabindex="0" role="button" aria-label="${esc(name)}${a ? ` 第${attemptOrdinals(s.doc).get(a.id)}次` : ""} ${x.start} 至 ${x.end - 1}"><i class="edge start" data-side="start"></i><strong>${esc(name)}</strong><small>${x.start}–${x.end - 1}${a ? ` · 第${attemptOrdinals(s.doc).get(a.id)}次 · ${a.outcome ? outcomes[a.outcome] : "结果暂缺"}` : ""}</small><i class="edge end" data-side="end"></i></div>`;
    }).join("");
    try {
      const r = ranges(s);
      if (r.start < hi && r.end > lo)
        $("track").innerHTML += `<div class="range-overlay" style="left:${Math.max(0, visiblePercent(r.start))}%;width:${(Math.min(hi, r.end) - Math.max(lo, r.start)) / (hi - lo) * 100}%"></div>`;
    } catch {}
    if (frame >= lo && frame < hi)
      $("track").innerHTML += `<div class="playhead" style="left:${visiblePercent(frame)}%"></div>`;
    $("ruler").querySelectorAll(".tick").forEach((tick, i) => {
      tick.style.left = `${i / 6 * 100}%`;
    });
    $("track").querySelectorAll("[data-segment]").forEach((el) => {
      const x = s.doc.segments.find((x) => x.id === el.dataset.segment);
      el.style.left = `${Math.max(0, visiblePercent(x.start))}%`;
      el.style.width = `${(Math.min(hi, x.end) - Math.max(lo, x.start)) / (hi - lo) * 100}%`;
    });
    const overlay = $("track").querySelector(".range-overlay");
    if (overlay) {
      try {
        const r = ranges(s);
        overlay.style.left = `${Math.max(0, visiblePercent(r.start))}%`;
        overlay.style.width = `${(Math.min(hi, r.end) - Math.max(lo, r.start)) / (hi - lo) * 100}%`;
      } catch {}
    }
    renderPlayhead();
    $("zoom-status").textContent = `局部 ${lo}–${hi - 1} · ${hi - lo} / ${s.head.frames} 帧`;
  }
  function renderPlayhead() {
    let head = $("track").querySelector(".playhead");
    if (frame < zoomStart || frame >= zoomEnd) {
      head?.remove();
      return;
    }
    if (!head) {
      head = document.createElement("div");
      head.className = "playhead";
      $("track").append(head);
    }
    head.style.left = `${visiblePercent(frame)}%`;
  }
  let gesture = null;
  function pointFrame(e, host, edge = false) {
    const rect = host.getBoundingClientRect();
    return Math.max(zoomStart, Math.min(zoomEnd - (edge ? 0 : 1), Math.round(zoomStart + (e.clientX - rect.left) / rect.width * (zoomEnd - zoomStart))));
  }
  $("timeline").addEventListener("pointerdown", (e) => {
    if (!active || e.button !== 0)
      return;
    const target = e.target, segmentEl = target.closest("[data-segment]"), isRuler = !!target.closest("#ruler"), host = isRuler ? $("ruler") : $("track");
    const id = segmentEl?.dataset.segment, s = id ? active.doc.segments.find((s) => s.id === id) : undefined;
    const side = target.dataset.side;
    const at = pointFrame(e, host, !!side);
    if (s && tool !== "range" && !side && tool !== "move") {
      active.input.selected = s.id;
      renderEditor();
      renderTimeline();
      backup(active);
      return;
    }
    const mode = side ? side : isRuler || tool === "browse" ? "scrub" : tool === "move" && s ? "move" : "range";
    gesture = {
      mode,
      id,
      anchor: at,
      current: at,
      original: s ? copy(s) : undefined,
      before: copy(active.doc),
      pointer: e.pointerId,
      host
    };
    host.setPointerCapture(e.pointerId);
    e.preventDefault();
    if (mode === "scrub") {
      pause();
      safe(() => seek(at, true));
    }
  });
  $("timeline").addEventListener("pointermove", (e) => {
    if (!gesture || !active)
      return;
    gesture.current = pointFrame(e, gesture.host, gesture.mode === "start" || gesture.mode === "end");
    if (gesture.mode === "scrub") {
      safe(() => seek(gesture.current, true));
    } else if (gesture.mode === "range") {
      active.input.first = String(Math.min(gesture.anchor, gesture.current));
      active.input.last = String(Math.max(gesture.anchor, gesture.current));
      value("range-first").value = active.input.first;
      value("range-last").value = active.input.last;
      renderTimeline();
    } else {
      try {
        const g = gesture;
        active.doc = g.mode === "move" ? move(g.before, active.head.frames, g.id, g.original.start + g.current - g.anchor) : resize(g.before, active.head.frames, g.id, g.mode, g.current);
        renderTimeline();
      } catch {}
    }
  });
  $("timeline").addEventListener("pointerup", (e) => {
    if (!gesture || !active)
      return;
    const g = gesture;
    gesture = null;
    try {
      g.host.releasePointerCapture(e.pointerId);
    } catch {}
    if (g.mode === "start" || g.mode === "end" || g.mode === "move") {
      const after = copy(active.doc);
      active.doc = g.before;
      if (JSON.stringify(after) !== JSON.stringify(g.before))
        change(after, g.mode);
      renderEditor();
    } else if (g.mode === "range") {
      rememberInput();
    } else
      safe(() => seek(g.current));
  });
  $("timeline").addEventListener("pointercancel", () => {
    if (gesture && active) {
      if (gesture.mode === "scrub")
        seek(frame);
      active.doc = gesture.before;
      gesture = null;
      renderTimeline();
    }
  });
  function renderNewForm() {
    const s = current();
    for (const [id, key] of [
      ["range-first", "first"],
      ["range-last", "last"],
      ["new-note", "note"],
      ["new-description", "description"],
      ["new-outcome", "outcome"]
    ])
      value(id).value = s.input[key] || "";
    value("new-label").innerHTML = s.doc.templateSnapshot.subtasks.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("") + ["NULL", "UNKNOWN", "OUT_OF_TEMPLATE"].map((l) => `<option value="${l}">${labels[l]}</option>`).join("");
    value("new-label").value = s.input.label;
    renderAssociation();
  }
  function renderAssociation() {
    const s = current(), label = value("new-label").value, formal = s.doc.templateSnapshot.subtasks.find((t) => t.id === label);
    const opts = s.doc.attempts.filter((a) => !a.archived && (!formal || a.subtaskId === label));
    value("association").innerHTML = (formal || label === "OUT_OF_TEMPLATE" ? '<option value="new">新的一次操作</option>' : "") + (formal ? "" : '<option value="none">不属于某次操作</option>') + (label === "NULL" ? "" : opts.map((a) => `<option value="${esc(a.id)}">接着前一次操作：${esc(attemptText(a))} · ${a.outcome ? outcomes[a.outcome] : "结果暂缺"}</option>`).join(""));
    value("association").value = s.input.association;
    if (!value("association").value)
      value("association").selectedIndex = 0;
    s.input.association = value("association").value;
    $("new-outcome-label").hidden = value("association").value !== "new";
    $("description-label").hidden = label !== "OUT_OF_TEMPLATE";
    $("definition").textContent = formal ? `${formal.definition || "尚未补充业务定义"}
包含：${formal.includes || "—"}
排除：${formal.excludes || "—"}
开始：${formal.startCondition || "—"}
结束：${formal.endCondition || "—"}` : label === "NULL" ? "明确观察到背景，不属于某次操作。静止本身不能证明是背景。" : label === "UNKNOWN" ? "证据不足；可由人明确关联已有尝试，不自动新建或计数。" : "明确存在模板外活动，请填写动作描述；可明确建立或关联尝试。";
  }
  function newSegment() {
    const s = current(), r = ranges(s), label = value("new-label").value, formal = s.doc.templateSnapshot.subtasks.find((t) => t.id === label), association = value("association").value;
    let attempt;
    let attemptId = association === "none" ? undefined : association;
    if (association === "new") {
      attemptId = uid();
      attempt = {
        id: attemptId,
        subtaskId: formal?.id,
        subtaskName: formal?.name,
        values: {},
        outcome: value("new-outcome").value || undefined
      };
    }
    const segment = {
      id: uid(),
      ...r,
      label: formal ? "SUBTASK" : label,
      ...formal ? { subtaskId: formal.id } : {},
      ...attemptId ? { attemptId } : {},
      note: value("new-note").value,
      description: value("new-description").value,
      values: {}
    };
    if (segment.label === "NULL")
      delete segment.attemptId;
    return { r, segment, attempt };
  }
  function createSegment(relabel = false) {
    rememberInput();
    const s = current(), { r, segment, attempt } = newSegment();
    if (!relabel && s.doc.segments.some((x) => x.start < r.end && x.end > r.start))
      throw Error("范围已有内容；请明确使用“重标选中范围”，两侧原内容会保留");
    change(replace(s.doc, s.head.frames, r.start, r.end, segment, attempt), relabel ? "range-relabel" : "create");
    s.input.selected = segment.id;
    renderEditor();
  }
  function fieldControl(f, v, key) {
    const desc = f.description || f.scale;
    const inputKey = `field:${f.scope}:${key}:${f.id}`;
    const pending = current().input[inputKey];
    if (pending !== undefined)
      v = pending;
    let control = "";
    if (f.kind === "single" || f.kind === "bool") {
      const opts = f.kind === "bool" ? [
        { value: "true", label: "是" },
        { value: "false", label: "否" }
      ] : (f.options || []).map((x) => ({ value: x, label: x }));
      control = `<select data-field="${esc(f.id)}" data-key="${esc(key)}"><option value="">留空</option>${opts.map((o) => `<option value="${esc(o.value)}" ${String(v) === o.value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`;
    } else if (f.kind === "multi")
      control = `<select multiple data-field="${esc(f.id)}" data-key="${esc(key)}">${(f.options || []).map((o) => `<option ${Array.isArray(v) && v.includes(o) ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    else if (f.kind === "textarea")
      control = `<textarea rows="2" data-field="${esc(f.id)}" data-key="${esc(key)}">${esc(v ?? "")}</textarea>`;
    else
      control = `<input data-field="${esc(f.id)}" data-key="${esc(key)}" type="${["rating", "number"].includes(f.kind) ? "number" : "text"}" ${f.kind === "rating" ? `min="${f.minimum ?? 1}" max="${f.maximum ?? 5}" step="${f.step ?? 1}"` : 'step="any"'} value="${esc(v ?? "")}">`;
    return `<label>${esc(f.label)}${control}${desc ? `<span class="description">${esc(desc)}</span>` : ""}</label>`;
  }
  function bindFields(host, scope, getValues, update) {
    host.querySelectorAll("[data-field]").forEach((el) => el.addEventListener("input", () => safe(() => {
      const f = current().doc.templateSnapshot.fields.find((f) => f.id === el.dataset.field);
      let v = el.value || null;
      if (f.kind === "multi")
        v = Array.from(el.selectedOptions).map((x) => x.value);
      if (v !== null && ["rating", "number"].includes(f.kind)) {
        v = Number(v);
        if (!Number.isFinite(v) || el instanceof HTMLInputElement && !el.validity.valid) {
          current().input[`field:${scope}:${el.dataset.key}:${f.id}`] = el.value;
          backup(current());
          note("输入尚未有效，已保留在编辑中草稿");
          return;
        }
      }
      if (v !== null && f.kind === "bool")
        v = v === "true";
      delete current().input[`field:${scope}:${el.dataset.key}:${f.id}`];
      edit("field", (d) => update(d, f.id, v));
    })));
  }
  function renderEditor() {
    const s = current(), seg = selected(s), host = $("segment-editor");
    if (!seg) {
      host.className = "empty";
      host.innerHTML = "点击主轨道的片段正文，播放头保持原位。";
      return;
    }
    host.className = "";
    const a = s.doc.attempts.find((a) => a.id === seg.attemptId), related = a ? s.doc.segments.filter((x) => x.attemptId === a.id) : [];
    const name = seg.label === "SUBTASK" ? s.doc.templateSnapshot.subtasks.find((x) => x.id === seg.subtaskId)?.name : labels[seg.label];
    host.innerHTML = `<strong>${esc(name)}</strong><div class="row" style="margin-top:12px"><label>首帧（包含）<input id="selected-first" value="${esc(s.input[`first:${seg.id}`] ?? seg.start)}" inputmode="numeric"></label><label>末帧（包含）<input id="selected-last" value="${esc(s.input[`last:${seg.id}`] ?? seg.end - 1)}" inputmode="numeric"></label></div><div class="row"><button id="copy-range">用此片段范围重标</button></div><label>此片段局部备注<textarea id="selected-note" rows="2">${esc(seg.note || "")}</textarea></label>${seg.label === "OUT_OF_TEMPLATE" ? `<label>模板外活动描述<textarea id="selected-description">${esc(seg.description || "")}</textarea></label>` : ""}<div class="field-grid" id="segment-fields">${s.doc.templateSnapshot.fields.filter((f) => f.scope === "segment").map((f) => fieldControl(f, seg.values?.[f.id], seg.id)).join("")}</div>${a ? `<div class="attempt-info"><strong>本 Episode · ${esc(attemptText(a))}</strong><br>共 ${related.length} 个片段：${related.map((x) => `${x.start}–${x.end - 1}`).join("、")}<br>下方结果与评分作用于这些全部片段；局部备注保持各自归属。</div><label>整次执行结果<select id="selected-outcome"><option value="">尚未填写</option>${Object.entries(outcomes).map(([v, l]) => `<option value="${v}" ${a.outcome === v ? "selected" : ""}>${l}</option>`).join("")}</select></label><div class="field-grid" id="attempt-fields">${s.doc.templateSnapshot.fields.filter((f) => f.scope === "attempt" && f.id !== "attempt-outcome").map((f) => fieldControl(f, a.values[f.id], a.id)).join("")}</div><div class="row"><button id="whole-label">修改本次操作的标签</button></div>` : '<p class="hint">此片段未指定操作。可使用“用此片段范围重标”后选择对应操作。</p>'}<div class="row"><button id="delete-segment" class="danger">删除片段</button></div>${seg.risks?.length ? `<p class="special-help">标注提示：${esc(seg.risks.join("；"))}</p>` : ""}`;
    for (const [id, side] of [
      ["selected-first", "start"],
      ["selected-last", "end"]
    ])
      value(id).oninput = () => {
        s.input[`${side === "start" ? "first" : "last"}:${seg.id}`] = value(id).value;
        backup(s);
        const raw = value(id).value;
        if (!/^\d+$/.test(raw))
          return;
        try {
          change(resize(s.doc, s.head.frames, seg.id, side, Number(raw) + (side === "end" ? 1 : 0)), "boundary");
          delete s.input[`first:${seg.id}`];
          delete s.input[`last:${seg.id}`];
        } catch (e) {
          note(e.message);
        }
      };
    value("selected-note").oninput = () => edit("segment-note", (d) => {
      d.segments.find((x) => x.id === seg.id).note = value("selected-note").value;
    });
    if (seg.label === "OUT_OF_TEMPLATE")
      value("selected-description").oninput = () => edit("description", (d) => {
        d.segments.find((x) => x.id === seg.id).description = value("selected-description").value;
      });
    bindFields($("segment-fields"), "segment", () => seg.values || {}, (d, id, v) => {
      const x = d.segments.find((x) => x.id === seg.id);
      (x.values ??= {})[id] = v;
    });
    if (a) {
      value("selected-outcome").onchange = () => edit("attempt-outcome", (d) => {
        d.attempts.find((x) => x.id === a.id).outcome = value("selected-outcome").value || undefined;
      });
      bindFields($("attempt-fields"), "attempt", () => a.values, (d, id, v) => {
        d.attempts.find((x) => x.id === a.id).values[id] = v;
      });
      btn("whole-label", () => wholeLabelDialog(a));
    }
    btn("copy-range", () => {
      s.input.first = String(seg.start);
      s.input.last = String(seg.end - 1);
      s.input.label = seg.subtaskId || seg.label;
      s.input.association = seg.attemptId || "none";
      s.input.note = seg.note || "";
      s.input.description = seg.description || "";
      renderNewForm();
      renderTimeline();
      backup(s);
    });
    btn("delete-segment", () => {
      edit("delete", (d) => {
        d.segments = d.segments.filter((x) => x.id !== seg.id);
      });
      s.input.selected = "";
      renderEditor();
    });
  }
  function renderFields() {
    const s = current();
    $("episode-fields").innerHTML = '<div class="field-grid">' + s.doc.templateSnapshot.fields.filter((f) => f.scope === "episode").map((f) => fieldControl(f, s.doc.values[f.id], "episode")).join("") + "</div>";
    bindFields($("episode-fields"), "episode", () => s.doc.values, (d, id, v) => {
      d.values[id] = v;
    });
    renderFrameFields();
    renderAtoms();
  }
  function renderAtoms() {
    const s = current();
    $("manual-atoms").innerHTML = (s.doc.manualAtoms || []).map((a, i) => `<label>${esc(a.style || "语言")} · ${esc(a.role)} · ${esc(a.timestamp)} 秒<textarea data-atom-index="${i}">${esc(a.content || "")}</textarea>${a.tool_calls ? `<details><summary>保留的工具调用</summary><pre>${esc(JSON.stringify(a.tool_calls, null, 2))}</pre></details>` : ""}</label>`).join("") || '<p class="hint">此 Episode 尚无语言/VQA 记录。</p>';
    $("manual-atoms").querySelectorAll("[data-atom-index]").forEach((el) => el.oninput = () => edit("manual-observation", (d) => {
      d.manualAtoms[Number(el.dataset.atomIndex)].content = el.value;
    }));
    value("vqa-question").value = s.input.vqaQuestion || "";
    value("vqa-answer").value = s.input.vqaAnswer || "";
  }
  function renderFrameFields() {
    if (!active)
      return;
    const s = active, n = s.doc.frames.find((f) => f.frame === frame);
    $("frame-fields").innerHTML = '<div class="field-grid">' + s.doc.templateSnapshot.fields.filter((f) => f.scope === "frame").map((f) => fieldControl(f, n?.values[f.id], String(frame))).join("") + "</div>";
    bindFields($("frame-fields"), "frame", () => n?.values || {}, (d, id, v) => {
      annotationFrame();
      let row = d.frames.find((f) => f.frame === frame);
      if (!row) {
        row = { id: uid(), frame, values: {} };
        d.frames.push(row);
      }
      row.values[id] = v;
    });
  }
  function modal(html) {
    $("dialog-body").innerHTML = html;
    const d = $("dialog");
    if (!d.open)
      d.showModal();
  }
  function closeModal() {
    $("dialog").close();
  }
  function conflictDialog() {
    const s = current();
    if (!s.remote)
      return;
    modal(`<h2>对照冲突内容</h2><p>本地基于修订 ${s.head.revision}，服务器现为修订 ${s.remote.revision}。两份都可先下载保留；来源版本不同不允许覆盖。</p><div class="diff"><div><h3>本地修改</h3><pre>${esc(JSON.stringify(s.doc, null, 2))}</pre></div><div><h3>服务器内容</h3><pre>${esc(JSON.stringify(s.remote.document, null, 2))}</pre></div></div><div class="row"><button id="download-conflict">下载双方备份</button><button id="use-remote">归档本地，使用服务器</button><button id="use-local" class="primary">核对后，以本地内容提交新修订</button></div>`);
    btn("download-conflict", () => download({ local: s.doc, remote: s.remote }, `episode-${episode}-conflict.json`));
    btn("use-remote", () => {
      s.undo.edit(s.doc, s.remote.document);
      s.head = s.remote;
      s.doc = copy(s.head.document);
      s.remote = undefined;
      s.dirty = false;
      s.error = "";
      backup(s);
      closeModal();
      renderAll(false);
    });
    btn("use-local", async () => {
      if (s.head.source_signature !== s.remote.source_signature)
        throw Error("来源不同，请保留旧草稿并单独核对，不能套到新来源");
      s.head = copy(s.remote);
      s.remote = undefined;
      s.dirty = true;
      s.error = "";
      backup(s);
      await sync(s);
      closeModal();
      renderAll(false);
    });
  }
  function wholeLabelDialog(a) {
    const s = current();
    modal(`<h2>修改本次操作的标签</h2><p>会修改同一尝试关联的全部正式片段：${s.doc.segments.filter((x) => x.attemptId === a.id).map((x) => `${x.start}–${x.end - 1}`).join("、")}；特殊状态仍保持独立。</p><select id="whole-step">${s.doc.templateSnapshot.subtasks.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}</select><button id="apply-whole">明确应用整次变更</button>`);
    btn("apply-whole", () => {
      change(relabelAttempt(s.doc, s.head.frames, a.id, value("whole-step").value), "relabel-whole-attempt");
      closeModal();
      renderAll(false);
    });
  }
  function download(data, name) {
    const link = document.createElement("a"), url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    link.href = url;
    link.download = name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function loadLibrary() {
    const r = await fetch("/api/task-templates");
    if (!r.ok)
      throw Error("共享模板服务不可用，当前快照不变");
    library = await r.json();
    return library;
  }
  async function templateDialog() {
    await loadLibrary();
    modal(`<h2>共享任务模板</h2><p>历史标注继续绑定实际快照。保存共享库不会自动升级当前 Episode。</p><div class="row"><select id="library-choice">${library.templates.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}</select><button id="edit-library">编辑所选模板</button><button id="new-template">新建模板</button><button id="upgrade-library" class="primary">查看差异并升级当前 Episode</button></div><hr><div class="row"><label>从文件导入模板<input id="template-file" type="file" accept=".json,application/json"></label><button id="download-template">下载所选模板</button></div><p class="hint">继续支持在网页中新建、编辑，也可以导入 JSON。<a href="/workbench-assets/template-example.json" download>下载格式示例</a> · <a href="/workbench-assets/screw-alignment-en.json" download>对准螺丝英文模板（4 类）</a> · <a href="/workbench-assets/screw-workflow-en.json" download>全流程英文模板（8 类）</a></p><div id="template-import-preview"></div><div id="template-form"></div>`);
    btn("download-template", () => {
      const t = library.templates.find((t) => t.id === value("library-choice").value);
      if (!t)
        throw Error("请选择要下载的模板");
      download(t, `${t.id}.json`);
    });
    value("template-file").onchange = () => safe(async () => {
      const file = value("template-file").files?.[0];
      if (!file)
        return;
      if (file.size > 4 * 1024 * 1024)
        throw Error("模板文件不能超过 4 MiB");
      let data;
      try {
        data = JSON.parse(await file.text());
      } catch {
        throw Error("模板文件不是有效的 JSON，请参考下载的格式示例");
      }
      const result = await request("templates/validate", data);
      const incoming = result.templates;
      $("template-import-preview").innerHTML = `<h3>导入预览 · ${incoming.length} 套模板</h3><p>确认后才保存到共享库。已经生成的标注和正在运行的任务继续使用原模板。</p>${incoming.map((t, i) => `<details open><summary>${esc(t.name)} · ${t.subtasks.length} 个动作标签</summary><ul>${t.subtasks.map((st) => `<li><strong>${esc(st.name)}</strong><p>${esc(st.definition || "未填写定义")}</p></li>`).join("")}</ul>${library.templates.some((old) => old.id === t.id) ? `<label>存在同 ID 模板，请选择<select data-import-choice="${i}"><option value="">请选择处理方式</option><option value="new">保存为新的模板</option><option value="update">更新共享库中同 ID 的模板</option><option value="skip">跳过这套模板</option></select></label>` : "<p>新增到共享模板库</p>"}</details>`).join("")}<button id="confirm-template-import" class="primary">确认导入</button>`;
      btn("confirm-template-import", async () => {
        const next = copy(library.templates);
        for (const [i, t] of incoming.entries()) {
          const index = next.findIndex((old) => old.id === t.id);
          if (index < 0)
            next.push(t);
          else {
            const choice = root.querySelector(`[data-import-choice="${i}"]`).value;
            if (!choice)
              throw Error(`请为“${t.name}”选择重复模板的处理方式`);
            if (choice === "new")
              next.push({ ...t, id: uid(), name: `${t.name}（副本）` });
            else if (choice === "update")
              next[index] = t;
          }
        }
        if (JSON.stringify(next) === JSON.stringify(library.templates)) {
          note("已跳过全部模板，共享库未修改。");
          return;
        }
        const response = await fetch("/api/task-templates", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            revision: library.revision,
            templates: next
          })
        });
        if (!response.ok) {
          const error = await response.json();
          throw Error(typeof error.detail === "string" ? error.detail : "模板导入失败，请重新打开模板库后重试");
        }
        library = await response.json();
        await templateDialog();
        note("模板已导入；已有标注和运行中的任务继续使用各自的原模板。");
      });
    });
    btn("edit-library", () => editTemplate(library.templates.find((t) => t.id === value("library-choice").value) || current().doc.templateSnapshot));
    btn("new-template", () => editTemplate({ id: uid(), name: "新任务模板", subtasks: [], fields: [] }));
    btn("upgrade-library", () => {
      const t = library.templates.find((t) => t.id === value("library-choice").value);
      if (!t)
        throw Error("请选择共享模板");
      upgradeDialog(t);
    });
  }
  function editTemplate(initial) {
    let t = copy(initial);
    const render = () => {
      const host = $("template-form");
      host.innerHTML = `<h3>模板定义（稳定 ID 保持不变）</h3><label>名称<input id="template-name" value="${esc(t.name)}"></label><div id="template-steps">${t.subtasks.map((s, i) => `<details class="template-step" data-step="${i}"><summary>${esc(s.name || "新标签")} · ${esc(s.id)}</summary>${["name", "definition", "includes", "excludes", "startCondition", "endCondition"].map((key, j) => `<label>${["名称", "业务定义", "包含动作", "排除动作", "可观察开始判据", "可观察结束判据"][j]}<textarea rows="2" data-property="${key}">${esc(s[key] || "")}</textarea></label>`).join("")}<button data-remove-step="${i}">删除此标签（旧快照仍保留）</button></details>`).join("")}</div><button id="add-step">添加正式标签</button><h3>自定义字段与评分尺度</h3><div id="template-fields">${t.fields.map((f, i) => `<details class="template-field" data-template-field="${i}"><summary>${esc(f.label)} · ${esc(f.id)}</summary><label>字段名称<input data-property="label" value="${esc(f.label)}"></label><div class="row"><label>归属<select data-property="scope">${["episode", "attempt", "segment", "frame"].map((v, j) => `<option value="${v}" ${f.scope === v ? "selected" : ""}>${["Episode", "整次尝试", "局部片段", "单帧观察"][j]}</option>`).join("")}</select></label><label>类型<select data-property="kind">${["rating", "number", "text", "textarea", "single", "multi", "bool"].map((v) => `<option value="${v}" ${f.kind === v ? "selected" : ""}>${v}</option>`).join("")}</select></label></div><label>含义说明<textarea data-property="description">${esc(f.description || "")}</textarea></label><label>评分尺度及各分值含义<textarea data-property="scale">${esc(f.scale || "")}</textarea></label><div class="row"><label>评分下限<input data-property="minimum" type="number" value="${f.minimum ?? ""}"></label><label>评分上限<input data-property="maximum" type="number" value="${f.maximum ?? ""}"></label><label>步长<input data-property="step" type="number" value="${f.step ?? ""}"></label></div><label>选项（每行一项）<textarea data-property="options">${esc((f.options || []).join(`
`))}</textarea></label><button data-remove-field="${i}">删除此字段</button></details>`).join("")}</div><button id="add-field">添加自定义字段</button><button id="save-template" class="primary">保存到共享库</button>`;
      const read = () => {
        t.name = value("template-name").value;
        host.querySelectorAll("[data-step]").forEach((el) => el.querySelectorAll("[data-property]").forEach((input) => {
          t.subtasks[Number(el.dataset.step)][input.dataset.property] = input.value || undefined;
        }));
        host.querySelectorAll("[data-template-field]").forEach((el) => el.querySelectorAll("[data-property]").forEach((input) => {
          const key = input.dataset.property, f = t.fields[Number(el.dataset.templateField)];
          f[key] = key === "options" ? input.value.split(`
`).map((s) => s.trim()).filter(Boolean) : ["minimum", "maximum", "step"].includes(key) ? input.value === "" ? undefined : Number(input.value) : input.value || undefined;
        }));
        localStorage.setItem(`bp-template-edit:${dataset}`, JSON.stringify(t));
      };
      host.querySelectorAll("input,textarea,select").forEach((el) => el.addEventListener("input", () => safe(read)));
      host.querySelectorAll("[data-remove-step]").forEach((el) => el.onclick = () => {
        read();
        t.subtasks.splice(Number(el.dataset.removeStep), 1);
        render();
      });
      host.querySelectorAll("[data-remove-field]").forEach((el) => el.onclick = () => {
        read();
        t.fields.splice(Number(el.dataset.removeField), 1);
        render();
      });
      btn("add-step", () => {
        read();
        t.subtasks.push({ id: uid(), name: "" });
        render();
      });
      btn("add-field", () => {
        read();
        t.fields.push({
          id: uid(),
          label: "",
          scope: "attempt",
          kind: "rating",
          minimum: 1,
          maximum: 5
        });
        render();
      });
      btn("save-template", async () => {
        read();
        const templates = [
          ...library.templates.filter((x) => x.id !== t.id),
          t
        ];
        const r = await fetch("/api/task-templates", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ revision: library.revision, templates })
        });
        const result = await r.json();
        if (!r.ok)
          throw Error(typeof result.detail === "string" ? result.detail : Array.isArray(result.detail) ? result.detail.map((d) => `${(d.loc || []).filter((v) => v !== "body").join(" → ")}: ${d.msg}`).join("；") : "请求失败，请刷新后重试");
        library = result;
        localStorage.removeItem(`bp-template-edit:${dataset}`);
        note("共享模板已保存；当前 Episode 仍使用原快照。需要时显式升级。");
        await templateDialog();
      });
    };
    render();
  }
  function upgradeDialog(template) {
    const s = current(), diff = templateDiff(s.doc.templateSnapshot, template), used = [
      ...new Set(s.doc.attempts.filter((a) => !a.archived && a.subtaskId).map((a) => a.subtaskId))
    ];
    modal(`<h2>显式解决模板升级</h2><p>对照名称、含义、边界判据及字段。相同 ID 也不代表含义相同；每个已用标签都要明确选择对应。字段定义变化的当前值会归档并清空，原值完整留在历史。</p><pre>${esc(JSON.stringify(diff, null, 2))}</pre>${used.map((id) => `<label>${esc(s.doc.templateSnapshot.subtasks.find((t) => t.id === id)?.name || id)} (${esc(id)})<select data-map="${esc(id)}"><option value="">尚未解决，继续旧快照</option>${template.subtasks.map((t) => `<option value="${esc(t.id)}">明确映射为 ${esc(t.name)} (${esc(t.id)})</option>`).join("")}</select></label>`).join("")}<button id="apply-upgrade" class="primary">采用此快照及明确映射</button>`);
    btn("apply-upgrade", () => {
      const mapping = {};
      $("dialog-body").querySelectorAll("[data-map]").forEach((el) => {
        if (el.value)
          mapping[el.dataset.map] = el.value;
      });
      const next = upgrade(s.doc, s.head.frames, template, mapping);
      const changed = s.doc.templateSnapshot.fields.filter((old) => JSON.stringify(old) !== JSON.stringify(template.fields.find((f) => f.id === old.id))).map((f) => f.id);
      for (const id of changed) {
        delete next.values[id];
        next.attempts.forEach((a) => {
          if (!a.archived)
            delete a.values[id];
        });
        next.segments.forEach((x) => {
          if (x.values)
            delete x.values[id];
        });
        next.frames.forEach((f) => delete f.values[id]);
      }
      s.mapping = mapping;
      change(next, "template-upgrade");
      closeModal();
      renderAll(false);
    });
  }
  function selectionRows() {
    return [...selection.entries].sort((a, b) => a[0] - b[0]).map(([ep, entry]) => ({
      ep,
      entry,
      row: allRows.find((r) => r.episode === ep)
    }));
  }
  async function exportSelection(scope = "selected") {
    const ids = scope === "all" ? availableRows().map((r) => r.episode) : [...selection.entries.keys()];
    if (!ids.length)
      throw Error("请先选择要导出的数据，或选择导出全部数据");
    for (const ep of ids) {
      const session = sessions.get(ep);
      if (!session)
        continue;
      await sync(session);
      if (session.dirty || session.remote || session.readFailure)
        throw Error(`Episode ${ep} 尚未保存，请先处理保存问题`);
    }
    const latest = await request(`${dataset}/episodes`);
    if (latest.source_signature !== signature)
      throw Error("原始数据已变化，请刷新页面再导出");
    allRows = latest.episodes;
    const revisions = {};
    for (const ep of ids) {
      const row = allRows.find((r) => r.episode === ep);
      if (!row)
        throw Error(`Episode ${ep} 已不存在，请重新选择`);
      revisions[ep] = row.revision;
    }
    return {
      source_signature: signature,
      episodes: ids,
      revisions,
      filter: scope === "all" ? {} : copy(filterState),
      origins: Object.fromEntries(ids.map((ep) => [ep, scope === "all" ? "all" : "manual"]))
    };
  }
  function deliveryDialog() {
    modal(`<h2>导出标注与数据</h2><p>使用最新保存的标注。未人工检查的结果也可以导出，文件中会保留真实来源与检查状态。</p><label>导出范围<select id="export-scope"><option value="selected">已选 ${selection.entries.size} 条</option><option value="all">全部 ${availableRows().length} 条</option></select></label><p class="hint">在左侧 Episode 列表勾选要导出的条目；“全部”包含当前数据集的全部可用 Episode。</p><div class="row"><button id="export-bundle" class="primary">下载标注文件（JSON）</button><button id="export-subset">生成数据副本（含视频与标注）</button></div><p class="hint">JSON 只包含标注；数据副本包含所选条目的原始数据和标注，生成后可在数据列表中预览、下载。</p><div id="export-state" role="status"></div>`);
    const exportState = $("export-state");
    const busy = (state) => ["export-bundle", "export-subset"].forEach((id) => {
      const el = $(id);
      if (el)
        el.disabled = state;
    });
    btn("export-bundle", async () => {
      busy(true);
      try {
        const bundle = await request(`${dataset}/bundle`, await exportSelection(value("export-scope").value));
        download(bundle, `bodypipeline-${dataset}-annotations.json`);
        exportState.textContent = `已下载 ${bundle.episodes.length} 条标注。`;
      } finally {
        if (!disposed)
          busy(false);
      }
    });
    btn("export-subset", async () => {
      busy(true);
      try {
        const job = await request(`${dataset}/exports`, await exportSelection(value("export-scope").value)), ds = dataset;
        exportState.textContent = "正在生成数据副本，可以继续编辑；本次导出内容已固定。";
        (async () => {
          while (!disposed) {
            await new Promise((r) => setTimeout(r, 1500));
            if (disposed)
              return;
            try {
              const result = await request(`${ds}/exports/${job.id}`), host = $("export-state");
              if (host)
                host.innerHTML = `${esc({ QUEUED: "等待生成数据副本", RUNNING: "正在生成数据副本", CONVERTED: "数据副本已生成", FAILED: "生成失败", INTERRUPTED: "生成已中断" }[result.state] || result.stage)}${result.error ? ` · ${esc(result.error)}` : ""}${result.viewer_url ? ` · <a href="${esc(result.viewer_url)}">预览生成的数据</a>` : ""}`;
              if (!["QUEUED", "RUNNING"].includes(result.state))
                return;
            } catch (e) {
              note(e.message);
              return;
            }
          }
        })();
      } finally {
        if (!disposed)
          busy(false);
      }
    });
  }
  function importAnnotationsDialog() {
    modal('<h2>导入标注文件</h2><p>导入内容保留为可比较的另一份结果。已有修改不会被覆盖；在“其他 AI 结果与导入内容”中选择需要替换的范围。</p><label>选择导出的标注 JSON<input id="import-bundle" type="file" accept=".json,application/json"></label>');
    value("import-bundle").onchange = () => safe(async () => {
      const file = value("import-bundle").files?.[0];
      if (!file)
        return;
      let bundle;
      try {
        bundle = JSON.parse(await file.text());
      } catch {
        throw Error("文件不是有效的 JSON，请选择导出的标注文件");
      }
      const result = await request(`${dataset}/import`, { bundle });
      closeModal();
      note(`已导入 ${result.candidates.length} 条标注，可在“其他 AI 结果与导入内容”中查看。`);
    });
  }
  async function batchReviewDialog() {
    if (!selection.entries.size)
      throw Error("请在数据列表或导出窗口中选择实际检查过的数据");
    modal(`<h2>批量检查整条 Episode</h2><p>仅对你实际检查过的数据使用。将标记 ${selection.entries.size} 条：${[...selection.entries.keys()].map((ep) => `Episode ${ep}`).join("、")}。有空白、缺少结果或存在未处理问题的条目需要先修改；操作不会补写内容。</p><button id="apply-batch" class="primary">这些 Episode 已检查，保存检查状态</button>`);
    btn("apply-batch", async () => {
      const proposed = await exportSelection();
      await request(`${dataset}/confirm`, proposed);
      for (const ep of proposed.episodes)
        sessions.delete(ep);
      closeModal();
      await refresh(false);
      await openEpisode(episode);
    });
  }
  async function candidateDialog() {
    const s = current(), result = await request(`${dataset}/episodes/${episode}/candidates`), items = result.candidates;
    modal(`<h2>其他 AI 结果与导入内容</h2><p>这里用于比较后来生成的 AI 结果或导入内容。手动编辑只会保存，不会重跑 AI。替换范围前请先比较；已有内容会保留修改记录。</p><select id="candidate-choice">${items.map((c) => `<option value="${esc(c.id)}">${c.provenance?.annotation ? "AI 结果" : "导入标注"} · ${new Date(c.created_at * 1000).toLocaleString()}</option>`).join("")}</select><div id="candidate-detail"></div>`);
    const detail = () => {
      const c = items.find((x) => x.id === value("candidate-choice").value);
      if (!c) {
        $("candidate-detail").innerHTML = '<div class="empty">暂无其他结果。首次 AI 标注会自动出现在工作台，后续结果与导入内容在这里比较。</div>';
        return;
      }
      $("candidate-detail").innerHTML = `<p>使用模板：${esc(c.document.templateSnapshot.name)}<br>应用所选范围 ${s.input.first}–${s.input.last}。会替换范围内片段和帧观察；范围外片段保留，Episode 字段保持当前值。来自同一候选的同一次尝试共享身份与结果。</p><details><summary>查看标注内容</summary><ul>${c.document.segments.map((seg) => `<li>第 ${seg.start}–${seg.end - 1} 帧 · ${esc(c.document.templateSnapshot.subtasks.find((st) => st.id === seg.subtaskId)?.name || labels[seg.label])}${seg.note ? ` · ${esc(seg.note)}` : ""}</li>`).join("")}</ul></details><label><input id="candidate-ack" type="checkbox"> 我已比较这份结果与当前标注，允许替换所选范围</label><label><input id="candidate-keep" type="checkbox"> 若已采用的同一尝试有人工改动，明确保留当前共享字段</label><div class="row"><button id="candidate-upgrade">先解决候选模板差异</button><button id="apply-candidate" class="primary">明确采用所选范围</button></div>`;
      btn("candidate-upgrade", () => upgradeDialog(c.document.templateSnapshot));
      btn("apply-candidate", async () => {
        await sync(s);
        if (s.dirty || s.remote)
          throw Error("请先处理当前草稿同步");
        const r = ranges(s), before = copy(s.doc);
        const response = await request(`${dataset}/episodes/${episode}/apply`, {
          revision: s.head.revision,
          source_signature: s.head.source_signature,
          candidate: c.id,
          start: r.start,
          end: r.end,
          acknowledge_current_revision: value("candidate-ack").checked,
          shared_attempt_policy: value("candidate-keep").checked ? "keep-existing" : "require-match"
        });
        s.undo.edit(before, response.document);
        s.head = { ...s.head, ...response };
        s.doc = copy(response.document);
        s.dirty = false;
        backup(s);
        closeModal();
        renderAll(false);
      });
    };
    value("candidate-choice").onchange = detail;
    detail();
  }
  async function historyDialog() {
    const result = await request(`${dataset}/episodes/${episode}/history`);
    const names = {
      "human-review": "检查状态更新",
      "batch-confirm": "批量检查",
      "candidate-apply": "采用其他标注",
      "ai-initial": "AI 初始标注",
      boundary: "调整片段边界",
      "segment-note": "修改备注",
      field: "修改字段",
      undo: "撤销修改",
      redo: "重做修改",
      restore: "恢复内容",
      edit: "保存修改"
    };
    modal(`<h2>修改记录</h2><p>每次保存都保留当时的标注和检查状态。工作台支持撤销与重做。</p>${result.history.map((h) => `<details><summary>${h.updated_at ? new Date(h.updated_at * 1000).toLocaleString() : "初始内容"} · ${esc(names[h.operation] || "标注修改")} · ${h.complete ? "已人工检查" : "未人工检查"}</summary><p>${h.document.segments.length} 个片段 · ${esc(h.document.templateSnapshot.name)}</p><ul>${h.document.segments.map((seg) => `<li>第 ${seg.start}–${seg.end - 1} 帧：${esc(seg.subtaskName || labels[seg.label] || "标注片段")}${seg.note ? ` · ${esc(seg.note)}` : ""}</li>`).join("")}</ul></details>`).join("") || "<p>还没有保存记录。</p>"}`);
  }
  for (const [id, key] of [
    ["vqa-question", "vqaQuestion"],
    ["vqa-answer", "vqaAnswer"]
  ])
    value(id).oninput = () => {
      current().input[key] = value(id).value;
      backup(current());
    };
  btn("add-vqa", () => {
    annotationFrame();
    const question = value("vqa-question").value.trim(), answer = value("vqa-answer").value.trim();
    if (!question && !answer)
      throw Error("请填写问题或观察");
    edit("add-vqa", (d) => {
      d.manualAtoms ??= [];
      if (question)
        d.manualAtoms.push({
          role: "user",
          style: "vqa",
          content: question,
          timestamp: frame / current().head.fps,
          camera: null,
          tool_calls: null
        });
      if (answer)
        d.manualAtoms.push({
          role: "assistant",
          style: "vqa",
          content: answer,
          timestamp: frame / current().head.fps,
          camera: null,
          tool_calls: null
        });
    });
    renderAtoms();
  });
  btn("dialog-close", closeModal);
  btn("first", () => {
    pause();
    return seek(0);
  });
  btn("last", () => {
    pause();
    return seek(current().head.frames - 1);
  });
  btn("back", () => {
    pause();
    return seek(frame - 1);
  });
  btn("forward", () => {
    pause();
    return seek(frame + 1);
  });
  btn("play", () => playing ? pause() : play());
  value("frame").onchange = () => {
    const target = Number(value("frame").value);
    pause();
    safe(() => seek(target));
  };
  value("scrub").oninput = () => {
    const target = Number(value("scrub").value);
    safe(() => seek(target, true));
  };
  value("scrub").onchange = () => safe(() => seek(Number(value("scrub").value)));
  function setTool(next) {
    tool = next;
    for (const [id, v] of [
      ["browse", "browse"],
      ["range-tool", "range"],
      ["move-tool", "move"]
    ])
      $(id).classList.toggle("active", v === next);
    $("tool-hint").textContent = next === "range" ? "在主轨框选范围，然后明确贴标签或重标。框选与标记起止使用同一种记录。" : next === "move" ? "明确移动模式：拖片段正文整体移动；不会填补空白，重叠会被拒绝。" : "标尺浏览画面 · 片段正文只选中 · 两侧把手调界 · 首末帧均包含";
  }
  btn("browse", () => setTool("browse"));
  btn("range-tool", () => setTool("range"));
  btn("move-tool", () => setTool("move"));
  function zoom(factor) {
    const s = current(), size = Math.max(5, Math.min(s.head.frames, Math.round((zoomEnd - zoomStart) * factor)));
    zoomStart = Math.max(0, Math.min(s.head.frames - size, frame - Math.round(size / 2)));
    zoomEnd = zoomStart + size;
    renderTimeline();
  }
  btn("zoom-in", () => zoom(0.5));
  btn("zoom-out", () => zoom(2));
  btn("zoom-all", () => {
    zoomStart = 0;
    zoomEnd = current().head.frames;
    renderTimeline();
  });
  btn("pan-left", () => {
    const width = zoomEnd - zoomStart;
    zoomStart = Math.max(0, zoomStart - Math.round(width * 0.8));
    zoomEnd = zoomStart + width;
    renderTimeline();
  });
  btn("pan-right", () => {
    const width = zoomEnd - zoomStart;
    zoomStart = Math.min(current().head.frames - width, zoomStart + Math.round(width * 0.8));
    zoomEnd = zoomStart + width;
    renderTimeline();
  });
  function historyStep(redo = false) {
    const s = current();
    s.doc = redo ? s.undo.redo(s.doc) : s.undo.undo(s.doc);
    s.dirty = true;
    s.operation = redo ? "redo" : "undo";
    s.retryAt = 0;
    backup(s);
    renderAll(false);
    schedule();
  }
  btn("undo", () => historyStep());
  btn("redo", () => historyStep(true));
  btn("mark-start", () => {
    value("range-first").value = String(annotationFrame());
    rememberInput();
    renderTimeline();
  });
  btn("mark-end", () => {
    value("range-last").value = String(annotationFrame());
    rememberInput();
    renderTimeline();
  });
  btn("continue-range", () => {
    const s = current(), r = ranges(s);
    if (r.end >= s.head.frames)
      throw Error("已经到最后一帧");
    value("range-first").value = String(r.end);
    value("range-last").value = String(r.end);
    rememberInput();
    renderTimeline();
  });
  for (const id of [
    "range-first",
    "range-last",
    "new-note",
    "new-description",
    "new-outcome"
  ])
    value(id).oninput = () => {
      rememberInput();
      renderTimeline();
    };
  value("new-label").onchange = () => {
    current().input.label = value("new-label").value;
    current().input.association = "";
    renderAssociation();
    rememberInput();
  };
  value("association").onchange = () => {
    current().input.association = value("association").value;
    renderAssociation();
    rememberInput();
  };
  btn("create", () => createSegment());
  btn("replace-range", () => createSegment(true));
  btn("unselect", () => {
    current().input.selected = "";
    renderEditor();
    renderTimeline();
  });
  btn("complete", finishEpisodeReview);
  function jumpEpisode(ep) {
    if (!Number.isInteger(ep) || !availableRows().some((row) => row.episode === ep))
      throw Error("请输入当前数据集中可用的 Episode 编号");
    if (options.navigation) {
      const index = navigationRows().findIndex((row) => row.episode === ep);
      if (index >= 0) {
        page = Math.floor(index / 10);
        renderEpisodes();
      }
    }
    return openEpisode(ep);
  }
  if (!options.navigation)
    btn("jump-episode", () => {
      const raw = value("episode-number").value.trim();
      return jumpEpisode(raw ? Number(raw) : NaN);
    });
  btn("count-evidence", () => {
    const s = current();
    modal(`<h2>动作次数的判断依据</h2><p>与“完成”分别记录。明确证据可以处理遮挡或其他数据来源；后续内容变化会使此依据失效。不确定时仍可人工加入已选数据。</p><label>目标标签<select id="evidence-step">${s.doc.templateSnapshot.subtasks.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}</select></label><label>人工核查依据<textarea id="evidence-reason" rows="4"></textarea></label><button id="apply-count-evidence">记录此内容修订的计数依据</button>`);
    btn("apply-count-evidence", async () => {
      await direct({
        count_evidence: {
          [value("evidence-step").value]: value("evidence-reason").value
        }
      });
      closeModal();
      note("计数依据已绑定当前内容；可以重新运行恰好次数筛选。");
    });
  });
  btn("history", historyDialog);
  btn("candidates", candidateDialog);
  btn("templates", templateDialog);
  btn("delivery", deliveryDialog);
  btn("batch-review", batchReviewDialog);
  btn("import-annotations", importAnnotationsDialog);
  async function refreshNavigation() {
    await refresh(false);
    if (!active && allRows.length) {
      await openEpisode(allRows.some((row) => row.episode === episode) ? episode : allRows[0].episode);
    } else if (active?.readFailure) {
      backup(active);
      sessions.delete(episode);
      await openEpisode(episode);
    }
  }
  async function applyFilter(next) {
    if (next.count && !/^\d+$/.test(next.count))
      throw Error("出现次数需要填写零或正整数");
    if (active)
      await sync(active);
    filterState = {
      ...next,
      template_signature: next.subtask ? active?.head.template_signature || next.template_signature : undefined
    };
    await refresh();
  }
  function selectRows(items) {
    items.forEach((row) => selection.add(row.episode, row.revision, filterState.subtask ? "filter" : "manual", row.counts));
    persistSelection();
    renderSelectionCount();
    renderEpisodes();
  }
  function selectEpisode(ep, checked) {
    const row = availableRows().find((row) => row.episode === ep);
    if (!row)
      throw Error("该 Episode 已不可用，请刷新列表");
    if (checked)
      selection.add(ep, row.revision, filterState.subtask ? "filter" : "manual", row.counts);
    else
      selection.remove(ep);
    persistSelection();
    renderSelectionCount();
    renderEpisodes();
  }
  const disconnectNavigation = options.navigation?.connect({
    open: jumpEpisode,
    filter: applyFilter,
    select: selectEpisode,
    selectPage: () => selectRows(navigationRows().slice(page * 10, (page + 1) * 10)),
    selectResults: () => selectRows(navigationRows()),
    clearSelection: () => {
      selection.entries.clear();
      persistSelection();
      renderSelectionCount();
      renderEpisodes();
    },
    page: (next) => {
      page = Math.max(0, Math.min(next, Math.ceil(navigationRows().length / 10) - 1));
      renderEpisodes();
    },
    refresh: refreshNavigation,
    visibilityChanged: () => {
      if (!navigationInitialized)
        return;
      for (const ep of options.navigation.getVisibility().excluded)
        selection.remove(ep);
      persistSelection();
      renderSelectionCount();
      renderEpisodes();
    }
  });
  if (!options.navigation) {
    btn("refresh", refreshNavigation);
    btn("filter", () => applyFilter({
      subtask: value("filter-step").value,
      count: value("filter-count").value,
      basis: value("filter-basis").value
    }));
    btn("clear-filter", () => {
      value("filter-step").value = "";
      value("filter-count").value = "";
      return applyFilter({});
    });
    btn("prev-page", () => {
      page = Math.max(0, page - 1);
      renderEpisodes();
    });
    btn("next-page", () => {
      page = Math.min(Math.max(0, Math.ceil(navigationRows().length / 10) - 1), page + 1);
      renderEpisodes();
    });
    btn("select-page", () => selectRows(navigationRows().slice(page * 10, (page + 1) * 10)));
    btn("select-results", () => selectRows(navigationRows()));
  }
  value("dataset").onchange = () => safe(() => openDataset(value("dataset").value));
  listen("keydown", (event) => {
    const e = event;
    if (options.native && !(event.target instanceof Node && root.contains(event.target)) && !root.matches(":hover"))
      return;
    if (!active || editableTarget(e.target) || $("dialog").open)
      return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      safe(() => historyStep(e.shiftKey));
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") {
      e.preventDefault();
      safe(() => historyStep(true));
    } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      pause();
      safe(() => seek(frame + (e.key === "ArrowLeft" ? -1 : 1)));
    } else if (options.navigation && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
      e.preventDefault();
      const ordered = navigationRows(), next = e.key === "ArrowDown" ? ordered.find((row) => row.episode > episode) : [...ordered].reverse().find((row) => row.episode < episode);
      if (next)
        safe(() => openEpisode(next.episode));
    } else if (e.key === " ") {
      e.preventDefault();
      playing ? pause() : play();
    }
  });
  listen("beforeunload", (event) => {
    const e = event;
    sessions.forEach(backup);
    if ([...sessions.values()].some((s) => s.dirty || s.localError)) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
  listen("online", () => {
    for (const s of sessions.values())
      s.retryAt = 0;
    schedule();
  });
  listen("offline", () => note("网络离线：继续编辑，草稿在本机保留；未缓存的画面不可伪装为已定位。"));
  const autosaveInterval = setInterval(() => {
    for (const s of sessions.values())
      if (s.dirty && !s.remote && !s.readFailure && !s.saving && Date.now() >= s.retryAt)
        safe(() => sync(s));
  }, 2000);
  async function init() {
    root.classList.toggle("native-workspace", !!options.native);
    if (options.native)
      root.querySelector(".episode-picker")?.removeAttribute("open");
    value("dataset").disabled = !!options.native;
    try {
      const result = await request("datasets");
      value("dataset").innerHTML = result.datasets.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("");
      if (!dataset)
        dataset = result.datasets[0]?.id || "";
      value("dataset").value = dataset;
      if (dataset)
        await openDataset(dataset);
      else
        note("请先在 Server 登记一个可读取的 LeRobot 数据集。");
    } catch (e) {
      note(e.message);
      if (dataset) {
        value("dataset").innerHTML = `<option value="${esc(dataset)}">${esc(dataset)}（本地恢复）</option>`;
        await openDataset(dataset);
      }
    }
  }
  const ready = init();
  safe(() => ready);
  return {
    ready,
    seekTime(seconds) {
      if (disposed)
        return;
      requestedTime = seconds;
      if (!active)
        return;
      const target = Math.round(seconds * active.head.fps);
      if (Math.abs(target - frame) > 0) {
        pause();
        seek(target);
      }
    },
    destroy() {
      if (disposed)
        return;
      sessions.forEach(backup);
      for (const s of sessions.values())
        if (s.dirty && !s.saving)
          safe(() => sync(s));
      disposed = true;
      ++loadGeneration;
      ++datasetGeneration;
      ++listGeneration;
      lifecycle.abort();
      clearTimeout(saveTimer);
      clearInterval(autosaveInterval);
      disconnectNavigation?.();
      transport?.dispose();
      transport = undefined;
      root.querySelector("#dialog")?.close();
    }
  };
}
if (typeof document !== "undefined" && document.body?.hasAttribute("data-workbench-standalone"))
  mountWorkbench(document.body);
export {
  mountWorkbench
};
