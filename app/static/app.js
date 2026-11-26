const $ = (sel) => document.querySelector(sel);
const state = { selected: null, page: 1, pageSize: 50, status: "", total: 0 };
const ACTIVE = ["draft", "generating_audio", "running"];

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const detail = body?.detail ?? body;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail, null, 2));
  }
  return body;
}

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const badge = (s) => `<span class="badge ${esc(s)}">${esc(s.replace(/_/g, " "))}</span>`;
const fmt = (d) => (d ? new Date(d).toLocaleString() : "");

async function loadOptions() {
  const opts = await api("/api/options");
  $("#language").innerHTML = opts.languages.map((l) => `<option value="${l.code}">${esc(l.name)}</option>`).join("");
  $("#voice").innerHTML = opts.voices.map((v) => `<option ${v === opts.default_voice ? "selected" : ""}>${v}</option>`).join("");
  $("#providers").textContent = `TTS: ${opts.tts_provider} · Telephony: ${opts.telephony_provider} · Storage: ${opts.storage_backend}`;
  const statuses = ["pending", "queued", "initiated", "ringing", "in_progress", "retry_scheduled", "completed", "no_answer", "busy", "failed", "cancelled"];
  $("#status-filter").innerHTML += statuses.map((s) => `<option value="${s}">${s.replace(/_/g, " ")}</option>`).join("");
}

async function loadCampaigns() {
  const campaigns = await api("/api/campaigns");
  $("#campaigns tbody").innerHTML = campaigns.map((c) => {
    const k = c.status_counts;
    return `<tr data-id="${c.id}" class="${c.id === state.selected ? "selected" : ""}">
      <td>${c.id}</td><td>${esc(c.name)}</td><td>${esc(c.language)}</td><td>${badge(c.status)}</td>
      <td>${c.total_calls}</td><td>${k.completed}</td><td>${k.failed + k.no_answer + k.busy}</td><td>${fmt(c.created_at)}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">No campaigns yet</td></tr>`;
}

async function loadDetail() {
  if (!state.selected) return;
  const c = await api(`/api/campaigns/${state.selected}`);
  $("#detail").classList.remove("hidden");
  $("#detail-title").innerHTML = `${esc(c.name)} ${badge(c.status)}`;
  $("#detail-meta").textContent = `Language ${c.language} · Voice ${c.voice} · ${c.personalized ? "Personalised audio per recipient" : "One shared audio"} · File ${c.source_filename || "-"}`;
  $("#detail-message").textContent = c.spoken_message || c.message;
  $("#detail-error").textContent = c.error || "";
  const audio = $("#detail-audio");
  if (c.has_audio) {
    const src = `/api/campaigns/${c.id}/audio?v=${encodeURIComponent(c.status)}`;
    if (audio.dataset.src !== src) { audio.src = src; audio.dataset.src = src; }
    audio.classList.remove("hidden");
  } else audio.classList.add("hidden");
  $("#btn-start").disabled = c.status !== "ready";
  $("#btn-regen").disabled = !["draft", "ready", "audio_failed"].includes(c.status);
  $("#btn-cancel").disabled = ["completed", "cancelled"].includes(c.status);
  $("#btn-export").href = `/api/campaigns/${c.id}/export`;
  $("#detail-counts").innerHTML = Object.entries(c.status_counts).filter(([, n]) => n).map(([s, n]) => `${badge(s)} ${n}`).join(" ");

  const q = new URLSearchParams({ page: state.page, page_size: state.pageSize });
  if (state.status) q.set("status", state.status);
  const calls = await api(`/api/campaigns/${c.id}/calls?${q}`);
  state.total = calls.total;
  $("#calls tbody").innerHTML = calls.items.map((x) => `<tr>
    <td>${esc(x.name)}</td><td>${esc(x.phone_number)}</td><td>${badge(x.status)}</td><td>${x.attempts}</td>
    <td>${esc(x.hangup_cause)}</td><td>${x.duration_seconds ?? ""}</td><td>${fmt(x.updated_at)}</td><td class="error">${esc(x.last_error)}</td></tr>`).join("")
    || `<tr><td colspan="8" class="muted">No calls</td></tr>`;
  const pages = Math.max(1, Math.ceil(calls.total / state.pageSize));
  $("#page-info").textContent = `Page ${state.page} of ${pages} (${calls.total} calls)`;
  $("#prev").disabled = state.page <= 1;
  $("#next").disabled = state.page >= pages;
  return c;
}

async function refresh() {
  try {
    await loadCampaigns();
    await loadDetail();
  } catch (e) { console.error(e); }
}

$("#campaigns").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-id]");
  if (!row) return;
  state.selected = Number(row.dataset.id);
  state.page = 1;
  refresh();
});

$("#create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = $("#create-result");
  const button = e.target.querySelector("button");
  button.disabled = true;
  out.className = "muted";
  out.textContent = "Uploading…";
  try {
    const data = await api("/api/campaigns", { method: "POST", body: new FormData(e.target) });
    const s = data.import_summary;
    out.className = "success";
    out.textContent = `Campaign created: ${s.imported} recipients imported, ${s.duplicates} duplicates skipped, ${s.invalid} invalid rows.`
      + (s.errors.length ? "\n" + s.errors.slice(0, 10).map((r) => `Row ${r.row}: ${r.error}`).join("\n") : "");
    state.selected = data.campaign.id;
    state.page = 1;
    e.target.reset();
    await loadOptions().catch(() => {});
    refresh();
  } catch (err) {
    out.className = "error";
    out.textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

async function action(path, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  try { await api(path, { method: "POST" }); } catch (e) { alert(e.message); }
  refresh();
}
$("#btn-start").onclick = () => action(`/api/campaigns/${state.selected}/start`, "Start calling all recipients now?");
$("#btn-regen").onclick = () => action(`/api/campaigns/${state.selected}/regenerate-audio`);
$("#btn-cancel").onclick = () => action(`/api/campaigns/${state.selected}/cancel`, "Cancel all pending calls in this campaign?");
$("#status-filter").onchange = (e) => { state.status = e.target.value; state.page = 1; loadDetail(); };
$("#prev").onclick = () => { state.page--; loadDetail(); };
$("#next").onclick = () => { state.page++; loadDetail(); };

loadOptions().then(refresh);
setInterval(refresh, 4000);
