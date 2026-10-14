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
};
const stageNames = {
  download: "Download audio",
  parse: "Parse audio",
  export: "Export transcripts",
  pipeline: "Full pipeline",
  discover: "Discover collection",
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
      throw Error(`Request failed (${response.status})`);
    }
    const detail = data.detail;
    throw Error(
      Array.isArray(detail)
        ? detail.map((x) => x.msg).join("\n")
        : detail || "Request failed",
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
  return new Date(value).toLocaleString(undefined, {
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
    status.replaceAll("_", " "),
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
    library: "Resource center",
    jobs: "Job queue",
    settings: "Configuration",
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
function renderSidebar() {
  $("#library-count").textContent = state.resources.length;
  $("#queue-count").textContent = state.jobs.filter((j) =>
    activeStates.includes(j.state),
  ).length;
  $("#collections").innerHTML = state.collections
    .map(
      (c) =>
        `<button class="collection-item ${state.collection === c.id ? "active" : ""}" data-collection="${c.id}"><span>▱</span><span class="collection-name">${esc(c.name)}</span><span class="nav-count">${c.count}</span></button>`,
    )
    .join("");
  $("#mobile-collection").innerHTML =
    '<option value="">All collections</option>' +
    state.collections
      .map(
        (c) =>
          `<option value="${c.id}" ${state.collection === c.id ? "selected" : ""}>${esc(c.name)} (${c.count})</option>`,
      )
      .join("");
}
function renderLibrary() {
  const all = state.resources;
  const collection = state.collections.find((c) => c.id === state.collection);
  $("#library-title").textContent = collection
    ? collection.name
    : "Resource center";
  const metrics = [
    ["Total resources", all.length, "Across your library", "▤"],
    [
      "Audio ready",
      all.filter((r) => r.audio.length).length,
      "Downloaded and available",
      "↓",
    ],
    [
      "Transcribed",
      all.filter((r) => r.parsed.length).length,
      "Ready for your next idea",
      "≋",
    ],
    [
      "Active jobs",
      state.jobs.filter((j) => activeStates.includes(j.state)).length,
      "Working in the background",
      "↗",
    ],
  ];
  $("#stats").innerHTML = metrics
    .map(
      ([label, n, note, icon]) =>
        `<div class="stat"><div class="stat-name">${label}</div><div class="stat-number">${n}</div><div class="stat-note">${note}</div><span class="stat-icon">${icon}</span></div>`,
    )
    .join("");
  const rows = filtered();
  $("#resource-rows").innerHTML = rows
    .map(
      (r) =>
        `<tr><td class="check-cell"><input type="checkbox" data-select="${r.id}" aria-label="Select ${esc(r.title)}" ${state.selected.has(r.id) ? "checked" : ""}></td><td><div class="resource-cell"><span class="media-icon">▷</span><div><button class="resource-name" data-resource="${r.id}">${esc(r.title)}</button><a class="resource-url" href="${esc(r.url)}" target="_blank" rel="noreferrer">${esc(r.url)}</a></div></div></td><td>${esc(
          r.collections
            .map((id) => state.collections.find((c) => c.id === id)?.name)
            .filter(Boolean)
            .join(", "),
        )}</td><td>${r.audio.length ? badge("✓ Ready", "green") : badge("Not downloaded")}</td><td>${r.parsed.length ? badge(r.outputs.length ? "✓ Exported" : "✓ Parsed", "green") : badge("Not parsed")}</td><td><button class="icon-button" data-resource="${r.id}" aria-label="Open ${esc(r.title)}">↗</button></td></tr>`,
    )
    .join("");
  $("#library-empty").hidden = rows.length > 0;
  if (all.length) {
    $("#library-empty h2").textContent = "No resources in this view";
    $("#library-empty p").textContent =
      "Try another collection, filter, or search. You can also add more resources.";
    $("#empty-import").textContent = "＋ Add resources";
  } else {
    $("#library-empty h2").textContent = "A home for your next transcript";
    $("#library-empty p").textContent =
      "Add a Bilibili collection or paste video links. Download and transcribe them whenever you’re ready.";
    $("#empty-import").textContent = "＋ Add your first resources";
  }
  $("#resource-count").textContent =
    `${rows.length} resource${rows.length === 1 ? "" : "s"}${state.search ? " matching your search" : ""}`;
  renderSelection();
}
function renderSelection() {
  const n = state.selected.size;
  $("#selection-label").textContent = n
    ? `${n} resource${n === 1 ? "" : "s"} selected`
    : "Select resources to start a job";
  $$("[data-stage]").forEach((x) => (x.disabled = !n));
  const rows = filtered();
  const count = rows.filter((r) => state.selected.has(r.id)).length;
  $("#select-all").checked = rows.length > 0 && count === rows.length;
  $("#select-all").indeterminate = count > 0 && count < rows.length;
}
function controls(job) {
  const button = (action, label) =>
    `<button data-control="${action}" data-job="${job.id}">${label}</button>`;
  return `<div class="job-controls">${["queued", "running"].includes(job.state) ? button("pause", "Pause") : ""}${job.state === "paused" ? button("resume", "Resume") : ""}${["failed", "cancelled"].includes(job.state) ? button("retry", "Retry incomplete") : ""}${["queued", "running", "paused", "pausing"].includes(job.state) ? button("cancel", "Cancel") : ""}</div>`;
}
function renderJobs() {
  $("#jobs-list").innerHTML = state.jobs.length
    ? state.jobs
        .map(
          (j) =>
            `<article class="job-card"><div class="job-card-head"><button class="job-title" data-job-detail="${j.id}">${stageNames[j.kind]} <span class="muted">#${j.id}</span></button>${statusBadge(j.state)}${j.dry_run ? badge("Dry run", "orange") : ""}<time class="job-time">${date(j.created)}</time></div><div class="job-meta"><span>${j.completed} / ${j.total} complete${j.failed ? ` · ${j.failed} failed` : ""}</span><span>${Math.round(j.progress || 0)}%</span></div>${progress(j.progress)}<div class="job-bottom"><small>${j.state === "pausing" ? "Pausing after the current resource finishes." : j.state === "paused" ? "Resume when you’re ready. Completed resources are preserved." : j.state === "failed" ? "Open details for errors. Retry processes only incomplete resources." : "Settings and progress are saved with this job."}</small>${controls(j)}<button class="text-button" data-job-detail="${j.id}">View details →</button></div></article>`,
        )
        .join("")
    : `<div class="empty-jobs"><div class="empty-icon">☷</div><h2>Room for your next task</h2><p>Select resources in your library to queue a download, transcription, or export.</p><button data-nav="library">Go to resource center →</button></div>`;
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
    $("#connection").textContent = "Connected to local server";
    $("#connection-dot").style.background = "#63a185";
    if (state.detail?.kind === "job" && $("#detail-dialog").open)
      await renderJobDetail(state.detail.id);
  } catch (error) {
    $("#connection").textContent = "Server offline · retrying";
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
    mode === "links" ? "Add to General" : "Queue discovery";
  if (!$("#import-dialog").open) $("#import-dialog").showModal();
}
async function openJob(stage) {
  state.stage = stage;
  try {
    state.settings = await api("/settings");
  } catch (e) {
    toast(e.message, true);
    return;
  }
  $("#job-dialog-title").textContent = stageNames[stage];
  $("#job-description").textContent =
    `${state.selected.size} selected resource(s). ${{ download: "Save audio locally for later parsing.", parse: "Transcribe downloaded audio into reusable transcript data.", export: "Create files from saved transcript data.", pipeline: "Download → parse → export, in one job." }[stage]}`;
  const s = state.settings;
  $("#job-config-summary").innerHTML =
    `Downloader: ${s.downloader.timeout}s timeout · ${s.downloader.retries} retries<br>Parser: ${esc(s.parser.model)} · ${esc(s.parser.device)} · ${esc(s.parser.language)}<br>Output: ${s.output.formats.map(esc).join(", ").toUpperCase()}<br><span class="muted">Change defaults in Configuration before starting.</span>`;
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
    ? "DRY RUN · NO RESOURCE CHANGES"
    : "JOB DETAILS";
  $("#detail-content").innerHTML =
    `<div class="job-card-head">${statusBadge(job.state)}<span class="muted">${date(job.created)}</span></div><div class="job-bottom">${controls(job)}<small>Pause takes effect after the current resource. Cancel stops it.</small></div><div class="detail-section"><h3>Resources & progress</h3>${job.items.map((i) => `<div class="item-row"><div class="item-row-head"><strong>${esc(i.title || job.config.collection?.name || "Collection discovery")}</strong>${statusBadge(i.state)}</div>${progress(i.progress)}<p>${esc(i.stage ? i.stage + " · " : "")}${esc(i.message || "Waiting for the worker")}</p>${job.dry_run && i.state === "completed" ? renderPlan(i.result) : ""}${job.kind === "discover" && !job.dry_run && i.state === "completed" ? `<p>Added ${i.result.added} memberships · ${i.result.existing} already present.</p>` : ""}<div class="item-actions"><button data-log="${i.id}">View log</button>${i.resource_id ? `<button data-resource="${i.resource_id}">Open resource</button>` : ""}</div></div>`).join("")}</div><div class="detail-section"><h3>Activity</h3><ul class="events">${job.events.map((e) => `<li><time>${date(e.created)}</time>${esc(e.message)}</li>`).join("")}</ul></div><div class="detail-section"><h3>Configuration snapshot</h3><pre>${esc(JSON.stringify(job.config, null, 2))}</pre></div>`;
}
function renderPlan(result) {
  if (result.steps)
    return `<div class="job-plan">${result.steps.map((s) => (typeof s === "string" ? esc(s) : `<strong>${esc(s.stage)}</strong> — ${esc(s.action)}`)).join("<br>")}</div>`;
  return `<pre>${esc(JSON.stringify(result, null, 2))}</pre>`;
}
async function openJobDetail(id) {
  state.detail = { kind: "job", id };
  $("#detail-title").textContent = "Loading job…";
  $("#detail-content").textContent = "";
  if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
  try {
    await renderJobDetail(id);
  } catch (e) {
    toast(e.message, true);
  }
}
function openResource(id) {
  const r = state.resources.find((r) => r.id === id);
  if (!r) return;
  state.detail = { kind: "resource", id };
  $("#detail-eyebrow").textContent = "RESOURCE DETAILS";
  $("#detail-title").textContent = r.title;
  const files = (category, paths) =>
    paths
      .map((file, index) => {
        const path = category === "audio" ? file.path : file;
        const name = path.split(/[\\/]/).pop();
        const url = `/api/resources/${r.id}/files/${category}/${index}`;
        return `<div class="file-row"><span>${esc(name)}</span>${category !== "audio" ? `<button data-preview="${url}">Preview</button>` : ""}<a href="${url}?download=true">Download ↓</a></div>${category === "audio" ? `<audio controls preload="none" src="${url}"></audio>` : ""}`;
      })
      .join("");
  $("#detail-content").innerHTML =
    `<p><a href="${esc(r.url)}" target="_blank" rel="noreferrer">Open original video ↗</a></p><div class="detail-section"><h3>Audio · ${r.audio.length} part(s)</h3>${files("audio", r.audio) || "<p>No audio yet. Select this resource and run Download.</p>"}</div><div class="detail-section"><h3>Transcript data</h3>${files("parsed", r.parsed) || "<p>No transcript yet. Download audio, then run Parse.</p>"}</div><div class="detail-section"><h3>Exported files</h3>${files("outputs", r.outputs) || "<p>Run Export after parsing to create readable files.</p>"}</div><pre class="resource-preview" id="file-preview" hidden></pre>`;
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
      .catch((e) => toast(e.message, true));
    return;
  }
  const options = (values, current) =>
    values
      .map(
        (value) =>
          `<option value="${value}" ${value === current ? "selected" : ""}>${value}</option>`,
      )
      .join("");
  $("#settings-form").innerHTML =
    `<section class="settings-card"><div><h3>01 &nbsp; Downloader</h3><p>Network behavior for collection discovery and audio downloads.</p></div><div class="settings-fields"><label>Request timeout (seconds)<input name="timeout" type="number" min="5" max="600" value="${s.downloader.timeout}" required></label><label>Retries<input name="retries" type="number" min="0" max="20" value="${s.downloader.retries}" required></label><p class="field-note span-two">Audio stays in the persistent data directory. Existing audio is reused unless you enable reprocessing when starting a job.</p></div></section><section class="settings-card"><div><h3>02 &nbsp; Parser</h3><p>Local speech recognition with faster-whisper. The first use of a model downloads its weights.</p></div><div class="settings-fields"><label>Whisper model<select name="model">${options(["tiny", "base", "small", "medium", "large-v3", "turbo"], s.parser.model)}</select></label><label>Language<input name="language" value="${esc(s.parser.language)}" required maxlength="10"><span class="field-note">zh, en, ja… or auto to detect</span></label><label>Device<select name="device">${options(["cpu", "auto", "cuda"], s.parser.device)}</select></label><label>Compute precision<select name="compute_type">${options(["int8", "default", "auto", "float16", "float32"], s.parser.compute_type)}</select></label><label>Beam size<input name="beam_size" type="number" min="1" max="10" value="${s.parser.beam_size}" required></label><label class="checkbox-label"><input name="vad_filter" type="checkbox" ${s.parser.vad_filter ? "checked" : ""}>Skip silent sections (VAD)</label><p class="field-note span-two">CPU + int8 works with the included Docker image. CUDA requires a separately configured NVIDIA environment. Larger models use more memory; tiny or base is a good starting point.</p></div></section><section class="settings-card"><div><h3>03 &nbsp; Output</h3><p>Export reusable transcript data without repeating speech recognition.</p></div><div><label>File formats</label><div class="format-options">${["txt", "vtt", "srt", "json"].map((f) => `<label class="checkbox-label"><input type="checkbox" name="formats" value="${f}" ${s.output.formats.includes(f) ? "checked" : ""}>${f.toUpperCase()}</label>`).join("")}</div><label class="checkbox-label" style="margin-top:25px"><input name="timestamps" type="checkbox" ${s.output.timestamps ? "checked" : ""}>Include timestamps in plain text</label><p class="field-note">Each export creates a versioned folder. Current files are available from resource details; previous files remain on disk.</p></div></section><p class="form-error" id="settings-error" role="alert"></p>`;
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
      toast(`${d.control[0].toUpperCase() + d.control.slice(1)} requested`);
      await refresh();
    }
    if (d.log) {
      const log = await api(`/items/${d.log}/log`);
      state.detail = { kind: "log" };
      $("#detail-title").textContent = "Worker log";
      $("#detail-content").innerHTML = `<pre>${esc(log.text)}</pre>`;
    }
    if (d.preview) {
      target.disabled = true;
      const response = await fetch(d.preview);
      if (!response.ok)
        throw Error("File unavailable; rerun the appropriate stage.");
      const preview = $("#file-preview");
      preview.textContent = await response.text();
      preview.hidden = false;
      preview.scrollIntoView({ behavior: "smooth" });
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    target.disabled = false;
  }
});
$("#search").addEventListener("input", (e) => {
  state.search = e.target.value;
  renderLibrary();
});
$("#mobile-collection").addEventListener("change", (e) => {
  state.collection = e.target.value ? Number(e.target.value) : null;
  state.selected.clear();
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
        `${result.added} added to General · ${result.existing} already present`,
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
      toast("Collection discovery queued");
      show("jobs");
      await openJobDetail(result.id);
    }
    $("#import-dialog").close();
    await refresh();
  } catch (error) {
    $("#import-error").textContent = error.message;
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
    toast("Job queued");
    await refresh();
    await openJobDetail(result.id);
  } catch (error) {
    $("#job-error").textContent = error.message;
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
    toast("Settings saved for new jobs");
  } catch (error) {
    $("#settings-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$("#detail-dialog").addEventListener("close", () => (state.detail = null));
refresh();
setInterval(refresh, 2000);
