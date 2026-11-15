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
  let templateLibrary = { revision: 0, templates: [] };
  let templateChoiceInitialized = false;
  let candidates = [];
  let rows = [];
  let selectedId = null;
  let detailRow = null;
  let episodes = [];
  let logs = ["run"];
  let currentLog = "run";
  let viewerEpisode = null;
  let viewerData = null;
  let viewerClips = [];
  let viewerCamera = null;
  let viewerTime = 0;
  let viewerToken = 0;
  let visible = false;
  let bound = false;
  let timer = null;
  let initializing = null;
  let loading = false;
  let detailLoading = false;
  let submitting = false;
  let capTouched = false;
  let listSignature = "";
  let subtask = null;
  let subtaskLoading = false;
  let subtaskSubmitting = false;
  let subtaskName = "";
  let subtaskSignature = "";
  let detailSignature = "";
  let resumeTarget = null;
  let modelFetching = false;
  let modelFetchTimer = null;
  let modelFetchPromise = null;
  let modelFetchedFor = "";

  const esc = (value) => ui?.esc(value) ?? String(value ?? "");
  const money = (value) => {
    if (value == null) return "未知";
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
  const isCodex = () => $("annotation-backend").value === "codex";
  const backendAvailable = () => Boolean(isCodex() ? options?.backends?.codex?.available : options?.available);
  const backendLabel = (row) => row.backend === "codex" ? "本机 Codex" : "API / Argus";

  function renderTemplates() {
    const select = $("annotation-template");
    const previous = select.value;
    templateLibrary = options?.template_library || templateLibrary;
    const templates = templateLibrary.templates || [];
    select.innerHTML = '<option value="">自由分析（不使用模板）</option>' + templates.map(
      (t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`,
    ).join("");
    select.options[0].disabled = isCodex();
    if (isCodex()) select.options[0].textContent = "自由分析请使用 API / Argus";
    if (previous && !templates.some((t) => t.id === previous)) {
      select.insertAdjacentHTML("beforeend", `<option value="${esc(previous)}">原模板已删除，请重新选择</option>`);
    }
    select.value = !templateChoiceInitialized && templates.length ? templates[0].id : previous;
    if (templates.length) templateChoiceInitialized = true;
    renderTemplateSummary();
  }

  function renderTemplateSummary() {
    const id = $("annotation-template").value;
    const template = templateLibrary.templates.find((t) => t.id === id);
    $("annotation-boundary-options").hidden = !template;
    $("annotation-template-summary").textContent = template
      ? `模板标注：${template.subtasks.length} 个固定 Subtask · ${template.fields.length} 个自定义字段。独立尝试与片段，专用提示词与业务校验，AI 初稿待人工核对。之后修改共享模板不会改变这次快照。${options?.template_available ? "" : "服务器需重新运行 scripts/install_argus.sh 安装模板支持。"}`
      : id ? "原模板已删除，请刷新并重新选择。" : "自由分析保留 Argus 原生提示词、解析、结果与可直接导出的原生分析，不需要模板。模板标注使用固定 Subtask 和自定义字段。";
    updateSubmit();
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
    if (isCodex()) return "codex";
    const { baseUrl, apiKey } = modelInputs();
    return baseUrl + "\n" + apiKey;
  }

  function canFetchModels() {
    if (isCodex()) return false;
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

  function pricedModels() {
    // A spend-capped run can only dispatch a model whose rate the server (or
    // Argus's own table) knows; the server rejects the rest with the same rule.
    const names = options?.priced_models;
    return Array.isArray(names) ? new Set(names) : null;
  }

  function modelPriced(model) {
    const priced = pricedModels();
    if (!priced) return true;
    return priced.has(String(model || "").split("/").pop());
  }

  function renderModelOptions(models) {
    const select = $("annotation-model");
    const previous = select.value;
    const priced = pricedModels();
    select.innerHTML = models.length
      ? models
          .map((model) => {
            const base = String(model).split("/").pop();
            const note = priced && !priced.has(base) ? " · 无单价" : "";
            return `<option value="${esc(model)}">${esc(model + note)}</option>`;
          })
          .join("")
      : '<option value="">网关未返回模型</option>';
    if (models.includes(previous)) {
      select.value = previous;
    } else {
      select.value = models[0] || "";
    }
    updateModelPricing();
    updateSubmit();
  }

  function updateModelPricing() {
    const model = $("annotation-model").value;
    const status = $("annotation-model-price");
    if (!status) return;
    if (isCodex()) { status.hidden = true; return; }
    if (!model || modelPriced(model)) {
      status.textContent = "";
      status.hidden = true;
      return;
    }
    const names = Array.from(pricedModels() || []);
    status.hidden = false;
    if (options?.unlimited) {
      status.textContent = "该模型没有已知单价，费用统计可能不完整；当前自动标注不限额。";
      return;
    }
    status.textContent =
      "该模型没有已知单价，费用上限无法保证；请服务器在 argus_prices 中补充单价，或改用：" +
      (names.join("、") || "已知单价的模型");
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
    if (isCodex()) return;
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
    $("annotation-backend").addEventListener("change", () => {
      clearTimeout(modelFetchTimer);
      modelFetchedFor = "";
      applyOptions();
    });
    $("annotation-scope").addEventListener("change", () => { $("annotation-selected-episodes").disabled = $("annotation-scope").value === "full"; updateSubmit(); });
    $("annotation-template").addEventListener("change", () => {
      templateChoiceInitialized = true;
      renderTemplateSummary();
    });
    $("annotation-template-refresh").addEventListener("click", async () => {
      const button = $("annotation-template-refresh");
      button.disabled = true;
      try {
        const library = await ui.api("/api/task-templates");
        options.template_library = library;
        renderTemplates();
      } catch (error) { ui.notice(error.message, true); }
      finally { button.disabled = false; }
    });
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
    $("annotation-model").addEventListener("change", () => {
      updateModelPricing();
      updateSubmit();
    });
    $("annotation-resume-form").addEventListener("submit", submitResume);
    $("annotation-resume-close").addEventListener("click", closeResume);
    $("annotation-resume-cancel").addEventListener("click", closeResume);
    $("annotation-resume-dialog").addEventListener("close", () => {
      resumeTarget = null;
      $("annotation-resume-api-key").value = "";
    });
    $("annotation-detail-close").addEventListener("click", () => {
      closeViewer();
      selectedId = null;
      detailRow = null;
      $("annotation-detail-panel").hidden = true;
    });
    $("annotation-log-select").addEventListener("change", (event) => {
      currentLog = event.target.value || "run";
      void loadLog(currentLog);
    });
    panel.addEventListener("input", (event) => {
      if (event.target.matches("[data-subtask-name]")) {
        subtaskName = event.target.value;
      }
    });
    panel.addEventListener("click", (event) => {
      const select = event.target.closest("[data-annotation-select]");
      if (select) {
        selectAnnotation(select.dataset.annotationSelect);
        return;
      }
      const workbench = event.target.closest("[data-annotation-workbench]");
      if (workbench) {
        void ui.api(`/api/annotations/${workbench.dataset.annotationWorkbench}/workbench`, "POST", {}).then(result => { window.location.href = result.url; }).catch(error => ui.notice(error.message, true));
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
      if (event.target.closest("[data-subtask-refresh]")) {
        void loadSubtask(true);
        return;
      }
      if (event.target.closest("[data-subtask-scroll]")) {
        $("annotation-subtask-panel").scrollIntoView({ behavior: "smooth", block: "start" });
        void loadSubtask(true);
        return;
      }
      if (event.target.closest("[data-subtask-export]")) {
        void startSubtaskExport();
        return;
      }
      if (event.target.closest("[data-annotation-viewer-close]")) {
        closeViewer();
        return;
      }
      const camera = event.target.closest("[data-viewer-camera]");
      if (camera) {
        selectViewerCamera(camera.dataset.viewerCamera);
        return;
      }
      const seek = event.target.closest("[data-viewer-seek]");
      if (seek) {
        seekViewer(Number(seek.dataset.viewerSeek));
        return;
      }
      const timeline = event.target.closest("[data-viewer-timeline]");
      if (timeline) {
        const rect = timeline.getBoundingClientRect();
        const duration = viewerDuration();
        if (rect.width > 0) {
          seekViewer((duration * (event.clientX - rect.left)) / rect.width);
        }
        return;
      }
      const episode = event.target.closest("[data-annotation-episode]");
      if (episode) {
        void showEpisode(episode.dataset.annotationEpisode);
        return;
      }
      const raw = event.target.closest("[data-annotation-episode-json]");
      if (raw) {
        void showEpisodeJson(raw.dataset.annotationEpisodeJson);
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
      "annotation-template",
      "annotation-scope",
      "annotation-boundary-target",
      "annotation-boundary-window",
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

  function applyOptions(preserveValues = false) {
    if (!options) return;
    const formValues = preserveValues ? Object.fromEntries([
      "annotation-model", "annotation-reasoning", "annotation-concurrency",
      "annotation-sample-fps", "annotation-note", "annotation-base-url",
    ].map(id => [id, $(id).value])) : null;
    const cli = isCodex() ? options.backends?.codex : null;
    renderTemplates();
    const defaults = { ...options.defaults, ...(cli?.defaults || {}) };
    const models = cli?.models || options.models || [];
    $("annotation-model").innerHTML = models
      .map((model) => `<option value="${esc(model)}">${esc(model)}</option>`)
      .join("") || '<option value="">服务器未配置模型</option>';
    $("annotation-model").value = cli?.default_model || options.default_model || models[0] || "";
    updateModelPricing();
    $("annotation-cap").value = Number(defaults.cap_usd || 0).toFixed(2);
    $("annotation-concurrency").value = String(defaults.concurrency || 8);
    $("annotation-reasoning").value = cli?.default_reasoning || defaults.reasoning || "";
    $("annotation-inspection").value = cli ? "off" : defaults.inspection || "auto";
    $("annotation-sample-fps").value = defaults.sample_fps ? String(defaults.sample_fps) : "";
    $("annotation-max-tokens").value = cli ? "" : String(defaults.max_tokens || 12000);
    $("annotation-max-tokens").placeholder = cli ? "CLI 自动管理" : "";
    $("annotation-field-dictionary").checked = cli ? false : Boolean(defaults.field_dictionary);
    $("annotation-note").value = defaults.note || "";
    const defaultBaseUrl = String(options.default_base_url || "").trim();
    if (defaultBaseUrl) {
      $("annotation-base-url").value = defaultBaseUrl;
    }
    const needsKey = !cli && !options.api_key_configured;
    $("annotation-api-key").required = needsKey;
    $("annotation-api-key-required").hidden = !needsKey;
    $("annotation-base-url").placeholder = options.gateway_configured
      ? "留空使用服务器默认 baseURL"
      : "https://gateway.example.com/v1";
    capTouched = false;
    const available = backendAvailable();
    setFormEnabled(available);
    $("annotation-gateway-fields").hidden = Boolean(cli);
    $("annotation-base-url").disabled = Boolean(cli) || !available;
    $("annotation-api-key").disabled = Boolean(cli) || !available;
    $("annotation-model-refresh").hidden = Boolean(cli);
    $("annotation-max-tokens").disabled = Boolean(cli) || !available;
    $("annotation-max-tokens").required = !cli;
    $("annotation-token-help").hidden = !cli;
    $("annotation-inspection").disabled = Boolean(cli) || !available;
    $("annotation-field-dictionary").disabled = Boolean(cli) || !available;
    Array.from($("annotation-reasoning").options).forEach(o => { o.disabled = Boolean(cli) && !cli.capabilities?.reasoning?.includes(o.value); });
    $("annotation-backend-status").textContent = cli
      ? "复用服务器上的本机 Codex 登录，支持模板标注，无需网关密钥；实际金额未知。"
      : "API / Argus 使用服务器或本次任务的模型网关。自由分析与模板标注均可用。";
    $("annotation-backend-diagnostics").hidden = !cli;
    $("annotation-backend-diagnostic-text").textContent = cli
      ? `${cli.version || "Codex CLI"} · ${cli.reason || ""} · provider ${cli.provider || "—"}${cli.provider_url ? ` (${cli.provider_url})` : ""}。${cli.notice || ""} 每次请求最多 ${cli.limits?.max_images || "—"} 张采样图像；分段最多 ${cli.limits?.max_part_seconds || "—"} 秒，并按实际相机数与输入预算继续收紧。`
      : "";
    if (cli) setModelStatus("模型来自 CLI / 受信部署允许列表，不请求 API 网关。推理强度会明确传入 CLI。");
    if (available) {
      $("annotation-unavailable").hidden = true;
      $("annotation-help").textContent = cli ? "使用服务器用户的 Codex CLI 登录执行模板批量任务；无需 baseURL 或 API Key。"
        : options.api_key_configured
        ? "提交后在独立队列执行，不会阻塞转换或 Doctor；服务器已配置默认模型密钥，本次可留空。"
        : "提交后在独立队列执行，不会阻塞转换或 Doctor；请填写本次任务的模型 API Key。";
    } else {
      showUnavailable(cli?.reason || options.reason || "服务器未找到可用的 Argus");
    }
    $("annotation-execution-support").textContent = options.execution_contract_v2
      ? options.unlimited
        ? "已支持限定范围，以及每次逻辑请求最多三次自动重试；费用与用量继续记录，当前不限额。"
        : "已支持限定范围，以及每次逻辑请求最多三次自动重试与累计预算预留。"
      : "当前为旧 Argus：可用原生全量分析；试跑、选择范围及新的统一重试和预算契约需要先运行 scripts/install_argus.sh 更新工具链。";
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
    if (formValues) {
      Object.entries(formValues).forEach(([id, value]) => { $(id).value = value; });
      updateModelPricing();
      updateSubmit();
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
    if (isCodex()) {
      const cap = $("annotation-cap");
      cap.required = false; cap.disabled = true; cap.value = ""; cap.placeholder = "不限额";
      box.textContent = "不设任务、每日或 Episode 费用上限。保留 CLI 调用、状态与 token 用量；实际金额未知。";
      $("annotation-boundary-cost").textContent = "复用粗定位与边界加密取证。同一请求最多首次加三次 CLI 调用；CLI 内部连接恢复由 Codex 管理。认证、额度不足及未知调用状态停止自动重发。";
      return;
    }
    $("annotation-boundary-cost").textContent = options.unlimited
      ? "粗定位后在窗口内加密采样，额外调用与重试的费用、用量及未结算状态继续记录；当前不限额。"
      : "粗定位后在窗口内加密采样，额外调用、重试和未结算预留均计入预算。";
    if (options.unlimited) {
      const cap = $("annotation-cap");
      cap.required = false;
      cap.disabled = true;
      cap.value = "";
      cap.placeholder = "不限额";
      cap.removeAttribute("max");
      box.textContent = `自动标注不设单任务、每日或每 Episode 费用上限。今日已结算 ${money(options.used_today_usd)}；实际费用继续统计。`;
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
    const needsKey = !isCodex() && !options?.api_key_configured;
    const model = $("annotation-model").value;
    const usable =
      backendAvailable() &&
      Boolean($("annotation-dataset").value) &&
      ($("annotation-scope").value === "full" || options?.execution_contract_v2) &&
      Boolean(model) &&
      (isCodex() || options?.unlimited || modelPriced(model)) &&
      (!isCodex() || Boolean($("annotation-template").value)) &&
      (!$("annotation-template").value || (options?.template_available && templateLibrary.templates.some((t) => t.id === $("annotation-template").value))) &&
      (!needsKey || Boolean($("annotation-api-key").value.trim())) &&
      (!modelFetching || isCodex()) &&
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
      const wasAvailable = backendAvailable();
      options = freshOptions || options;
      renderTemplates();
      candidates = candidateResult.datasets || [];
      rows = list || [];
      if (options && !backendAvailable()) {
        showUnavailable((isCodex() ? options.backends?.codex?.reason : options.reason) || "后端不可用");
      } else if (!wasAvailable && backendAvailable()) {
        applyOptions(true);
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
              <td>${esc(backendLabel(row))} · ${esc(row.model || "—")}<span class="sub">${esc(row.note ? row.note.slice(0, 48) : "无额外说明")}</span></td>
              <td>${esc(progress)}<span class="sub">${esc(row.stage || "")}</span><div class="progress"><span data-annotation-width="${percent}"></span></div></td>
              <td>${money(row.cost_usd)}<span class="sub">${row.cap_usd == null ? "不限额" : `上限 ${money(row.cap_usd)}`}</span></td>
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
    subtask = null;
    subtaskName = "";
    subtaskSignature = "";
    closeViewer();
    $("annotation-episode-detail").hidden = true;
    $("annotation-log-output").textContent = "正在读取日志…";
    $("annotation-log-select").innerHTML = '<option value="run">run</option>';
    $("annotation-episode-list").innerHTML = '<tr><td colspan="6" class="empty">正在读取 Episode 结果…</td></tr>';
    $("annotation-subtask").innerHTML = '<p class="small muted">正在读取标注覆盖率…</p>';
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
      row.task_template && row.episodes_done ? `<button class="primary" type="button" data-annotation-workbench="${esc(row.id)}">发送独立候选到标注工作区</button>` : "",
      ACTIVE.has(row.state)
        ? `<button class="danger" type="button" data-annotation-cancel="${esc(row.id)}" ${row.cancel_requested ? "disabled" : ""}>${row.cancel_requested ? "正在取消" : "取消任务"}</button>`
        : "",
      (RESUMABLE.has(row.state) || (row.state === "COMPLETED" && ["trial","selection"].includes(row.run_scope)))
        ? `<button class="secondary" type="button" data-annotation-resume="${esc(row.id)}">${row.state === "COMPLETED" ? "明确继续全量 / 复用成功结果" : options?.unlimited ? "续跑未完成请求" : "续跑未完成请求 / 调整预算"}</button>`
        : "",
      row.episodes_done || row.episode_count
        ? `<a class="secondary" href="/api/annotations/${esc(row.id)}/export" download>导出完整结果</a>`
        : "",
      !ACTIVE.has(row.state) && (row.episodes_done || row.episode_count)
        ? `<button class="primary" type="button" data-subtask-scroll>生成 Subtask 数据集</button>`
        : "",
    ]
      .filter(Boolean)
      .join("");
    const html = `
      <div class="annotation-summary">
        <div><span class="small muted">状态</span><div>${stateBadge(row.state)} ${esc(row.stage || "")}</div></div>
        <div><span class="small muted">进度</span><div>${done} / ${total || "?"} Episodes</div><div class="progress"><span data-annotation-width="${percent}"></span></div></div>
        <div><span class="small muted">已结算费用</span><div>${money(row.cost_usd)} / ${row.cap_usd == null ? "不限额" : `上限 ${money(row.cap_usd)}`}</div><span class="sub">${row.backend === "codex" ? "CLI 不提供美元账单" : row.cost_complete ? "费用完整" : "费用仍在结算或为估算"} · 预留 ${money(row.reserved_cost_usd)}</span></div>
        <div><span class="small muted">Episode 失败</span><div>${row.episodes_failed || 0}</div><span class="sub">退出码 ${esc(row.exit_code ?? "—")}</span></div>
      </div>
      <div class="annotation-meta small muted">
        <span>数据集：${esc(row.dataset_name)} · ${esc(row.robot_type || "未知机器人")} · rig ${esc(row.rig || "—")}</span>
        <span>后端：${esc(backendLabel(row))} · 模型 ${esc(row.model || "—")} · 并发 ${esc(row.concurrency ?? "—")} · 推理 ${esc(row.reasoning || "默认")} · 采样 ${esc(row.sample_fps || "默认")} Hz</span>
        ${row.backend === "codex" ? `<span>CLI 调用 ${esc(row.cli_call_count || 0)} · 输入 ${esc(row.cli_usage?.input_tokens || 0)} / 输出 ${esc(row.cli_usage?.output_tokens || 0)} tokens · 实际金额未知</span>` : ""}
        <span>模板：${esc(row.task_template?.name || "自由分析")} ${row.task_template ? `· 模板库版本 ${esc(row.template_library_revision)} · AI 初稿待人工确认` : ""}</span>
        <span>路径：${esc(row.dataset_path || "—")}</span>
        <span>创建 ${time(row.created_at)} · 开始 ${time(row.started_at)} · 结束 ${time(row.finished_at)}</span>
      </div>
      ${row.backend === "codex" && row.cli_calls?.length ? `<details><summary>CLI 调用回执（最近 ${Math.min(20, row.cli_calls.length)} 条，完整记录见导出）</summary><div class="table-wrap"><table><thead><tr><th>调用 ID / CLI 会话</th><th>状态</th><th>图像</th><th>用量 / 金额</th></tr></thead><tbody>${row.cli_calls.slice(-20).map(call => `<tr><td>${esc(call.id)}<span class="sub">${esc(call.thread_id || "尚无会话回执")}</span></td><td>${esc(call.status)}<span class="sub">${esc(call.error || "")}</span></td><td>${esc(call.images)}</td><td>输入 ${esc(call.usage?.input_tokens ?? "未知")} / 输出 ${esc(call.usage?.output_tokens ?? "未知")}<span class="sub">实际金额未知</span></td></tr>`).join("")}</tbody></table></div></details>` : ""}
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
      await loadSubtask(false);
    } catch (error) {
      if (visible) ui.notice(error.message, true);
    } finally {
      detailLoading = false;
    }
  }

  // ------------------------------------------------------------ subtask export
  async function loadSubtask(force) {
    const id = selectedId;
    const box = $("annotation-subtask");
    if (!id || !box || subtaskLoading) return;
    subtaskLoading = true;
    try {
      const value = await ui.api(`/api/annotations/${id}/subtask-export-preview`);
      if (selectedId !== id) return;
      subtask = value;
      renderSubtask(force);
    } catch (error) {
      if (selectedId !== id) return;
      subtask = null;
      box.innerHTML = `<p class="warning">${esc(error.message)}</p>`;
    } finally {
      subtaskLoading = false;
    }
  }

  const percentText = (value) => `${Math.round(100 * number(value, 0))}%`;

  // A subtask export is not an annotation, so it gets its own wording instead
  // of reusing "标注中" for a job that is only copying and rewriting frames.
  const SUBTASK_STATE_LABELS = {
    QUEUED: "排队中",
    RUNNING: "生成中",
    CONVERTED: "已完成",
    FAILED: "失败",
    INTERRUPTED: "已中断",
    CANCELLED: "已取消",
    // A finished export can be removed through dataset management, and the
    // history keeps showing that so a missing dataset is not a surprise.
    DELETING: "正在删除",
    DELETED: "已删除",
    DELETE_FAILED: "删除失败",
  };
  function subtaskStateLabel(state) {
    return SUBTASK_STATE_LABELS[state] || state || "—";
  }

  function renderSubtask(force) {
    const box = $("annotation-subtask");
    if (!box || !subtask) return;
    const data = subtask;
    const focused = document.activeElement?.matches?.("[data-subtask-name]");
    if (!force && focused) return;
    const history = data.exports || [];
    const running = history.find((item) => ACTIVE.has(item.state));
    const latest = history[0];
    const ready = Boolean(data.available && data.can_export) && !running;
    const summary = data.summary;
    const defaultName = subtaskName || (data.dataset?.name ? `${data.dataset.name}-subtasks` : "");
    const skipped = data.skipped || [];
    const previewEpisodes = data.episodes || [];
    const vocabulary = data.vocabulary || [];
    const rows = previewEpisodes
      .map(
        (item) => `<tr>
          <td>Episode ${esc(item.episode_index)}<span class="sub">${esc(item.episode_id)}</span></td>
          <td>${esc(item.frames)} 帧<span class="sub">${esc(Number(item.duration_s || 0).toFixed(1))} 秒</span></td>
          <td>${esc(item.labels)} 段<span class="sub">覆盖 ${percentText(item.coverage)}</span></td>
        </tr>`,
      )
      .join("");
    const exportRows = history
      .map(
        (item) => `<tr>
          <td><strong class="task-name">${esc(item.dataset_name || item.id.slice(0, 12))}</strong><span class="sub">${time(item.created_at)}</span></td>
          <td>${esc(subtaskStateLabel(item.state))}<span class="sub">${esc(item.stage || "")}</span></td>
          <td>${item.episode_count ? esc(item.episode_count) + " Episodes" : "—"}${item.state === "CONVERTED" && item.dataset_path ? `<span class="sub"><code>${esc(item.dataset_path)}</code></span>` : ""}${item.error ? `<span class="sub job-error">${esc(item.error)}</span>` : ""}</td>
        </tr>`,
      )
      .join("");
    const body = !data.available
      ? `<p class="warning">${esc(data.reason || "暂时无法生成 Subtask 数据集")}</p>`
      : `
        <p class="small muted">把该标注的逐帧事件写成 LeRobot v3.0 的 <code>subtask_index</code>，另存为独立数据集（原数据集不变）。未被标注覆盖的帧保留 <code>-1</code>（${esc(summary.unannotated_text)}），不会用整体任务或猜测的空闲动作填充。</p>
        <div class="annotation-summary subtask-summary">
          <div><span class="small muted">可导出 Episode</span><div>${esc(summary.annotated_episodes)} / ${esc(summary.dataset_episodes)}</div><span class="sub">跳过 ${esc(summary.skipped_episodes)} 条标注</span></div>
          <div><span class="small muted">逐帧覆盖</span><div>${percentText(summary.ratio)}</div><span class="sub">${esc(summary.labelled_frames)} / ${esc(summary.frames)} 帧已标注</span></div>
          <div><span class="small muted">Subtask 词表</span><div>${esc(summary.vocabulary_size)} 条</div><span class="sub">含未标注占位</span></div>
          <div><span class="small muted">来源格式</span><div>LeRobot ${esc(data.dataset?.format || "—")} → v3.0</div><span class="sub">${esc(data.dataset?.total_episodes || 0)} Episodes</span></div>
        </div>
        ${data.annotations_running ? '<p class="small muted">标注任务仍在运行，完成或停止后才能生成。</p>' : ""}
        <div class="table-scroll"><table>
          <thead><tr><th>标注 Episode</th><th>帧数</th><th>Subtask 区间</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="3" class="empty">没有可用标注的 Episode。</td></tr>'}</tbody>
        </table></div>
        ${skipped.length ? `<details class="subtask-skipped"><summary>跳过的标注（${skipped.length} 条）</summary><ul>${skipped.map((item) => `<li>${esc(item.episode_id)}：${esc(item.reason)}</li>`).join("")}</ul></details>` : ""}
        ${vocabulary.length ? `<details class="subtask-skipped"><summary>预览 Subtask 词表（${vocabulary.length} 条）</summary><ol start="0">${vocabulary.map((item) => `<li>${esc(item.subtask)}</li>`).join("")}</ol></details>` : ""}
        <div class="subtask-actions">
          <label for="annotation-subtask-name">新数据集名称</label>
          <input id="annotation-subtask-name" data-subtask-name type="text" maxlength="2000" value="${esc(defaultName)}" placeholder="留空则使用「原数据集名-subtasks」" ${ready ? "" : "disabled"} />
          <button class="primary" type="button" data-subtask-export ${ready ? "" : "disabled"}>${running ? "正在生成…" : "生成 Subtask 数据集"}</button>
        </div>
        <p class="small muted">生成在后台队列中执行，不需要模型密钥；完成后自动登记到数据集管理，可预览、质检或删除。</p>
        ${exportRows ? `<div class="table-scroll"><table><thead><tr><th>导出任务</th><th>状态</th><th>结果</th></tr></thead><tbody>${exportRows}</tbody></table></div>` : ""}
        ${latest && latest.error ? `<p class="warning">上次导出失败：${esc(latest.error)}</p>` : ""}
      `;
    const signature = body;
    if (subtaskSignature === signature) return;
    subtaskSignature = signature;
    box.innerHTML = body;
  }

  async function startSubtaskExport() {
    if (!selectedId || subtaskSubmitting) return;
    if (!subtask?.can_export) return;
    const input = $("annotation-subtask-name");
    const name = (subtaskName || input?.value || "").trim();
    subtaskSubmitting = true;
    const button = document.querySelector("[data-subtask-export]");
    if (button) {
      button.disabled = true;
      button.textContent = "正在提交…";
    }
    try {
      const job = await ui.api(`/api/annotations/${selectedId}/subtask-exports`, "POST", {
        name: name || null,
      });
      subtaskName = "";
      ui.notice(`已提交 Subtask 数据集生成：${job.dataset_name}。完成后可在数据集管理中找到。`);
      await loadSubtask(true);
    } catch (error) {
      ui.notice(error.message, true);
      await loadSubtask(true);
    } finally {
      subtaskSubmitting = false;
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
          <td>${labels}<div class="action-row">${item.labels_available ? `<button class="secondary" type="button" data-annotation-episode="${esc(item.id)}">查看预览</button>` : ""}<button class="text-button" type="button" data-annotation-episode-json="${esc(item.id)}">原始结果</button></div></td>
        </tr>`;
      })
      .join("");
  }

  const number = (value, fallback = 0) => {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  };
  const seconds = (value) => `${number(value).toFixed(1)}s`;
  const ARM_LABELS = { left: "左臂", right: "右臂", both: "双臂" };

  function viewerRows(key) {
    const rows = viewerData?.[key];
    return Array.isArray(rows) ? rows.filter((row) => row && typeof row === "object") : [];
  }

  function viewerCompletion() {
    const completion = viewerData?.completion;
    if (completion && typeof completion === "object") return completion;
    const nested = viewerData?.labels?.completion;
    return nested && typeof nested === "object" ? nested : {};
  }

  function viewerOutcome() {
    let outcome = viewerCompletion().task_completed;
    if (outcome === true) outcome = "success";
    if (outcome === false) outcome = "failure";
    return typeof outcome === "string" ? outcome : "";
  }

  function viewerDuration() {
    const values = [number(viewerData?.duration_s, 0)];
    const completion = viewerCompletion();
    values.push(number(completion.completed_at_s, 0), number(completion.goal_reached_at_s, 0));
    for (const row of viewerRows("event_labels")) {
      values.push(number(row.t_s ?? row.start_s, 0), number(row.end_s, 0));
    }
    for (const row of [...viewerRows("key_events"), ...viewerRows("state_changes")]) {
      values.push(number(row.t_s, 0));
    }
    const last = Math.max(0, ...values.filter((value) => Number.isFinite(value)));
    return last > 0 ? last : 1;
  }

  function viewerEvents() {
    const duration = viewerDuration();
    const rows = viewerRows("event_labels").map((row) => {
      const start = Math.max(0, number(row.t_s ?? row.start_s, 0));
      const end = number(row.end_s, 0);
      return { ...row, start, end: end > start ? end : null };
    });
    return rows.map((row, index) => ({
      ...row,
      end: row.end ?? (index + 1 < rows.length ? rows[index + 1].start : duration),
    }));
  }

  function viewerTone(event) {
    const text = String(event.contribution || "").toLowerCase();
    if (text.includes("advanc")) return "advance";
    if (text.includes("hold")) return "hold";
    if (text.includes("retract") || text.includes("retreat")) return "retract";
    if (text.includes("idle")) return "idle";
    return "other";
  }

  function renderViewer() {
    const body = $("annotation-viewer-body");
    if (!body || !viewerData) return;
    const duration = viewerDuration();
    const events = viewerEvents();
    const completion = viewerCompletion();
    const outcome = viewerOutcome();
    const model = viewerData._meta?.model || viewerData.model || "—";
    const prompt = viewerData.episode_prompt || viewerData.labels?.task_summary || "未记录任务描述";
    $("annotation-viewer-title").textContent = `Episode ${viewerEpisode} 可视化`;
    $("annotation-viewer-meta").textContent = `${seconds(duration)} · ${OUTCOME_LABELS[outcome] || "未判断"} · ${model}`;

    const percent = (value) => Math.min(100, Math.max(0, (100 * number(value, 0)) / duration));
    const video = viewerClips.length
      ? `<div class="viewer-cameras">${viewerClips
          .map(
            (clip) =>
              `<button type="button" class="secondary${clip.camera === viewerCamera ? " active" : ""}" data-viewer-camera="${esc(clip.camera)}">${esc(clip.label)}</button>`,
          )
          .join("")}</div>
        <video class="viewer-video" id="annotation-viewer-video" controls playsinline preload="metadata"></video>`
      : `<p class="empty small muted">本次运行没有可播放的预览视频（clips 阶段未生成）。事件时间线仍可查看。</p>`;

    const eventRows = events
      .map((event) => {
        const arm = ARM_LABELS[event.arm] || event.arm || "";
        const target = [event.object, event.carry_phase].filter(Boolean).join(" · ");
        return `<button type="button" class="viewer-event" data-viewer-seek="${event.start}" data-viewer-start="${event.start}" data-viewer-end="${event.end}">
          <span class="viewer-event-time">${seconds(event.start)}</span>
          <span class="viewer-event-body"><strong>${esc(event.verb_class || "未描述动作")}</strong>${target ? `<span class="sub">${esc(target)}</span>` : ""}${viewerData.task_document ? `<span class="sub">${esc([event.outcome && ({success:"成功",failure:"失败",unknown:"无法判断"})[event.outcome], templateValues(event.values, "attempt"), event.description, event.note].filter(Boolean).join(" · "))}</span>` : ""}</span>
          <span class="viewer-event-tag">${esc([arm, event.contribution].filter(Boolean).join(" · "))}</span>
        </button>`;
      })
      .join("");

    const keyRows = viewerRows("key_events")
      .map(
        (row) => `<li><span class="viewer-list-time">${seconds(row.t_s)}</span><strong>${esc(row.label || row.kind || "关键动作")}</strong>${row.note ? `<span class="sub">${esc(row.note)}</span>` : ""}</li>`,
      )
      .join("");
    const stateRows = viewerRows("state_changes")
      .map(
        (row) => `<li><span class="viewer-list-time">${seconds(row.t_s)}</span><strong>${esc(row.object || "状态变化")}</strong><span class="sub">${esc(row.from || "—")} → ${esc(row.to || "—")}</span>${row.seen_in ? `<span class="sub viewer-list-note">${esc(row.seen_in)}</span>` : ""}</li>`,
      )
      .join("");
    const completionHtml = completion.task_completed
      ? `<div class="viewer-completion">
          <h3>完成判定</h3>
          <p class="small"><strong>${esc(OUTCOME_LABELS[outcome] || outcome || "未判断")}</strong>${completion.completed_at_s != null ? ` · ${seconds(completion.completed_at_s)}` : ""}</p>
          ${completion.reason ? `<p class="small muted">${esc(completion.reason)}</p>` : ""}
          ${completion.success_predicate ? `<p class="small muted">判定标准：${esc(completion.success_predicate)}</p>` : ""}
        </div>`
      : "";

    body.innerHTML = `
      <p class="viewer-prompt"><strong>${viewerData.task_document ? "模板标注草稿" : "自由分析 · 任务摘要"}：</strong>${esc(prompt)}</p>
      ${viewerData.task_document ? `<p class="small"><strong>模板初稿：${esc(viewerData.task_document.templateSnapshot.name)}</strong> · ${viewerData.task_document.attempts.length} 次尝试 · ${(viewerData.task_document.version === 2 ? viewerData.task_document.segments.filter(s => s.label === "NULL").length : (viewerData.task_document.nullSegments || []).length)} 段 NULL · ${viewerData.task_document.frames.length} 条单帧标注${viewerData.task_document.version === 2 ? ` · UNKNOWN ${viewerData.task_document.segments.filter(s => s.label === "UNKNOWN").length} 段 · 模板外 ${viewerData.task_document.segments.filter(s => s.label === "OUT_OF_TEMPLATE").length} 段` : ""}<br>${esc(templateValues(viewerData.task_document.values, "episode"))}<br><span class="muted">可直接发送独立候选到统一工作区检查、修改和保存，无需先复制数据集。</span></p>` : ""}
      ${(viewerData.template_warnings || []).map((warning) => `<p class="warning small">${esc(warning)}</p>`).join("")}
      <div class="viewer-stage">${video}</div>
      <div class="viewer-timeline" data-viewer-timeline role="slider" tabindex="0" aria-label="事件时间线">
        <div class="viewer-track" id="annotation-viewer-track"><span class="viewer-playhead" id="annotation-viewer-playhead"></span></div>
        <div class="viewer-ticks"><span>0s</span><span>${seconds(duration / 2)}</span><span>${seconds(duration)}</span></div>
      </div>
      <div class="viewer-grid">
        <div>
          <h3>事件时间线（${events.length}）</h3>
          <div class="viewer-events">${eventRows || '<p class="empty small muted">没有事件记录</p>'}</div>
        </div>
        <div class="viewer-side">
          <h3>关键动作（${viewerRows("key_events").length}）</h3>
          <ul class="viewer-list">${keyRows || '<li class="muted small">未记录关键动作</li>'}</ul>
          <h3>状态变化（${viewerRows("state_changes").length}）</h3>
          <ul class="viewer-list">${stateRows || '<li class="muted small">未记录状态变化</li>'}</ul>
          ${completionHtml}
        </div>
      </div>`;

    // Positions are set through CSSOM: the page's CSP allows styles from
    // 'self' but not inline style attributes parsed from markup.
    const track = $("annotation-viewer-track");
    const playhead = $("annotation-viewer-playhead");
    if (track && playhead) {
      const place = (node, left, width) => {
        node.style.left = `${left}%`;
        if (width != null) node.style.width = `${width}%`;
        track.insertBefore(node, playhead);
      };
      for (const event of events) {
        const start = percent(event.start);
        const node = document.createElement("span");
        node.className = `viewer-segment ${viewerTone(event)}`;
        node.title = [seconds(event.start), event.verb_class, event.object]
          .filter(Boolean)
          .join(" · ");
        place(node, start, Math.min(100 - start, Math.max(0.3, percent(event.end) - start)));
      }
      for (const [key, tone] of [
        ["key_events", "key"],
        ["state_changes", "state"],
      ]) {
        for (const row of viewerRows(key)) {
          const node = document.createElement("span");
          node.className = `viewer-marker ${tone}`;
          node.title =
            key === "key_events"
              ? row.label || row.kind || "关键动作"
              : row.object || "状态变化";
          place(node, percent(row.t_s), null);
        }
      }
    }

    const timeline = body.querySelector("[data-viewer-timeline]");
    timeline?.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      seekViewer(viewerTime + (event.key === "ArrowRight" ? 0.5 : -0.5));
    });
    const player = $("annotation-viewer-video");
    if (player) {
      player.addEventListener("timeupdate", () => {
        if (!player.seeking) paintViewerTime(player.currentTime);
      });
      player.addEventListener("seeked", () => paintViewerTime(player.currentTime));
      selectViewerCamera(viewerCamera, { seek: viewerTime, play: false });
    }
    paintViewerTime(viewerTime);
  }

  function paintViewerTime(value) {
    viewerTime = Math.max(0, number(value, 0));
    const playhead = $("annotation-viewer-playhead");
    if (playhead) playhead.style.left = `${Math.min(100, (100 * viewerTime) / viewerDuration())}%`;
    $("annotation-viewer-body")
      ?.querySelectorAll("[data-viewer-start]")
      .forEach((row) => {
        const start = number(row.dataset.viewerStart, 0);
        const end = number(row.dataset.viewerEnd, start);
        row.classList.toggle("active", viewerTime >= start && viewerTime < Math.max(end, start + 0.001));
      });
  }

  function selectViewerCamera(camera, options = {}) {
    const clip = viewerClips.find((item) => item.camera === camera);
    const video = $("annotation-viewer-video");
    if (!clip || !video) return;
    const time = Number.isFinite(options.seek) ? options.seek : viewerTime;
    const wasPlaying =
      options.play ?? (Boolean(video.getAttribute("src")) && !video.paused && !video.ended);
    viewerCamera = clip.camera;
    $("annotation-viewer-body")
      ?.querySelectorAll("[data-viewer-camera]")
      .forEach((button) => {
        button.classList.toggle("active", button.dataset.viewerCamera === clip.camera);
      });
    if (video.dataset.camera === clip.camera && video.getAttribute("src")) return;
    video.dataset.camera = clip.camera;
    video.src = clip.url;
    video.load();
    const restore = () => {
      video.onloadedmetadata = null;
      try {
        video.currentTime = Math.min(Math.max(0, time), video.duration || time);
      } catch {
        // A clip shorter than the requested time keeps its own start.
      }
      if (wasPlaying) void video.play().catch(() => {});
    };
    if (video.readyState >= 1) restore();
    else video.onloadedmetadata = restore;
  }

  function seekViewer(value) {
    const time = Math.min(Math.max(0, number(value, 0)), viewerDuration());
    const video = $("annotation-viewer-video");
    if (video && video.getAttribute("src")) {
      try {
        video.currentTime = time;
      } catch {
        // The element is between sources; the playhead still follows the click.
      }
    }
    paintViewerTime(time);
  }

  function closeViewer() {
    viewerToken += 1;
    viewerEpisode = null;
    viewerData = null;
    viewerClips = [];
    viewerCamera = null;
    viewerTime = 0;
    const video = $("annotation-viewer-video");
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    const body = $("annotation-viewer-body");
    if (body) body.innerHTML = "";
    const box = $("annotation-episode-viewer");
    if (box) box.hidden = true;
  }

  async function showEpisode(episodeId) {
    const id = selectedId;
    if (!id) return;
    const token = ++viewerToken;
    viewerEpisode = episodeId;
    viewerData = null;
    viewerClips = [];
    viewerCamera = null;
    viewerTime = 0;
    $("annotation-episode-viewer").hidden = false;
    $("annotation-viewer-title").textContent = `Episode ${episodeId} 可视化`;
    $("annotation-viewer-meta").textContent = "";
    $("annotation-viewer-body").innerHTML = '<p class="empty small muted">正在读取 Episode 预览…</p>';
    $("annotation-episode-viewer").scrollIntoView({ behavior: "smooth", block: "start" });
    try {
      const [data, clips] = await Promise.all([
        ui.api(`/api/annotations/${id}/episodes/${encodeURIComponent(episodeId)}`),
        ui.api(`/api/annotations/${id}/episodes/${encodeURIComponent(episodeId)}/clips`).catch(
          () => ({ clips: [] }),
        ),
      ]);
      if (token !== viewerToken || selectedId !== id) return;
      viewerData = data && typeof data === "object" ? data : {};
      viewerClips = Array.isArray(clips.clips) ? clips.clips : [];
      viewerCamera = viewerClips[0]?.camera || null;
      renderViewer();
    } catch (error) {
      if (token !== viewerToken) return;
      $("annotation-viewer-body").innerHTML = `<p class="warning small">${esc(error.message)}</p>`;
    }
  }

  function templateValues(values, scope) {
    const fields = viewerData?.task_document?.templateSnapshot.fields || [];
    return fields.filter((f) => f.scope === scope && values?.[f.id] != null).map((f) => {
      const value = values[f.id];
      return `${f.label}：${f.kind === "rating" ? `${value} 星` : Array.isArray(value) ? value.join("、") : typeof value === "boolean" ? value ? "是" : "否" : value}`;
    }).join(" · ") || "未判断的字段留空";
  }

  async function showEpisodeJson(episodeId) {
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
    const scope = $("annotation-scope").value;
    const selectedEpisodes = scope === "full" ? null : [...new Set($("annotation-selected-episodes").value.split(/[,，\s]+/).filter(Boolean).map(Number))];
    if (scope !== "full" && (!selectedEpisodes.length || selectedEpisodes.some(n => !Number.isInteger(n) || n < 0) || (scope === "trial" && selectedEpisodes.length > 5))) {
      ui.notice("请明确选择有效来源 Episode 编号；试跑选择一至五条，不会自动全量。", true); return;
    }
    const sample = $("annotation-sample-fps").value;
    const sampleFps = sample === "" ? null : Number(sample);
    const baseUrl = $("annotation-base-url").value.trim();
    const apiKey = $("annotation-api-key").value.trim();
    const body = {
      job_id: jobId,
      backend: $("annotation-backend").value,
      run_scope: scope, episodes: selectedEpisodes,
      boundary_target_s: Number($("annotation-boundary-target").value),
      boundary_window_s: Number($("annotation-boundary-window").value),
      template_id: $("annotation-template").value || null,
      template_revision: $("annotation-template").value ? templateLibrary.revision : null,
      model: $("annotation-model").value,
      cap_usd: options?.unlimited ? null : Number($("annotation-cap").value),
      concurrency: Number($("annotation-concurrency").value),
      reasoning: $("annotation-reasoning").value || null,
      inspection: $("annotation-inspection").value,
      sample_fps: sampleFps && sampleFps > 0 ? sampleFps : null,
      field_dictionary: $("annotation-field-dictionary").checked,
      max_tokens: isCodex() ? null : Number($("annotation-max-tokens").value),
      note: $("annotation-note").value.trim() || null,
      base_url: isCodex() ? null : baseUrl || null,
      api_key: isCodex() ? null : apiKey || null,
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
      ui.notice(`已提交「${row.dataset_name}」自动标注：${row.model}，${row.cap_usd == null ? "费用不限额" : `费用上限 ${money(row.cap_usd)}`}。`);
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
    $("annotation-continue-full").checked = row.state === "COMPLETED" && row.run_scope === "trial";
    $("annotation-resume-cap").value = "";
    $("annotation-resume-cap").disabled = Boolean(options?.unlimited);
    $("annotation-resume-cap").closest("label").hidden = Boolean(options?.unlimited);
    $("annotation-resume-cost").textContent = options?.unlimited
      ? "模板任务会保留有效初稿，对不符合模板的模型回复重新请求；费用与用量继续记录，当前不限额。"
      : "模板任务会保留有效初稿，对不符合模板的模型回复重新请求；重试费用计入本次预算。";
    const cli = row.backend === "codex";
    const configured = cli || Boolean(options?.api_key_configured);
    ["annotation-resume-base-url", "annotation-resume-api-key"].forEach(id => {
      $(id).hidden = cli; $(id).disabled = cli;
      document.querySelector(`label[for="${id}"]`).hidden = cli;
    });
    $("annotation-resume-base-url").value = row.base_url || "";
    $("annotation-resume-api-key").value = "";
    $("annotation-resume-api-key").required = !configured;
    $("annotation-resume-hint").textContent = cli
      ? "沿用原 Codex 模型、提供方、模板与采样配置，并复用已成功结果。未知调用状态不会自动重新发送；费用仍为未知。"
      : configured
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
    const body = { continue_full: $("annotation-continue-full").checked };
    if ($("annotation-resume-cap").value) body.cap_usd = Number($("annotation-resume-cap").value);
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
    $("annotation-viewer-video")?.pause();
  }

  window.AnnotationUI = { configure, show, hide };
})();
