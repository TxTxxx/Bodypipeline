"use strict";
window.ClientVideo = (() => {
  let active = null;
  const escape = value => String(value ?? "").replace(/[&<>"']/g,
    character => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[character]));
  const changed = () => window.dispatchEvent(new Event("video-download-changed"));
  const canChooseLocation = () => window.isSecureContext && typeof window.showSaveFilePicker === "function";

  function message(text, error = false) {
    const box = document.getElementById("notice");
    box.hidden = false;
    box.className = error ? "error" : "";
    box.textContent = text;
  }

  function button(row) {
    if (row.state !== "SAVED" || row.deleted_at || row.delete_requested || row.discard_requested
        || !row.video_download_supported) return "";
    const id = row.capture_id || row.id;
    if (active?.id === id) {
      return `<button type="button" class="secondary" data-video-cancel="${escape(id)}"${active.selecting ? " disabled" : ""}>${active.selecting ? "选择保存位置…" : "取消下载"}</button> `
        + (active.bytes ? `<span class="sub">已下载 ${(active.bytes / 1048576).toFixed(1)} MB</span>` : "");
    }
    const ready = !row.capture_failed && !row.delete_pending
      && (!row.conversion_state || row.conversion_state === "DONE");
    const path = row.published_path || row.published_raw_path || row.saved_path || "episode";
    const filename = path.split("/").pop().replace(/\.(hdf5|raw)$/, "") + ".mp4";
    const hint = canChooseLocation() ? "先选择本机保存位置，再生成并下载拼接视频" : "点击后生成拼接视频，并由浏览器下载到你的电脑";
    return `<button type="button" class="secondary" data-video-download="${escape(id)}" data-video-filename="${escape(filename)}"${!ready || active ? " disabled" : ""} title="${ready ? hint : "等待 HDF5 转换完成后可下载视频"}">download</button> `;
  }

  function browserDownload(id, filename) {
    // Native form navigation lets the browser stream the attachment to its
    // download directory on HTTP, without buffering the whole MP4 in memory.
    const frame = document.createElement("iframe");
    frame.hidden = true;
    frame.name = "video-download-" + Date.now() + "-" + Math.random().toString(36).slice(2);
    frame.addEventListener("load", () => {
      const document = frame.contentDocument;
      if (!document || document.location.href === "about:blank") return;
      const text = document.body?.textContent?.trim();
      if (!text) return;
      try {
        const error = JSON.parse(text);
        message(typeof error.detail === "string" ? error.detail : "视频下载失败，请重试。", true);
      } catch {
        message("视频下载未能开始，请刷新页面后重试。", true);
      }
      frame.remove();
    });
    const form = document.createElement("form");
    form.hidden = true;
    form.method = "POST";
    form.action = `/api/captures/${encodeURIComponent(id)}/video`;
    form.target = frame.name;
    document.body.append(frame, form);
    message(`正在生成并下载 ${filename}。保存位置由浏览器设置决定，可在浏览器下载列表查看进度或取消。`);
    form.submit();
    form.remove();
  }

  async function download(id, filename) {
    if (active) return;
    if (!canChooseLocation()) {
      browserDownload(id, filename);
      return;
    }

    const task = {id, selecting: true, bytes: 0, controller: new AbortController()};
    active = task;
    changed();
    let writable;
    try {
      // Keep the picker in the original click activation. No server request,
      // encoding or local writer is started before the user chooses a location.
      const handle = await window.showSaveFilePicker({
        suggestedName: filename,
        types: [{description: "MP4 视频", accept: {"video/mp4": [".mp4"]}}],
      });
      writable = await handle.createWritable();
      task.selecting = false;
      changed();
      message("已选择本机保存位置，正在生成并下载视频…");
      const response = await fetch(`/api/captures/${encodeURIComponent(id)}/video`, {
        method: "POST", signal: task.controller.signal,
      });
      if (!response.ok) {
        const error = await response.json();
        throw Error(error.detail || "视频生成失败");
      }
      const reader = response.body.getReader();
      let updatedAt = 0;
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        await writable.write(value);
        task.bytes += value.byteLength;
        if (performance.now() - updatedAt >= 1000) {
          changed();
          updatedAt = performance.now();
        }
      }
      await writable.close();
      writable = null;
      message(`视频已保存到你选择的本机位置：${handle.name}`);
    } catch (error) {
      task.controller.abort();
      if (writable) await writable.abort().catch(() => {});
      message(error.name === "AbortError" ? (task.selecting ? "已取消选择，未生成视频。" : "已取消视频生成和下载。") : error.message,
        error.name !== "AbortError");
    } finally {
      if (active === task) active = null;
      changed();
    }
  }

  document.addEventListener("click", event => {
    const target = event.target.closest("[data-video-download]");
    if (target) void download(target.dataset.videoDownload, target.dataset.videoFilename);
    if (event.target.closest("[data-video-cancel]") && active && !active.selecting) active.controller.abort();
  });
  window.addEventListener("pagehide", () => active?.controller.abort());
  return {button};
})();
