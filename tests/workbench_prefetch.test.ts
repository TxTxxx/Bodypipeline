import { test, expect } from "bun:test";
import {
  EpisodePreloader,
  mediaUrl,
  type PreparedEpisode,
  type PrefetchTarget,
} from "../pipeline/server/viewer_ui/workbench-prefetch";

const target = (
  episode = 1,
  dataset = "one",
  signature = "source-a",
): PrefetchTarget => ({ dataset, episode, signature });
function prepared(t: PrefetchTarget) {
  const video = {
    released: false,
    pause() {},
    removeAttribute() {
      this.released = true;
    },
    load() {},
  };
  return {
    head: {
      dataset: t.dataset,
      episode: t.episode,
      source_signature: t.signature,
    },
    media: { source_signature: t.signature },
    videos: new Map([["camera", video]]),
  } as unknown as PreparedEpisode;
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test("route remount adopts the same video buffers and cancels background work", async () => {
  const data = prepared(target());
  let signal!: AbortSignal;
  const cache = new EpisodePreloader(
    async (_, s) => {
      signal = s;
      return data;
    },
    async () => true,
    8,
  );
  cache.prepare(target(), () => {});
  await tick();
  cache.leave();
  cache.retainFor(target());
  const result = await cache.take(target());
  expect(result).toBe(data);
  expect(result!.videos.get("camera")).toBe(data.videos.get("camera"));
  expect(signal.aborted).toBe(true);
  await new Promise((resolve) => setTimeout(resolve, 12));
  expect((result!.videos.get("camera") as any).released).toBe(false);
  expect(cache.matches(target())).toBe(false);
});

test("a new next Episode cancels the old slot; a late response cannot replace it", async () => {
  const old = deferred<PreparedEpisode>(),
    latest = prepared(target(4));
  const signals: AbortSignal[] = [];
  const cache = new EpisodePreloader(
    async (t, s) => {
      signals.push(s);
      return t.episode === 1 ? old.promise : latest;
    },
    async () => true,
  );
  cache.prepare(target(), () => {});
  cache.prepare(target(4), () => {});
  const obsolete = prepared(target());
  old.resolve(obsolete);
  await tick();
  expect(signals[0].aborted).toBe(true);
  expect(obsolete.videos.size).toBe(0);
  expect(cache.matches(target(4))).toBe(true);
  expect(await cache.take(target(4))).toBe(latest);
});

test("repeated playback updates do not launch duplicate preloads", async () => {
  let requests = 0;
  const cache = new EpisodePreloader(
    async (t) => {
      requests++;
      return prepared(t);
    },
    async () => true,
  );
  for (let i = 0; i < 100; i++) cache.prepare(target(), () => {});
  await tick();
  expect(requests).toBe(1);
  cache.clear();
});

test("dataset switches and source changes release every retained video", async () => {
  for (const next of [target(1, "other"), target(1, "one", "new-source")]) {
    const data = prepared(target());
    const video = data.videos.get("camera") as any;
    const cache = new EpisodePreloader(
      async () => data,
      async () => true,
    );
    cache.prepare(target(), () => {});
    await tick();
    expect(await cache.take(next)).toBeUndefined();
    cache.retainFor(next);
    expect(video.released).toBe(true);
    expect(data.videos.size).toBe(0);
  }
});

test("leaving the workspace expires the single retained slot", async () => {
  const data = prepared(target());
  const video = data.videos.get("camera") as any;
  const cache = new EpisodePreloader(
    async () => data,
    async () => true,
    5,
  );
  cache.prepare(target(), () => {});
  await tick();
  cache.leave();
  await new Promise((resolve) => setTimeout(resolve, 12));
  expect(video.released).toBe(true);
  expect(await cache.take(target())).toBeUndefined();
});

test("failed or mismatched reads fall back without retaining stale data", async () => {
  const wrong = prepared(target(1, "one", "wrong-source"));
  for (const load of [
    async () => {
      throw Error("offline");
    },
    async () => wrong,
  ]) {
    const states: (boolean | null)[] = [];
    const cache = new EpisodePreloader(load, async () => true);
    cache.prepare(target(), (state) => states.push(state));
    await tick();
    expect(await cache.take(target())).toBeUndefined();
    expect(states.at(-1)).toBeNull();
  }
  expect(wrong.videos.size).toBe(0);
});

test("media keys preserve shared-video paths and the verified source", () => {
  expect(
    mediaUrl("data one", "signature", "videos/camera left/chunk-0.mp4"),
  ).toBe(
    "/api/annotation-workbench/data%20one/media/videos/camera%20left/chunk-0.mp4?signature=signature",
  );
});
