"use strict";
const $ = (id) => document.getElementById(id);
let endpoint = "", token = "", timer = null, refreshing = false;
let recordings = [], jobs = [];
// Retain pending request IDs across network retries; credentials are never persisted.
const pending = new Map();
$("endpoint").value = localStorage.getItem("transcript-endpoint") || location.origin;
function message(text, error = false) { $("message").textContent = text; $("message").className = error ? "error" : ""; }
function node(tag, text, className) { const e = document.createElement(tag); if (text != null) e.textContent = text; if (className) e.className = className; return e; }
async function api(path, body) {
  const response = await fetch(endpoint + path, { method: body === undefined ? "GET" : "POST",
    headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { const data = await response.json(); if (typeof data.detail === "string") detail = data.detail; } catch (_) { /* keep status */ }
    throw new Error(detail);
  }
  return response.json();
}
async function pages(path) {
  let cursor = null; const result = [];
  do { const page = await api(path + (cursor ? "?cursor=" + encodeURIComponent(cursor) : "")); result.push(...page.items); cursor = page.cursor; } while (cursor);
  return result;
}
function draw() {
  const checked = new Set([...document.querySelectorAll(".select-audio:checked")].map(e => e.value));
  $("audio").replaceChildren();
  for (const audio of recordings) {
    const row = node("tr"), cell = node("td"), checkbox = node("input");
    checkbox.type = "checkbox"; checkbox.className = "select-audio"; checkbox.value = audio.id;
    checkbox.checked = checked.has(audio.id); checkbox.setAttribute("aria-label", "Select " + audio.title); cell.append(checkbox);
    const title = node("td", audio.title); title.append(node("small", audio.id.slice(0, 12)));
    row.append(cell, title, node("td", audio.duration ? `${(audio.duration / 3600).toFixed(2)} h` : "Unknown"), node("td", `${(audio.size / 1024 ** 2).toFixed(1)} MB`)); $("audio").append(row);
  }
  $("audio-empty").hidden = recordings.length > 0;
  $("jobs-empty").hidden = jobs.length > 0;
  $("jobs").replaceChildren();
  for (const job of jobs.sort((a, b) => b.created - a.created)) {
    const card = node("article", null, "job"), info = node("div"), actions = node("div", null, "actions");
    info.append(node("h3", job.title), node("span", job.state.replaceAll("_", " "), "state " + job.state), node("p", `${job.options.model} · ${new Date(job.created * 1000).toLocaleString()}`));
    if (job.error) info.append(node("p", job.error));
    if (job.task_id) info.append(node("p", "Task: " + job.task_id));
    const action = (label, callback) => { const b = node("button", label, "secondary"); b.onclick = async () => { b.disabled = true; try { await callback(); await refresh(); } catch (e) { message(e.message, true); } finally { b.disabled = false; } }; actions.append(b); };
    const formats = Object.keys(job.outputs || {});
    if (job.raw_key) formats.push("raw");
    for (const fmt of formats) action(fmt.toUpperCase(), async () => {
        const data = await api(`/api/jobs/${job.id}/files/${fmt}`);
        const link = node("a"); link.href = data.url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.click();
      });
    if (job.state === "completed") {
      action("Re-export", () => api(`/api/jobs/${job.id}/export`, {}));
    } else if (job.state === "needs_attention") {
      action("Recover task ID", async () => {
        const taskId = prompt("Provider task ID from the DashScope console. This resumes it without a new paid submission.");
        if (taskId) await api(`/api/jobs/${job.id}/retry`, { task_id: taskId.trim() });
      });
    } else if (job.state === "failed") {
      action("Retry saved work", () => api(`/api/jobs/${job.id}/retry`, {}));
    } else if (job.state !== "dismissed") {
      action("Resume stalled workflow", () => api(`/api/jobs/${job.id}/resume`, {}));
    }
    if (["failed", "needs_attention"].includes(job.state)) action("Dismiss / release slot", async () => {
      if (confirm("Have you checked the provider console and confirmed this task is no longer running? Dismissing releases its concurrency slot; it does not cancel or refund a provider task.")) await api(`/api/jobs/${job.id}/dismiss`, { checked_provider: true });
    });
    card.append(info, actions); $("jobs").append(card);
  }
}
async function refresh() {
  if (refreshing || !token) return;
  refreshing = true;
  try { [recordings, jobs] = await Promise.all([pages("/api/audio"), pages("/api/jobs")]); draw(); }
  catch (e) { message(e.message, true); }
  finally { refreshing = false; }
}
$("connect").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const url = new URL($("endpoint").value);
    if (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname))) throw new Error("Use HTTPS for the cloud API.");
    endpoint = url.href.replace(/\/$/, ""); token = $("token").value;
    const config = await api("/api/config"); localStorage.setItem("transcript-endpoint", endpoint);
    $("connection").textContent = `${config.region} · ${config.max_active} active slots`;
    $("refresh").disabled = $("start").disabled = false; message("Connected. Select uploaded recordings to start recognition.");
    clearInterval(timer); await refresh(); timer = setInterval(() => { if (!document.hidden) refresh(); }, 15000);
  } catch (e) { token = ""; clearInterval(timer); message(e.message, true); }
};
$("disconnect").onclick = () => { token = ""; $("token").value = ""; clearInterval(timer); recordings = []; jobs = []; draw(); $("connection").textContent = "Disconnected"; $("refresh").disabled = $("start").disabled = true; message("Disconnected."); };
$("refresh").onclick = refresh;
$("all").onchange = () => { for (const c of document.querySelectorAll(".select-audio")) c.checked = $("all").checked; };
$("start").onclick = async () => {
  const selected = [...document.querySelectorAll(".select-audio:checked")].map(e => e.value);
  if (!selected.length) return message("Select at least one recording.");
  if (!confirm(`Start paid recognition for ${selected.length} recording(s)? This creates new jobs even if previous results exist.`)) return;
  $("start").disabled = true;
  try {
    for (const id of selected) {
      const options = { model: $("model").value, language: $("language").value };
      const key = JSON.stringify([id, options]);
      if (!pending.has(key)) pending.set(key, crypto.randomUUID());
      await api("/api/jobs", { audio_id: id, options, request_id: pending.get(key) });
      pending.delete(key);
      const checkbox = [...document.querySelectorAll(".select-audio")].find(e => e.value === id); if (checkbox) checkbox.checked = false;
    }
    message("Jobs started. You can close this page."); await refresh();
  } catch (e) { message(e.message + ". Retry to resume submissions that did not return a response.", true); }
  finally { $("start").disabled = false; }
};
