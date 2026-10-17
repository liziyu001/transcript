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
  sort: "added-desc",
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
  cloud: "云端解析任务",
  metadata: "刷新视频信息",
  download: "下载并上传",
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
function parserSummary(parser) { return `${parser.model} · 北京 · ${parser.language}`; }
function durationText(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "时长未知";
  const value = Math.round(seconds);
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  return `${hours}:${String(minutes).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
}
function metadataSummary(resource) {
  const metadata = resource.metadata || {};
  return `总时长 ${durationText(metadata.duration)}${metadata.part_count ? ` · ${metadata.part_count} 个分P` : ""}`;
}
function renderMetadata(resource) {
  const metadata = resource.metadata || {};
  return `<h3>视频信息 · ${esc(metadataSummary(resource))}</h3>
    <p class="muted">合集导入直接保存总时长。需要分P信息时，可手动刷新视频信息，无需下载音频。${resource.metadata_updated ? `更新于 ${esc(date(resource.metadata_updated))}` : ""}</p>
    ${resource.metadata_error ? `<p class="form-error">最近一次获取失败：${esc(messageZh(resource.metadata_error))}。可刷新重试；已有文件和信息会保留。</p>` : ""}
    ${(metadata.pages || []).map((part) => `<div class="file-row"><a href="${esc(resource.url)}?p=${encodeURIComponent(part.page)}" target="_blank" rel="noreferrer">P${esc(part.page)} · ${esc(part.title)}</a><span>${esc(durationText(part.duration))}</span></div>`).join("")}
    <button data-metadata="${resource.id}">↻ 刷新视频信息（含分P）</button>`;
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
function compareResources(a, b) {
  const fallback = String(b.id).localeCompare(String(a.id));
  if (state.sort.startsWith("duration-")) {
    const left = a.metadata?.duration;
    const right = b.metadata?.duration;
    const leftKnown = Number.isFinite(left) && left >= 0;
    const rightKnown = Number.isFinite(right) && right >= 0;
    // Unknown lengths stay last in either direction; zero is a known duration.
    if (leftKnown !== rightKnown) return leftKnown ? -1 : 1;
    if (!leftKnown) return fallback;
    return (state.sort === "duration-desc" ? right - left : left - right) || fallback;
  }
  if (state.sort.startsWith("title-")) {
    const order = (a.title || "").localeCompare(b.title || "", "zh-CN", { numeric: true });
    return (state.sort === "title-desc" ? -order : order) || fallback;
  }
  const order = (Date.parse(a.created) || 0) - (Date.parse(b.created) || 0);
  return state.sort === "added-asc" ? order || -fallback : -order || fallback;
}
function filtered() {
  return state.resources.filter(
    (r) =>
      (state.collection === null || r.collections.includes(state.collection)) &&
      (state.filter === "all" ||
        (state.filter === "ready"
          ? r.audio.length && !r.parsed_complete
          : r.parsed_complete)) &&
      `${r.title} ${r.url}`.toLowerCase().includes(state.search.toLowerCase()),
  ).sort(compareResources);
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
  $("#collection-tools").hidden = !collection || collection.id === "general";
  $("#library-title").textContent = collection
    ? collectionName(collection)
    : "资源中心";
  const metrics = [
    ["资源总数", all.length, "资源库中的全部资源", "▤"],
    [
      "音频已就绪",
      all.filter((r) => r.audio.length).length,
      "已上传 OSS，可直接解析",
      "↓",
    ],
    [
      "已转录",
      all.filter((r) => r.parsed_complete).length,
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
        `<tr data-resource-row="${r.id}"><td class="check-cell"><input type="checkbox" data-select="${r.id}" aria-label="选择 ${esc(r.title)}" ${state.selected.has(r.id) ? "checked" : ""}></td><td><div class="resource-cell"><span class="media-icon">▷</span><div><button class="resource-name" data-resource="${r.id}">${esc(r.title)}</button><a class="resource-url" href="${esc(r.url)}" target="_blank" rel="noreferrer">${esc(r.url)}</a><div class="resource-duration">${esc(metadataSummary(r))}${r.metadata_error ? " · 信息获取失败，可重试" : ""}</div></div></div></td><td>${esc(
          r.collections
            .map((id) =>
              collectionName(state.collections.find((c) => c.id === id)),
            )
            .filter(Boolean)
            .join(", "),
        )}</td><td>${r.audio.length ? badge("✓ 已就绪", "green") : badge(r.cleanup_pending?.length ? "清理待重试" : r.audio_deleted_count ? "已清理" : "未下载")}</td><td>${r.parsed_complete ? badge(r.exported_complete ? "✓ 已导出" : "✓ 已解析", "green") : r.parsed.length ? badge(`解析中 ${r.parsed.length}/${r.part_total || r.audio.length}`, "blue") : badge("未解析")}</td><td><button class="icon-button" data-resource="${r.id}" aria-label="打开 ${esc(r.title)}">↗</button></td></tr>`,
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
  $("#cleanup-selected").disabled = !n;
  $("#bundle-selected").disabled = !n;
  $$("[data-stage]").forEach((x) => (x.disabled = !n));
  const rows = filtered();
  const count = rows.filter((r) => state.selected.has(r.id)).length;
  $("#select-all").checked = rows.length > 0 && count === rows.length;
  $("#select-all").indeterminate = count > 0 && count < rows.length;
}
function controls(job) {
  const button = (action, label) => `<button data-control="${action}" data-job="${job.id}">${label}</button>`;
  if (job.cloud) return `<div class="job-controls">${["failed", "needs_attention"].includes(job.remote_state) ? button("retry", "重试已保存任务") + button("recover", "恢复提供方任务 ID") + button("dismiss", "检查后释放占用") : ["queued", "submitting", "running", "exporting"].includes(job.remote_state) ? button("resume", "恢复停滞工作流") : ""}</div>`;
  return `<div class="job-controls">${["queued", "running"].includes(job.state) ? button("pause", "暂停") : ""}${job.state === "paused" ? button("resume", "继续") : ""}${["failed", "cancelled"].includes(job.state) ? button("retry", "重试未完成项") : ""}${["queued", "running", "paused", "pausing"].includes(job.state) ? button("cancel", "取消") : ""}</div>`;
}
function renderJobs() {
  $("#jobs-list").innerHTML = state.jobs.length
    ? state.jobs
        .map(
          (j) =>
            `<article class="job-card"><div class="job-card-head"><button class="job-title" data-job-detail="${j.id}">${stageNames[j.kind]} <span class="muted">#${j.id.slice(-8)}</span></button>${statusBadge(j.state)}${j.dry_run ? badge("试运行", "orange") : ""}<time class="job-time">${date(j.created)}</time></div><div class="job-meta"><span>已完成 ${j.completed} / ${j.total}${j.failed ? ` · ${j.failed} 项失败` : ""}</span><span>${Math.round(j.progress || 0)}%</span></div>${progress(j.progress)}<div class="job-bottom"><small>${j.state === "pausing" ? "当前资源处理完成后暂停。" : j.state === "paused" ? "可随时继续，已完成的资源会保留。" : j.state === "failed" ? "打开详情查看错误；重试仅处理未完成的资源。" : "任务配置与进度已保存。"}</small>${controls(j)}<button class="text-button" data-job-detail="${j.id}">查看详情 →</button></div></article>`,
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
      if (state.detail?.kind === "resource" && $("#resource-metadata")) {
        const resource = state.resources.find((r) => r.id === state.detail.id);
        if (resource) {
          $("#resource-metadata").innerHTML = renderMetadata(resource);
          $("#detail-title").textContent = resource.title;
        }
      }
    }
    $("#connection").textContent = "已连接云端资源库";
    $("#cloud-error").hidden = true;
    $("#connection-dot").style.background = "#63a185";
    if (state.detail?.kind === "job" && $("#detail-dialog").open)
      await renderJobDetail(state.detail.id);
  } catch (error) {
    $("#connection").textContent = "云端连接失败 · 请检查配置";
    $("#cloud-error").textContent = error.message;
    $("#cloud-error").hidden = false;
    $("#connection-dot").style.background = "#c47f67";
    $("#connection").title = error.message;
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
    `已选择 ${state.selected.size} 个资源。 ${{ download: "在本机下载音频，上传到 OSS 后可进行云端解析。", parse: "在云端解析 OSS 音频，结果保存在 OSS。", export: "从已保存的转录数据生成文件。", pipeline: "在一个任务中依次完成本地下载 → 上传 OSS → 云端解析与导出。" }[stage]}`;
  const s = state.settings;
  $("#job-config-summary").innerHTML =
    `下载器: 超时 ${s.downloader.timeout} 秒 · 重试 ${s.downloader.retries} 次<br>解析器: ${esc(parserSummary(s.parser))}<br>输出: ${s.output.formats.map(esc).join(", ").toUpperCase()}<br><span class="muted">如需调整默认设置，请先前往配置管理。切换解析器后，请勾选重新处理以替换已有转录。</span>`;
  $("#job-error").textContent = "";
  $("#job-dry").checked = false;
  $("#job-force").checked = false;
  $("#job-force").disabled = ["export", "metadata"].includes(stage);
  if (stage === "metadata") {
    $("#job-description").textContent = `已选择 ${state.selected.size} 个资源。仅获取标题、分P信息和总时长，不下载音频。`;
    $("#job-config-summary").textContent = `请求超时 ${s.downloader.timeout} 秒 · 重试 ${s.downloader.retries} 次。已有音频和转录结果会保留。`;
  }
  $("#job-dialog").showModal();
}
async function renderJobDetail(id) {
  const job = await api("/jobs/" + id);
  if (state.detail?.kind !== "job" || state.detail.id !== id) return;
  const signature = JSON.stringify(job);
  if (state.detail.signature === signature) return;
  state.detail.signature = signature;
  $("#detail-title").textContent = `${stageNames[job.kind]} #${job.id.slice(-8)}`;
  $("#detail-eyebrow").textContent = job.dry_run
    ? "试运行 · 不修改资源"
    : "任务详情";
  $("#detail-content").innerHTML =
    `<div class="job-card-head">${statusBadge(job.state)}<span class="muted">${date(job.created)}</span></div><div class="job-bottom">${controls(job)}<small>暂停在本地阶段结束后生效；取消终止本地进程。已提交的云端任务继续运行。</small></div><div class="detail-section"><h3>资源与进度</h3>${job.items.map((i) => `<div class="item-row"><div class="item-row-head"><strong>${esc(i.title || job.config.collection?.name || "合集解析")}</strong>${statusBadge(i.state)}</div>${progress(i.progress)}<p>${esc(i.stage ? stageNames[i.stage] + " · " : "")}${esc(messageZh(i.message) || "等待工作进程处理")}</p>${job.dry_run && i.state === "completed" ? renderPlan(i.result) : ""}${job.kind === "discover" && !job.dry_run && i.state === "completed" ? `<p>新增 ${i.result.added} 个资源 · ${i.result.existing} 个已存在。</p>` : ""}<div class="item-actions"><button data-log="${i.id}">查看日志</button>${i.resource_id ? `<button data-resource="${i.resource_id}">打开资源</button>` : ""}</div></div>`).join("")}</div><div class="detail-section"><h3>任务动态</h3><ul class="events">${job.events.map((e) => `<li><time>${date(e.created)}</time>${esc(messageZh(e.message))}</li>`).join("")}</ul></div><div class="detail-section"><h3>任务配置快照</h3><pre>${esc(JSON.stringify(configZh(job.config), null, 2))}</pre></div>`;
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
        const name = `${index + 1} · ${path.split(/[\\/]/).pop()}`;
        const url = `/api/resources/${r.id}/files/${category}/${index}`;
        return `<div class="file-row"><span>${esc(name)}</span>${category !== "audio" ? `<button data-preview="${url}">预览</button>` : ""}<a href="${url}?download=true">下载 ↓</a></div>${category === "audio" ? `<audio controls preload="none" src="${url}"></audio>` : ""}`;
      })
      .join("");
  $("#detail-content").innerHTML =
    `<p><button data-cleanup="${r.id}">清理 OSS 文件</button> <button data-refresh-resource="${r.id}">刷新文件列表</button> <button data-edit-resource="${r.id}">编辑资源与合集</button></p><p><a href="${esc(r.url)}" target="_blank" rel="noreferrer">打开原始视频 ↗</a></p><div class="detail-section" id="resource-metadata">${renderMetadata(r)}</div><div class="detail-section"><h3>OSS 音频 · ${r.audio.length} 个分段</h3>${files("audio", r.audio) || "<p>暂无 OSS 音频，请运行下载并上传。</p>"}</div><div class="detail-section"><h3>转录数据</h3>${files("parsed", r.parsed) || "<p>暂无转录数据，请先下载音频，再运行解析。</p>"}</div><div class="detail-section"><h3>OSS 导出文件</h3>${files("outputs", r.outputs) || "<p>解析完成后运行导出，即可生成文本或字幕文件。</p>"}</div><pre class="resource-preview" id="file-preview" hidden></pre>`;
  if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
}
function renderSettings() {
  const s = state.settings;
  if (!s) { api("/settings").then(x => { state.settings = x; renderSettings(); }).catch(e => toast(e.message, true)); return; }
  const options = (values, current) => values.map(v => `<option value="${v}" ${v === current ? "selected" : ""}>${esc(v)}</option>`).join("");
  $("#settings-form").innerHTML = `<section class="settings-card"><div><h3>01 &nbsp; 本地下载与上传</h3><p>合集解析、视频信息查询和音频下载在本机执行。上传完成后，音频保存在 OSS。</p></div><div class="settings-fields"><label>请求超时（秒）<input name="timeout" type="number" min="5" max="600" value="${s.downloader.timeout}" required></label><label>重试次数<input name="retries" type="number" min="0" max="20" value="${s.downloader.retries}" required></label><p class="field-note span-two">本地缓存和断点保存在 Compose 数据卷。已有 OSS 音频默认复用；重新下载需勾选“重新处理”。</p></div></section>
  <section class="settings-card"><div><h3>02 &nbsp; 云端解析</h3><p>使用北京地域的阿里云百炼 API。密钥由云端部署配置，上传凭据由本地环境提供。</p></div><div class="settings-fields"><label>解析模型<select name="model">${options(["qwen-audio-3.1-asr-flash-filetrans", "qwen3-asr-flash-filetrans", "paraformer-v2"], s.parser.model)}</select></label><label>识别语言<select name="language">${options(["auto", "zh", "en", "ja", "ko", "de", "fr", "ru", "es"], s.parser.language)}</select><span class="field-note">auto 自动识别 · zh 中文 · en 英文</span></label><label>云端并发数<input name="processing_concurrency" type="number" min="1" max="16" value="${s.processing_concurrency}" required></label><p class="field-note span-two">调整并发数影响后续云端任务。已提交的任务会继续运行。每个音频文件最多 12 小时 / 2 GB。</p></div></section>
  <section class="settings-card"><div><h3>03 &nbsp; OSS 输出</h3><p>从已保存的识别结果导出，不会重复调用语音识别。</p></div><div><label>文件格式</label><div class="format-options">${["txt", "vtt", "srt", "json"].map(f => `<label class="checkbox-label"><input type="checkbox" name="formats" value="${f}" ${s.output.formats.includes(f) ? "checked" : ""}>${f.toUpperCase()}</label>`).join("")}</div><label class="checkbox-label"><input name="timestamps" type="checkbox" ${s.output.timestamps ? "checked" : ""}>纯文本添加时间戳</label><p class="field-note">文件保存在 OSS，下载时生成临时链接。重新导出会保留历史版本。</p></div></section><p id="settings-error" class="form-error" role="alert"></p>`;
}
document.addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  const d = target.dataset;
  try {
    if (d.bundle) openBundle();
    if (d.cleanup) openCleanup(d.cleanup === "selected" ? [...state.selected] : [d.cleanup]);
    if (d.refreshResource) openResource(d.refreshResource);
    if (d.editResource) {
      const r = state.resources.find(x => x.id === d.editResource);
      state.detail = {kind: "edit"};
      $("#detail-title").textContent = "编辑云端资源";
      $("#detail-content").innerHTML = `<label>标题<input id="edit-title" maxlength="500" value="${esc(r.title)}"></label><h3>所属合集</h3>${state.collections.map(c => `<label class="checkbox-label"><input type="checkbox" name="membership" value="${c.id}" ${r.collections.includes(c.id) ? "checked" : ""}>${esc(collectionName(c))}</label>`).join("")}<button class="primary" data-save-resource="${r.id}">保存到云端</button>`;
    }
    if (d.saveResource) {
      await api(`/resources/${d.saveResource}`, {method: "PATCH", body: JSON.stringify({title: $("#edit-title").value.trim(), collections: $$("[name=membership]:checked").map(x => x.value)})});
      await refresh(); openResource(d.saveResource); toast("云端资源已更新");
    }
    if (d.nav) {
      if (d.nav === "library") {
        state.collection = null;
        renderSidebar();
        renderLibrary();
      }
      show(d.nav);
    }
    if (d.collection) {
      state.collection = d.collection;
      state.currentPage = 1;
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
    if (d.jobDetail) await openJobDetail(d.jobDetail);
    if (d.resource) openResource(d.resource);
    if (d.metadata) {
      target.disabled = true;
      const job = await post("/jobs", { kind: "metadata", resource_ids: [d.metadata] });
      await refresh();
      await openJobDetail(job.id);
    }
    if (d.control) {
      target.disabled = true;
      let action = d.control, body;
      if (action === "recover") { const id = prompt("输入已在百炼确认的提供方任务 ID："); if (!id) return; body = {task_id: id.trim()}; action = "retry"; }
      if (action === "dismiss") { if (!confirm("已检查百炼任务状态？释放占用不会取消或退款已提交的任务。")) return; body = {checked_provider: true}; }
      await post(`/jobs/${d.job}/${action}`, body);
      toast("操作已提交");
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
      const response = await fetch(d.preview + "?preview=true");
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
$("#resource-sort").addEventListener("change", (e) => {
  state.sort = e.target.value;
  state.currentPage = 1;
  renderLibrary();
});
$("#mobile-collection").addEventListener("change", (e) => {
  state.collection = e.target.value || null;
  state.selected.clear();
  state.currentPage = 1;
  renderSidebar();
  renderLibrary();
});
$("#resource-rows").addEventListener("change", (e) => {
  if (e.target.dataset.select) {
    const id = e.target.dataset.select;
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
        `已添加 ${result.added} 个资源至通用合集 · ${result.existing} 个已存在${result.metadata_job_id ? " · 视频信息查询已排队" : ""}`,
      );
      $("#links-input").value = "";
      state.collection = "general";
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
      processing_concurrency: Number(data.get("processing_concurrency")),
      downloader: {timeout: Number(data.get("timeout")), retries: Number(data.get("retries"))},
      parser: {model: data.get("model"), language: data.get("language")},
      output: {formats: data.getAll("formats"), timestamps: data.has("timestamps")},
    };
    state.settings = await api("/settings", {
      method: "PUT",
      body: JSON.stringify(settings),
    });
    renderSettings();
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
  const resourceId = row.dataset.resourceRow;
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
    }
  });
  menu.style.left = event.pageX + "px";
  menu.style.top = event.pageY + "px";
  menu.hidden = false;
});
function hideContextMenu() {
  $("#context-menu").hidden = true;
  state.contextMenuResource = null;
}
document.addEventListener("click", event => {
  if (!event.target.closest("#context-menu")) hideContextMenu();
});
$("#context-menu").addEventListener("click", async event => {
  const button = event.target.closest("[data-action]");
  if (!button || !state.contextMenuResource) return;
  const resource = state.contextMenuResource;
  const action = button.dataset.action;
  hideContextMenu();
  try {
    if (["download", "parse", "export"].includes(action)) {
      state.selected = new Set([resource.id]);
      await openJob(action);
    } else if (action === "cleanup") {
      openCleanup([resource.id]);
    } else if (action === "delete-resource") {
      if (confirm(`归档「${resource.title}」？云端文件与任务历史会保留。重新导入链接可恢复记录。`)) {
        await api(`/resources/${resource.id}`, {method: "DELETE"});
        state.selected.delete(resource.id); await refresh(); toast("资源已归档，OSS 文件已保留");
      }
    }
  } catch (error) {
    toast(messageZh(error.message), true);
  }
});
refresh();
setInterval(refresh, 5000);

$("#sync-collection").onclick = async () => {
  const c = state.collections.find(x => x.id === state.collection); if (!c?.url) return;
  try { const job = await post("/import/collection", {url: c.url, name: c.name}); show("jobs"); await openJobDetail(job.id); } catch(e) { toast(e.message, true); }
};
$("#rename-collection").onclick = async () => {
  const c = state.collections.find(x => x.id === state.collection);
  const name = prompt("合集名称：", c.name); if (!name?.trim()) return;
  try { await api(`/collections/${c.id}`, {method: "PATCH", body: JSON.stringify({name: name.trim()})}); await refresh(); } catch(e) { toast(e.message, true); }
};
$("#remove-collection").onclick = async () => {
  const c = state.collections.find(x => x.id === state.collection);
  if (!confirm(`移除合集「${c.name}」？资源、音频和结果会保留。`)) return;
  try { await api(`/collections/${c.id}`, {method: "DELETE"}); state.collection = null; await refresh(); } catch(e) { toast(e.message, true); }
};

let cleanupIds = [];
const cleanupDescriptions = {
  audio: "永久删除 OSS 音频。保留转录数据和导出文件，可继续下载、重新导出。再次解析需重新下载并上传音频。未解析的音频也会删除。",
  outputs: "删除所有历史导出文件，保留原始转录数据，可重新导出。",
  results: "删除原始转录数据及所有历史导出文件。恢复结果需要重新解析，可能产生费用。",
  all: "删除音频、原始转录数据及所有历史导出文件。恢复需要重新下载、上传和解析。"
};
function describeCleanup() {
  $("#cleanup-description").textContent = cleanupDescriptions[$("#cleanup-category").value];
  $("#cleanup-confirm").checked = false;
}
function openCleanup(ids) {
  cleanupIds = ids;
  const rows = state.resources.filter(r => ids.includes(r.id));
  const bytes = rows.reduce((n, r) => n + (r.oss_audio_bytes || 0), 0);
  $("#cleanup-summary").textContent = `已选择 ${rows.length} 个资源；当前音频约 ${(bytes / 1024 / 1024).toFixed(1)} MB；其中 ${rows.filter(r => r.parsed_complete).length} 个已完成解析。`;
  $("#cleanup-category").value = rows.find(r => r.cleanup_pending?.length)?.cleanup_pending[0] || "audio";
  $("#cleanup-error").textContent = "";
  describeCleanup();
  $("#cleanup-dialog").showModal();
}
$("#cleanup-category").onchange = describeCleanup;
$("#cleanup-form").onsubmit = async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  $("#cleanup-error").textContent = "";
  try {
    await post("/cleanup", {resource_ids: cleanupIds, category: $("#cleanup-category").value, confirmed: $("#cleanup-confirm").checked});
    $("#cleanup-dialog").close();
    toast("OSS 文件已清理，资源记录已保留");
  } catch (e) {
    $("#cleanup-error").textContent = messageZh(e.message);
  } finally {
    button.disabled = false;
    await refresh();
    if (state.detail?.kind === "resource") openResource(state.detail.id);
  }
};

let bundleSelection = [], bundleCollection = null, bundleBusy = false;
function openBundle() {
  if (bundleBusy) { $("#bundle-dialog").showModal(); return; }
  bundleSelection = [...state.selected];
  bundleCollection = state.collection;
  $("#bundle-summary").textContent = `已选择 ${bundleSelection.length} 个资源。${bundleCollection ? "按当前合集组织目录。" : "按所属合集组织目录；属于多个合集的视频会在各目录中保存一份。"}`;
  $("#bundle-error").textContent = "";
  $("#bundle-dialog").showModal();
}
$("#bundle-form").onsubmit = async event => {
  event.preventDefault();
  const formats = $$("[name=bundle-format]:checked").map(x => x.value);
  if (!formats.length) { $("#bundle-error").textContent = "请至少选择一种格式。"; return; }
  if (bundleBusy) return;
  bundleBusy = true;
  const button = event.submitter;
  button.disabled = true; button.textContent = "正在从 OSS 下载并打包…";
  $("#bundle-error").textContent = "";
  try {
    const response = await fetch("/api/download-results", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({resource_ids: bundleSelection, formats, collection_id: bundleCollection})});
    if (!response.ok) {
      let message = `下载失败（HTTP ${response.status}）`;
      try { const data = await response.json(); if (typeof data.detail === "string") message = data.detail; } catch {}
      throw Error(message);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url; link.download = `转录结果_${new Date().toISOString().slice(0,10)}.zip`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    const missing = Number(response.headers.get("X-Bundle-Missing") || 0);
    $("#bundle-dialog").close();
    toast(missing ? `ZIP 已下载；${missing} 项结果缺失，详见下载清单.json` : "ZIP 已下载，按合集、视频和分P保存");
  } catch (e) { $("#bundle-error").textContent = messageZh(e.message); }
  finally { bundleBusy = false; button.disabled = false; button.textContent = "打包并下载"; }
};
