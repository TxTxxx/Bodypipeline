"use strict";
// Automatic annotation page: create Argus review tasks over registered datasets,
// follow their progress and inspect per-episode labels. Everything robot-specific
// arrives from the server's Provider/Profile, never from this page.
(() => {
  const $ = (id) => document.getElementById(id);
  const ACTIVE = new Set(["QUEUED", "RUNNING"]);
  const RESUMABLE = new Set(["PARTIAL", "FAILED", "INTERRUPTED", "CANCELLED"]);
  const STATE_LABELS = {
    QUEUED: "排队中",
    RUNNING: "标注中",
    COMPLETED: "已完成",
    PARTIAL: "部分完成",
    FAILED: "失败",
    INTERRUPTED: "已中断",
    CANCELLED: "已取消",
  };
  const STATE_CLASSES = {
    QUEUED: "busy",
    RUNNING: "busy",
    COMPLETED: "",
    PARTIAL: "warn",
    FAILED: "error",
    INTERRUPTED: "warn",
    CANCELLED: "warn",
  };
  const OUTCOME_LABELS = {
    success: "完成",
    success_then_undone: "完成后被撤销",
    partial: "部分完成",
    failure: "未完成",
    unclear: "无法判断",
  };
  const panel = $("annotation-list")?.closest("[data-server-panel]");
  if (!panel) return;

  let ui = null;
  let options = null;
  let candidates = [];
  let rows = [];
  let selectedId = null;
  let detailRow = null;
  let episodes = [];
  let logs = ["run"];
  let currentLog = "run";
  let visible = false;
  let bound = false;
  let timer = null;
  let initializing = null;
  let loading = false;
  let detailLoading = false;
  let submitting = false;
  let capTouched = false;
  let listSignature = "";
  let detailSignature = "";
  let resumeTarget = null;
  let modelFetching = false;
  let modelFetchTimer = null;
  let modelFetchPromise = null;
  let modelFetchedFor = "";

  const esc = (value) => ui?.esc(value) ?? String(value ?? "");
  const money = (value) => {
    const n = Number(value);
    return Number.isFinite(n) ? "$" + n.toFixed(4) : "—";
  };
  const time = (value) => {
    const n = Number(value);
    return n ? new Date(n * 1000).toLocaleString("zh-CN") : "—";
  };
  function stateBadge(state) {
    const label = STATE_LABELS[state] || state;
    return `<span class="badge ${STATE_CLASSES[state] || ""}">${esc(label)}</span>`;
  }
  function selectedCandidate() {
    return candidates.find((item) => item.job_id === $("annotation-dataset").value) || null;
  }
  function scopedCandidates() {
    const scope = $("server-robot-type")?.value || "";
    return scope ? candidates.filter((item) => item.robot_type === scope) : [];
  }
  function activeCount() {
    return rows.filter((row) => ACTIVE.has(row.state)).length;
  }

  function modelInputs() {
    return {
      baseUrl: $("annotation-base-url").value.trim(),
      apiKey: $("annotation-api-key").value.trim(),
    };
  }

  function modelSignature() {
    const { baseUrl, apiKey } = modelInputs();
    return baseUrl + "\n" + apiKey;
  }

  function canFetchModels() {
    if (!options?.available) return false;
    const { baseUrl, apiKey } = modelInputs();
    return Boolean(
      (baseUrl || options.gateway_configured) &&
        (apiKey || options.api_key_configured),
    );
  }

  function setModelStatus(text) {
    $("annotation-model-status").textContent = text;
  }

  function updateModelControls() {
    const button = $("annotation-model-refresh");
    const enabled = Boolean(options?.available) && canFetchModels() && !modelFetching;
    button.disabled = !enabled;
    button.textContent = modelFetching ? "正在获取…" : "获取模型列表";
    updateSubmit();
  }

  function renderModelOptions(models) {
    const select = $("annotation-model");
    const previous = select.value;
    select.innerHTML = models.length
      ? models
          .map((model) => `<option value="${esc(model)}">${esc(model)}</option>`)
          .join("")
      : '<option value="">网关未返回模型</option>';
    if (models.includes(previous)) {
      select.value = previous;
    } else {
      select.value = models[0] || "";
    }
    updateSubmit();
  }

  async function fetchModels(force = false) {
    if (!canFetchModels()) return null;
    if (modelFetching) return modelFetchPromise;
    const signature = modelSignature();
    if (!force && signature === modelFetchedFor) return null;
    const { baseUrl, apiKey } = modelInputs();
    modelFetching = true;
    updateModelControls();
    setModelStatus("正在从网关获取模型列表…");
    modelFetchPromise = (async () => {
      try {
        const result = await ui.api("/api/annotations/models", "POST", {
          base_url: baseUrl || null,
          api_key: apiKey || null,
        });
        if (signature !== modelSignature()) return;
        const models = Array.isArray(result.models)
          ? result.models.filter((model) => typeof model === "string" && model.trim())
          : [];
        renderModelOptions(models);
        modelFetchedFor = signature;
        setModelStatus(
          models.length
            ? `已从网关获取 ${models.length} 个模型。`
            : "网关未返回模型；请确认 baseURL 指向网关根地址。",
        );
      } catch (error) {
        if (signature === modelSignature()) {
          modelFetchedFor = "";
          setModelStatus("获取模型列表失败：" + error.message);
        }
      } finally {
        modelFetching = false;
        modelFetchPromise = null;
        updateModelControls();
        if (signature !== modelSignature() && canFetchModels()) {
          scheduleModelFetch();
        }
      }
    })();
    return modelFetchPromise;
  }

  function scheduleModelFetch() {
    clearTimeout(modelFetchTimer);
    modelFetchTimer = null;
    updateModelControls();
    if (!options?.available) return;
    const { baseUrl, apiKey } = modelInputs();
    if (!(baseUrl || options.gateway_configured) || !(apiKey || options.api_key_configured)) {
      setModelStatus("填写 baseURL 与 API Key 后自动获取网关模型；未填写时使用服务器允许列表。");
      return;
    }
    setModelStatus("准备从网关获取模型列表…");
    modelFetchTimer = setTimeout(() => {
      modelFetchTimer = null;
      void fetchModels(false);
    }, 400);
  }

  function configure({ api, notice, esc: escFn, bytes }) {
    ui = { api, notice, esc: escFn, bytes };
    if (!bound) {
      bind();
      bound = true;
    }
    void init();
  }

  function bind() {
    $("annotation-form").addEventListener("submit", submit);
    $("server-robot-type")?.addEventListener("change", () => {
      renderDatasetOptions();
      updateSubmit();
    });
    $("annotation-dataset").addEventListener("change", () => {
      capTouched = false;
      renderDatasetSummary();
      renderBudget();
      updateSubmit();
    });
    $("annotation-cap").addEventListener("input", () => {
      capTouched = true;
    });
    $("annotation-api-key").addEventListener("input", updateModelControls);
    $("annotation-base-url").addEventListener("input", updateModelControls);
    $("annotation-api-key").addEventListener("change", scheduleModelFetch);
    $("annotation-base-url").addEventListener("change", scheduleModelFetch);
    $("annotation-model-refresh").addEventListener("click", () => {
      void fetchModels(true);
    });
    $("annotation-resume-form").addEventListener("submit", submitResume);
    $("annotation-resume-close").addEventListener("click", closeResume);
    $("annotation-resume-cancel").addEventListener("click", closeResume);
    $("annotation-resume-dialog").addEventListener("close", () => {
      resumeTarget = null;
      $("annotation-resume-api-key").value = "";
    });
    $("annotation-detail-close").addEventListener("click", () => {
      selectedId = null;
      detailRow = null;
      $("annotation-detail-panel").hidden = true;
    });
    $("annotation-log-select").addEventListener("change", (event) => {
      currentLog = event.target.value || "run";
      void loadLog(currentLog);
    });
    panel.addEventListener("click", (event) => {
      const select = event.target.closest("[data-annotation-select]");
      if (select) {
        selectAnnotation(select.dataset.annotationSelect);
        return;
      }
      const cancel = event.target.closest("[data-annotation-cancel]");
      if (cancel) {
        void cancelAnnotation(cancel.dataset.annotationCancel);
        return;
      }
      const resume = event.target.closest("[data-annotation-resume]");
      if (resume) {
        openResume(resume.dataset.annotationResume);
        return;
      }
      if (event.target.closest("[data-annotation-reload]")) {
        void loadDetail(true);
        return;
      }
      if (event.target.closest("[data-annotation-log-refresh]")) {
        void loadLog(currentLog);
        return;
      }
      const episode = event.target.closest("[data-annotation-episode]");
      if (episode) {
        void showEpisode(episode.dataset.annotationEpisode);
      }
    });
  }

  async function init() {
    if (!ui) return;
    if (initializing) return initializing;
    initializing = (async () => {
      try {
        options = await ui.api("/api/annotations/options");
        applyOptions();
      } catch (error) {
        showUnavailable(error.message);
      }
    })();
    try {
      await initializing;
    } finally {
      initializing = null;
    }
  }

  function setFormEnabled(enabled) {
    [
      "annotation-dataset",
      "annotation-model",
      "annotation-base-url",
      "annotation-api-key",
      "annotation-cap",
      "annotation-concurrency",
      "annotation-reasoning",
      "annotation-inspection",
      "annotation-sample-fps",
      "annotation-max-tokens",
      "annotation-field-dictionary",
      "annotation-note",
    ].forEach((id) => {
      $(id).disabled = !enabled;
    });
    updateSubmit();
    updateModelControls();
  }

  function applyOptions() {
    if (!options) return;
    const defaults = options.defaults || {};
    $("annotation-model").innerHTML = (options.models || [])
      .map((model) => `<option value="${esc(model)}">${esc(model)}</option>`)
      .join("") || '<option value="">服务器未配置模型</option>';
    $("annotation-model").value = options.default_model || (options.models || [])[0] || "";
    $("annotation-cap").value = Number(defaults.cap_usd || 0).toFixed(2);
    $("annotation-concurrency").value = String(defaults.concurrency || 8);
    $("annotation-reasoning").value = defaults.reasoning || "";
    $("annotation-inspection").value = defaults.inspection || "auto";
    $("annotation-sample-fps").value = defaults.sample_fps ? String(defaults.sample_fps) : "";
    $("annotation-max-tokens").value = String(defaults.max_tokens || 12000);
    $("annotation-field-dictionary").checked = Boolean(defaults.field_dictionary);
    $("annotation-note").value = defaults.note || "";
    const defaultBaseUrl = String(options.default_base_url || "").trim();
    if (defaultBaseUrl) {
      $("annotation-base-url").value = defaultBaseUrl;
    }
    const needsKey = !options.api_key_configured;
    $("annotation-api-key").required = needsKey;
    $("annotation-api-key-required").hidden = !needsKey;
    $("annotation-base-url").placeholder = options.gateway_configured
      ? "留空使用服务器默认 baseURL"
      : "https://gateway.example.com/v1";
    capTouched = false;
    const available = Boolean(options.available);
    setFormEnabled(available);
    if (available) {
      $("annotation-unavailable").hidden = true;
      $("annotation-help").textContent = options.api_key_configured
        ? "提交后在独立队列执行，不会阻塞转换或 Doctor；服务器已配置默认模型密钥，本次可留空。"
        : "提交后在独立队列执行，不会阻塞转换或 Doctor；请填写本次任务的模型 API Key。";
    } else {
      showUnavailable(options.reason || "服务器未找到可用的 Argus");
    }
    renderBudget();
    updateModelControls();
    if (
      available &&
      options.gateway_configured &&
      options.api_key_configured &&
      !modelFetchedFor
    ) {
      scheduleModelFetch();
    }
  }

  function showUnavailable(reason) {
    setFormEnabled(false);
    const box = $("annotation-unavailable");
    box.hidden = false;
    box.textContent = "自动标注暂不可用：" + reason;
    $("annotation-help").textContent = "部署 Argus 后重启 Server 即可使用。";
    setModelStatus("自动标注不可用，无法获取模型列表。");
  }

  function renderDatasetOptions() {
    const select = $("annotation-dataset");
    const list = scopedCandidates();
    const scope = $("server-robot-type")?.value || "";
    const previous = select.value;
    const html = !scope
      ? '<option value="">请先选择机器人类型</option>'
      : list.length
        ? list
            .map((item) => {
              const active = item.annotation_active ? " · 标注中" : "";
              const last = item.annotation_last?.state
                ? ` · 上次 ${STATE_LABELS[item.annotation_last.state] || item.annotation_last.state}`
                : "";
              return `<option value="${esc(item.job_id)}">${esc(item.dataset_name)} · ${esc(item.episode_count ?? "?")} episodes · LeRobot ${esc(item.codebase_version || "?")}${esc(active + last)}</option>`;
            })
            .join("")
        : '<option value="">该机器人类型下没有可标注的数据集</option>';
    if (select.dataset.options !== html) {
      select.dataset.options = html;
      select.innerHTML = html;
    }
    select.value = list.some((item) => item.job_id === previous)
      ? previous
      : list.length
        ? list[0].job_id
        : "";
    renderDatasetSummary();
    renderBudget();
  }

  function renderDatasetSummary() {
    const item = selectedCandidate();
    const box = $("annotation-dataset-summary");
    if (!item) {
      box.textContent = "请选择机器人类型和已登记的数据集。";
      return;
    }
    const last = item.annotation_last
      ? `<span class="sub">上次任务：${esc(STATE_LABELS[item.annotation_last.state] || item.annotation_last.state)} · ${esc(item.annotation_last.model || "—")} · ${money(item.annotation_last.cost_usd)}</span>`
      : "";
    box.innerHTML =
      `<strong>${esc(item.dataset_name)}</strong>` +
      `<span class="sub">${esc(item.episode_count ?? "?")} episodes · ${esc(item.frame_count ?? "?")} 帧 · LeRobot ${esc(item.codebase_version || "?")}</span>` +
      `<span class="sub">${esc(item.dataset_path)}</span>` +
      last;
  }

  function renderBudget() {
    const box = $("annotation-budget");
    if (!options) {
      box.textContent = "正在读取预算…";
      return;
    }
    const defaults = options.defaults || {};
    const item = selectedCandidate();
    const count = Math.max(1, Number(item?.episode_count || 1));
    const perEpisode = Number(defaults.per_episode_budget_usd || 0);
    const dailyCap = Number(defaults.daily_cap_usd || 0);
    const used = Number(options.used_today_usd || 0);
    const taskCap = Math.min(Number(defaults.cap_usd || 0), perEpisode * count);
    const available = Math.max(0, Math.min(taskCap, dailyCap - used));
    box.textContent =
      `本数据集最多 ${money(taskCap)}（${money(perEpisode)} × ${count}）；` +
      `今日已结算 ${money(used)} / ${money(dailyCap)}，当前可用约 ${money(available)}。`;
    const cap = $("annotation-cap");
    cap.max = available > 0 ? available.toFixed(2) : String(taskCap.toFixed(2));
    if (!capTouched) {
      const limit = available > 0 ? available : taskCap;
      cap.value = Math.max(0.01, Math.min(Number(cap.value || taskCap || 0.01), limit || 0.01)).toFixed(2);
    }
  }

  function updateSubmit() {
    const button = $("annotation-submit");
    const needsKey = !options?.api_key_configured;
    const usable =
      Boolean(options?.available) &&
      Boolean($("annotation-dataset").value) &&
      Boolean($("annotation-model").value) &&
      (!needsKey || Boolean($("annotation-api-key").value.trim())) &&
      !modelFetching &&
      !submitting;
    button.disabled = !usable;
    button.textContent = submitting ? "正在提交…" : "提交自动标注";
  }

  async function refresh() {
    if (!ui || loading) return;
    if (!options) await init();
    if (!options) return;
    loading = true;
    try {
      const [candidateResult, list, freshOptions] = await Promise.all([
        ui.api("/api/annotations/candidates"),
        ui.api("/api/annotations"),
        ui.api("/api/annotations/options"),
      ]);
      const wasAvailable = Boolean(options?.available);
      options = freshOptions || options;
      candidates = candidateResult.datasets || [];
      rows = list || [];
      if (options && !options.available) {
        showUnavailable(options.reason || "服务器未找到可用的 Argus");
      } else if (!wasAvailable && options?.available) {
        applyOptions();
      }
      renderDatasetOptions();
      renderList();
      $("nav-annotation-count").textContent = String(activeCount());
      if (selectedId) {
        const row = rows.find((item) => item.id === selectedId);
        if (row) {
          detailRow = row;
          renderDetail();
          await loadDetail(false);
        } else {
          selectedId = null;
          detailRow = null;
          $("annotation-detail-panel").hidden = true;
        }
      }
    } catch (error) {
      if (visible) ui.notice(error.message, true);
    } finally {
      loading = false;
      updateSubmit();
    }
  }

  function renderList() {
    const body = $("annotation-list");
    const html = rows.length
      ? rows
          .map((row) => {
            const total = Number(row.input_episode_count || row.units || row.episode_count || 0);
            const done = Number(row.episodes_done || 0);
            const percent = total ? Math.min(100, Math.round((100 * done) / total)) : 0;
            const progress = `${done} / ${total || "?"}`;
            const actions = [
              `<button class="secondary" data-annotation-select="${esc(row.id)}">详情</button>`,
              ACTIVE.has(row.state)
                ? `<button class="danger" data-annotation-cancel="${esc(row.id)}" ${row.cancel_requested ? "disabled" : ""}>${row.cancel_requested ? "取消中" : "取消"}</button>`
                : "",
              RESUMABLE.has(row.state)
                ? `<button class="secondary" data-annotation-resume="${esc(row.id)}">续跑</button>`
                : "",
              row.episodes_done || row.episode_count
                ? `<a class="secondary" href="/api/annotations/${esc(row.id)}/export" download>导出</a>`
                : "",
            ]
              .filter(Boolean)
              .join("");
            return `<tr>
              <td><strong class="task-name">${esc(row.dataset_name)}</strong><span class="sub">${esc(row.id.slice(0, 12))} · ${time(row.created_at)}</span><span class="sub">${esc(row.robot_type || "未知机器人")} · ${esc(row.rig || "")}</span></td>
              <td>${esc(row.model || "—")}<span class="sub">${esc(row.note ? row.note.slice(0, 48) : "无额外说明")}</span></td>
              <td>${esc(progress)}<span class="sub">${esc(row.stage || "")}</span><div class="progress"><span data-annotation-width="${percent}"></span></div></td>
              <td>${money(row.cost_usd)}<span class="sub">上限 ${money(row.cap_usd)}</span></td>
              <td>${stateBadge(row.state)}${row.error ? `<span class="sub job-error">${esc(row.error.slice(0, 120))}</span>` : ""}</td>
              <td><div class="action-row">${actions}</div></td>
            </tr>`;
          })
          .join("")
      : '<tr><td colspan="6" class="empty">选择机器人类型和数据集后，提交第一个自动标注任务。</td></tr>';
    if (listSignature !== html) {
      listSignature = html;
      body.innerHTML = html;
      body.querySelectorAll("[data-annotation-width]").forEach((element) => {
        element.style.width = element.dataset.annotationWidth + "%";
      });
    }
  }

  function selectAnnotation(id) {
    selectedId = id;
    detailRow = rows.find((row) => row.id === id) || null;
    episodes = [];
    logs = ["run"];
    currentLog = "run";
    $("annotation-episode-detail").hidden = true;
    $("annotation-log-output").textContent = "正在读取日志…";
    $("annotation-log-select").innerHTML = '<option value="run">run</option>';
    $("annotation-episode-list").innerHTML = '<tr><td colspan="6" class="empty">正在读取 Episode 结果…</td></tr>';
    $("annotation-detail-panel").hidden = false;
    renderDetail();
    $("annotation-detail-panel").scrollIntoView({ behavior: "smooth", block: "start" });
    void loadDetail(true);
  }

  function renderDetail() {
    if (!detailRow) return;
    const row = detailRow;
    const total = Number(row.input_episode_count || row.units || row.episode_count || 0);
    const done = Number(row.episodes_done || 0);
    const percent = total ? Math.min(100, Math.round((100 * done) / total)) : 0;
    const actions = [
      `<button class="secondary" type="button" data-annotation-reload>刷新</button>`,
      ACTIVE.has(row.state)
        ? `<button class="danger" type="button" data-annotation-cancel="${esc(row.id)}" ${row.cancel_requested ? "disabled" : ""}>${row.cancel_requested ? "正在取消" : "取消任务"}</button>`
        : "",
      RESUMABLE.has(row.state)
        ? `<button class="secondary" type="button" data-annotation-resume="${esc(row.id)}">续跑未完成 Episode</button>`
        : "",
      row.episodes_done || row.episode_count
        ? `<a class="secondary" href="/api/annotations/${esc(row.id)}/export" download>导出完整结果</a>`
        : "",
    ]
      .filter(Boolean)
      .join("");
    const html = `
      <div class="annotation-summary">
        <div><span class="small muted">状态</span><div>${stateBadge(row.state)} ${esc(row.stage || "")}</div></div>
        <div><span class="small muted">进度</span><div>${done} / ${total || "?"} Episodes</div><div class="progress"><span data-annotation-width="${percent}"></span></div></div>
        <div><span class="small muted">已结算费用</span><div>${money(row.cost_usd)} / 上限 ${money(row.cap_usd)}</div><span class="sub">${row.cost_complete ? "费用完整" : "费用仍在结算或为估算"} · 预留 ${money(row.reserved_cost_usd)}</span></div>
        <div><span class="small muted">Episode 失败</span><div>${row.episodes_failed || 0}</div><span class="sub">退出码 ${esc(row.exit_code ?? "—")}</span></div>
      </div>
      <div class="annotation-meta small muted">
        <span>数据集：${esc(row.dataset_name)} · ${esc(row.robot_type || "未知机器人")} · rig ${esc(row.rig || "—")}</span>
        <span>模型：${esc(row.model || "—")} · 并发 ${esc(row.concurrency ?? "—")} · 推理 ${esc(row.reasoning || "默认")} · 采样 ${esc(row.sample_fps || "默认")}</span>
        <span>路径：${esc(row.dataset_path || "—")}</span>
        <span>创建 ${time(row.created_at)} · 开始 ${time(row.started_at)} · 结束 ${time(row.finished_at)}</span>
      </div>
      ${row.note ? `<p class="annotation-note small"><strong>任务说明：</strong>${esc(row.note)}</p>` : ""}
      ${row.error ? `<p class="warning annotation-error">${esc(row.error)}</p>` : ""}
      <div class="action-row annotation-actions">${actions}</div>
    `;
    if (detailSignature !== html) {
      detailSignature = html;
      $("annotation-detail").innerHTML = html;
      $("annotation-detail").querySelectorAll("[data-annotation-width]").forEach((element) => {
        element.style.width = element.dataset.annotationWidth + "%";
      });
    }
    $("annotation-detail-title").textContent = (row.dataset_name || "数据集") + " · 标注详情";
    $("annotation-detail-panel").hidden = false;
  }

  async function loadDetail(force) {
    const id = selectedId;
    if (!id || detailLoading) return;
    detailLoading = true;
    try {
      const [row, result, logList] = await Promise.all([
        ui.api(`/api/annotations/${id}`),
        ui.api(`/api/annotations/${id}/episodes`),
        ui.api(`/api/annotations/${id}/logs`),
      ]);
      if (selectedId !== id) return;
      detailRow = row;
      episodes = result.episodes || [];
      logs = logList.logs?.length ? logList.logs : ["run"];
      renderDetail();
      renderEpisodes();
      renderLogOptions();
      if (force) await loadLog(currentLog);
    } catch (error) {
      if (visible) ui.notice(error.message, true);
    } finally {
      detailLoading = false;
    }
  }

  function renderLogOptions() {
    const select = $("annotation-log-select");
    const signature = logs.join("|");
    if (select.dataset.logs !== signature) {
      select.dataset.logs = signature;
      select.innerHTML = logs.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join("");
    }
    if (!logs.includes(currentLog)) currentLog = logs[0] || "run";
    select.value = currentLog;
  }

  async function loadLog(name) {
    const id = selectedId;
    if (!id) return;
    try {
      const result = await ui.api(`/api/annotations/${id}/logs/${encodeURIComponent(name)}`);
      if (selectedId !== id) return;
      $("annotation-log-output").textContent = result.text || "(日志为空)";
    } catch (error) {
      $("annotation-log-output").textContent = error.message;
    }
  }

  function renderEpisodes() {
    const body = $("annotation-episode-list");
    const count = episodes.length;
    $("annotation-episode-count").textContent = count ? `${count} 个 Episode` : "尚无结果";
    if (!count) {
      body.innerHTML = '<tr><td colspan="6" class="empty">Argus 尚未生成可读取的 Episode 标注。运行中可稍后刷新。</td></tr>';
      return;
    }
    body.innerHTML = episodes
      .map((item) => {
        const outcome =
          item.outcome ||
          (item.completed === true ? "success" : item.completed === false ? "failure" : "");
        const completion = outcome
          ? (OUTCOME_LABELS[outcome] || outcome) +
            (item.failure_kind ? ` · ${esc(item.failure_kind)}` : "")
          : "—";
        const labels = item.labels_available
          ? "可用"
          : `<span class="job-error">${esc(item.label_failure?.why || "标注失败")}</span>`;
        return `<tr>
          <td><strong class="task-name">${esc(item.episode_id || item.id)}</strong><span class="sub">${esc(item.id)} · ${esc(item.duration_s ? Number(item.duration_s).toFixed(1) + " 秒" : "时长未知")}</span></td>
          <td>${esc(item.prompt || "—")}</td>
          <td>${esc(completion)}</td>
          <td>${esc(item.events || 0)} 事件<span class="sub">${esc(item.key_events || 0)} 关键动作 · ${esc(item.state_changes || 0)} 状态变化</span></td>
          <td>${esc(item.model || "—")}</td>
          <td>${labels}<button class="text-button" type="button" data-annotation-episode="${esc(item.id)}">查看原始结果</button></td>
        </tr>`;
      })
      .join("");
  }

  async function showEpisode(episodeId) {
    const id = selectedId;
    if (!id) return;
    const box = $("annotation-episode-detail");
    try {
      const result = await ui.api(`/api/annotations/${id}/episodes/${encodeURIComponent(episodeId)}`);
      if (selectedId !== id) return;
      box.hidden = false;
      box.open = true;
      box.querySelector("summary").textContent = "Episode " + episodeId + " 原始结果";
      $("annotation-episode-json").textContent = JSON.stringify(result, null, 2);
      box.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      ui.notice(error.message, true);
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (submitting) return;
    const jobId = $("annotation-dataset").value;
    if (!jobId) {
      ui.notice("请先选择数据集。", true);
      return;
    }
    if (canFetchModels() && modelFetchedFor !== modelSignature()) {
      // Do not submit a model from a previous gateway or a stale field value.
      await fetchModels(false);
    }
    if (!$("annotation-model").value) {
      ui.notice("请先获取并选择标注模型。", true);
      return;
    }
    const sample = $("annotation-sample-fps").value;
    const sampleFps = sample === "" ? null : Number(sample);
    const baseUrl = $("annotation-base-url").value.trim();
    const apiKey = $("annotation-api-key").value.trim();
    const body = {
      job_id: jobId,
      model: $("annotation-model").value,
      cap_usd: Number($("annotation-cap").value),
      concurrency: Number($("annotation-concurrency").value),
      reasoning: $("annotation-reasoning").value || null,
      inspection: $("annotation-inspection").value,
      sample_fps: sampleFps && sampleFps > 0 ? sampleFps : null,
      field_dictionary: $("annotation-field-dictionary").checked,
      max_tokens: Number($("annotation-max-tokens").value),
      note: $("annotation-note").value.trim() || null,
      base_url: baseUrl || null,
      api_key: apiKey || null,
    };
    submitting = true;
    updateSubmit();
    try {
      const row = await ui.api("/api/annotations", "POST", body);
      // The key is not part of the task record; do not keep it in the page
      // either once the server has accepted the run.
      $("annotation-api-key").value = "";
      updateModelControls();
      selectedId = row.id;
      ui.notice(`已提交「${row.dataset_name}」自动标注：${row.model}，费用上限 ${money(row.cap_usd)}。`);
      await refresh();
      selectAnnotation(row.id);
    } catch (error) {
      ui.notice(error.message, true);
    } finally {
      submitting = false;
      updateSubmit();
    }
  }

  async function cancelAnnotation(id) {
    if (!confirm("确认取消该自动标注？当前模型请求会终止，已产生费用和已完成结果保留。")) return;
    try {
      await ui.api(`/api/annotations/${id}/cancel`, "POST");
      ui.notice("已请求取消自动标注。");
      await refresh();
      if (selectedId === id) await loadDetail(true);
    } catch (error) {
      ui.notice(error.message, true);
    }
  }

  function openResume(id) {
    const row = rows.find((item) => item.id === id) || detailRow;
    if (!row) return;
    resumeTarget = id;
    const configured = Boolean(options?.api_key_configured);
    $("annotation-resume-base-url").value = row.base_url || "";
    $("annotation-resume-api-key").value = "";
    $("annotation-resume-api-key").required = !configured;
    $("annotation-resume-hint").textContent = configured
      ? "服务器已配置默认模型密钥；API Key 可留空，也可为本次续跑填写专用密钥。"
      : "服务器未配置默认模型密钥，必须填写本次续跑的 API Key。";
    $("annotation-resume-dialog").showModal();
  }

  function closeResume() {
    resumeTarget = null;
    $("annotation-resume-api-key").value = "";
    $("annotation-resume-dialog").close();
  }

  async function submitResume(event) {
    event.preventDefault();
    const id = resumeTarget;
    if (!id) return;
    const baseUrl = $("annotation-resume-base-url").value.trim();
    const apiKey = $("annotation-resume-api-key").value.trim();
    const body = {};
    if (baseUrl) body.base_url = baseUrl;
    if (apiKey) body.api_key = apiKey;
    const button = $("annotation-resume-submit");
    button.disabled = true;
    try {
      await ui.api(`/api/annotations/${id}/resume`, "POST", body);
      closeResume();
      ui.notice("已重新加入标注队列，将沿用已完成的分段和结果。");
      await refresh();
      if (selectedId === id) await loadDetail(true);
    } catch (error) {
      ui.notice(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function show() {
    visible = true;
    void init();
    void refresh();
    if (!timer) {
      timer = setInterval(() => {
        if (visible && !document.hidden) void refresh();
      }, 5000);
    }
  }

  function hide() {
    visible = false;
    clearInterval(timer);
    timer = null;
  }

  window.AnnotationUI = { configure, show, hide };
})();
