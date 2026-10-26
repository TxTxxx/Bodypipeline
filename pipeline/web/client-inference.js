"use strict";
window.ClientInference = (() => {
  const $ = (id) => document.getElementById("inf-" + id);
  const valid = (value) => typeof value === "number" && Number.isFinite(value);
  let visible = false, version = 0, api, config, latest = null, pending = false;
  let telemetryTimer, cameraTimer, statusTimer, episodeTimer, paused = false, lastSample = null;
  let episodeRows = [], episodeFilter = "all", episodeBusy = false;
  let armActions = {}, armState = {busy: false, enableDisabled: false, resetAvailable: false};
  let samples = [], charts = [], cameraViews = [], resizeObserver;
  let chartLayout = "", jointCount = 0, checkpointFilled = false, promptFilled = false;
  const controllers = new Set();
  const maxSamples = 320, windowSeconds = 60;
  const escape = value => String(value ?? "").replace(/[&<>"']/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[character]));
  const bytes = value => {const size=Number(value)||0;return size>=1073741824?`${(size/1073741824).toFixed(1)} GB`:size>=1048576?`${(size/1048576).toFixed(1)} MB`:size>=1024?`${(size/1024).toFixed(0)} KB`:`${size} B`;};

  function pendingEpisode(row) {
    return row.state === "SAVED" && row.upload_state !== "ACKED" && !row.delete_requested && !row.deleted_at;
  }
  function renderEpisodes() {
    $("episode-total").textContent = episodeRows.filter(row => row.state !== "DISCARDED").length;
    $("episode-saved").textContent = episodeRows.filter(row => (row.published_raw_path || row.import_state === "DONE") && row.state !== "DISCARDED").length;
    $("episode-pending").textContent = episodeRows.filter(pendingEpisode).length;
    $("episode-uploaded").textContent = episodeRows.filter(row => row.upload_state === "ACKED" && row.state !== "DISCARDED").length;
    const rows = episodeRows.filter(row => episodeFilter === "all" || (episodeFilter === "recording" && row.state === "RECORDING")
      || (episodeFilter === "pending" && pendingEpisode(row)) || (episodeFilter === "uploaded" && row.upload_state === "ACKED" && row.state !== "DISCARDED"));
    $("episode-summary").textContent = `${episodeRows.length} 个 Episodes · ${episodeRows.filter(row=>row.state==="RECORDING").length} 个录制中`;
    $("episode-list").innerHTML = rows.length ? rows.map(row => {
      const discarded = row.state === "DISCARDED", recording = row.state === "RECORDING";
      const deleted = !!row.deleted_at || !!row.deleted;
      const deleting = !!row.delete_pending, deleteFailed = row.delete_state === "RETRY";
      const saved = row.import_state === "DONE", failed = row.import_state === "RETRY" || row.state === "ERROR";
      const localLabel = deleted ? "已删除" : deleting ? (row.delete_state === "WAITING_UPLOAD" ? "等待传输结束后删除" : "正在删除")
        : deleteFailed ? "删除失败 · 自动重试" : discarded ? "已丢弃" : recording ? "逐帧保存中" : row.state === "RECOVERY_REQUIRED" ? "原始帧已保留 · 需恢复"
        : failed ? (row.published_raw_path ? "转换失败 · raw 已保留" : "保存失败 · 原始帧已保留") : saved ? "HDF5 已保存" : row.published_raw_path ? "raw 已保存 · 正在转 HDF5" : "原始帧已保存 · 正在归档";
      const localClass = deleted || deleting ? "" : deleteFailed ? "error" : recording ? "live" : failed ? "error" : saved ? "done" : "";
      let uploadLabel = discarded ? "不上传" : recording ? "保存后自动上传" : row.published_raw_path ? "转换完成后自动上传" : "等待本地保存";
      let uploadClass = "", progress = "";
      if (!discarded && saved) {
        const names = {QUEUED:"等待上传",UPLOADING:"正在上传",ACKED:"已上传",RETRY:"上传失败 · 自动重试"};
        uploadLabel = names[row.upload_state] || "等待上传";
        uploadClass = row.upload_state === "ACKED" ? "done" : row.upload_state === "RETRY" ? "error" : "";
        if (["UPLOADING","RETRY"].includes(row.upload_state)) {
          const percentage = row.total_bytes ? Math.min(100,Math.max(0,Math.round((row.uploaded_bytes||0)/row.total_bytes*100))) : 0;
          progress = `<div class="dagger-upload-progress" role="progressbar" aria-label="上传进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percentage}"><span style="width:${percentage}%"></span></div><span class="sub">${percentage}% · ${bytes(row.uploaded_bytes)} / ${bytes(row.total_bytes)}</span>`;
        }
      }
      const timestamp = row.started_at ? new Date(row.started_at*1000).toLocaleString("zh-CN",{hour12:false}) : "—";
      const path = row.saved_path || row.source_path || "等待分配保存位置";
      const rawPath = row.published_raw_path && row.published_raw_path !== path && !deleted ? `<span class="sub">raw：${escape(row.published_raw_path)}</span>` : "";
      const remote = row.remote_path ? `<span class="sub">远端：${escape(row.remote_path)}</span>` : "";
      const error = row.delete_error || row.upload_error || row.conversion_error || row.import_error || row.error;
      const retry = !discarded && !row.delete_requested && (row.upload_state === "RETRY" || row.import_state === "RETRY" || row.state === "ERROR")
        ? `<button type="button" class="secondary" data-dagger-retry="${escape(row.id)}"${episodeBusy?" disabled":""}>${saved?"重试上传":"重试保存"}</button>` : "";
      const remove = deleted ? '<span class="muted small">已移入回收站</span>'
        : `<button type="button" class="danger" data-dagger-delete="${escape(row.id)}"${episodeBusy||deleting||recording?" disabled":""}${recording?' title="请先保留或丢弃当前纠偏"':""}>${deleting?"正在删除…":deleteFailed?"重试删除":"删除"}</button>`;
      if(deleted) {uploadLabel="已移入回收站";uploadClass="";progress="";}
      else if(row.delete_requested) {uploadLabel=row.delete_state==="WAITING_UPLOAD"?"传输结束后自动回收":"停止后续上传 · 正在回收";progress="";}
      return `<tr${recording?' class="is-recording"':""}><td><strong>Episode ${String(row.episode_index||1).padStart(3,"0")}</strong><span class="sub dagger-episode-task">${escape(row.checkpoint_name||row.checkpoint?.split("/").pop()||row.task_name||"纠偏")}</span><span class="sub">${escape(timestamp)} · ${escape(row.session_id?.slice(-8)||"")}</span></td>
        <td><strong>${Number(row.duration_s||0).toFixed(1)} s</strong><span class="sub">${Number(row.saved_frames||row.frames||0)} 帧已落盘</span></td>
        <td><span class="dagger-state ${localClass}">${escape(localLabel)}</span></td><td><span class="dagger-state ${uploadClass}">${escape(uploadLabel)}</span>${progress}${error?`<span class="sub job-error">${escape(error)}</span>`:""}</td>
        <td class="dagger-episode-path"><span>${escape(path)}</span>${rawPath}${remote}</td><td><div class="dagger-episode-actions">${retry}${window.ClientVideo.button(row)}${deleted?"":`<button type="button" class="secondary" data-dagger-copy="${escape(path)}">复制路径</button>`}${remove}</div></td></tr>`;
    }).join("") : `<tr><td colspan="6" class="empty">${episodeRows.length?"当前筛选条件下没有 Episodes。":"尚无纠偏记录。进入 DAgger 后点击“拖动接管”，新的 Episode 将实时出现在这里。"}</td></tr>`;
  }
  window.addEventListener("video-download-changed", () => {
    if (visible) renderEpisodes();
  });
  async function pollEpisodes(rev) {
    if (!visible || rev !== version) return;
    try {
      const data = await request("/api/inference/dagger/episodes");
      if (!visible || rev !== version) return;
      episodeRows = data.episodes || [];
      $("episode-live").textContent = data.enabled ? "自动保存 · 自动上传" : "Episode 同步未启用";
      $("episode-live").className = "badge" + (data.error ? " warn" : "");
      $("episode-error").hidden = !data.error;
      $("episode-error").textContent = data.error ? "Episode 同步暂未更新，已保存数据保留：" + data.error : "";
      renderEpisodes();
    } catch (error) {
      if (visible && rev === version) {$("episode-error").hidden=false;$("episode-error").textContent=error.message;}
    } finally {if (visible && rev===version) episodeTimer=setTimeout(()=>pollEpisodes(rev),1000);}
  }
  function message(text, error = false) {
    $("message").textContent = text;
    $("message").className = "inference-message" + (error ? " error" : "");
    $("config-feedback").classList.toggle("is-error", error);
  }
  async function request(path, binary = false) {
    const controller = new AbortController(); controllers.add(controller);
    const timer = setTimeout(() => controller.abort(), binary ? 4500 : 9500);
    try {
      const response = await fetch(path, {signal: controller.signal, cache: "no-store"});
      if (!response.ok) throw Error(response.status === 401 ? "登录已失效，请重新登录。" : "暂未收到实时数据");
      return binary ? await response.blob() : await response.json();
    } finally {clearTimeout(timer); controllers.delete(controller);}
  }
  function controls() {
    const caps = latest?.capabilities || {};
    const locked = pending || armState.busy;
    $("arm-enable").disabled = locked || latest?.preparing || latest?.dagger_active;
    $("arm-prepare").disabled = locked || latest?.preparing || latest?.dagger_active || !latest?.robot_enable_supported || (latest?.hardware_enable_required && !latest?.hardware_enabled);
    $("arm-reset").disabled = locked || latest?.preparing || latest?.dagger_active || !latest?.episode_reset_supported || (latest?.hardware_enable_required && !latest?.hardware_enabled);
    $("mode-switch").disabled = locked || latest?.preparing;
    $("pause").disabled = pending || !latest?.connected || !latest?.inference_active;
    $("start").disabled = locked || latest?.preparing || latest?.dagger_active || !latest?.connected || !caps.inference || (latest?.robot_enable_supported && !latest?.robot_enabled);
    $("stage2").disabled = locked || latest?.preparing || !latest?.connected || !caps.stage2;
    $("dagger").disabled = locked || latest?.preparing || latest?.dagger_active || !latest?.connected || !caps.dagger || (latest?.robot_enable_supported && !latest?.robot_enabled);
    const dagger = latest?.dagger || {}, teaching = dagger.mode === "human";
    for (const action of ["takeover", "resume", "save", "discard", "finish"]) {
      const supported = latest?.dagger_controls?.includes(action);
      $("dagger-" + action).hidden = !supported;
      $("dagger-" + action).disabled = locked || latest?.preparing || !latest?.dagger_active
        || (action === "takeover" && teaching) || (action === "discard" && !dagger.segment_id)
        || (action === "resume" && dagger.mode === "policy");
    }
    const stage2 = latest?.stage2_active || latest?.mode === "stage2" || latest?.state === "STAGE2";
    const feedbackReady = stage2 && latest?.session_id && latest?.feedback_allowed && !latest?.feedback_recorded;
    $("failed").disabled = $("success").disabled = locked || !latest?.connected || !feedbackReady;
    for (const [id, feature] of [["start", "inference"], ["stage2", "stage2"], ["dagger", "dagger"]]) {
      $(id).title = latest?.feature_messages?.[feature] || "";
    }
    $("checkpoint").disabled = $("prompt").disabled = pending || latest?.dagger_active;
  }
  function renderStatus(row) {
    latest = row;
    if (row.control_mode) setControlMode({mode: row.control_mode});
    $("connection").textContent = row.connected ? "执行服务已连接" : "执行服务未连接";
    $("connection").className = "badge" + (row.connected ? "" : " warn");
    $("policy-state").textContent = row.preparing ? "准备中" : row.inference_active ? "推理中" : row.hardware_enable_required && !row.hardware_enabled ? "待使能" : row.robot_enable_supported && !row.robot_enabled ? "待预备" : "待启动";
    if (!checkpointFilled && row.checkpoint_path) {
      if (!$("checkpoint").value) $("checkpoint").value = row.checkpoint_path;
      checkpointFilled = true;
    }
    if (!promptFilled && row.prompt) {
      if (!$("prompt").value) $("prompt").value = row.prompt;
      promptFilled = true;
    }
    $("loaded-model").textContent = row.checkpoint_path || (row.connected ? "尚未加载模型权重" : "等待模型服务连接");
    $("model-indicator").classList.toggle("is-online", !!row.policy_connected && !!row.checkpoint_path);
    updatePromptCount();
    $("stage-state").textContent = row.stage2_active || row.mode === "stage2" || row.state === "STAGE2" ? "Stage 2" : "未进入";
    const dagger = row.dagger || {};
    const daggerModes = {human: "人工纠偏中", policy: "策略执行中", hold: "已暂停"};
    $("dagger-state").textContent = row.dagger_active ? (daggerModes[dagger.mode] || "DAgger 中") : "未进入";
    $("dagger-feedback").textContent = row.dagger_active
      ? `已保留 ${dagger.saved_segments || 0} 段 · 已丢弃 ${dagger.discarded_segments || 0} 段${dagger.segment_id ? ` · 当前 ${dagger.frames || 0} 帧` : ""}`
      : dagger.output && dagger.saved_segments ? `采集已结束，保留 ${dagger.saved_segments} 段纠偏` : "进入 DAgger 后可人工接管纠偏。";
    $("dagger-output").textContent = dagger.output ? `数据目录：${dagger.output}` : "";
    $("feedback").textContent = row.last_feedback === "success" ? "本回合已标记 Success" : row.last_feedback === "failed" ? "本回合已标记 Failed" : "尚未标记结果";
    if (!pending) message(row.message || (row.connected ? "执行服务已连接，选择权重并输入 prompt 后开始。" : "推理执行服务尚未连接，实时相机和关节监控可独立使用。"));
    for (const [id, feature] of [["policy-state", "inference"], ["stage-state", "stage2"], ["dagger-state", "dagger"]]) {
      const connected = !!row.capabilities?.[feature];
      if (!connected) $(id).textContent = "未连接";
      $(id).className = connected ? "badge" : "badge inference-offline";
    }
    controls();
  }
  async function pollStatus(rev) {
    if (!visible || rev !== version) return;
    try {
      const row = await request("/api/inference/status");
      if (!visible || rev !== version) return;
      renderStatus(row);
    } catch (error) {
      if (visible && rev === version) {renderStatus({connected: false}); message(error.message, true);}
    } finally {
      if (visible && rev === version) statusTimer = setTimeout(() => pollStatus(rev), 2000);
    }
  }
  function createCameras() {
    $("cameras").replaceChildren(); cameraViews = [];
    const cameras = config.profile.cameras;
    $("camera-labels").textContent = cameras.map((camera) => camera.label).join(" / ");
    $("cameras").style.setProperty("--inference-camera-count", Math.max(1, cameras.length));
    for (const camera of cameras) {
      const view = document.createElement("div"); view.className = "inference-camera-view";
      const img = document.createElement("img"); img.alt = camera.label + " 实时视角";
      const empty = document.createElement("div"); empty.className = "inference-camera-empty"; empty.textContent = "等待相机图像";
      const caption = document.createElement("div"); caption.className = "camera-caption";
      caption.textContent = camera.label;
      view.append(img, empty, caption); $("cameras").append(view);
      cameraViews.push({camera, img, empty, url: null});
    }
  }
  async function pollCameras(rev) {
    if (!visible || rev !== version) return;
    if (!paused && !document.hidden) {
      let received = 0;
      await Promise.allSettled(cameraViews.map(async (view) => {
        try {
          const blob = await request(`/api/cameras/${encodeURIComponent(view.camera.id)}/frame?preview=full`, true);
          if (!visible || rev !== version || paused) return;
          const url = URL.createObjectURL(blob);
          if (view.url) URL.revokeObjectURL(view.url);
          view.url = url; view.img.src = url; view.empty.hidden = true; received++;
        } catch {
          if (visible && rev === version) {view.empty.hidden = false; view.empty.textContent = "相机暂未更新";}
        }
      }));
      if (visible && rev === version && !paused) $("camera-status").textContent = `${received} / ${cameraViews.length} 路在线`;
    }
    if (visible && rev === version) cameraTimer = setTimeout(() => pollCameras(rev), 750);
  }
  function jointLayout(row = {}) {
    const names = row.names || config.profile.joint_names || [];
    const units = row.units || config.profile.joint_units || [];
    const groups = row.groups || config.profile.joint_groups || [
      {label: "关节反馈", indices: names.map((_, index) => index)},
    ];
    return {names, units, groups};
  }
  function createCharts(layout = jointLayout()) {
    $("joint-groups").replaceChildren(); charts = [];
    const {names, units, groups} = layout;
    jointCount = names.length;
    chartLayout = JSON.stringify(layout);
    for (const [groupIndex, spec] of groups.entries()) {
      const group = document.createElement("div"); group.className = "inference-arm-group";
      const title = document.createElement("h3"); title.textContent = spec.label;
      const grid = document.createElement("div"); grid.className = "inference-joint-grid";
      for (const [joint, index] of spec.indices.entries()) {
        const tile = document.createElement("div"); tile.className = "inference-joint-tile";
        const label = document.createElement("div"); label.className = "inference-joint-label";
        const name = document.createElement("strong"); name.textContent = spec.labels?.[joint] || names[index] || `J${index + 1}`;
        const unit = document.createElement("span"); unit.textContent = units[index] || "rad";
        label.append(name, unit);
        const value = document.createElement("output"); value.className = "inference-joint-value"; value.textContent = "—";
        const canvas = document.createElement("canvas");
        canvas.setAttribute("role", "img");
        canvas.setAttribute("aria-label", `${spec.label} ${name.textContent} 实时曲线`);
        tile.title = names[index] || name.textContent;
        tile.append(label, value, canvas); grid.append(tile);
        charts.push({canvas, value, index, color: ["#4268d4", "#238b83", "#9965b5"][groupIndex % 3]});
      }
      group.append(title, grid); $("joint-groups").append(group);
    }
    resizeObserver?.disconnect();
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(() => drawCharts());
      resizeObserver.observe($("joint-groups"));
    }
    drawCharts();
  }
  function drawCharts() {
    const now = Date.now() / 1000, start = now - windowSeconds;
    for (const chart of charts) {
      const canvas = chart.canvas, w = Math.max(90, canvas.clientWidth || 140), h = 100;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      }
      const ctx = canvas.getContext("2d");
      if (!ctx) continue;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
      const left = 29, right = w - 5, top = 10, bottom = h - 19;
      const data = samples.filter((sample) => sample.time >= start);
      const values = data.map((sample) => sample.positions[chart.index]).filter(valid);
      let min = values.length ? Math.min(...values) : -0.5, max = values.length ? Math.max(...values) : 0.5;
      const padding = Math.max(0.03, (max - min) * 0.15); min -= padding; max += padding;
      ctx.font = "9px sans-serif"; ctx.lineWidth = 1;
      for (let tick = 0; tick <= 2; tick++) {
        const y = top + (bottom - top) * tick / 2;
        ctx.strokeStyle = "#e8edf4"; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
        ctx.fillStyle = "#929eae"; ctx.textAlign = "right"; ctx.fillText((max - (max - min) * tick / 2).toFixed(2), left - 4, y + 3);
      }
      ctx.textAlign = "left"; ctx.fillText("−60s", left, h - 4);
      ctx.textAlign = "right"; ctx.fillText("现在", right, h - 4);
      ctx.strokeStyle = chart.color; ctx.lineWidth = 1.6; ctx.beginPath();
      let drawing = false, previous = null;
      for (const sample of data) {
        const q = sample.positions[chart.index];
        if (!valid(q)) {drawing = false; previous = null; continue;}
        const x = left + (sample.time - start) / windowSeconds * (right - left);
        const y = bottom - (q - min) / (max - min) * (bottom - top);
        if (!drawing || (previous !== null && sample.time - previous > 1)) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
        drawing = true; previous = sample.time;
      }
      ctx.stroke();
    }
  }
  function showPositions(row) {
    const positions = Array.isArray(row.positions) ? row.positions : [];
    const layout = jointLayout(row);
    if (JSON.stringify(layout) !== chartLayout) createCharts(layout);
    for (const chart of charts) chart.value.textContent = valid(positions[chart.index]) ? positions[chart.index].toFixed(3) : "—";
    const count = positions.filter(valid).length;
    if (visible) document.getElementById("health-label").textContent = jointCount > 0 && count === jointCount ? "● 机器人观测在线" : count ? "○ 部分关节反馈离线" : "○ 等待机器人观测";
    $("joint-status").textContent = row.simulated ? `模拟数据 · ${count} / ${jointCount} 路` : count ? `${count} / ${jointCount} 路在线 · 5 Hz 刷新` : "关节反馈离线";
    if (row.sample_id && row.sample_id === lastSample) {drawCharts(); return;}
    lastSample = row.sample_id || null;
    const time = valid(row.timestamp) ? row.timestamp : Date.now() / 1000;
    samples.push({time, positions: Array.from({length: jointCount}, (_, index) => valid(positions[index]) ? positions[index] : null)});
    samples = samples.filter((sample) => sample.time >= Date.now() / 1000 - windowSeconds).slice(-maxSamples);
    drawCharts();
  }
  async function pollJoints(rev) {
    if (!visible || rev !== version) return;
    try {
      if (!document.hidden) {
        const row = await request("/api/joints");
        if (visible && rev === version) showPositions(row);
      }
    } catch {
      if (visible && rev === version) showPositions({positions: [], timestamp: Date.now() / 1000});
    } finally {
      if (visible && rev === version) telemetryTimer = setTimeout(() => pollJoints(rev), 200);
    }
  }
  function requestId() {
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
  }
  async function execute(operation) {
    if (pending) return;
    const caps = latest?.capabilities || {};
    const capability = operation === "start" ? "inference" : operation;
    if (!latest?.connected || !caps[capability]) {message("当前执行服务尚不支持此操作。", true); return;}
    const checkpoint = $("checkpoint").value.trim(), prompt = $("prompt").value.trim();
    if (!checkpoint || !prompt) {message("请填写权重路径和 prompt。", true); return;}
    pending = true; controls(); message("正在提交操作，请稍候…");
    try {
      const row = await api(`/api/inference/${operation}`, "POST", {checkpoint_path: checkpoint, prompt, request_id: requestId()});
      renderStatus({...latest, ...row});
      message(row.message || "操作已提交，等待执行服务更新状态。");
    } catch (error) {message(error.message, true);}
    finally {pending = false; controls();}
  }
  async function feedback(result) {
    if (pending || !latest?.session_id) return;
    pending = true; controls();
    try {
      const row = await api("/api/inference/feedback", "POST", {result, session_id: latest.session_id, request_id: requestId()});
      renderStatus({...latest, ...row});
      message(row.message || "回合结果已提交。");
    } catch (error) {message(error.message, true);}
    finally {pending = false; controls();}
  }
  async function daggerControl(action) {
    if (pending || !latest?.dagger?.session_id) return;
    pending = true; controls();
    const messages = {takeover: "正在切换人工接管，等待状态显示人工纠偏中再拖动…", resume: "正在保留纠偏并接回策略…",
      save: "正在保留本段纠偏并暂停…", discard: "正在丢弃本段纠偏并暂停…", finish: "正在保留纠偏并结束采集…"};
    message(messages[action]);
    try {
      const row = await api("/api/inference/dagger/control", "POST", {action, session_id: latest.dagger.session_id, request_id: requestId()});
      renderStatus(row);
      message(row.preparing ? messages[action] : row.message || "操作已提交。");
    } catch (error) {message(error.message, true);}
    finally {pending = false; controls();}
  }
  function setControlMode(row) {
    const names = {capture: "采集模式", inference: "推理模式"};
    $("mode-label").textContent = row.switching ? "正在切换模式…" : names[row.mode] ? "当前：" + names[row.mode] : "控制模式未就绪";
  }
  async function switchInferenceMode(force = false) {
    if (!config?.mode_switching_enabled) return;
    const current = await api("/api/control-mode");
    const result = force || current.mode !== "inference" || current.error
      ? await api("/api/control-mode", "POST", {mode: "inference"}) : current;
    setControlMode(result);
    window.dispatchEvent(new CustomEvent("control-mode-changed", {detail: result}));
  }
  $("mode-switch").onclick = async () => {
    if (pending) return;
    pending = true; controls(); message("正在保存当前任务并切换到推理模式…");
    try {await switchInferenceMode(true); renderStatus(await api("/api/inference/status"));}
    catch (error) {message(error.message, true);}
    finally {pending = false; controls();}
  };
  async function nativeControl(operation) {
    if (pending) return;
    pending = true; controls();
    message(operation === "enable" ? "正在启动与恢复 CAN、相机和双臂控制器…" : operation === "prepare" ? "正在进入推理准备位姿…" : operation === "reset" ? "正在暂停推理并返回合拢位置…" : "正在暂停推理…");
    try {
      if (operation === "enable") await switchInferenceMode();
      renderStatus(await api(`/api/inference/${operation}`, "POST", {}));
    } catch (error) {message(error.message, true);}
    finally {pending = false; controls();}
  }
  $("arm-enable").onclick = () => void nativeControl("enable");
  $("arm-prepare").onclick = () => void nativeControl("prepare");
  $("arm-reset").onclick = () => void nativeControl("reset");
  $("pause").onclick = () => void nativeControl("pause");
  function updatePromptCount() {
    $("prompt-count").textContent = `${$("prompt").value.length} / ${$("prompt").maxLength}`;
  }
  $("checkpoint").addEventListener("input", () => {checkpointFilled = true;});
  $("prompt").addEventListener("input", () => {promptFilled = true; updatePromptCount();});
  $("start").onclick = () => void execute("start");
  $("stage2").onclick = () => void execute("stage2");
  $("dagger").onclick = () => void execute("dagger");
  for (const button of document.querySelectorAll("[data-dagger-filter]")) {
    button.onclick = () => {
      episodeFilter=button.dataset.daggerFilter;
      for (const item of document.querySelectorAll("[data-dagger-filter]")) {const selected=item===button;item.classList.toggle("is-selected",selected);item.setAttribute("aria-pressed",String(selected));}
      renderEpisodes();
    };
  }
  $("episode-list").onclick = async event => {
    const retry=event.target.closest("[data-dagger-retry]"), copy=event.target.closest("[data-dagger-copy]");
    const deletion=event.target.closest("[data-dagger-delete]");
    if(deletion && !episodeBusy) {
      episodeBusy=true;renderEpisodes();
      try {
        const result=await api(`/api/inference/dagger/episodes/${encodeURIComponent(deletion.dataset.daggerDelete)}/delete`,"POST",{});
        const row=episodeRows.find(item=>item.id===deletion.dataset.daggerDelete);
        if(row)Object.assign(row,{delete_requested:true,delete_pending:!result.deleted,delete_error:null});
        message(result.message || "已提交删除，本地及已上传副本将移入回收站。");
      } catch(error){message(error.message,true);}finally{episodeBusy=false;renderEpisodes();}
    } else if (retry && !episodeBusy) {
      episodeBusy=true;renderEpisodes();
      try {await api(`/api/inference/dagger/episodes/${encodeURIComponent(retry.dataset.daggerRetry)}/retry`,"POST",{});message("已重新加入保存或上传队列。");}
      catch(error){message(error.message,true);}finally{episodeBusy=false;renderEpisodes();}
    } else if(copy) {
      try {
        if(navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(copy.dataset.daggerCopy);
        else {const area=document.createElement("textarea");area.value=copy.dataset.daggerCopy;area.style.position="fixed";area.style.opacity="0";document.body.append(area);area.select();document.execCommand("copy");area.remove();}
        message("已复制数据路径。");
      } catch {message("复制失败，请直接选择表格中的路径。",true);}
    }
  };
  for (const action of ["takeover", "resume", "save", "discard", "finish"]) {
    $("dagger-" + action).onclick = () => void daggerControl(action);
  }
  $("failed").onclick = () => void feedback("failed");
  $("success").onclick = () => void feedback("success");
  $("chart-clear").onclick = () => {samples = []; lastSample = null; drawCharts();};
  $("preview-toggle").onclick = () => {
    paused = !paused; $("preview-toggle").textContent = paused ? "恢复预览" : "暂停预览";
    $("camera-status").textContent = paused ? "预览已暂停" : "正在恢复预览";
  };
  function deactivate() {
    visible = false; version++;
    clearTimeout(telemetryTimer); clearTimeout(cameraTimer); clearTimeout(statusTimer); clearTimeout(episodeTimer);
    for (const controller of controllers) controller.abort();
    controllers.clear(); resizeObserver?.disconnect();
    for (const view of cameraViews) {if (view.url) URL.revokeObjectURL(view.url); view.url = null;}
  }
  function syncArmControls(state) {
    armState = state;
    controls();
  }
  async function activate(apiFn, settings, actions = {}) {
    deactivate(); api = apiFn; config = settings; armActions = actions; visible = true;
    $("mode-panel").hidden = !settings.mode_switching_enabled;
    const rev = version; latest = null; samples = []; lastSample = null;
    paused = false; $("preview-toggle").textContent = "暂停预览";
    createCameras(); createCharts(); controls();
    void pollStatus(rev); void pollCameras(rev); void pollJoints(rev); void pollEpisodes(rev);
  }
  return {activate, deactivate, syncArmControls, setControlMode};
})();
