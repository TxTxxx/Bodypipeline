"use strict";
window.ClientReplay = (() => {
  const $ = (id) => document.getElementById("cr-" + id);
  const running = (row) => ["PREPARING", "RUNNING", "STOPPING", "RECOVERY_REQUIRED"].includes(row?.state);
  let api, visible = false, timer, version = 0, choiceVersion = 0;
  let latest = null, selected = null, episodes = [], source = "captures", directory = "";
  let pending = false, connected = false, requestId = null;
  const labels = {IDLE:"空闲", PREPARING:"准备数据", RUNNING:"回放中", STOPPING:"停止中", STOPPED:"已停止", COMPLETED:"全部帧已发送", FAILED:"回放失败", RECOVERY_REQUIRED:"上次回放中断"};
  function message(text = "") {$("message").hidden = !text; $("message").textContent = text;}
  function controls() {
    const locked = pending || running(latest);
    for (const id of ["source-captures", "source-files", "dataset", "root", "up"]) $(id).disabled = locked || !connected;
    $("refresh").disabled = pending;
    $("up").disabled ||= !directory;
    $("start").disabled = locked || !connected || !selected?.supported || !latest?.enabled;
    $("stop").disabled = pending || !connected || !running(latest);
  }
  function resetSelection() {
    choiceVersion++; selected = null; requestId = null;
    $("selected").textContent = "选择左侧 Episode";
    $("details").textContent = latest?.provider?.input_description || "请选择当前机器人支持的轨迹文件。";
    controls();
  }
  function call(operation, method = "GET", body, query) {
    const suffix = query ? "?" + new URLSearchParams(query) : "";
    return api(`/api/replay/${operation}${suffix}`, method, body);
  }
  function render(row) {
    latest = row; connected = true;
    if (visible) document.getElementById("mode-badge").textContent = row.simulated ? "REPLAY · 模拟回放" : "REPLAY · 真机回放";
    $("connection").textContent = row.simulated ? "已连接 · 本地模拟" : "客户端已连接";
    $("simulation").hidden = !row.simulated;
    $("provider").textContent = row.provider ? `${row.robot_type} · ${row.provider.label}` : row.unavailable_reason;
    if (!selected) $("details").textContent = row.provider?.input_description || row.unavailable_reason;
    $("start").textContent = row.simulated ? "开始模拟回放" : "开始真机回放";
    $("mode-note").textContent = row.simulated
      ? "本机模拟执行，不加载 ROS 或发送机器人运动指令。"
      : "由当前机器人执行。停止按钮结束本次回放进程。";
    $("state").textContent = row.simulated && row.state === "COMPLETED" ? "模拟完成"
      : row.simulated && row.state === "RUNNING" ? "模拟回放中" : labels[row.state] || row.state;
    $("progress").max = row.frames || 1; $("progress").value = row.frame || 0;
    $("counter").textContent = `${row.frame || 0} / ${row.frames || 0} 帧`;
    $("log").textContent = (row.log || []).join("\n");
    if (running(row) || (!selected && row.path)) $("selected").textContent = row.path;
    if (row.error) message(row.error);
    controls();
  }
  async function select(root, path, button) {
    if (pending || running(latest)) return;
    resetSelection(); message();
    const rev = version, choice = choiceVersion;
    try {
      const info = await call("inspect", "GET", undefined, {root, path});
      if (rev !== version || choice !== choiceVersion || running(latest)) return;
      selected = {...info, root, path};
      $("selected").textContent = info.path;
      $("details").textContent = info.supported ? `${info.frames} 帧${info.fps ? ` · ${info.fps} Hz` : ""} · ${info.description}` : info.error;
      document.querySelectorAll(".client-replay-entry.selected").forEach((entry) => entry.classList.remove("selected"));
      button?.classList.add("selected"); controls();
    } catch (error) {if (rev === version && choice === choiceVersion) message(error.message);}
  }
  function renderEpisodes() {
    $("episodes").replaceChildren();
    const rows = episodes.filter((row) => row.collection_id === $("dataset").value);
    for (const row of rows) {
      const button = document.createElement("button");
      button.type = "button"; button.className = "client-replay-entry";
      button.dataset.captureId = row.id;
      button.textContent = `Episode ${row.id.slice(0,8)}${row.available ? "" : " · 文件不存在"}`;
      const date = document.createElement("small"); date.textContent = new Date(row.created_at * 1000).toLocaleString("zh-CN");
      button.append(date); button.disabled = !row.available;
      button.onclick = () => select(row.root, row.path, button);
      $("episodes").append(button);
    }
    if (!rows.length) $("episodes").textContent = "这台机器人暂无已保留的采集。也可切换到机器人本地文件。";
  }
  function showSource(value) {
    source = value;
    $("capture-source").hidden = value !== "captures";
    $("file-source").hidden = value !== "files";
    $("source-captures").classList.toggle("active", value === "captures");
    $("source-files").classList.toggle("active", value === "files");
  }
  async function browse() {
    const root = $("root").value, rev = version, dir = directory;
    if (!root) {$("files").textContent = "客户端尚未配置数据目录。"; return;}
    try {
      const value = await call("files", "GET", undefined, {root, directory: dir});
      if (rev !== version || dir !== directory || root !== $("root").value) return;
      $("directory").textContent = dir || "/"; $("files").replaceChildren();
      for (const entry of value.entries) {
        const button = document.createElement("button");
        button.type = "button"; button.className = "client-replay-entry";
        button.textContent = `${entry.directory ? "▸" : "▷"} ${entry.name}`;
        button.onclick = () => {
          if (pending || running(latest)) return;
          if (entry.directory) {resetSelection(); directory = entry.path; void browse();}
          else void select(root, entry.path, button);
        };
        $("files").append(button);
      }
      if (!value.entries.length) $("files").textContent = "此目录没有当前适配器支持的轨迹文件或子目录。";
      if (value.truncated) message("仅显示前 500 项，请选择更具体的数据目录。");
      controls();
    } catch (error) {if (rev === version) message(error.message);}
  }
  async function loadData() {
    clearTimeout(timer);
    const rev = ++version;
    latest = null; connected = false;
    episodes = []; directory = ""; resetSelection(); message();
    $("dataset").replaceChildren(); $("root").replaceChildren(); $("episodes").replaceChildren(); $("files").replaceChildren();
    $("progress").value = 0; $("counter").textContent = "0 / 0 帧"; $("log").textContent = "";
    $("state").textContent = "空闲"; $("simulation").hidden = true;
    $("connection").textContent = "读取本机状态…";
    try {
      const [row, captures] = await Promise.all([call("status"), call("captures")]);
      if (rev !== version) return;
      render(row); episodes = captures;
      const groups = new Map(episodes.map((episode) => [episode.collection_id, episode.dataset_name]));
      for (const [id, name] of groups) $("dataset").add(new Option(name, id));
      for (const root of row.roots) $("root").add(new Option(`${root.id} · ${root.path}`, root.id));
      renderEpisodes();
      const captureId = new URLSearchParams(location.hash.split("?")[1] || "").get("capture");
      const chosen = episodes.find((episode) => episode.id === captureId);
      if (chosen) {
        showSource("captures"); $("dataset").value = chosen.collection_id; renderEpisodes();
        const button = Array.from($("episodes").children).find((entry) => entry.dataset.captureId === chosen.id);
        await select(chosen.root, chosen.path, button);
      } else if (captureId) message("这条采集记录尚未保留或本地文件不可用。");
      if (source === "files") await browse();
      if (!row.enabled) message(row.unavailable_reason || "当前机器人尚未启用回放，可查看数据，但暂不能执行。");
    } catch (error) {
      if (rev === version) {$("connection").textContent = "客户端不可达"; message(error.message); controls();}
    } finally {if (rev === version && visible) timer = setTimeout(poll, 1000);}
  }
  async function poll() {
    if (!visible) return;
    const rev = version;
    try {
      const row = await call("status");
      if (rev === version) render(row);
    } catch (error) {
      if (rev === version) {connected = false; $("connection").textContent = "连接中断"; message(error.message); controls();}
    } finally {if (rev === version && visible) timer = setTimeout(poll, 1000);}
  }
  async function activate(fetchApi, robotId) {
    api = fetchApi;
    visible = true;
    $("robot").textContent = robotId;
    await loadData();
  }
  function deactivate() {visible = false; version++; clearTimeout(timer);}
  $("refresh").onclick = () => void loadData();
  $("dataset").onchange = () => {resetSelection(); renderEpisodes();};
  $("source-captures").onclick = () => {resetSelection(); showSource("captures");};
  $("source-files").onclick = () => {resetSelection(); showSource("files"); void browse();};
  $("root").onchange = () => {resetSelection(); directory = ""; void browse();};
  $("up").onclick = () => {resetSelection(); directory = directory.split("/").slice(0,-1).join("/"); void browse();};
  $("start").onclick = async () => {
    if (!selected || pending || running(latest) || !connected) return;
    if (!latest.simulated && !confirm(`在 ${$("robot").textContent} 上回放 ${selected.path}？机械臂将执行这条轨迹。`)) return;
    const rev = version;
    pending = true; controls(); message();
    requestId ||= Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2,"0")).join("");
    try {
      const row = await call("start", "POST", {root:selected.root, path:selected.path, request_id:requestId});
      if (rev === version) {render(row); requestId = null;}
    } catch (error) {if (rev === version) message(error.message);}
    finally {pending = false; controls();}
  };
  $("stop").onclick = async () => {
    const rev = version;
    pending = true; controls(); message();
    try {const row = await call("stop", "POST", {}); if (rev === version) render(row);}
    catch (error) {if (rev === version) message(error.message);}
    finally {pending = false; controls();}
  };
  return {activate, deactivate};
})();
