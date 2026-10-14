/* Buildless, same-origin UI. Text from sources is escaped before rendering. */
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const state = {
  view: "library",
  collection: null,
  filter: "all",
  search: "",
  selected: new Set(),
  resources: [],
  collections: [],
  jobs: [],
  settings: null,
  importMode: "links",
  stage: "pipeline",
  detail: null,
  loading: false,
  currentPage: 1,
  pageSize: 25,
  contextMenuResource: null,
};
const stageNames = {
  download: "下载音频",
  parse: "解析音频",
  export: "导出转录结果",
  pipeline: "完整流程",
  discover: "解析合集",
};
const activeStates = ["queued", "running", "pausing", "cancelling"];
let toastTimer;
function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), 4500);
}
async function api(path, options = {}) {
  const response = await fetch("/api" + path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!response.ok) {
    let data;
    try {
      data = await response.json();
    } catch {
      throw Error(`请求失败 (${response.status})`);
    }
    const detail = data.detail;
    throw Error(
      Array.isArray(detail)
        ? detail.map(validationZh).join("\n")
        : messageZh(detail) || "请求失败",
    );
  }
  return response.json();
}
function post(path, body) {
  return api(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
function date(value) {
  return new Date(value).toLocaleString("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
function badge(label, color = "") {
  return `<span class="badge ${color}">${esc(label)}</span>`;
}
function statusBadge(status) {
  return badge(
    statusNames[status] || status,
    {
      completed: "green",
      running: "blue",
      queued: "orange",
      failed: "red",
      paused: "orange",
      pausing: "orange",
      cancelled: "",
      cancelling: "orange",
      pending: "",
    }[status] || "",
  );
}
function progress(value) {
  return `<div class="progress-track"><div class="progress-fill" style="width:${Math.max(0, Math.min(100, value || 0))}%"></div></div>`;
}
function show(view) {
  state.view = view;
  if (view === "library") {
    renderSidebar();
    renderLibrary();
  }
  $$(".view").forEach((x) => (x.hidden = x.id !== view + "-view"));
  $$("[data-nav]").forEach((x) =>
    x.classList.toggle("active", x.dataset.nav === view),
  );
  $("#breadcrumb").textContent = {
    library: "资源中心",
    jobs: "任务队列",
    settings: "配置管理",
  }[view];
  if (view === "settings") renderSettings();
  if (view === "jobs") renderJobs();
}
function filtered() {
  return state.resources.filter(
    (r) =>
      (state.collection === null || r.collections.includes(state.collection)) &&
      (state.filter === "all" ||
        (state.filter === "ready"
          ? r.audio.length && !r.parsed.length
          : r.parsed.length)) &&
      `${r.title} ${r.url}`.toLowerCase().includes(state.search.toLowerCase()),
  );
}
function paginated(rows) {
  const start = (state.currentPage - 1) * state.pageSize;
  const end = start + state.pageSize;
  return rows.slice(start, end);
}
function totalPages(rows) {
  return Math.max(1, Math.ceil(rows.length / state.pageSize));
}
function renderSidebar() {
  $("#library-count").textContent = state.resources.length;
  $("#queue-count").textContent = state.jobs.filter((j) =>
    activeStates.includes(j.state),
  ).length;
  $("#collections").innerHTML = state.collections
    .map(
      (c) =>
        `<button class="collection-item ${state.collection === c.id ? "active" : ""}" data-collection="${c.id}"><span>▱</span><span class="collection-name">${esc(collectionName(c))}</span><span class="nav-count">${c.count}</span></button>`,
    )
    .join("");
  $("#mobile-collection").innerHTML =
    '<option value="">全部合集</option>' +
    state.collections
      .map(
        (c) =>
          `<option value="${c.id}" ${state.collection === c.id ? "selected" : ""}>${esc(collectionName(c))} (${c.count})</option>`,
      )
      .join("");
}
function renderLibrary() {
  const all = state.resources;
  const collection = state.collections.find((c) => c.id === state.collection);
  $("#library-title").textContent = collection
    ? collectionName(collection)
    : "资源中心";
  const metrics = [
    ["资源总数", all.length, "资源库中的全部资源", "▤"],
    [
      "音频已就绪",
      all.filter((r) => r.audio.length).length,
      "已下载，可直接使用",
      "↓",
    ],
    [
      "已转录",
      all.filter((r) => r.parsed.length).length,
      "转录完成，随时导出",
      "≋",
    ],
    [
      "进行中的任务",
      state.jobs.filter((j) => activeStates.includes(j.state)).length,
      "后台自动处理",
      "↗",
    ],
  ];
  $("#stats").innerHTML = metrics
    .map(
      ([label, n, note, icon]) =>
        `<div class="stat"><div class="stat-name">${label}</div><div class="stat-number">${n}</div><div class="stat-note">${note}</div><span class="stat-icon">${icon}</span></div>`,
    )
    .join("");
  const allRows = filtered();
  const maxPages = totalPages(allRows);
  if (state.currentPage > maxPages) state.currentPage = maxPages;
  const rows = paginated(allRows);
  $("#resource-rows").innerHTML = rows
    .map(
      (r) =>
        `<tr data-resource-row="${r.id}"><td class="check-cell"><input type="checkbox" data-select="${r.id}" aria-label="选择 ${esc(r.title)}" ${state.selected.has(r.id) ? "checked" : ""}></td><td><div class="resource-cell"><span class="media-icon">▷</span><div><button class="resource-name" data-resource="${r.id}">${esc(r.title)}</button><a class="resource-url" href="${esc(r.url)}" target="_blank" rel="noreferrer">${esc(r.url)}</a></div></div></td><td>${esc(
          r.collections
            .map((id) =>
              collectionName(state.collections.find((c) => c.id === id)),
            )
            .filter(Boolean)
            .join(", "),
        )}</td><td>${r.audio.length ? badge("✓ 已就绪", "green") : badge("未下载")}</td><td>${r.parsed.length ? badge(r.outputs.length ? "✓ 已导出" : "✓ 已解析", "green") : badge("未解析")}</td><td><button class="icon-button" data-resource="${r.id}" aria-label="打开 ${esc(r.title)}">↗</button></td></tr>`,
    )
    .join("");
  $("#library-empty").hidden = rows.length > 0;
  if (all.length) {
    $("#library-empty h2").textContent = "当前视图没有资源";
    $("#library-empty p").textContent =
      "请切换合集、调整筛选或搜索条件，也可以添加更多资源。";
    $("#empty-import").textContent = "＋ 添加资源";
  } else {
    $("#library-empty h2").textContent = "从添加第一份资源开始";
    $("#library-empty p").textContent =
      "导入哔哩哔哩合集，或粘贴视频链接。 随时下载音频并转录为文字。";
    $("#empty-import").textContent = "＋ 添加第一份资源";
  }
  $("#resource-count").textContent =
    `${allRows.length} 个资源${state.search ? "符合搜索条件" : ""}`;
  $("#page-info").textContent = `第 ${state.currentPage} / ${maxPages} 页`;
  $("#prev-page").disabled = state.currentPage <= 1;
  $("#next-page").disabled = state.currentPage >= maxPages;
  renderSelection();
}
function renderSelection() {
  const n = state.selected.size;
  $("#selection-label").textContent = n
    ? `已选择 ${n} 个资源`
    : "选择资源后即可创建任务";
  $$("[data-stage]").forEach((x) => (x.disabled = !n));
  const rows = filtered();
  const count = rows.filter((r) => state.selected.has(r.id)).length;
  $("#select-all").checked = rows.length > 0 && count === rows.length;
  $("#select-all").indeterminate = count > 0 && count < rows.length;
}
function controls(job) {
  const button = (action, label) =>
    `<button data-control="${action}" data-job="${job.id}">${label}</button>`;
  return `<div class="job-controls">${["queued", "running"].includes(job.state) ? button("pause", "暂停") : ""}${job.state === "paused" ? button("resume", "继续") : ""}${["failed", "cancelled"].includes(job.state) ? button("retry", "重试未完成项") : ""}${["queued", "running", "paused", "pausing"].includes(job.state) ? button("cancel", "取消") : ""}</div>`;
}
function renderJobs() {
  $("#jobs-list").innerHTML = state.jobs.length
    ? state.jobs
        .map(
          (j) =>
            `<article class="job-card"><div class="job-card-head"><button class="job-title" data-job-detail="${j.id}">${stageNames[j.kind]} <span class="muted">#${j.id}</span></button>${statusBadge(j.state)}${j.dry_run ? badge("试运行", "orange") : ""}<time class="job-time">${date(j.created)}</time></div><div class="job-meta"><span>已完成 ${j.completed} / ${j.total}${j.failed ? ` · ${j.failed} 项失败` : ""}</span><span>${Math.round(j.progress || 0)}%</span></div>${progress(j.progress)}<div class="job-bottom"><small>${j.state === "pausing" ? "当前资源处理完成后暂停。" : j.state === "paused" ? "可随时继续，已完成的资源会保留。" : j.state === "failed" ? "打开详情查看错误；重试仅处理未完成的资源。" : "任务配置与进度已保存。"}</small>${controls(j)}<button class="text-button" data-job-detail="${j.id}">查看详情 →</button></div></article>`,
        )
        .join("")
    : `<div class="empty-jobs"><div class="empty-icon">☷</div><h2>还没有任务</h2><p>在资源库中选择资源，即可创建下载、转录或导出任务。</p><button data-nav="library">前往资源中心 →</button></div>`;
}
async function refresh() {
  if (state.loading) return;
  state.loading = true;
  try {
    const [library, jobs] = await Promise.all([api("/library"), api("/jobs")]);
    const changed = JSON.stringify([library, jobs]) !== state.lastLibrary;
    state.lastLibrary = JSON.stringify([library, jobs]);
    state.resources = library.resources;
    state.collections = library.collections;
    state.jobs = jobs;
    state.selected = new Set(
      [...state.selected].filter((id) =>
        state.resources.some((r) => r.id === id),
      ),
    );
    if (changed) {
      renderSidebar();
      if (state.view === "library") renderLibrary();
      if (state.view === "jobs") renderJobs();
    }
    $("#connection").textContent = "已连接本地服务";
    $("#connection-dot").style.background = "#63a185";
    if (state.detail?.kind === "job" && $("#detail-dialog").open)
      await renderJobDetail(state.detail.id);
  } catch (error) {
    $("#connection").textContent = "服务已断开 · 正在重连";
    $("#connection-dot").style.background = "#c47f67";
  } finally {
    state.loading = false;
  }
}
function openImport(mode = "links") {
  state.importMode = mode;
  $("#import-error").textContent = "";
  $$("[data-import-mode]").forEach((b) =>
    b.classList.toggle("active", b.dataset.importMode === mode),
  );
  $("#links-fields").hidden = mode !== "links";
  $("#collection-fields").hidden = mode !== "collection";
  $("#import-submit").textContent =
    mode === "links" ? "添加至通用合集" : "加入合集解析队列";
  if (!$("#import-dialog").open) $("#import-dialog").showModal();
}
async function openJob(stage) {
  state.stage = stage;
  try {
    state.settings = await api("/settings");
  } catch (e) {
    toast(messageZh(e.message), true);
    return;
  }
  $("#job-dialog-title").textContent = stageNames[stage];
  $("#job-description").textContent =
    `已选择 ${state.selected.size} 个资源。 ${{ download: "将音频保存到本地，以便后续解析。", parse: "将已下载的音频转为可复用的转录数据。", export: "从已保存的转录数据生成文件。", pipeline: "在一个任务中依次完成下载 → 解析 → 导出。" }[stage]}`;
  const s = state.settings;
  $("#job-config-summary").innerHTML =
    `下载器: 超时 ${s.downloader.timeout} 秒 · 重试 ${s.downloader.retries} 次<br>解析器: ${esc(s.parser.model)} · ${esc(s.parser.device)} · ${esc(s.parser.language)}<br>输出: ${s.output.formats.map(esc).join(", ").toUpperCase()}<br><span class="muted">如需调整默认设置，请先前往配置管理。</span>`;
  $("#job-error").textContent = "";
  $("#job-dry").checked = false;
  $("#job-force").checked = false;
  $("#job-force").disabled = stage === "export";
  $("#job-dialog").showModal();
}
async function renderJobDetail(id) {
  const job = await api("/jobs/" + id);
  if (state.detail?.kind !== "job" || state.detail.id !== id) return;
  const signature = JSON.stringify(job);
  if (state.detail.signature === signature) return;
  state.detail.signature = signature;
  $("#detail-title").textContent = `${stageNames[job.kind]} #${job.id}`;
  $("#detail-eyebrow").textContent = job.dry_run
    ? "试运行 · 不修改资源"
    : "任务详情";
  $("#detail-content").innerHTML =
    `<div class="job-card-head">${statusBadge(job.state)}<span class="muted">${date(job.created)}</span></div><div class="job-bottom">${controls(job)}<small>暂停会等待当前资源处理完成；取消会立即终止当前处理。</small></div><div class="detail-section"><h3>资源与进度</h3>${job.items.map((i) => `<div class="item-row"><div class="item-row-head"><strong>${esc(i.title || job.config.collection?.name || "合集解析")}</strong>${statusBadge(i.state)}</div>${progress(i.progress)}<p>${esc(i.stage ? stageNames[i.stage] + " · " : "")}${esc(messageZh(i.message) || "等待工作进程处理")}</p>${job.dry_run && i.state === "completed" ? renderPlan(i.result) : ""}${job.kind === "discover" && !job.dry_run && i.state === "completed" ? `<p>新增 ${i.result.added} 个资源 · ${i.result.existing} 个已存在。</p>` : ""}<div class="item-actions"><button data-log="${i.id}">查看日志</button>${i.resource_id ? `<button data-resource="${i.resource_id}">打开资源</button>` : ""}</div></div>`).join("")}</div><div class="detail-section"><h3>任务动态</h3><ul class="events">${job.events.map((e) => `<li><time>${date(e.created)}</time>${esc(messageZh(e.message))}</li>`).join("")}</ul></div><div class="detail-section"><h3>任务配置快照</h3><pre>${esc(JSON.stringify(configZh(job.config), null, 2))}</pre></div>`;
}
function renderPlan(result) {
  if (result.steps)
    return `<div class="job-plan">${result.steps.map((s) => (typeof s === "string" ? esc(messageZh(s)) : `<strong>${esc(stageNames[s.stage] || s.stage)}</strong> — ${esc(messageZh(s.action))}`)).join("<br>")}</div>`;
  return `<pre>${esc(JSON.stringify(result, null, 2))}</pre>`;
}
async function openJobDetail(id) {
  state.detail = { kind: "job", id };
  $("#detail-title").textContent = "正在加载任务…";
  $("#detail-content").textContent = "";
  if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
  try {
    await renderJobDetail(id);
  } catch (e) {
    toast(messageZh(e.message), true);
  }
}
function openResource(id) {
  const r = state.resources.find((r) => r.id === id);
  if (!r) return;
  state.detail = { kind: "resource", id };
  $("#detail-eyebrow").textContent = "资源详情";
  $("#detail-title").textContent = r.title;
  const files = (category, paths) =>
    paths
      .map((file, index) => {
        const path = category === "audio" ? file.path : file;
        const name = path.split(/[\\/]/).pop();
        const url = `/api/resources/${r.id}/files/${category}/${index}`;
        return `<div class="file-row"><span>${esc(name)}</span>${category !== "audio" ? `<button data-preview="${url}">预览</button>` : ""}<a href="${url}?download=true">下载 ↓</a></div>${category === "audio" ? `<audio controls preload="none" src="${url}"></audio>` : ""}`;
      })
      .join("");
  $("#detail-content").innerHTML =
    `<p><a href="${esc(r.url)}" target="_blank" rel="noreferrer">打开原始视频 ↗</a></p><div class="detail-section"><h3>音频 · ${r.audio.length} 个分段</h3>${files("audio", r.audio) || "<p>暂无音频，请选择此资源并运行下载。</p>"}</div><div class="detail-section"><h3>转录数据</h3>${files("parsed", r.parsed) || "<p>暂无转录数据，请先下载音频，再运行解析。</p>"}</div><div class="detail-section"><h3>已导出的文件</h3>${files("outputs", r.outputs) || "<p>解析完成后运行导出，即可生成文本或字幕文件。</p>"}</div><pre class="resource-preview" id="file-preview" hidden></pre>`;
  if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
}
function renderSettings() {
  const s = state.settings;
  if (!s) {
    api("/settings")
      .then((x) => {
        state.settings = x;
        if (state.view === "settings") renderSettings();
      })
      .catch((e) => toast(messageZh(e.message), true));
    return;
  }
  const options = (values, current) =>
    values
      .map(
        (value) =>
          `<option value="${value}" ${value === current ? "selected" : ""}>${esc(optionNames[value] || value)}</option>`,
      )
      .join("");
  $("#settings-form").innerHTML =
    `<section class="settings-card"><div><h3>01 &nbsp; 下载器</h3><p>设置合集解析和音频下载的网络参数。</p></div><div class="settings-fields"><label>请求超时（秒）<input name="timeout" type="number" min="5" max="600" value="${s.downloader.timeout}" required></label><label>重试次数<input name="retries" type="number" min="0" max="20" value="${s.downloader.retries}" required></label><p class="field-note span-two">音频会持久保存。默认复用已有音频，创建任务时启用重新处理即可重新下载。</p></div></section><section class="settings-card"><div><h3>02 &nbsp; 解析器</h3><p>使用 faster-whisper 在本地识别语音。首次使用模型时会下载模型权重。</p></div><div class="settings-fields"><label>Whisper 模型<select name="model">${options(["tiny", "base", "small", "medium", "large-v3", "turbo"], s.parser.model)}</select></label><label>识别语言<input name="language" value="${esc(s.parser.language)}" required maxlength="10"><span class="field-note">zh 中文、en 英文、ja 日文；auto 自动检测</span></label><label>计算设备<select name="device">${options(["cpu", "auto", "cuda"], s.parser.device)}</select></label><label>计算精度<select name="compute_type">${options(["int8", "default", "auto", "float16", "float32"], s.parser.compute_type)}</select></label><label>束搜索宽度<input name="beam_size" type="number" min="1" max="10" value="${s.parser.beam_size}" required></label><label class="checkbox-label"><input name="vad_filter" type="checkbox" ${s.parser.vad_filter ? "checked" : ""}>跳过静音片段（VAD）</label><p class="field-note span-two">随附的 Docker 镜像支持 CPU + int8。CUDA 需要另行配置 NVIDIA 环境。较大的模型会占用更多内存，建议从 tiny 或 base 开始。</p></div></section><section class="settings-card"><div><h3>03 &nbsp; 输出</h3><p>直接导出已有转录数据，无需重复进行语音识别。</p></div><div><label>文件格式</label><div class="format-options">${["txt", "vtt", "srt", "json"].map((f) => `<label class="checkbox-label"><input type="checkbox" name="formats" value="${f}" ${s.output.formats.includes(f) ? "checked" : ""}>${f.toUpperCase()}</label>`).join("")}</div><label class="checkbox-label" style="margin-top:25px"><input name="timestamps" type="checkbox" ${s.output.timestamps ? "checked" : ""}>在纯文本中添加时间戳</label><p class="field-note">每次导出都会创建独立的版本文件夹。当前文件可在资源详情中查看，历史文件仍保留在磁盘上。</p></div></section><p class="form-error" id="settings-error" role="alert"></p>`;
}
document.addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  const d = target.dataset;
  try {
    if (d.nav) {
      if (d.nav === "library") {
        state.collection = null;
        renderSidebar();
        renderLibrary();
      }
      show(d.nav);
    }
    if (d.collection) {
      state.collection = Number(d.collection);
      state.selected.clear();
      show("library");
      renderSidebar();
      renderLibrary();
    }
    if (d.close) {
      $("#" + d.close).close();
      if (d.close === "detail-dialog") state.detail = null;
    }
    if (d.importMode) openImport(d.importMode);
    if (d.filter) {
      state.filter = d.filter;
      state.selected.clear();
      $$("[data-filter]").forEach((b) =>
        b.classList.toggle("active", b === target),
      );
      renderLibrary();
    }
    if (d.stage) await openJob(d.stage);
    if (d.jobDetail) await openJobDetail(Number(d.jobDetail));
    if (d.resource) openResource(Number(d.resource));
    if (d.control) {
      target.disabled = true;
      await post(`/jobs/${d.job}/${d.control}`);
      toast(`已请求${actionNames[d.control]}`);
      await refresh();
    }
    if (d.log) {
      const log = await api(`/items/${d.log}/log`);
      state.detail = { kind: "log" };
      $("#detail-title").textContent = "工作进程日志";
      $("#detail-content").innerHTML = `<pre>${esc(messageZh(log.text))}</pre>`;
    }
    if (d.preview) {
      target.disabled = true;
      const response = await fetch(d.preview);
      if (!response.ok) throw Error("文件不可用，请重新运行对应阶段。");
      const preview = $("#file-preview");
      preview.textContent = await response.text();
      preview.hidden = false;
      preview.scrollIntoView({ behavior: "smooth" });
    }
  } catch (error) {
    toast(messageZh(error.message), true);
  } finally {
    target.disabled = false;
  }
});
$("#search").addEventListener("input", (e) => {
  state.search = e.target.value;
  state.currentPage = 1;
  renderLibrary();
});
$("#mobile-collection").addEventListener("change", (e) => {
  state.collection = e.target.value ? Number(e.target.value) : null;
  state.selected.clear();
  state.currentPage = 1;
  renderSidebar();
  renderLibrary();
});
$("#resource-rows").addEventListener("change", (e) => {
  if (e.target.dataset.select) {
    const id = Number(e.target.dataset.select);
    e.target.checked ? state.selected.add(id) : state.selected.delete(id);
    renderSelection();
  }
});
$("#select-all").addEventListener("change", (e) => {
  filtered().forEach((r) =>
    e.target.checked ? state.selected.add(r.id) : state.selected.delete(r.id),
  );
  renderLibrary();
});
$("#import-open").onclick = () => openImport();
$("#empty-import").onclick = () => openImport();
$("#new-collection").onclick = () => openImport("collection");
$("#hint-settings").onclick = () => show("settings");
$("#import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const submit = $("#import-submit");
  submit.disabled = true;
  $("#import-error").textContent = "";
  try {
    if (state.importMode === "links") {
      const urls = $("#links-input")
        .value.split(/\r?\n/)
        .map((s) => s.trim())
        .filter((s) => s && !s.startsWith("#"));
      const result = await post("/import/links", { urls });
      toast(
        `已添加 ${result.added} 个资源至通用合集 · ${result.existing} 个已存在`,
      );
      $("#links-input").value = "";
      state.collection = 1;
      state.filter = "all";
      state.search = "";
      $("#search").value = "";
      $$("[data-filter]").forEach((b) =>
        b.classList.toggle("active", b.dataset.filter === "all"),
      );
      show("library");
    } else {
      const result = await post("/import/collection", {
        url: $("#collection-url").value,
        name: $("#collection-name").value.trim(),
        dry_run: $("#collection-dry").checked,
      });
      toast("合集解析已加入队列");
      show("jobs");
      await openJobDetail(result.id);
    }
    $("#import-dialog").close();
    await refresh();
  } catch (error) {
    $("#import-error").textContent = messageZh(error.message);
  } finally {
    submit.disabled = false;
  }
});
$("#job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const submit = e.submitter;
  submit.disabled = true;
  try {
    const result = await post("/jobs", {
      kind: state.stage,
      resource_ids: [...state.selected],
      dry_run: $("#job-dry").checked,
      force: $("#job-force").checked,
    });
    $("#job-dialog").close();
    show("jobs");
    toast("任务已加入队列");
    await refresh();
    await openJobDetail(result.id);
  } catch (error) {
    $("#job-error").textContent = messageZh(error.message);
  } finally {
    submit.disabled = false;
  }
});
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = new FormData(e.target);
  const button = e.submitter;
  button.disabled = true;
  try {
    const settings = {
      downloader: {
        timeout: Number(data.get("timeout")),
        retries: Number(data.get("retries")),
      },
      parser: {
        model: data.get("model"),
        device: data.get("device"),
        compute_type: data.get("compute_type"),
        language: data.get("language"),
        beam_size: Number(data.get("beam_size")),
        vad_filter: data.has("vad_filter"),
      },
      output: {
        formats: data.getAll("formats"),
        timestamps: data.has("timestamps"),
      },
    };
    state.settings = await api("/settings", {
      method: "PUT",
      body: JSON.stringify(settings),
    });
    $("#settings-error").textContent = "";
    toast("配置已保存，将用于新任务");
  } catch (error) {
    $("#settings-error").textContent = messageZh(error.message);
  } finally {
    button.disabled = false;
  }
});
$("#detail-dialog").addEventListener("close", () => (state.detail = null));
$("#prev-page").addEventListener("click", () => {
  if (state.currentPage > 1) {
    state.currentPage--;
    renderLibrary();
  }
});
$("#next-page").addEventListener("click", () => {
  const maxPages = totalPages(filtered());
  if (state.currentPage < maxPages) {
    state.currentPage++;
    renderLibrary();
  }
});
$("#page-size").addEventListener("change", (e) => {
  state.pageSize = Number(e.target.value);
  state.currentPage = 1;
  renderLibrary();
});
// Context menu handlers
document.addEventListener("contextmenu", (event) => {
  const row = event.target.closest("[data-resource-row]");
  if (!row) {
    hideContextMenu();
    return;
  }
  event.preventDefault();
  const resourceId = Number(row.dataset.resourceRow);
  const resource = state.resources.find((r) => r.id === resourceId);
  if (!resource) return;
  state.contextMenuResource = resource;
  const menu = $("#context-menu");
  const menuItems = menu.querySelectorAll("[data-action]");
  menuItems.forEach((item) => {
    const action = item.dataset.action;
    if (action === "parse") {
      item.disabled = !resource.audio.length;
    } else if (action === "export") {
      item.disabled = !resource.parsed.length;
    } else if (action === "delete-audio") {
      item.disabled = !resource.audio.length;
    } else if (action === "delete-parsed") {
      item.disabled = !resource.parsed.length;
    } else if (action === "delete-outputs") {
      item.disabled = !resource.outputs.length;
    }
  });
  menu.style.left = event.pageX + "px";
  menu.style.top = event.pageY + "px";
  menu.hidden = false;
});
document.addEventListener("click", (event) => {
  if (!event.target.closest("#context-menu")) {
    hideContextMenu();
  }
});
function hideContextMenu() {
  $("#context-menu").hidden = true;
  state.contextMenuResource = null;
}
$("#context-menu").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button || !state.contextMenuResource) return;
  const action = button.dataset.action;
  const resource = state.contextMenuResource;
  hideContextMenu();
  try {
    if (action === "download") {
      state.selected.clear();
      state.selected.add(resource.id);
      await openJob("download");
    } else if (action === "parse") {
      state.selected.clear();
      state.selected.add(resource.id);
      await openJob("parse");
    } else if (action === "export") {
      state.selected.clear();
      state.selected.add(resource.id);
      await openJob("export");
    } else if (action === "delete-audio") {
      if (
        confirm(`确定要删除「${resource.title}」的音频文件吗？\n\n此操作不可撤销。`)
      ) {
        await api(`/resources/${resource.id}/files/audio`, { method: "DELETE" });
        toast("已删除音频文件");
        await refresh();
      }
    } else if (action === "delete-parsed") {
      if (
        confirm(
          `确定要删除「${resource.title}」的转录数据吗？\n\n此操作不可撤销。`,
        )
      ) {
        await api(`/resources/${resource.id}/files/parsed`, {
          method: "DELETE",
        });
        toast("已删除转录数据");
        await refresh();
      }
    } else if (action === "delete-outputs") {
      if (
        confirm(
          `确定要删除「${resource.title}」的导出文件吗？\n\n此操作不可撤销。`,
        )
      ) {
        await api(`/resources/${resource.id}/files/outputs`, {
          method: "DELETE",
        });
        toast("已删除导出文件");
        await refresh();
      }
    } else if (action === "delete-resource") {
      if (
        confirm(
          `确定要删除资源记录「${resource.title}」吗？\n\n这将删除该资源的所有文件（音频、转录数据、导出文件）和数据库记录。\n\n此操作不可撤销！`,
        )
      ) {
        await api(`/resources/${resource.id}`, { method: "DELETE" });
        toast("已删除资源记录");
        state.selected.delete(resource.id);
        await refresh();
      }
    }
  } catch (error) {
    toast(messageZh(error.message), true);
  }
});
refresh();
setInterval(refresh, 2000);
