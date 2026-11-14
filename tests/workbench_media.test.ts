import { test, expect } from "bun:test";
import { nearestFrame, presentedFrame, seekTime, LatestSeek, VideoTransport, type Media } from "../pipeline/server/viewer_ui/workbench-media";

const media: Media = {
  frames: 120, fps: 30, duration: 4,
  timestamps: Array.from({ length: 120 }, (_, i) => i / 30),
  cameras: [4, 4 + 7 / 30, 4 + 19 / 30].map((start, i) => ({ key: `c${i}`, path: `${i}.mp4`, start, end: start + 4 })),
};

test("presented PTS maps each shared-video/camera offset; not requested currentTime", () => {
  media.cameras.forEach((c, i) => {
    for (const n of [0, 1, 78, 119]) {
      expect(presentedFrame(media, i, c.start + n / 30)).toBe(n);
      const t = seekTime(media, i, n);
      expect(t).toBeGreaterThan(c.start + n / 30);
      expect(t).toBeLessThan(c.start + (n + 1) / 30);
    }
    expect(presentedFrame(media, i, c.start - 1 / 30)).toBeNull();
    expect(presentedFrame(media, i, c.end)).toBeNull();
  });
  expect(presentedFrame(media, 0, NaN)).toBeNull();
  expect(nearestFrame([0, .04, .11, .19], .102)).toBe(2);
});

test("one in-flight seek and only latest pending target, including backwards", () => {
  const queue = new LatestSeek();
  queue.request(1); expect(queue.take()).toBe(1);
  for (let i = 2; i < 1000; i++) queue.request(i);
  expect(queue.take()).toBeNull();
  queue.request(0); queue.finish();
  expect(queue.take()).toBe(0);
  queue.finish();
  expect(queue.take()).toBeNull(); // seeked without rVFC must not reissue endlessly
});

// A DOM-independent clock harness. Actual pixels, codec seeks and timing are
// separately verified by workbench_video_probe against the real browser.
function harness(native = true) {
  let id = 0;
  const raf = new Map<number, FrameRequestCallback>();
  globalThis.requestAnimationFrame = fn => { raf.set(++id, fn); return id; };
  globalThis.cancelAnimationFrame = n => { raf.delete(n); };
  const hosts = media.cameras.map(() => {
    class Video extends EventTarget {
      readyState = 4; seeking = false; playbackRate = 1; time = 0;
      seeks: number[] = []; callbacks = new Map<number, Function>(); stopped = false;
      get currentTime() { return this.time; }
      set currentTime(t: number) { this.time = t; this.seeking = true; this.seeks.push(t); }
      requestVideoFrameCallback: any = native ? (fn: Function) => { this.callbacks.set(++id, fn); return id; } : undefined;
      cancelVideoFrameCallback(n: number) { this.callbacks.delete(n); }
      pause() {}
      async play() {}
      removeAttribute() { this.stopped = true; }
      load() {}
      finish(pts: number) {
        this.seeking = false; this.dispatchEvent(new Event("seeked"));
        this.present(pts);
      }
      present(pts: number) {
        const callbacks = [...this.callbacks.values()]; this.callbacks.clear();
        callbacks.forEach(fn => fn(0, { mediaTime: pts }));
      }
    }
    const video = new Video(), image = { hidden: true }, tag = { textContent: "" };
    return { dataset: {} as Record<string, string>, video, image,
      querySelector: (q: string) => q === "video" ? video : q === "img" ? image : tag };
  });
  const updates: any[] = []; let settled = 0, calls = 0;
  const controller = new VideoTransport({
    media, hosts: hosts as any,
    exact: async () => { calls++; throw Error("not used while browsing"); },
    update: (...args) => updates.push(args), settled: () => settled++, error: () => {},
  });
  return { controller, hosts, updates, get settled() { return settled; }, get calls() { return calls; },
    flush: () => { const callbacks = [...raf.values()]; raf.clear(); callbacks.forEach(fn => fn(0)); } };
}

test("rapid browsing is bounded, actual cameras must all present before alignment", () => {
  const h = harness();
  h.controller.seek(2, true); h.flush();
  for (let n = 3; n < 99; n++) h.controller.seek(n, true);
  h.flush();
  expect(h.hosts.map(h => h.video.seeks.length)).toEqual([1, 1, 1]);
  h.hosts.forEach((c, i) => c.video.finish(media.cameras[i].start + 2 / 30));
  h.flush();
  expect(h.hosts.map(h => h.video.seeks.length)).toEqual([2, 2, 2]);
  h.controller.seek(98); // pointer release, no duplicate native seek
  h.hosts[0].video.finish(media.cameras[0].start + 98 / 30);
  h.hosts[1].video.finish(media.cameras[1].start + 98 / 30);
  expect(h.controller.alignedFrame).toBeNull();
  h.hosts[2].video.finish(media.cameras[2].start + 98 / 30);
  expect(h.controller.alignedFrame).toBe(98);
  expect(h.settled).toBe(1);
  expect(h.calls).toBe(0);
  h.controller.pause(); h.controller.pause();
  h.flush();
  expect(h.hosts.map(h => h.video.seeks.length)).toEqual([2, 2, 2]);
  h.controller.dispose();
});

test("dispose cancels native callbacks/range loads and rejects late Episode callbacks", () => {
  const h = harness();
  h.controller.seek(22, true); h.flush();
  const late = [...h.hosts[0].video.callbacks.values()][0];
  h.controller.dispose();
  const count = h.updates.length;
  late(0, { mediaTime: media.cameras[0].start + 22 / 30 });
  expect(h.updates.length).toBe(count);
  expect(h.hosts.every(c => c.video.stopped && c.video.callbacks.size === 0)).toBe(true);
});

test("seeked/currentTime without presentation callbacks cannot certify a data frame", () => {
  const h = harness(false);
  h.controller.seek(15, true); h.flush();
  h.hosts.forEach((c, i) => c.video.finish(seekTime(media, i, 15)));
  expect(h.controller.alignedFrame).toBeNull();
  expect(h.settled).toBe(0);
  h.controller.dispose();
});

test("late native callbacks cannot relabel the visible precise PNG", () => {
  const h = harness();
  h.controller.seek(6); h.flush();
  h.hosts.forEach((c, i) => c.video.finish(media.cameras[i].start + 6 / 30));
  h.hosts.forEach(c => { c.image.hidden = false; c.dataset.clock = "decoded"; });
  const before = h.hosts.map(c => ({ ...c.dataset }));
  h.hosts.forEach((c, i) => c.video.finish(media.cameras[i].start + 1 / 30));
  expect(h.hosts.map(c => c.dataset)).toEqual(before);
  expect(h.controller.alignedFrame).toBe(6);
  h.controller.dispose();
});

test("obsolete play Promise rejection cannot pause the newer play intent", async () => {
  const h = harness();
  const promises: { resolve: () => void; reject: (e: Error) => void }[] = [];
  h.hosts.forEach(c => c.video.play = () => new Promise<void>((resolve, reject) => {
    promises.push({ resolve, reject });
  }));
  h.controller.seek(5); h.flush();
  h.hosts.forEach((c, i) => c.video.finish(media.cameras[i].start + 5 / 30));
  const old = h.controller.play();
  await Promise.resolve();
  expect(promises.length).toBe(3);
  h.controller.seek(8); h.flush();
  h.hosts.forEach((c, i) => c.video.finish(media.cameras[i].start + 8 / 30));
  const latest = h.controller.play();
  await Promise.resolve();
  expect(promises.length).toBe(6);
  promises[0].reject(Error("old play interrupted by seek"));
  await old;
  expect(h.controller.playing).toBe(true);
  expect(h.controller.target).toBe(8);
  promises.slice(3).forEach(p => p.resolve());
  await latest;
  expect(h.controller.playing).toBe(true);
  h.controller.dispose();
});

test("playing away from a completed seek does not suppress seeking back to it", async () => {
  const h = harness();
  h.controller.seek(5); h.flush();
  h.hosts.forEach((c, i) => c.video.finish(media.cameras[i].start + 5 / 30));
  await h.controller.play();
  h.hosts.forEach((c, i) => c.video.present(media.cameras[i].start + 15 / 30));
  h.controller.seek(5); h.flush();
  expect(h.hosts.map(c => c.video.seeks.length)).toEqual([2, 2, 2]);
  h.controller.dispose();
});
