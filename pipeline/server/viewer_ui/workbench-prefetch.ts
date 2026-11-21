import type { Head } from "./workbench-model";
import { seekTime, type Media } from "./workbench-media";

export interface PrefetchTarget {
  dataset: string;
  episode: number;
  signature: string;
}
export interface PreparedEpisode {
  head: Head;
  etag?: string;
  media: Media;
  videos: Map<string, HTMLVideoElement>;
}
type Slot = PrefetchTarget & {
  abort: AbortController;
  data?: PreparedEpisode;
  ready: Promise<PreparedEpisode | undefined>;
  warmed?: boolean;
};
export const mediaUrl = (dataset: string, signature: string, path: string) =>
  `/api/annotation-workbench/${encodeURIComponent(dataset)}/media/${path.split("/").map(encodeURIComponent).join("/")}?signature=${encodeURIComponent(signature)}`;
export const releaseVideos = (videos: Map<string, HTMLVideoElement>) => {
  videos.forEach((video) => {
    video.pause();
    video.removeAttribute("src");
    video.load();
  });
  videos.clear();
};

/** One look-ahead slot survives a Viewer route remount, never an unbounded list. */
export class EpisodePreloader {
  private slot?: Slot;
  private expiry?: ReturnType<typeof setTimeout>;
  constructor(
    private load: (
      target: PrefetchTarget,
      signal: AbortSignal,
    ) => Promise<PreparedEpisode>,
    private warm: (
      data: PreparedEpisode,
      signal: AbortSignal,
    ) => Promise<boolean>,
    private lifetime = 20000,
  ) {}
  matches(target: PrefetchTarget) {
    const s = this.slot;
    return (
      !!s &&
      s.dataset === target.dataset &&
      s.episode === target.episode &&
      s.signature === target.signature
    );
  }
  isReady(target: PrefetchTarget) {
    return this.matches(target) && this.slot?.warmed === true;
  }
  prepare(target: PrefetchTarget, status: (ready: boolean | null) => void) {
    if (this.matches(target)) return;
    this.clear();
    const slot: Slot = {
      ...target,
      abort: new AbortController(),
      ready: Promise.resolve(undefined),
    };
    this.slot = slot;
    slot.ready = this.load(target, slot.abort.signal)
      .then((data) => {
        if (this.slot !== slot || slot.abort.signal.aborted) {
          releaseVideos(data.videos);
          return undefined;
        }
        slot.data = data;
        if (
          data.head.source_signature !== target.signature ||
          data.media.source_signature !== target.signature
        )
          throw Error("来源已变化");
        void this.warm(data, slot.abort.signal)
          .then((ready) => {
            if (this.slot === slot && !slot.abort.signal.aborted) {
              slot.warmed = ready;
              status(ready || null);
            }
          })
          .catch(() => {
            if (this.slot === slot && !slot.abort.signal.aborted) status(null);
          });
        return data;
      })
      .catch(() => {
        if (this.slot === slot) {
          this.clear();
          status(null);
        }
        return undefined;
      });
    status(false);
  }
  async take(target: PrefetchTarget) {
    if (!this.matches(target)) return undefined;
    clearTimeout(this.expiry);
    const slot = this.slot!;
    const data = await slot.ready;
    if (this.slot !== slot) return undefined;
    this.slot = undefined;
    // Stop background warming; the adopted video nodes keep their real buffers.
    slot.abort.abort();
    return data;
  }
  retainFor(target: PrefetchTarget) {
    if (!this.matches(target)) this.clear();
    else clearTimeout(this.expiry);
  }
  leave() {
    clearTimeout(this.expiry);
    this.expiry = setTimeout(() => this.clear(), this.lifetime);
  }
  clear() {
    clearTimeout(this.expiry);
    const slot = this.slot;
    this.slot = undefined;
    slot?.abort.abort();
    if (slot?.data) releaseVideos(slot.data.videos);
  }
}

async function read(url: string, signal: AbortSignal) {
  const response = await fetch(url, {
    signal,
    cache: "no-store",
    priority: "low",
  });
  if (!response.ok) throw Error("预加载暂不可用");
  return {
    value: await response.json(),
    etag: response.headers.get("etag") || undefined,
  };
}
async function load(
  target: PrefetchTarget,
  signal: AbortSignal,
): Promise<PreparedEpisode> {
  const base = `/api/annotation-workbench/${encodeURIComponent(target.dataset)}/episodes/${target.episode}`;
  const [head, media] = await Promise.all([
    read(base + "?prefetch=true", signal),
    read(base + "/media", signal),
  ]);
  return {
    head: head.value,
    etag: head.etag,
    media: media.value,
    videos: new Map(),
  };
}
async function warm(data: PreparedEpisode, signal: AbortSignal) {
  // Metadata preload seeks to the Episode's actual offset inside shared videos.
  // Warm cameras serially; do not download an entire long video into a Blob.
  for (let index = 0; index < data.media.cameras.length; index++) {
    if (signal.aborted) return false;
    const camera = data.media.cameras[index];
    const video = document.createElement("video");
    video.preload = "metadata";
    video.muted = true;
    video.playsInline = true;
    video.dataset.prefetched = "true";
    data.videos.set(camera.key, video);
    await new Promise<void>((resolve) => {
      let sought = false;
      const finish = () => {
        clearTimeout(timer);
        for (const event of ["loadedmetadata", "loadeddata", "seeked", "error"])
          video.removeEventListener(event, ready);
        signal.removeEventListener("abort", finish);
        resolve();
      };
      const failed = () => {
        finish();
        // A failed/background timeout must be retryable on the real open.
        video.removeAttribute("src");
        video.load();
      };
      const ready = () => {
        if (video.error) return failed();
        if (video.readyState >= 1 && !sought) {
          sought = true;
          video.currentTime = seekTime(data.media, index, 0);
        }
        if (sought && !video.seeking && video.readyState >= 2) finish();
      };
      const timer = setTimeout(failed, 6000);
      signal.addEventListener("abort", finish, { once: true });
      for (const event of ["loadedmetadata", "loadeddata", "seeked", "error"])
        video.addEventListener(event, ready);
      video.src = mediaUrl(
        data.head.dataset,
        data.head.source_signature,
        camera.path,
      );
      video.load();
    });
  }
  return data.media.cameras.every((camera) => {
    const video = data.videos.get(camera.key);
    return video && !video.error && !video.seeking && video.readyState >= 2;
  });
}
export const episodePreloader = new EpisodePreloader(load, warm);
