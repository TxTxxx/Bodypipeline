/** Video presentation clock. Browsing never waits for a server-decoded image. */
export interface Media {
  frames: number;
  fps: number;
  source_signature?: string;
  timestamps: number[];
  duration: number;
  cameras: { key: string; path: string; start: number; end: number }[];
}

export function nearestFrame(times: number[], time: number): number {
  let lo = 0, hi = times.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (times[mid] < time) lo = mid + 1;
    else hi = mid;
  }
  return lo && time - times[lo - 1] <= times[lo] - time ? lo - 1 : lo;
}

export function presentedFrame(media: Media, camera: number, pts: number): number | null {
  const c = media.cameras[camera], time = pts - c.start;
  if (!Number.isFinite(pts) || pts < c.start - 1e-6 || pts >= c.end - 1e-6) return null;
  const n = nearestFrame(media.timestamps, time);
  return Math.abs(media.timestamps[n] - time) <= 0.51 / media.fps ? n : null;
}

export function seekTime(media: Media, camera: number, frame: number): number {
  const c = media.cameras[camera], start = c.start + media.timestamps[frame];
  // Seek inside the frame's display interval, not on a floating point boundary.
  const width = frame + 1 < media.frames
    ? media.timestamps[frame + 1] - media.timestamps[frame]
    : Math.min(1 / media.fps, c.end - start);
  return Math.min(c.end - 1e-6, start + width * 0.25);
}

/** A camera has one native seek in flight and one replaceable latest target. */
export class LatestSeek {
  target = 0;
  issued: number | null = null;
  completed: number | null = null;
  request(frame: number) { this.target = frame; }
  take(): number | null {
    if (this.issued !== null || this.completed === this.target) return null;
    return this.issued = this.target;
  }
  finish() { this.completed = this.issued; this.issued = null; }
}

interface Camera {
  host: HTMLElement;
  video: HTMLVideoElement;
  image: HTMLImageElement;
  queue: LatestSeek;
  shown: number | null;
  pts?: number;
  callback?: number;
  native: boolean;
  clean: (() => void)[];
}
export interface ExactFrame { blob: Blob; pts: number; frame: number }
interface Options {
  media: Media;
  hosts: HTMLElement[];
  exact: (frame: number, signal: AbortSignal) => Promise<ExactFrame[]>;
  update: (frame: number, state: string, aligned: boolean, playing: boolean) => void;
  settled: () => void;
  error: (message: string) => void;
}

export class VideoTransport {
  readonly cameras: Camera[];
  alignedFrame: number | null = null;
  target = 0;
  playing = false;
  private disposed = false;
  private browsing = false;
  private generation = 0;
  private playIntent = 0;
  private pumpFrame = 0;
  private fallbackTimer?: ReturnType<typeof setTimeout>;
  private fallback?: AbortController;
  private fallbackFrame: number | null = null;
  private objects: string[] = [];
  private listeners: { frame: number; resolve: (ready: boolean) => void }[] = [];
  private published = -1;

  constructor(private options: Options) {
    this.cameras = options.hosts.map(host => {
      const video = host.querySelector("video")!;
      const c: Camera = {
        host, video, image: host.querySelector("img")!, queue: new LatestSeek(),
        shown: null, native: typeof video.requestVideoFrameCallback === "function", clean: [],
      };
      const listen = (event: string, fn: () => void) => {
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
        if (!this.disposed) this.options.error("浏览器无法读取此视频；暂停精确画面仍可尝试服务端解码。");
      });
      if (!c.native) listen("timeupdate", () => {
        if (this.playing) this.present(c, video.currentTime, false);
      });
      return c;
    });
    this.cameras.forEach(c => this.watch(c));
  }

  private watch(c: Camera) {
    if (!c.native || this.disposed) return;
    c.callback = c.video.requestVideoFrameCallback((_now, metadata) => {
      if (this.disposed) return;
      this.present(c, metadata.mediaTime, true);
      this.watch(c);
    });
  }

  private present(c: Camera, pts: number, exact: boolean) {
    if (!c.image.hidden) return; // The visible exact fallback owns this camera.
    const i = this.cameras.indexOf(c), media = this.options.media;
    c.pts = pts;
    c.shown = exact ? presentedFrame(media, i, pts) : null;
    c.host.dataset.mediaPts = String(pts);
    c.host.dataset.clock = exact ? "presented" : "preview";
    if (c.shown === null) delete c.host.dataset.displayedFrame;
    else c.host.dataset.displayedFrame = String(c.shown);
    c.host.querySelector(".frame-tag")!.textContent = c.shown === null
      ? "视频预览" : `DATA FRAME ${c.shown}`;
    if (this.playing && i === 0) {
      const time = pts - media.cameras[0].start;
      this.target = Math.max(0, Math.min(media.frames - 1, nearestFrame(media.timestamps, time)));
      if (time >= media.timestamps[media.frames - 1] - 1e-6) {
        this.pause(media.frames - 1);
        return;
      }
      // Compare running media clocks; do not chase each callback's one-frame
      // scheduling difference. Small drift is corrected without repeated seeks.
      this.cameras.slice(1).forEach((other, j) => {
        if (other.video.seeking) return;
        const delta = other.video.currentTime - media.cameras[j + 1].start
          - (c.video.currentTime - media.cameras[0].start);
        if (Math.abs(delta) > 0.12) {
          other.video.currentTime = Math.min(media.cameras[j + 1].end - 1e-6,
            media.cameras[j + 1].start + c.video.currentTime - media.cameras[0].start);
        } else other.video.playbackRate = Math.abs(delta) > 1 / media.fps
          ? (delta > 0 ? 0.96 : 1.04) : 1;
      });
    }
    this.publish();
  }

  private publish() {
    if (this.disposed) return;
    const aligned = !this.playing && !this.browsing && this.cameras.length > 0
      && this.cameras.every(c => c.shown === this.target &&
        (!c.video.seeking || c.host.dataset.clock === "decoded"));
    this.alignedFrame = aligned ? this.target : null;
    const shown = this.cameras.map(c => c.shown === null ? "—" : c.shown).join(" / ");
    this.options.update(this.target,
      aligned ? `已对齐数据帧 ${this.target} · ${this.cameras.length} 个相机`
        : this.playing ? `连续播放 · 实际画面 ${shown}`
          : `预览目标 ${this.target} · 已显示 ${shown}`, aligned, this.playing);
    for (const listener of this.listeners)
      if (listener.frame !== this.target || aligned) listener.resolve(aligned && listener.frame === this.target);
    this.listeners = this.listeners.filter(l => l.frame === this.target && !aligned);
    if (aligned) {
      clearTimeout(this.fallbackTimer);
      this.fallback?.abort();
      if (this.published !== this.target) {
        this.published = this.target;
        this.options.settled();
      }
    }
  }

  private schedulePump() {
    if (this.disposed || this.playing || this.pumpFrame) return;
    this.pumpFrame = requestAnimationFrame(() => {
      this.pumpFrame = 0;
      if (this.disposed || this.playing) return;
      this.cameras.forEach((c, i) => {
        c.queue.request(this.target);
        if (c.video.readyState < 1 || c.video.seeking || c.queue.issued !== null
          || (c.shown === this.target)) return;
        const next = c.queue.take();
        if (next !== null) c.video.currentTime = seekTime(this.options.media, i, next);
      });
      this.publish();
    });
  }

  seek(frame: number, preview = false) {
    if (this.disposed) return;
    const next = Math.max(0, Math.min(this.options.media.frames - 1, Math.round(frame)));
    const changed = this.target !== next || this.playing;
    if (changed) {
      this.playIntent++;
      this.generation++;
      this.fallback?.abort();
      this.published = -1;
      this.objects.forEach(URL.revokeObjectURL);
      this.objects = [];
      this.cameras.forEach(c => {
        if (!c.image.hidden) {
          c.shown = null;
          delete c.host.dataset.displayedFrame;
          delete c.host.dataset.mediaPts;
          c.host.dataset.clock = "preview";
          c.host.querySelector(".frame-tag")!.textContent = "视频预览";
        }
        c.image.hidden = true;
      });
    }
    this.target = next;
    this.browsing = preview;
    this.playing = false;
    this.cameras.forEach(c => {
      c.video.pause();
      c.video.playbackRate = 1;
      c.queue.request(next);
    });
    this.publish();
    this.schedulePump();
    clearTimeout(this.fallbackTimer);
    if (!preview && this.alignedFrame === null) {
      // Only compatibility / missing presentation evidence uses PNG. A drag
      // has no such timer; repeated input replaces the target before dispatch.
      this.fallbackTimer = setTimeout(() => void this.precise(),
        this.cameras.every(c => c.native) ? 700 : 80);
    }
  }

  private async precise() {
    if (this.disposed || this.playing || this.browsing || this.alignedFrame !== null) return;
    if (this.fallback && !this.fallback.signal.aborted && this.fallbackFrame === this.target) return;
    this.fallback?.abort();
    const abort = this.fallback = new AbortController(), generation = this.generation,
      target = this.target;
    this.fallbackFrame = target;
    let objects: string[] = [];
    try {
      const frames = await this.options.exact(target, abort.signal);
      if (abort.signal.aborted || this.disposed || generation !== this.generation) return;
      if (frames.length !== this.cameras.length || frames.some((f, i) =>
        f.frame !== target || presentedFrame(this.options.media, i, f.pts) !== target))
        throw Error("精确画面时间戳与数据帧不一致");
      objects = frames.map(f => URL.createObjectURL(f.blob));
      const images = await Promise.all(objects.map(async url => {
        const image = new Image(); image.src = url; await image.decode(); return image;
      }));
      if (abort.signal.aborted || this.disposed || generation !== this.generation) {
        objects.forEach(URL.revokeObjectURL); return;
      }
      this.objects.forEach(URL.revokeObjectURL);
      this.objects = objects;
      objects = [];
      this.cameras.forEach((c, i) => {
        // Mount the already-decoded image, rather than certifying a new src
        // assignment whose visible image might still be the previous frame.
        const image = images[i];
        image.alt = c.image.alt;
        image.dataset.frameImage = c.image.dataset.frameImage;
        c.image.replaceWith(image);
        c.image = image;
        c.shown = target;
        c.host.dataset.displayedFrame = String(target);
        c.host.dataset.mediaPts = String(frames[i].pts);
        c.host.dataset.clock = "decoded";
        c.host.querySelector(".frame-tag")!.textContent = `DATA FRAME ${target}`;
      });
      this.publish();
    } catch (e: any) {
      if (!abort.signal.aborted && !this.disposed && generation === this.generation)
        this.options.error(`精确画面暂不可用：${e.message}。目标帧未被标为已对齐。`);
    } finally {
      objects.forEach(URL.revokeObjectURL);
      if (this.fallback === abort) this.fallbackFrame = null;
    }
  }

  private ready(): Promise<boolean> {
    if (this.alignedFrame === this.target) return Promise.resolve(true);
    const target = this.target;
    return new Promise(resolve => {
      const listener = { frame: target, resolve: (ok: boolean) => { clearTimeout(timer); resolve(ok); } };
      const timer = setTimeout(() => {
        this.listeners = this.listeners.filter(l => l !== listener); resolve(false);
      }, 8000);
      this.listeners.push(listener);
    });
  }

  async play() {
    if (this.playing || this.disposed) return;
    this.seek(this.target);
    const intent = ++this.playIntent;
    if (!await this.ready() || intent !== this.playIntent || this.disposed) return;
    this.fallback?.abort();
    this.generation++;
    this.playing = true;
    this.browsing = false;
    this.cameras.forEach(c => {
      c.image.hidden = true;
      c.queue.completed = null; // Playback advances away from the last seek.
    });
    try {
      await Promise.all(this.cameras.map(c => c.video.play()));
      if (!this.disposed && intent === this.playIntent) this.publish();
    } catch (e: any) {
      if (this.disposed || intent !== this.playIntent) return;
      this.pause();
      this.options.error(e.message);
    }
  }

  pause(at?: number) {
    ++this.playIntent;
    if (!this.playing && at === undefined) return;
    const shown = this.cameras[0]?.shown;
    this.seek(at ?? shown ?? this.target);
  }

  dispose() {
    this.disposed = true;
    this.playIntent++;
    this.fallback?.abort();
    clearTimeout(this.fallbackTimer);
    cancelAnimationFrame(this.pumpFrame);
    this.listeners.forEach(l => l.resolve(false));
    this.listeners = [];
    this.objects.forEach(URL.revokeObjectURL);
    this.cameras.forEach(c => {
      c.clean.forEach(fn => fn());
      if (c.callback !== undefined) c.video.cancelVideoFrameCallback(c.callback);
      c.video.pause();
      c.video.removeAttribute("src");
      c.video.load();
    });
  }
}
