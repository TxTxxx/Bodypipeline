import {
  copy,
  uid,
  operation,
  clean,
  resize,
  split,
  replace,
  move,
  mergeSegments,
  mergeAttempts,
  relabelAttempt,
  UndoHistory,
  LocalDrafts,
  EpisodeSelection,
  inclusive,
  gaps,
  editableTarget,
  templateDiff,
  upgrade,
  type Head,
  type Document,
  type Segment,
  type Attempt,
  type Template,
  type Field,
  type Value,
  type Backup,
} from "./workbench-model";
const $ = <T extends HTMLElement = HTMLElement>(id: string) =>
  document.getElementById(id) as T;
const esc = (v: unknown) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ]!,
  );
const value = (id: string) => $(id) as HTMLInputElement & HTMLSelectElement;
const btn = (id: string, run: () => unknown) =>
  $(id).addEventListener("click", () => safe(run));
const labels = {
  SUBTASK: "正式标签",
  NULL: "NULL · 已确认背景",
  UNKNOWN: "UNKNOWN · 证据不足",
  OUT_OF_TEMPLATE: "OUT OF TEMPLATE · 模板外活动",
};
const outcomes = { success: "成功", failure: "失败", unknown: "无法判断" };
const apiBase = "/api/annotation-workbench/";
let dataset = new URLSearchParams(location.search).get("dataset") || "",
  episode = Number(new URLSearchParams(location.search).get("episode") || 0);
let allRows: any[] = [],
  rows: any[] = [],
  page = 0,
  filterState: any = {},
  signature = "",
  library: { revision: number; templates: Template[] } = {
    revision: 0,
    templates: [],
  };
let drafts: LocalDrafts,
  selection = new EpisodeSelection(),
  active: Session | null = null,
  tool: "browse" | "range" | "move" = "browse",
  frame = 0,
  zoomStart = 0,
  zoomEnd = 1,
  playing = false,
  seekGeneration = 0;
interface Media {
  frames: number;
  fps: number;
  source_signature?: string;
  timestamps: number[];
  duration: number;
  cameras: { key: string; path: string; start: number; end: number }[];
}
interface Session {
  head: Head;
  doc: Document;
  undo: UndoHistory;
  dirty: boolean;
  input: Record<string, string>;
  media?: Media;
  saving?: Promise<void>;
  error?: string;
  remote?: Head;
  operation: string;
  mapping?: Record<string, string>;
  retryAt: number;
  localError?: string;
}
const sessions = new Map<number, Session>();
let loadGeneration = 0,
  datasetGeneration = 0,
  listGeneration = 0;
function note(text = "") {
  $("notice").textContent = text;
}
function safe(run: () => unknown) {
  try {
    const result = run();
    if (result instanceof Promise)
      result.catch((e) => note(String(e.message || e)));
  } catch (e: any) {
    note(e.message || String(e));
  }
}
async function request(path: string, body?: unknown) {
  const response = await fetch(apiBase + path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let result: any;
  try {
    result = await response.json();
  } catch {
    throw Error("服务未返回有效结果，本地草稿保留");
  }
  if (!response.ok) {
    const err: any = Error(
      typeof result.detail === "string"
        ? result.detail
        : JSON.stringify(result.detail),
    );
    err.status = response.status;
    throw err;
  }
  return result;
}
function current() {
  if (!active) throw Error("先选择 Episode");
  return active;
}
function selected(s = current()) {
  return s.doc.segments.find((x) => x.id === s.input.selected);
}
function ranges(s = current()) {
  return inclusive(Number(s.input.first), Number(s.input.last), s.head.frames);
}
function backup(s: Session) {
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
      at: Date.now(),
    } as Backup);
    s.localError = "";
  } catch (e: any) {
    s.localError = e.message;
    note(e.message);
  }
}
function rememberInput() {
  if (!active) return;
  for (const [id, key] of [
    ["range-first", "first"],
    ["range-last", "last"],
    ["new-label", "label"],
    ["association", "association"],
    ["new-note", "note"],
    ["new-description", "description"],
    ["new-outcome", "outcome"],
  ])
    active.input[key] = value(id).value;
  backup(active);
  $("input-status").textContent = active.localError
    ? "本地备份失败"
    : "编辑中输入已在本机保留";
}
function change(next: Document, name = "edit", render = true) {
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
function edit(name: string, modify: (doc: Document) => void, render = true) {
  const s = current();
  change(operation(s.doc, s.head.frames, name, modify), name, render);
}
let saveTimer: ReturnType<typeof setTimeout>;
function schedule() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    for (const s of sessions.values())
      if (s.dirty && !s.remote && !s.saving && Date.now() >= s.retryAt)
        safe(() => sync(s));
  }, 500);
}
async function sync(s: Session): Promise<void> {
  if (s.saving) return s.saving;
  if (!s.dirty || s.remote) return;
  const snapshot = copy(s.doc);
  const op = s.operation;
  const revision = s.head.revision;
  s.error = "";
  const promise = (async () => {
    try {
      const result = await request(
        `${s.head.dataset}/episodes/${s.head.episode}`,
        {
          revision,
          source_signature: s.head.source_signature,
          document: snapshot,
          operation: op,
          ...(s.mapping ? { template_mapping: s.mapping } : {}),
        },
      );
      s.head = { ...s.head, ...result };
      if (JSON.stringify(s.doc) === JSON.stringify(snapshot)) {
        s.doc = copy(result.document);
        s.dirty = false;
        s.mapping = undefined;
      }
      if (dataset === s.head.dataset && selection.entries.has(s.head.episode))
        renderSelectionCount();
      backup(s);
      if (active === s) {
        renderStatus();
        renderSummary();
        renderTimeline();
      }
      const row =
        dataset === s.head.dataset
          ? allRows.find((r) => r.episode === s.head.episode)
          : undefined;
      if (row) {
        Object.assign(row, {
          revision: s.head.revision,
          complete: s.head.complete,
          status: result.status,
        });
        renderEpisodes();
      }
    } catch (e: any) {
      s.error = e.message;
      s.retryAt = Date.now() + 8000;
      if (e.status === 409) {
        try {
          s.remote = await request(
            `${s.head.dataset}/episodes/${s.head.episode}`,
          );
        } catch {
          /* both local and old server remain in backup */
        }
      }
      backup(s);
      if (active === s) renderStatus();
    } finally {
      s.saving = undefined;
      if (active === s) renderStatus();
      if (s.dirty && !s.remote) schedule();
    }
  })();
  s.saving = promise;
  renderStatus();
  await promise;
}
async function direct(extra: Record<string, unknown>) {
  const s = current();
  await sync(s);
  if (s.dirty || s.remote) throw Error("请先同步或处理冲突；本地输入保留");
  const original = copy(s.doc);
  const response = await request(
    `${s.head.dataset}/episodes/${s.head.episode}`,
    {
      revision: s.head.revision,
      source_signature: s.head.source_signature,
      document: original,
      operation: "human-review",
      ...extra,
    },
  );
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
  if (!s) return;
  $("save-status").innerHTML = s.remote
    ? '<span class="error">版本冲突 · 双方已保留</span>'
    : s.saving
      ? '<span class="saving">正在同步…</span>'
      : s.error
        ? `<span class="error">未同步 · ${esc(s.error)}</span>`
        : s.dirty
          ? '<span class="saving">草稿已在本机 · 待同步</span>'
          : '<span class="synced">已同步服务器</span>';
  $("save-status").innerHTML +=
    `<br><span class="hint">Episode 修订 ${s.head.revision} · ${s.head.complete ? "人工已完成" : "尚未声明完成"}${s.localError ? " · 本地备份失败" : " · 本地备份可恢复"}</span>`;
  const c = $("conflict");
  c.hidden = !s.remote;
  if (s.remote) {
    c.innerHTML =
      '<strong>这个 Episode 已在其他窗口更新</strong><p>本地修改与服务器内容均保留。其他 Episode 可以继续工作。</p><button id="compare-conflict">对照双方并处理</button>';
    btn("compare-conflict", conflictDialog);
  }
  $("undo").toggleAttribute("disabled", !s.undo.past.length);
  $("redo").toggleAttribute("disabled", !s.undo.future.length);
}
function renderSummary() {
  if (!active) return;
  const s = active,
    blank = gaps(s.doc, s.head.frames);
  const pending = s.doc.segments.filter((x) => !x.review).length;
  $("document-summary").innerHTML =
    `<span>${s.doc.segments.length} 个片段</span><span>${new Set(s.doc.segments.map((x) => x.attemptId).filter(Boolean)).size} 次独立尝试</span><span>${blank.length ? `空白 ${blank.map(([a, b]) => `${a}–${b - 1}`).join("、")}` : "全片已有标注"}</span><span>${pending ? `${pending} 段待核对` : "当前内容均已核对"}</span><span>实际模板：${esc(s.doc.templateSnapshot.name)}</span>`;
  $("review-state").textContent =
    s.head.complete && !s.dirty ? "已完成" : "保存不等于人工确认";
}
function renderEpisodes() {
  const list = rows.slice(page * 10, (page + 1) * 10);
  $("episode-list").innerHTML =
    list
      .map(
        (r) =>
          `<div class="episode-row ${r.episode === episode ? "current" : ""}"><input type="checkbox" aria-label="加入 Episode ${r.episode}" data-add="${r.episode}" ${selection.entries.has(r.episode) ? "checked" : ""}><button data-open="${r.episode}"><strong>Episode ${String(r.episode).padStart(3, "0")}</strong> <span class="pill ${r.complete ? "green" : "amber"}">${r.complete ? "已完成" : "草稿"}</span><small>${r.frames} 帧 · 修订 ${r.revision}<br>${r.counts?.recorded_count == null ? "尚未选择计数标签" : `已记录 ${r.counts.recorded_count} 次 · ${r.counts.complete ? "次数确定" : "次数不确定"}`}</small></button></div>`,
      )
      .join("") ||
    '<div class="empty">没有匹配项。可查看全部并人工加入，空选择不会导出全量。</div>';
  $("page-status").textContent =
    `${page + 1} / ${Math.max(1, Math.ceil(rows.length / 10))} · ${rows.length} 条`;
  $("episode-list")
    .querySelectorAll<HTMLElement>("[data-open]")
    .forEach(
      (el) =>
        (el.onclick = () => safe(() => openEpisode(Number(el.dataset.open)))),
    );
  $("episode-list")
    .querySelectorAll<HTMLInputElement>("[data-add]")
    .forEach(
      (el) =>
        (el.onchange = () => {
          const r = rows.find((r) => r.episode === Number(el.dataset.add));
          if (el.checked)
            selection.add(
              r.episode,
              r.revision,
              filterState.subtask ? "filter" : "manual",
              r.counts,
            );
          else selection.remove(r.episode);
          persistSelection();
          renderSelectionCount();
        }),
    );
}
function persistSelection() {
  try {
    localStorage.setItem(
      `bp-selection:${dataset}`,
      JSON.stringify([...selection.entries]),
    );
  } catch (e) {
    note("选择清单本地备份失败，请保持页面打开");
  }
}
function renderSelectionCount() {
  $("selection-count").textContent = String(selection.entries.size);
}
async function refresh(reset = true) {
  const sourceDataset = dataset,
    generation = ++listGeneration;
  const chosenFilter = copy(filterState);
  let query = "";
  if (chosenFilter.subtask)
    query = `?subtask=${encodeURIComponent(chosenFilter.subtask)}${chosenFilter.count !== "" && chosenFilter.count !== undefined ? `&count=${encodeURIComponent(chosenFilter.count)}` : ""}&recorded=${chosenFilter.basis === "recorded"}${chosenFilter.template_signature ? `&template_signature=${encodeURIComponent(chosenFilter.template_signature)}` : ""}`;
  try {
    const result = await request(`${sourceDataset}/episodes${query}`);
    const completeRows = !query
      ? result.episodes
      : allRows.length
        ? allRows
        : (await request(`${sourceDataset}/episodes`)).episodes;
    if (sourceDataset !== dataset || generation !== listGeneration) return;
    rows = result.episodes;
    allRows = completeRows;
    signature = result.source_signature;
    try {
      localStorage.setItem(
        `bp-catalog:${sourceDataset}`,
        JSON.stringify({ rows: allRows, signature }),
      );
    } catch {
      note("列表本地备份失败，服务器读取结果仍保留。请保持页面打开。");
    }
  } catch (e: any) {
    if (sourceDataset !== dataset || generation !== listGeneration) return;
    const cached = localStorage.getItem(`bp-catalog:${sourceDataset}`);
    if (!cached) throw e;
    const old = JSON.parse(cached);
    allRows = old.rows;
    rows = allRows;
    signature = old.signature;
    note("来源列表使用本地备份；未同步修改继续保留。筛选需恢复连接后核实。");
  }
  if (reset) page = 0;
  renderEpisodes();
}
async function openDataset(id: string) {
  const generation = ++datasetGeneration;
  ++loadGeneration;
  ++listGeneration;
  for (const s of sessions.values()) {
    backup(s);
    safe(() => sync(s));
  }
  dataset = id;
  sessions.clear();
  active = null;
  selection = new EpisodeSelection();
  drafts = new LocalDrafts(localStorage, dataset);
  try {
    selection.entries = new Map(
      JSON.parse(localStorage.getItem(`bp-selection:${dataset}`) || "[]"),
    );
  } catch {}
  renderSelectionCount();
  filterState = {};
  allRows = [];
  rows = [];
  await refresh();
  if (generation !== datasetGeneration || dataset !== id) return;
  if (!allRows.length) {
    note("来源没有可用 Episode");
    return;
  }
  await openEpisode(
    allRows.some((r) => r.episode === episode) ? episode : allRows[0].episode,
  ); // Cache annotation heads to allow Episode switches while offline.
  const prefetchDataset = dataset,
    prefetchDrafts = drafts,
    prefetchRows = [...allRows];
  void (async () => {
    for (const row of prefetchRows) {
      if (dataset !== prefetchDataset) break;
      if (row.episode === episode) continue;
      try {
        if (prefetchDrafts.read(row.episode)) continue;
        const head = await request(
          `${prefetchDataset}/episodes/${row.episode}`,
        );
        prefetchDrafts.write(row.episode, {
          head,
          document: head.document,
          dirty: false,
          input: {},
          past: [],
          future: [],
          at: Date.now(),
        });
      } catch {
        break;
      }
    }
  })();
}
async function openEpisode(ep: number) {
  const load = ++loadGeneration,
    sourceDataset = dataset;
  if (active) backup(active);
  pause();
  episode = ep;
  const existing = sessions.get(ep);
  if (existing) active = existing;
  else {
    const saved = drafts.read(ep);
    let head: Head;
    try {
      head = await request(`${dataset}/episodes/${ep}`);
    } catch (e) {
      if (!saved)
        throw Error("这个 Episode 尚未缓存；已缓存的 Episode 仍可离线编辑");
      head = saved.head;
      note("正在使用本地备份，服务器同步尚未完成");
    }
    const sameSource =
      !saved || saved.head.source_signature === head.source_signature;
    if (load !== loadGeneration || dataset !== sourceDataset) return;
    const use =
      saved &&
      sameSource &&
      (saved.dirty || saved.head.revision === head.revision);
    const s: Session = {
      head,
      doc: use ? copy(saved!.document) : copy(head.document),
      undo: new UndoHistory(),
      input: use ? copy(saved!.input) : {},
      dirty: !!use && !!saved!.dirty,
      operation: saved?.operation || "edit",
      mapping: use ? saved!.mapping : undefined,
      retryAt: 0,
    };
    if (use) {
      s.undo.past = saved!.past || [];
      s.undo.future = saved!.future || [];
    }
    if (
      saved &&
      (!sameSource || (saved.dirty && saved.head.revision !== head.revision))
    ) {
      s.remote = copy(head);
      s.head = saved.head;
      s.doc = copy(saved.document);
      s.error = "服务器修订或来源与本地草稿不同";
    }
    sessions.set(ep, s);
    active = s;
  }
  if (load !== loadGeneration || dataset !== sourceDataset) return;
  const s = current();
  s.input = {
    first: "0",
    last: "0",
    label: s.doc.templateSnapshot.subtasks[0]?.id || "NULL",
    association: "new",
    note: "",
    description: "",
    outcome: "",
    ...s.input,
  };
  frame = Math.max(0, Math.min(s.head.frames - 1, Number(s.input.frame || 0)));
  zoomStart = 0;
  zoomEnd = s.head.frames;
  try {
    if (s.remote && s.remote.source_signature !== s.head.source_signature)
      throw Error("来源版本改变，旧草稿与新来源隔离，请先处理冲突");
    s.media = await request(`${sourceDataset}/episodes/${ep}/media`);
    if (
      s.media!.source_signature &&
      s.media!.source_signature !== s.head.source_signature
    ) {
      s.media = undefined;
      throw Error("来源媒体版本不同，禁止混用新画面和旧帧标注");
    }
    localStorage.setItem(
      `bp-media:${sourceDataset}:${s.head.source_signature}:${ep}`,
      JSON.stringify(s.media),
    );
  } catch (e: any) {
    const old = localStorage.getItem(
      `bp-media:${sourceDataset}:${s.head.source_signature}:${ep}`,
    );
    if (old) s.media = JSON.parse(old);
    else note(e.message);
  }
  if (load !== loadGeneration || dataset !== sourceDataset) return;
  const url = new URL(location.href);
  url.searchParams.set("dataset", dataset);
  url.searchParams.set("episode", String(ep));
  history.replaceState(null, "", url);
  renderAll();
  await seek(frame);
  if (s.dirty) schedule();
}
function renderAll(preview = true) {
  if (!active) return;
  $("episode-title").textContent =
    `Episode ${String(episode).padStart(3, "0")}`;
  value("scrub").max = String(active.head.frames - 1);
  value("frame").max = String(active.head.frames - 1);
  $("total").textContent =
    `/ ${active.head.frames - 1} · 共 ${active.head.frames} 帧`;
  if (preview) renderPreview();
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
  if (!active) return;
  const select = $("filter-step") as HTMLSelectElement;
  const old = select.value;
  select.innerHTML =
    '<option value="">全部 Episode</option>' +
    active.doc.templateSnapshot.subtasks
      .map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`)
      .join("");
  select.value = old;
}
function renderPreview() {
  const s = current();
  if (!s.media?.cameras.length) {
    $("preview").innerHTML =
      '<div class="empty">此来源没有可用视频。标注数据帧仍保留，不能把帧号当作画面验证。</div>';
    return;
  }
  $("preview").innerHTML = s.media.cameras
    .map(
      (c, i) =>
        `<div class="camera" data-camera="${esc(c.key)}"><span class="camera-label">${esc(c.key)}</span><video preload="auto" playsinline muted data-video="${i}" src="${apiBase}${encodeURIComponent(dataset)}/media/${c.path.split("/").map(encodeURIComponent).join("/")}"></video><img alt="${esc(c.key)} 当前数据帧画面" data-frame-image="${i}"><span class="frame-tag">定位中</span></div>`,
    )
    .join("");
  const primary = $("preview").querySelector<HTMLVideoElement>("video");
  if (primary)
    primary.ontimeupdate = () => {
      if (!playing || !s.media || active !== s) return;
      const time = Math.max(0, primary.currentTime - s.media.cameras[0].start);
      const times = s.media.timestamps;
      let low = 0,
        high = times.length - 1;
      while (low < high) {
        const mid = Math.ceil((low + high) / 2);
        if (times[mid] <= time + 1e-7) low = mid;
        else high = mid - 1;
      }
      frame = low;
      updateFrameNumber("连续播放 · 暂停后定位数据帧");
      renderTimeline();
      if (time >= s.media.duration - 1 / s.media.fps / 2) pause();
    };
}
function updateFrameNumber(state?: string) {
  value("frame").value = String(frame);
  value("scrub").value = String(frame);
  if (active) active.input.frame = String(frame);
  if (state) $("frame-state").textContent = state;
}
async function seek(target: number) {
  const s = current(),
    token = ++seekGeneration;
  frame = Math.max(0, Math.min(s.head.frames - 1, Math.round(target)));
  updateFrameNumber("正在解码实际画面…");
  renderTimeline();
  renderFrameFields();
  if (!s.media?.cameras.length) return;
  const chosen = frame;
  const cameras = Array.from(
    $("preview").querySelectorAll<HTMLElement>(".camera"),
  );
  cameras.forEach((c) => c.classList.add("loading"));
  const results = await Promise.allSettled(
    s.media.cameras.map(async (c, i) => {
      const url = `${apiBase}${dataset}/episodes/${episode}/frame/${chosen}?camera=${encodeURIComponent(c.key)}&signature=${s.head.source_signature}`;
      let blob: Blob;
      try {
        const response = await fetch(url);
        if (!response.ok) throw Error("画面定位失败");
        blob = await response.blob();
        if ("caches" in window) {
          const cache = await caches.open("bodypipeline-workbench-frames-v2");
          void cache.put(url, new Response(blob));
        }
      } catch (e) {
        if (!("caches" in window)) throw e;
        const old = await (
          await caches.open("bodypipeline-workbench-frames-v2")
        ).match(url);
        if (!old)
          throw Error("此帧画面未缓存；可以继续编辑，不能把帧号当作已定位画面");
        blob = await old.blob();
      }
      if (token !== seekGeneration || active !== s) return;
      const img = cameras[i]?.querySelector<HTMLImageElement>("img");
      if (!img) return;
      const previous = img.src;
      const object = URL.createObjectURL(blob);
      await new Promise<void>((resolve, reject) => {
        img.onload = () => resolve();
        img.onerror = () => reject(Error("画面解码失败"));
        img.src = object;
      });
      if (previous.startsWith("blob:")) URL.revokeObjectURL(previous);
      if (token !== seekGeneration) return;
      img.hidden = false;
      cameras[i].dataset.displayedFrame = String(chosen);
      cameras[i].querySelector(".frame-tag")!.textContent =
        `DATA FRAME ${chosen}`;
      cameras[i].classList.remove("loading");
      const video = cameras[i].querySelector("video")!;
      video.pause();
      video.currentTime = c.start + s.media!.timestamps[chosen];
    }),
  );
  if (token !== seekGeneration) return;
  const failed = results.find((r) => r.status === "rejected") as
    PromiseRejectedResult | undefined;
  $("frame-state").textContent = failed
    ? String(failed.reason.message)
    : `已对齐数据帧 ${chosen} · ${s.media.cameras.length} 个相机`;
  backup(s);
}
function pause() {
  if (playing && active?.media) {
    const primary = $("preview").querySelector<HTMLVideoElement>("video");
    if (primary) {
      const time = primary.currentTime - active.media.cameras[0].start;
      const ts = active.media.timestamps;
      let best = 0;
      for (let i = 1; i < ts.length; i++) {
        if (Math.abs(ts[i] - time) < Math.abs(ts[best] - time)) best = i;
      }
      frame = best;
    }
  }
  playing = false;
  $("play").textContent = "播放";
  $("preview")
    .querySelectorAll<HTMLVideoElement>("video")
    .forEach((v) => v.pause());
  if (active) safe(() => seek(frame));
}
function play() {
  const s = current();
  if (!s.media) return;
  if (s.remote && s.remote.source_signature !== s.head.source_signature)
    throw Error("来源变化后禁止播放新来源覆盖旧草稿画面");
  playing = true;
  ++seekGeneration;
  $("play").textContent = "暂停并精确定位";
  $("preview")
    .querySelectorAll<HTMLImageElement>("img")
    .forEach((img) => (img.hidden = true));
  $("preview")
    .querySelectorAll<HTMLVideoElement>("video")
    .forEach((v, i) => {
      v.currentTime = s.media!.cameras[i].start + s.media!.timestamps[frame];
      void v.play().catch((e) => {
        playing = false;
        note(e.message);
      });
    });
}
function visiblePercent(f: number) {
  return ((f - zoomStart) / (zoomEnd - zoomStart)) * 100;
}
function renderTimeline() {
  if (!active) return;
  const s = active,
    lo = zoomStart,
    hi = zoomEnd;
  $("ruler").innerHTML = Array.from(
    { length: 7 },
    (_, i) =>
      `<span class="tick" style="left:${(i / 6) * 100}%">${Math.round(lo + ((hi - lo) * i) / 6)}</span>`,
  ).join("");
  const segments = s.doc.segments.filter((x) => x.start < hi && x.end > lo);
  $("track").innerHTML = segments
    .map((x) => {
      const name =
        x.label === "SUBTASK"
          ? s.doc.templateSnapshot.subtasks.find((t) => t.id === x.subtaskId)
              ?.name
          : labels[x.label];
      const a = s.doc.attempts.find((a) => a.id === x.attemptId);
      return `<div class="segment ${x.label} ${s.input.selected === x.id ? "selected" : ""}" data-segment="${esc(x.id)}" style="left:${Math.max(0, visiblePercent(x.start))}%;width:${((Math.min(hi, x.end) - Math.max(lo, x.start)) / (hi - lo)) * 100}%" tabindex="0" role="button" aria-label="${esc(name)} ${x.start} 至 ${x.end - 1}"><i class="edge start" data-side="start"></i><strong>${esc(name)}</strong><small>${x.start}–${x.end - 1} · ${x.review ? "已核" : "待核"}${a ? ` · ${a.outcome ? outcomes[a.outcome] : "结果暂缺"}` : ""}</small><i class="edge end" data-side="end"></i></div>`;
    })
    .join("");
  try {
    const r = ranges(s);
    if (r.start < hi && r.end > lo)
      $("track").innerHTML +=
        `<div class="range-overlay" style="left:${Math.max(0, visiblePercent(r.start))}%;width:${((Math.min(hi, r.end) - Math.max(lo, r.start)) / (hi - lo)) * 100}%"></div>`;
  } catch {}
  if (frame >= lo && frame < hi)
    $("track").innerHTML +=
      `<div class="playhead" style="left:${visiblePercent(frame)}%"></div>`;
  // CSSOM assignments work under the Server's strict style-src policy;
  // style attributes parsed through innerHTML are intentionally refused.
  $("ruler")
    .querySelectorAll<HTMLElement>(".tick")
    .forEach((tick, i) => {
      tick.style.left = `${(i / 6) * 100}%`;
    });
  $("track")
    .querySelectorAll<HTMLElement>("[data-segment]")
    .forEach((el) => {
      const x = s.doc.segments.find((x) => x.id === el.dataset.segment)!;
      el.style.left = `${Math.max(0, visiblePercent(x.start))}%`;
      el.style.width = `${((Math.min(hi, x.end) - Math.max(lo, x.start)) / (hi - lo)) * 100}%`;
    });
  const overlay = $("track").querySelector<HTMLElement>(".range-overlay");
  if (overlay) {
    try {
      const r = ranges(s);
      overlay.style.left = `${Math.max(0, visiblePercent(r.start))}%`;
      overlay.style.width = `${((Math.min(hi, r.end) - Math.max(lo, r.start)) / (hi - lo)) * 100}%`;
    } catch {}
  }
  const head = $("track").querySelector<HTMLElement>(".playhead");
  if (head) head.style.left = `${visiblePercent(frame)}%`;
  $("zoom-status").textContent =
    `局部 ${lo}–${hi - 1} · ${hi - lo} / ${s.head.frames} 帧`;
}
let gesture: null | {
  mode: string;
  id?: string;
  anchor: number;
  original?: Segment;
  before: Document;
  current: number;
  pointer: number;
  host: HTMLElement;
} = null;
function pointFrame(e: PointerEvent, host: HTMLElement, edge = false) {
  const rect = host.getBoundingClientRect();
  return Math.max(
    zoomStart,
    Math.min(
      zoomEnd - (edge ? 0 : 1),
      Math.round(
        zoomStart +
          ((e.clientX - rect.left) / rect.width) * (zoomEnd - zoomStart),
      ),
    ),
  );
}
$("timeline").addEventListener("pointerdown", (e) => {
  if (!active || e.button !== 0) return;
  const target = e.target as HTMLElement,
    segmentEl = target.closest<HTMLElement>("[data-segment]"),
    isRuler = !!target.closest("#ruler"),
    host = isRuler ? $("ruler") : $("track");
  const id = segmentEl?.dataset.segment,
    s = id ? active.doc.segments.find((s) => s.id === id) : undefined;
  const side = target.dataset.side;
  const at = pointFrame(e, host, !!side);
  if (s && tool !== "range" && !side && tool !== "move") {
    active.input.selected = s.id;
    renderEditor();
    renderTimeline();
    backup(active);
    return;
  }
  const mode = side
    ? side
    : isRuler || tool === "browse"
      ? "scrub"
      : tool === "move" && s
        ? "move"
        : "range";
  gesture = {
    mode,
    id,
    anchor: at,
    current: at,
    original: s ? copy(s) : undefined,
    before: copy(active.doc),
    pointer: e.pointerId,
    host,
  };
  host.setPointerCapture(e.pointerId);
  e.preventDefault();
  if (mode === "scrub") {
    pause();
    safe(() => seek(at));
  }
});
$("timeline").addEventListener("pointermove", (e) => {
  if (!gesture || !active) return;
  gesture.current = pointFrame(
    e,
    gesture.host,
    gesture.mode === "start" || gesture.mode === "end",
  );
  if (gesture.mode === "scrub") {
    safe(() => seek(gesture!.current));
  } else if (gesture.mode === "range") {
    active.input.first = String(Math.min(gesture.anchor, gesture.current));
    active.input.last = String(Math.max(gesture.anchor, gesture.current));
    value("range-first").value = active.input.first;
    value("range-last").value = active.input.last;
    renderTimeline();
  } else {
    try {
      const g = gesture;
      active.doc =
        g.mode === "move"
          ? move(
              g.before,
              active.head.frames,
              g.id!,
              g.original!.start + g.current - g.anchor,
            )
          : resize(
              g.before,
              active.head.frames,
              g.id!,
              g.mode as "start" | "end",
              g.current,
            );
      renderTimeline();
    } catch {
      /* invalid drag stays at last legal preview */
    }
  }
});
$("timeline").addEventListener("pointerup", (e) => {
  if (!gesture || !active) return;
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
  } else safe(() => seek(g.current));
});
$("timeline").addEventListener("pointercancel", () => {
  if (gesture && active) {
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
    ["new-outcome", "outcome"],
  ])
    value(id).value = s.input[key] || "";
  value("new-label").innerHTML =
    s.doc.templateSnapshot.subtasks
      .map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`)
      .join("") +
    ["NULL", "UNKNOWN", "OUT_OF_TEMPLATE"]
      .map(
        (l) =>
          `<option value="${l}">${labels[l as keyof typeof labels]}</option>`,
      )
      .join("");
  value("new-label").value = s.input.label;
  renderAssociation();
}
function renderAssociation() {
  const s = current(),
    label = value("new-label").value,
    formal = s.doc.templateSnapshot.subtasks.find((t) => t.id === label);
  const opts = s.doc.attempts.filter(
    (a) => !a.archived && (!formal || a.subtaskId === label),
  );
  value("association").innerHTML =
    (formal || label === "OUT_OF_TEMPLATE"
      ? '<option value="new">明确建立一次新执行</option>'
      : "") +
    (formal ? "" : '<option value="none">不关联尝试</option>') +
    (label === "NULL"
      ? ""
      : opts
          .map(
            (a) =>
              `<option value="${esc(a.id)}">关联已有：${esc(a.subtaskName || "模板外活动")} · ${esc(a.id.slice(0, 8))} · ${a.outcome ? outcomes[a.outcome] : "结果暂缺"}</option>`,
          )
          .join(""));
  value("association").value = s.input.association;
  if (!value("association").value) value("association").selectedIndex = 0;
  s.input.association = value("association").value;
  $("new-outcome-label").hidden = value("association").value !== "new";
  $("description-label").hidden = label !== "OUT_OF_TEMPLATE";
  $("definition").textContent = formal
    ? `${formal.definition || "尚未补充业务定义"}\n包含：${formal.includes || "—"}\n排除：${formal.excludes || "—"}\n开始：${formal.startCondition || "—"}\n结束：${formal.endCondition || "—"}`
    : label === "NULL"
      ? "明确观察到背景，不关联尝试。静止本身不能证明是背景。"
      : label === "UNKNOWN"
        ? "证据不足；可由人明确关联已有尝试，不自动新建或计数。"
        : "明确存在模板外活动，请填写动作描述；可明确建立或关联尝试。";
}
function newSegment() {
  const s = current(),
    r = ranges(s),
    label = value("new-label").value,
    formal = s.doc.templateSnapshot.subtasks.find((t) => t.id === label),
    association = value("association").value;
  let attempt: Attempt | undefined;
  let attemptId = association === "none" ? undefined : association;
  if (association === "new") {
    attemptId = uid();
    attempt = {
      id: attemptId,
      subtaskId: formal?.id,
      subtaskName: formal?.name,
      values: {},
      outcome: (value("new-outcome").value || undefined) as Attempt["outcome"],
    };
  }
  const segment: Segment = {
    id: uid(),
    ...r,
    label: formal ? "SUBTASK" : (label as Segment["label"]),
    ...(formal ? { subtaskId: formal.id } : {}),
    ...(attemptId ? { attemptId } : {}),
    note: value("new-note").value,
    description: value("new-description").value,
    values: {},
  };
  if (segment.label === "NULL") delete segment.attemptId;
  return { r, segment, attempt };
}
async function createSegment(relabel = false, confirm = false) {
  rememberInput();
  const s = current(),
    { r, segment, attempt } = newSegment();
  if (
    !relabel &&
    s.doc.segments.some((x) => x.start < r.end && x.end > r.start)
  )
    throw Error("范围已有内容；请明确使用“重标选中范围”，两侧原内容会保留");
  change(
    replace(s.doc, s.head.frames, r.start, r.end, segment, attempt),
    relabel ? "range-relabel" : "create",
  );
  s.input.selected = segment.id;
  renderEditor();
  if (confirm) await direct({ confirm: [[r.start, r.end]] });
}
function fieldControl(f: Field, v: Value | undefined, key: string) {
  const desc = f.description || f.scale;
  let control = "";
  if (f.kind === "single" || f.kind === "bool") {
    const opts =
      f.kind === "bool"
        ? [
            { value: "true", label: "是" },
            { value: "false", label: "否" },
          ]
        : (f.options || []).map((x) => ({ value: x, label: x }));
    control = `<select data-field="${esc(f.id)}" data-key="${esc(key)}"><option value="">留空</option>${opts.map((o) => `<option value="${esc(o.value)}" ${String(v) === o.value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`;
  } else if (f.kind === "multi")
    control = `<select multiple data-field="${esc(f.id)}" data-key="${esc(key)}">${(f.options || []).map((o) => `<option ${Array.isArray(v) && v.includes(o) ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  else if (f.kind === "textarea")
    control = `<textarea rows="2" data-field="${esc(f.id)}" data-key="${esc(key)}">${esc(v ?? "")}</textarea>`;
  else
    control = `<input data-field="${esc(f.id)}" data-key="${esc(key)}" type="${["rating", "number"].includes(f.kind) ? "number" : "text"}" ${f.kind === "rating" ? `min="${f.minimum ?? 1}" max="${f.maximum ?? 5}" step="${f.step ?? 1}"` : 'step="any"'} value="${esc(v ?? "")}">`;
  return `<label>${esc(f.label)}${control}${desc ? `<span class="description">${esc(desc)}</span>` : ""}</label>`;
}
function bindFields(
  host: HTMLElement,
  scope: Field["scope"],
  getValues: () => Record<string, Value>,
  update: (d: Document, id: string, v: Value) => void,
) {
  host
    .querySelectorAll<
      HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
    >("[data-field]")
    .forEach((el) =>
      el.addEventListener("input", () =>
        safe(() => {
          const f = current().doc.templateSnapshot.fields.find(
            (f) => f.id === el.dataset.field,
          )!;
          let v: Value = el.value || null;
          if (f.kind === "multi")
            v = Array.from((el as HTMLSelectElement).selectedOptions).map(
              (x) => x.value,
            );
          if (v !== null && ["rating", "number"].includes(f.kind)) {
            v = Number(v);
            if (
              !Number.isFinite(v) ||
              (el instanceof HTMLInputElement && !el.validity.valid)
            ) {
              current().input[`field:${scope}:${f.id}`] = el.value;
              backup(current());
              note("输入尚未有效，已保留在编辑中草稿");
              return;
            }
          }
          if (v !== null && f.kind === "bool") v = v === "true";
          edit("field", (d) => update(d, f.id, v));
        }),
      ),
    );
}
function renderEditor() {
  const s = current(),
    seg = selected(s),
    host = $("segment-editor");
  if (!seg) {
    host.className = "empty";
    host.innerHTML = "点击主轨道的片段正文，播放头保持原位。";
    return;
  }
  host.className = "";
  const a = s.doc.attempts.find((a) => a.id === seg.attemptId),
    related = a ? s.doc.segments.filter((x) => x.attemptId === a.id) : [];
  const name =
    seg.label === "SUBTASK"
      ? s.doc.templateSnapshot.subtasks.find((x) => x.id === seg.subtaskId)
          ?.name
      : labels[seg.label];
  host.innerHTML = `<strong>${esc(name)} <span class="pill ${seg.review ? "green" : "amber"}">${seg.review ? "已核对" : "待核对"}</span></strong><div class="row" style="margin-top:12px"><label>首帧（包含）<input id="selected-first" value="${esc(s.input[`first:${seg.id}`] ?? seg.start)}" inputmode="numeric"></label><label>末帧（包含）<input id="selected-last" value="${esc(s.input[`last:${seg.id}`] ?? seg.end - 1)}" inputmode="numeric"></label></div><div class="row"><button id="copy-range">用此片段范围重标</button><button id="split">在当前帧切分</button><button id="merge-segment">与后段合并片段</button></div><label>此片段局部备注<textarea id="selected-note" rows="2">${esc(seg.note || "")}</textarea></label>${seg.label === "OUT_OF_TEMPLATE" ? `<label>模板外活动描述<textarea id="selected-description">${esc(seg.description || "")}</textarea></label>` : ""}<div class="field-grid" id="segment-fields">${s.doc.templateSnapshot.fields
    .filter((f) => f.scope === "segment")
    .map((f) => fieldControl(f, seg.values?.[f.id], seg.id))
    .join("")}</div>${
    a
      ? `<div class="attempt-info"><strong>整次尝试 ${esc(a.id.slice(0, 10))}</strong><br>共 ${related.length} 个片段：${related.map((x) => `${x.start}–${x.end - 1}`).join("、")}<br>下方结果与评分作用于这些全部片段；局部备注保持各自归属。</div><label>整次执行结果<select id="selected-outcome"><option value="">暂缺（草稿）</option>${Object.entries(
          outcomes,
        )
          .map(
            ([v, l]) =>
              `<option value="${v}" ${a.outcome === v ? "selected" : ""}>${l}</option>`,
          )
          .join(
            "",
          )}</select></label><div class="field-grid" id="attempt-fields">${s.doc.templateSnapshot.fields
          .filter((f) => f.scope === "attempt" && f.id !== "attempt-outcome")
          .map((f) => fieldControl(f, a.values[f.id], a.id))
          .join(
            "",
          )}</div><div class="row"><button id="whole-label">修改整次尝试标签</button><button id="merge-attempt">明确合并尝试</button></div>`
      : '<p class="hint">此片段没有尝试关联。可使用“用此片段范围重标”后明确选择关联。</p>'
  }<div class="row"><button id="confirm-selected">确认此片段</button><button id="boundary-evidence">比较边界前后画面</button><button id="delete-segment" class="danger">删除片段</button></div>${seg.risks?.length ? `<p class="special-help">待单独复核：${esc(seg.risks.join("；"))}</p>` : ""}${seg.evidence?.length ? `<details><summary>边界与来源证据</summary><pre>${esc(JSON.stringify(seg.evidence, null, 2))}</pre></details>` : ""}`;
  for (const [id, side] of [
    ["selected-first", "start"],
    ["selected-last", "end"],
  ] as const)
    value(id).oninput = () => {
      s.input[`${side === "start" ? "first" : "last"}:${seg.id}`] =
        value(id).value;
      backup(s);
      const raw = value(id).value;
      if (!/^\d+$/.test(raw)) return;
      try {
        change(
          resize(
            s.doc,
            s.head.frames,
            seg.id,
            side,
            Number(raw) + (side === "end" ? 1 : 0),
          ),
          "boundary",
        );
        delete s.input[`first:${seg.id}`];
        delete s.input[`last:${seg.id}`];
      } catch (e: any) {
        note(e.message);
      }
    };
  value("selected-note").oninput = () =>
    edit("segment-note", (d) => {
      d.segments.find((x) => x.id === seg.id)!.note =
        value("selected-note").value;
    });
  if (seg.label === "OUT_OF_TEMPLATE")
    value("selected-description").oninput = () =>
      edit("description", (d) => {
        d.segments.find((x) => x.id === seg.id)!.description = value(
          "selected-description",
        ).value;
      });
  bindFields(
    $("segment-fields"),
    "segment",
    () => seg.values || {},
    (d, id, v) => {
      const x = d.segments.find((x) => x.id === seg.id)!;
      (x.values ??= {})[id] = v;
    },
  );
  if (a) {
    value("selected-outcome").onchange = () =>
      edit("attempt-outcome", (d) => {
        d.attempts.find((x) => x.id === a.id)!.outcome = (value(
          "selected-outcome",
        ).value || undefined) as Attempt["outcome"];
      });
    bindFields(
      $("attempt-fields"),
      "attempt",
      () => a.values,
      (d, id, v) => {
        d.attempts.find((x) => x.id === a.id)!.values[id] = v;
      },
    );
    btn("whole-label", () => wholeLabelDialog(a));
    btn("merge-attempt", () => mergeAttemptDialog(a));
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
  btn("split", () => {
    change(split(s.doc, s.head.frames, seg.id, frame), "split");
    renderEditor();
  });
  btn("merge-segment", () => mergeSegmentDialog(seg));
  btn("confirm-selected", () => direct({ confirm: [[seg.start, seg.end]] }));
  btn("boundary-evidence", () => boundaryDialog(seg));
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
  $("episode-fields").innerHTML =
    '<div class="field-grid">' +
    s.doc.templateSnapshot.fields
      .filter((f) => f.scope === "episode")
      .map((f) => fieldControl(f, s.doc.values[f.id], "episode"))
      .join("") +
    "</div>";
  bindFields(
    $("episode-fields"),
    "episode",
    () => s.doc.values,
    (d, id, v) => {
      d.values[id] = v;
    },
  );
  renderFrameFields();
  renderAtoms();
}
function renderAtoms() {
  const s = current();
  $("manual-atoms").innerHTML =
    (s.doc.manualAtoms || [])
      .map(
        (a, i) =>
          `<label>${esc(a.style || "语言")} · ${esc(a.role)} · ${esc(a.timestamp)} 秒<textarea data-atom-index="${i}">${esc(a.content || "")}</textarea>${a.tool_calls ? `<details><summary>保留的工具调用</summary><pre>${esc(JSON.stringify(a.tool_calls, null, 2))}</pre></details>` : ""}</label>`,
      )
      .join("") || '<p class="hint">此 Episode 尚无语言/VQA 记录。</p>';
  $("manual-atoms")
    .querySelectorAll<HTMLTextAreaElement>("[data-atom-index]")
    .forEach(
      (el) =>
        (el.oninput = () =>
          edit("manual-observation", (d) => {
            d.manualAtoms![Number(el.dataset.atomIndex)].content = el.value;
          })),
    );
  value("vqa-question").value = s.input.vqaQuestion || "";
  value("vqa-answer").value = s.input.vqaAnswer || "";
}
function renderFrameFields() {
  if (!active) return;
  const s = active,
    n = s.doc.frames.find((f) => f.frame === frame);
  $("frame-fields").innerHTML =
    '<div class="field-grid">' +
    s.doc.templateSnapshot.fields
      .filter((f) => f.scope === "frame")
      .map((f) => fieldControl(f, n?.values[f.id], String(frame)))
      .join("") +
    "</div>";
  bindFields(
    $("frame-fields"),
    "frame",
    () => n?.values || {},
    (d, id, v) => {
      let row = d.frames.find((f) => f.frame === frame);
      if (!row) {
        row = { id: uid(), frame, values: {} };
        d.frames.push(row);
      }
      row.values[id] = v;
    },
  );
}
function modal(html: string) {
  $("dialog-body").innerHTML = html;
  const d = $("dialog") as HTMLDialogElement;
  if (!d.open) d.showModal();
}
function closeModal() {
  ($("dialog") as HTMLDialogElement).close();
}
function conflictDialog() {
  const s = current();
  if (!s.remote) return;
  modal(
    `<h2>对照冲突内容</h2><p>本地基于修订 ${s.head.revision}，服务器现为修订 ${s.remote.revision}。两份都可先下载保留；来源版本不同不允许覆盖。</p><div class="diff"><div><h3>本地修改</h3><pre>${esc(JSON.stringify(s.doc, null, 2))}</pre></div><div><h3>服务器内容</h3><pre>${esc(JSON.stringify(s.remote.document, null, 2))}</pre></div></div><div class="row"><button id="download-conflict">下载双方备份</button><button id="use-remote">归档本地，使用服务器</button><button id="use-local" class="primary">核对后，以本地内容提交新修订</button></div>`,
  );
  btn("download-conflict", () =>
    download(
      { local: s.doc, remote: s.remote },
      `episode-${episode}-conflict.json`,
    ),
  );
  btn("use-remote", () => {
    s.undo.edit(s.doc, s.remote!.document);
    s.head = s.remote!;
    s.doc = copy(s.head.document);
    s.remote = undefined;
    s.dirty = false;
    s.error = "";
    backup(s);
    closeModal();
    renderAll(false);
  });
  btn("use-local", async () => {
    if (s.head.source_signature !== s.remote!.source_signature)
      throw Error("来源不同，请保留旧草稿并单独核对，不能套到新来源");
    s.head = copy(s.remote!);
    s.remote = undefined;
    s.dirty = true;
    s.error = "";
    backup(s);
    await sync(s);
    closeModal();
    renderAll(false);
  });
}
function wholeLabelDialog(a: Attempt) {
  const s = current();
  modal(
    `<h2>修改整次尝试标签</h2><p>会修改同一尝试关联的全部正式片段：${s.doc.segments
      .filter((x) => x.attemptId === a.id)
      .map((x) => `${x.start}–${x.end - 1}`)
      .join(
        "、",
      )}；特殊状态仍保持独立。</p><select id="whole-step">${s.doc.templateSnapshot.subtasks.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}</select><button id="apply-whole">明确应用整次变更</button>`,
  );
  btn("apply-whole", () => {
    change(
      relabelAttempt(s.doc, s.head.frames, a.id, value("whole-step").value),
      "relabel-whole-attempt",
    );
    closeModal();
    renderAll(false);
  });
}
function mergeAttemptDialog(a: Attempt) {
  const s = current(),
    others = s.doc.attempts.filter(
      (x) => !x.archived && x.id !== a.id && x.subtaskId === a.subtaskId,
    );
  modal(
    `<h2>明确合并尝试</h2><p>只有人工确认实际是同一次执行时使用。原值完整留在历史，不自动选择成功或最高分。</p><label>另一次尝试<select id="merge-other">${others.map((x) => `<option value="${esc(x.id)}">${esc(x.id)} · ${x.outcome ? outcomes[x.outcome] : "结果暂缺"}</option>`).join("")}</select></label><div id="merge-values"></div><button id="apply-merge-attempt" class="primary">确认同一次执行并采用下方逐字段选择</button>`,
  );
  const render = () => {
    const b = s.doc.attempts.find((x) => x.id === value("merge-other").value);
    if (!b) {
      $("merge-values").innerHTML = "没有同标签的其他尝试";
      return;
    }
    const keys = new Set([...Object.keys(a.values), ...Object.keys(b.values)]);
    $("merge-values").innerHTML =
      `<label>最终执行结果<select id="merge-outcome"><option value="">暂缺</option>${Object.entries(
        outcomes,
      )
        .map(
          ([v, l]) =>
            `<option value="${v}" ${a.outcome === v ? "selected" : ""}>${l}</option>`,
        )
        .join(
          "",
        )}</select></label><p>当前结果：${a.outcome ? outcomes[a.outcome] : "暂缺"}；另次：${b.outcome ? outcomes[b.outcome] : "暂缺"}</p>` +
      [...keys]
        .map(
          (k) =>
            `<label>${esc(k)}<select data-resolve="${esc(k)}"><option value="a">保留当前：${esc(JSON.stringify(a.values[k] ?? null))}</option><option value="b">采用另次：${esc(JSON.stringify(b.values[k] ?? null))}</option><option value="null">清空</option></select></label>`,
        )
        .join("") +
      `<label>最终普通备注<textarea id="merge-note">${esc(a.note || "")}</textarea></label>`;
  };
  value("merge-other").onchange = render;
  render();
  btn("apply-merge-attempt", () => {
    const b = s.doc.attempts.find((x) => x.id === value("merge-other").value);
    if (!b) throw Error("没有可合并的尝试");
    const values: Record<string, Value> = {};
    $("merge-values")
      .querySelectorAll<HTMLSelectElement>("[data-resolve]")
      .forEach((el) => {
        const k = el.dataset.resolve!;
        values[k] =
          el.value === "a"
            ? (a.values[k] ?? null)
            : el.value === "b"
              ? (b.values[k] ?? null)
              : null;
      });
    change(
      mergeAttempts(s.doc, s.head.frames, a.id, b.id, {
        outcome: (value("merge-outcome").value ||
          undefined) as Attempt["outcome"],
        values,
        note: value("merge-note").value,
      }),
      "merge-attempts",
    );
    closeModal();
    renderAll(false);
  });
}
function mergeSegmentDialog(seg: Segment) {
  const s = current(),
    other = s.doc.segments.find((x) => x.start === seg.end);
  if (!other) throw Error("后方没有首尾贴合的片段");
  try {
    change(
      mergeSegments(s.doc, s.head.frames, seg.id, other.id),
      "merge-segments",
    );
    renderEditor();
  } catch (e: any) {
    if (seg.attemptId !== other.attemptId || seg.label !== other.label) throw e;
    modal(
      `<h2>合并片段局部信息</h2><p>两段属于同次尝试。请选择合并后的局部信息，原记录保留历史。</p><pre>${esc(JSON.stringify({ left: seg, right: other }, null, 2))}</pre><label>合并后局部备注<textarea id="merged-note">${esc([seg.note, other.note].filter(Boolean).join("\n"))}</textarea></label><label>合并后活动描述<textarea id="merged-description">${esc(seg.description || "")}</textarea></label><label>合并后局部字段 JSON<textarea id="merged-fields">${esc(JSON.stringify(seg.values || {}, null, 2))}</textarea></label><button id="apply-merge-segment">采用这些局部信息</button>`,
    );
    btn("apply-merge-segment", () => {
      change(
        mergeSegments(s.doc, s.head.frames, seg.id, other.id, {
          note: value("merged-note").value,
          values: JSON.parse(value("merged-fields").value),
          description: value("merged-description").value,
        }),
        "merge-segments",
      );
      closeModal();
      renderEditor();
    });
  }
}
async function boundaryDialog(seg: Segment) {
  const s = current();
  pause();
  const first = Math.max(0, seg.start - 1),
    next = seg.start;
  modal(
    `<h2>边界前后实际画面</h2><p>前一段末帧 ${first} 与当前段首帧 ${next}。下方为来源数据帧的服务端解码画面。</p><div class="diff">${[first, next].map((f) => `<div><h3>数据帧 ${f}</h3>${(s.media?.cameras || []).map((c) => `<img style="width:100%" src="${apiBase}${dataset}/episodes/${episode}/frame/${f}?camera=${encodeURIComponent(c.key)}&signature=${s.head.source_signature}" alt="${esc(c.key)} 数据帧 ${f}">`).join("")}</div>`).join("")}</div><p>${esc(s.doc.templateSnapshot.subtasks.find((t) => t.id === seg.subtaskId)?.startCondition || "按模板与证据人工判断，不使用模型置信度替代确认。")}</p><pre>${esc(JSON.stringify(seg.evidence || [], null, 2))}</pre>`,
  );
}
function download(data: unknown, name: string) {
  const link = document.createElement("a"),
    url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
    );
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function loadLibrary() {
  const r = await fetch("/api/task-templates");
  if (!r.ok) throw Error("共享模板服务不可用，当前快照不变");
  library = await r.json();
  return library;
}
async function templateDialog() {
  await loadLibrary();
  modal(
    `<h2>共享任务模板</h2><p>历史标注继续绑定实际快照。保存共享库不会自动升级当前 Episode。</p><div class="row"><select id="library-choice">${library.templates.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}</select><button id="edit-library">编辑所选模板</button><button id="new-template">新建模板</button><button id="upgrade-library" class="primary">查看差异并升级当前 Episode</button></div><div id="template-form"></div>`,
  );
  btn("edit-library", () =>
    editTemplate(
      library.templates.find((t) => t.id === value("library-choice").value) ||
        current().doc.templateSnapshot,
    ),
  );
  btn("new-template", () =>
    editTemplate({ id: uid(), name: "新任务模板", subtasks: [], fields: [] }),
  );
  btn("upgrade-library", () => {
    const t = library.templates.find(
      (t) => t.id === value("library-choice").value,
    );
    if (!t) throw Error("请选择共享模板");
    upgradeDialog(t);
  });
}
function editTemplate(initial: Template) {
  let t = copy(initial);
  const render = () => {
    const host = $("template-form");
    host.innerHTML = `<h3>模板定义（稳定 ID 保持不变）</h3><label>名称<input id="template-name" value="${esc(t.name)}"></label><div id="template-steps">${t.subtasks.map((s, i) => `<details class="template-step" data-step="${i}"><summary>${esc(s.name || "新标签")} · ${esc(s.id)}</summary>${["name", "definition", "includes", "excludes", "startCondition", "endCondition"].map((key, j) => `<label>${["名称", "业务定义", "包含动作", "排除动作", "可观察开始判据", "可观察结束判据"][j]}<textarea rows="2" data-property="${key}">${esc((s as any)[key] || "")}</textarea></label>`).join("")}<button data-remove-step="${i}">删除此标签（旧快照仍保留）</button></details>`).join("")}</div><button id="add-step">添加正式标签</button><h3>自定义字段与评分尺度</h3><div id="template-fields">${t.fields.map((f, i) => `<details class="template-field" data-template-field="${i}"><summary>${esc(f.label)} · ${esc(f.id)}</summary><label>字段名称<input data-property="label" value="${esc(f.label)}"></label><div class="row"><label>归属<select data-property="scope">${["episode", "attempt", "segment", "frame"].map((v, j) => `<option value="${v}" ${f.scope === v ? "selected" : ""}>${["Episode", "整次尝试", "局部片段", "单帧观察"][j]}</option>`).join("")}</select></label><label>类型<select data-property="kind">${["rating", "number", "text", "textarea", "single", "multi", "bool"].map((v) => `<option value="${v}" ${f.kind === v ? "selected" : ""}>${v}</option>`).join("")}</select></label></div><label>含义说明<textarea data-property="description">${esc(f.description || "")}</textarea></label><label>评分尺度及各分值含义<textarea data-property="scale">${esc(f.scale || "")}</textarea></label><div class="row"><label>评分下限<input data-property="minimum" type="number" value="${f.minimum ?? ""}"></label><label>评分上限<input data-property="maximum" type="number" value="${f.maximum ?? ""}"></label><label>步长<input data-property="step" type="number" value="${f.step ?? ""}"></label></div><label>选项（每行一项）<textarea data-property="options">${esc((f.options || []).join("\n"))}</textarea></label><button data-remove-field="${i}">删除此字段</button></details>`).join("")}</div><button id="add-field">添加自定义字段</button><button id="save-template" class="primary">保存到共享库</button>`;
    const read = () => {
      t.name = value("template-name").value;
      host.querySelectorAll<HTMLElement>("[data-step]").forEach((el) =>
        el
          .querySelectorAll<HTMLTextAreaElement>("[data-property]")
          .forEach((input) => {
            (t.subtasks[Number(el.dataset.step)] as any)[
              input.dataset.property!
            ] = input.value || undefined;
          }),
      );
      host
        .querySelectorAll<HTMLElement>("[data-template-field]")
        .forEach((el) =>
          el
            .querySelectorAll<HTMLInputElement>("[data-property]")
            .forEach((input) => {
              const key = input.dataset.property!,
                f = t.fields[Number(el.dataset.templateField)];
              (f as any)[key] =
                key === "options"
                  ? input.value
                      .split("\n")
                      .map((s) => s.trim())
                      .filter(Boolean)
                  : ["minimum", "maximum", "step"].includes(key)
                    ? input.value === ""
                      ? undefined
                      : Number(input.value)
                    : input.value || undefined;
            }),
        );
      localStorage.setItem(`bp-template-edit:${dataset}`, JSON.stringify(t));
    };
    host
      .querySelectorAll("input,textarea,select")
      .forEach((el) => el.addEventListener("input", () => safe(read)));
    host.querySelectorAll<HTMLElement>("[data-remove-step]").forEach(
      (el) =>
        (el.onclick = () => {
          read();
          t.subtasks.splice(Number(el.dataset.removeStep), 1);
          render();
        }),
    );
    host.querySelectorAll<HTMLElement>("[data-remove-field]").forEach(
      (el) =>
        (el.onclick = () => {
          read();
          t.fields.splice(Number(el.dataset.removeField), 1);
          render();
        }),
    );
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
        maximum: 5,
      });
      render();
    });
    btn("save-template", async () => {
      read();
      const templates = [...library.templates.filter((x) => x.id !== t.id), t];
      const r = await fetch("/api/task-templates", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision: library.revision, templates }),
      });
      const result = await r.json();
      if (!r.ok)
        throw Error(
          typeof result.detail === "string"
            ? result.detail
            : JSON.stringify(result.detail),
        );
      library = result;
      localStorage.removeItem(`bp-template-edit:${dataset}`);
      note("共享模板已保存；当前 Episode 仍使用原快照。需要时显式升级。");
      await templateDialog();
    });
  };
  render();
}
function upgradeDialog(template: Template) {
  const s = current(),
    diff = templateDiff(s.doc.templateSnapshot, template),
    used = [
      ...new Set(
        s.doc.attempts
          .filter((a) => !a.archived && a.subtaskId)
          .map((a) => a.subtaskId!),
      ),
    ];
  modal(
    `<h2>显式解决模板升级</h2><p>对照名称、含义、边界判据及字段。相同 ID 也不代表含义相同；每个已用标签都要明确选择对应。字段定义变化的当前值会归档并清空，原值完整留在历史。</p><pre>${esc(JSON.stringify(diff, null, 2))}</pre>${used.map((id) => `<label>${esc(s.doc.templateSnapshot.subtasks.find((t) => t.id === id)?.name || id)} (${esc(id)})<select data-map="${esc(id)}"><option value="">尚未解决，继续旧快照</option>${template.subtasks.map((t) => `<option value="${esc(t.id)}">明确映射为 ${esc(t.name)} (${esc(t.id)})</option>`).join("")}</select></label>`).join("")}<button id="apply-upgrade" class="primary">采用此快照及明确映射</button>`,
  );
  btn("apply-upgrade", () => {
    const mapping: Record<string, string> = {};
    $("dialog-body")
      .querySelectorAll<HTMLSelectElement>("[data-map]")
      .forEach((el) => {
        if (el.value) mapping[el.dataset.map!] = el.value;
      });
    const next = upgrade(s.doc, s.head.frames, template, mapping);
    const changed = s.doc.templateSnapshot.fields
      .filter(
        (old) =>
          JSON.stringify(old) !==
          JSON.stringify(template.fields.find((f) => f.id === old.id)),
      )
      .map((f) => f.id);
    for (const id of changed) {
      delete next.values[id];
      next.attempts.forEach((a) => delete a.values[id]);
      next.segments.forEach((x) => {
        if (x.values) delete x.values[id];
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
  return [...selection.entries]
    .sort((a, b) => a[0] - b[0])
    .map(([ep, entry]) => ({
      ep,
      entry,
      row: allRows.find((r) => r.episode === ep),
    }));
}
function deliveryDialog() {
  modal(
    `<h2>交付清单 · ${selection.entries.size} 个完整 Episode</h2><p>同一份明确清单用于标注包或独立数据集。包含失败、调整、其他标签、特殊状态和空白。草稿也能交付。</p><p>来源：${esc(value("dataset").selectedOptions?.[0]?.textContent || dataset)} · 来源版本 ${esc(signature.slice(0, 12))}</p><table class="dialog-table"><thead><tr><th>Episode</th><th>加入方式 / 次数</th><th>所选修订</th><th>审核状态</th><th></th></tr></thead><tbody>${selectionRows()
      .map(
        ({ ep, entry, row }) =>
          `<tr><td>Episode ${ep}</td><td>${esc(entry.origin === "filter" ? "筛选" : "人工")} · ${esc((entry.counts as any)?.recorded_count ?? "未统计")}</td><td>${entry.revision}${row && row.revision !== entry.revision ? ' <span class="error">已变化，需更新</span>' : ""}</td><td>${row?.complete ? "已完成" : "草稿 / 待核"}${row?.status?.gaps?.length ? " · 有空白" : ""}</td><td><button data-remove-selection="${ep}">移除</button></td></tr>`,
      )
      .join(
        "",
      )}</tbody></table><div class="row"><button id="refresh-selection">核对后更新清单至已保存修订</button><button id="export-bundle" class="primary">下载轻量 JSON 标注包</button><button id="export-subset">排队生成独立数据集子集</button><button id="batch-confirm">预览并批量确认正常 Episode</button></div><div id="export-state"></div><hr><label>重新读取标注包（作为独立候选，不覆盖草稿）<input id="import-bundle" type="file" accept=".json,application/json"></label>`,
  );
  $("dialog-body")
    .querySelectorAll<HTMLElement>("[data-remove-selection]")
    .forEach(
      (el) =>
        (el.onclick = () => {
          selection.remove(Number(el.dataset.removeSelection));
          persistSelection();
          renderSelectionCount();
          renderEpisodes();
          deliveryDialog();
        }),
    );
  btn("refresh-selection", async () => {
    for (const [ep] of selection.entries) {
      const s = sessions.get(ep);
      if (s) {
        await sync(s);
        if (s.dirty || s.remote)
          throw Error(`Episode ${ep} 尚未同步，请先处理`);
      }
    }
    const result = await request(`${dataset}/episodes`);
    allRows = result.episodes;
    signature = result.source_signature;
    for (const [ep, entry] of selection.entries) {
      const r = allRows.find((r) => r.episode === ep);
      if (!r) throw Error(`Episode ${ep} 已失效，请移除`);
      selection.entries.set(ep, { ...entry, revision: r.revision });
    }
    persistSelection();
    deliveryDialog();
  });
  const payload = () => {
    for (const [ep] of selection.entries) {
      const s = sessions.get(ep);
      if (s?.dirty || s?.remote)
        throw Error(`Episode ${ep} 尚未同步，请先更新清单`);
    }
    return selection.payload(signature, filterState);
  };
  btn("export-bundle", async () => {
    const bundle = await request(`${dataset}/bundle`, payload());
    download(bundle, `bodypipeline-${dataset}-annotations.json`);
    $("export-state").textContent =
      "标注包已生成，包含清单内完整 Episode 及真实草稿状态。";
  });
  btn("export-subset", async () => {
    const frozen = copy(payload());
    const job = await request(`${dataset}/exports`, frozen);
    const ds = dataset;
    $("export-state").textContent =
      `${job.stage}。选择、来源与标注修订已冻结。`;
    void (async () => {
      while (true) {
        await new Promise((r) => setTimeout(r, 1500));
        try {
          const result = await request(`${ds}/exports/${job.id}`);
          if ($("export-state"))
            $("export-state").innerHTML =
              `${esc(result.stage)}${result.error ? ` · ${esc(result.error)}` : ""}${result.state === "CONVERTED" ? ` · <a href="/workbench?dataset=${encodeURIComponent(job.id)}">打开独立子集</a>` : ""}`;
          if (!["QUEUED", "RUNNING"].includes(result.state)) break;
        } catch (e: any) {
          note(e.message);
          break;
        }
      }
    })();
  });
  btn("batch-confirm", () => {
    const proposed = payload();
    modal(
      `<h2>批量确认预览</h2><p>只确认已有标注并声明正常 Episode 完成。后端检查全覆盖、结果、模板外描述及未解决风险；有异常的 Episode 请单独检查。</p><pre>${esc(
        JSON.stringify(
          selectionRows().map(({ ep, row }) => ({
            episode: ep,
            status: row?.status,
            complete: row?.complete,
          })),
          null,
          2,
        ),
      )}</pre><button id="apply-batch" class="primary">我已核对，确认这些正常 Episode</button>`,
    );
    btn("apply-batch", async () => {
      await request(`${dataset}/confirm`, proposed);
      for (const ep of proposed.episodes) sessions.delete(ep);
      await refresh(false);
      await openEpisode(episode);
      note("已确认所选正常 Episode；保存历史保留原状态。");
      closeModal();
    });
  });
  value("import-bundle").onchange = () =>
    safe(async () => {
      const file = (value("import-bundle") as HTMLInputElement).files?.[0];
      if (!file) return;
      const result = await request(`${dataset}/import`, {
        bundle: JSON.parse(await file.text()),
      });
      note(`已读取 ${result.candidates.length} 个独立候选，当前草稿保留。`);
      closeModal();
      await candidateDialog();
    });
}
async function candidateDialog() {
  const s = current(),
    result = await request(`${dataset}/episodes/${episode}/candidates`),
    items = result.candidates;
  modal(
    `<h2>AI 与导入的独立候选</h2><p>先对照，再明确采用范围。不同模板快照先显式升级或映射；当前草稿和候选都会保留。</p><select id="candidate-choice">${items.map((c: any) => `<option value="${esc(c.id)}">${esc(c.run)} · 基于修订 ${c.base_revision} · ${new Date(c.created_at * 1000).toLocaleString()}</option>`).join("")}</select><div id="candidate-detail"></div>`,
  );
  const detail = () => {
    const c = items.find((x: any) => x.id === value("candidate-choice").value);
    if (!c) {
      $("candidate-detail").innerHTML =
        '<div class="empty">尚无候选。自动标注完成后可在任务中发送到工作区；标注包也可以重新读取为候选。</div>';
      return;
    }
    $("candidate-detail").innerHTML =
      `<p>候选模板：${esc(c.document.templateSnapshot.name)} · ${esc(c.template_signature.slice(0, 12))}<br>应用所选范围 ${s.input.first}–${s.input.last}。会替换范围内片段和帧观察；范围外片段保留，Episode 字段保持当前值。来自同一候选的同一次尝试共享身份与结果。</p><details><summary>对照候选内容与必要证据</summary><pre>${esc(JSON.stringify(c.document, null, 2))}</pre></details><label><input id="candidate-ack" type="checkbox"> 我已对照当前修订 ${s.head.revision} 与候选基线 ${c.base_revision}</label><label><input id="candidate-keep" type="checkbox"> 若已采用的同一尝试有人工改动，明确保留当前共享字段</label><div class="row"><button id="candidate-upgrade">先解决候选模板差异</button><button id="apply-candidate" class="primary">明确采用所选范围</button></div>`;
    btn("candidate-upgrade", () => upgradeDialog(c.document.templateSnapshot));
    btn("apply-candidate", async () => {
      await sync(s);
      if (s.dirty || s.remote) throw Error("请先处理当前草稿同步");
      const r = ranges(s),
        before = copy(s.doc);
      const response = await request(`${dataset}/episodes/${episode}/apply`, {
        revision: s.head.revision,
        source_signature: s.head.source_signature,
        candidate: c.id,
        start: r.start,
        end: r.end,
        acknowledge_current_revision: value("candidate-ack").checked,
        shared_attempt_policy: value("candidate-keep").checked
          ? "keep-existing"
          : "require-match",
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
  modal(
    `<h2>Episode 保存历史</h2><p>每次保存保留内容及核对状态。撤销/重做恢复的审核戳只能来自这些可信历史。</p>${result.history.map((h: any) => `<details><summary>修订 ${h.revision} · ${esc(h.operation || "来源初始快照")} · ${h.complete ? "已完成" : "草稿"}</summary><pre>${esc(JSON.stringify(h.document, null, 2))}</pre></details>`).join("")}`,
  );
}
for (const [id, key] of [
  ["vqa-question", "vqaQuestion"],
  ["vqa-answer", "vqaAnswer"],
])
  value(id).oninput = () => {
    current().input[key] = value(id).value;
    backup(current());
  };
btn("add-vqa", () => {
  const question = value("vqa-question").value.trim(),
    answer = value("vqa-answer").value.trim();
  if (!question && !answer) throw Error("请填写问题或观察");
  edit("add-vqa", (d) => {
    d.manualAtoms ??= [];
    if (question)
      d.manualAtoms.push({
        role: "user",
        style: "vqa",
        content: question,
        timestamp: frame / current().head.fps,
        camera: null,
        tool_calls: null,
      });
    if (answer)
      d.manualAtoms.push({
        role: "assistant",
        style: "vqa",
        content: answer,
        timestamp: frame / current().head.fps,
        camera: null,
        tool_calls: null,
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
btn("play", () => (playing ? pause() : play()));
value("frame").onchange = () => {
  pause();
  safe(() => seek(Number(value("frame").value)));
};
value("scrub").oninput = () => {
  pause();
  safe(() => seek(Number(value("scrub").value)));
};
value("scrub").onchange = () => safe(() => seek(Number(value("scrub").value)));
function setTool(next: typeof tool) {
  tool = next;
  for (const [id, v] of [
    ["browse", "browse"],
    ["range-tool", "range"],
    ["move-tool", "move"],
  ])
    $(id).classList.toggle("active", v === next);
  $("tool-hint").textContent =
    next === "range"
      ? "在主轨框选范围，然后明确贴标签或重标。框选与标记起止使用同一种记录。"
      : next === "move"
        ? "明确移动模式：拖片段正文整体移动；不会填补空白，重叠会被拒绝。"
        : "标尺浏览画面 · 片段正文只选中 · 两侧把手调界 · 首末帧均包含";
}
btn("browse", () => setTool("browse"));
btn("range-tool", () => setTool("range"));
btn("move-tool", () => setTool("move"));
function zoom(factor: number) {
  const s = current(),
    size = Math.max(
      5,
      Math.min(s.head.frames, Math.round((zoomEnd - zoomStart) * factor)),
    );
  zoomStart = Math.max(
    0,
    Math.min(s.head.frames - size, frame - Math.round(size / 2)),
  );
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
  zoomStart = Math.min(
    current().head.frames - width,
    zoomStart + Math.round(width * 0.8),
  );
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
  value("range-first").value = String(frame);
  rememberInput();
  renderTimeline();
});
btn("mark-end", () => {
  value("range-last").value = String(frame);
  rememberInput();
  renderTimeline();
});
btn("continue-range", () => {
  const s = current(),
    r = ranges(s);
  if (r.end >= s.head.frames) throw Error("已经到最后一帧");
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
  "new-outcome",
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
btn("create-confirm", () => createSegment(false, true));
btn("replace-range", () => createSegment(true));
btn("unselect", () => {
  current().input.selected = "";
  renderEditor();
  renderTimeline();
});
btn("confirm-range", () => {
  const r = ranges();
  return direct({ confirm: [[r.start, r.end]] });
});
btn("complete", () => direct({ complete: true }));
btn("count-evidence", () => {
  const s = current();
  modal(
    `<h2>目标 subtask 的计数依据</h2><p>与“完成”分别记录。明确证据可以处理遮挡或其他数据来源；后续内容变化会使此依据失效。不确定时仍可人工加入交付清单。</p><label>目标标签<select id="evidence-step">${s.doc.templateSnapshot.subtasks.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}</select></label><label>人工核查依据<textarea id="evidence-reason" rows="4"></textarea></label><button id="apply-count-evidence">记录此内容修订的计数依据</button>`,
  );
  btn("apply-count-evidence", async () => {
    await direct({
      count_evidence: {
        [value("evidence-step").value]: value("evidence-reason").value,
      },
    });
    closeModal();
    note("计数依据已绑定当前内容；可以重新运行恰好次数筛选。");
  });
});
btn("history", historyDialog);
btn("candidates", candidateDialog);
btn("templates", templateDialog);
btn("delivery", deliveryDialog);
btn("refresh", () => refresh(false));
btn("filter", async () => {
  await sync(current());
  filterState = {
    subtask: value("filter-step").value,
    count: value("filter-count").value,
    basis: value("filter-basis").value,
    template_signature: current().head.template_signature,
  };
  return refresh();
});
btn("clear-filter", () => {
  filterState = {};
  value("filter-step").value = "";
  value("filter-count").value = "";
  return refresh();
});
btn("prev-page", () => {
  page = Math.max(0, page - 1);
  renderEpisodes();
});
btn("next-page", () => {
  page = Math.min(Math.max(0, Math.ceil(rows.length / 10) - 1), page + 1);
  renderEpisodes();
});
function selectRows(items: any[]) {
  items.forEach((r) =>
    selection.add(
      r.episode,
      r.revision,
      filterState.subtask ? "filter" : "manual",
      r.counts,
    ),
  );
  persistSelection();
  renderSelectionCount();
  renderEpisodes();
}
btn("select-page", () => selectRows(rows.slice(page * 10, (page + 1) * 10)));
btn("select-results", () => selectRows(rows));
value("dataset").onchange = () =>
  safe(() => openDataset(value("dataset").value));
window.addEventListener("keydown", (e) => {
  if (
    !active ||
    editableTarget(e.target) ||
    ($("dialog") as HTMLDialogElement).open
  )
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
  } else if (e.key === " ") {
    e.preventDefault();
    playing ? pause() : play();
  }
});
window.addEventListener("beforeunload", (e) => {
  sessions.forEach(backup);
  if ([...sessions.values()].some((s) => s.dirty || s.localError)) {
    e.preventDefault();
    e.returnValue = "";
  }
});
window.addEventListener("online", () => {
  for (const s of sessions.values()) s.retryAt = 0;
  schedule();
});
window.addEventListener("offline", () =>
  note("网络离线：继续编辑，草稿在本机保留；未缓存的画面不可伪装为已定位。"),
);
setInterval(() => {
  for (const s of sessions.values())
    if (s.dirty && !s.remote && !s.saving && Date.now() >= s.retryAt)
      safe(() => sync(s));
}, 2000);
async function init() {
  try {
    const result = await request("datasets");
    value("dataset").innerHTML = result.datasets
      .map((d: any) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`)
      .join("");
    if (!dataset) dataset = result.datasets[0]?.id || "";
    value("dataset").value = dataset;
    if (dataset) await openDataset(dataset);
    else note("请先在 Server 登记一个可读取的 LeRobot 数据集。");
  } catch (e: any) {
    note(e.message);
    if (dataset) {
      value("dataset").innerHTML =
        `<option value="${esc(dataset)}">${esc(dataset)}（本地恢复）</option>`;
      await openDataset(dataset);
    }
  }
}
safe(init);
