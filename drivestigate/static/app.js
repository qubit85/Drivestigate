"use strict";

const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="drivestigate-token"]').content;
const state = {settings: null, defaults: null, depths: null, scans: [], selected: null, scan: null,
  connected: false, offset: 0, query: "", priority: "", view: "overview", profiles: [], polling: false};
const statusNames = {running: "Running", paused: "Paused", completed: "Run complete", stopped: "Stopped",
  pausing: "Pausing", stopping: "Stopping", queued: "Queued", waiting: "Waiting for drive"};
const depthNames = {quick: "Quick", balanced: "Balanced", thorough: "Thorough"};
const methodNames = {metadata: "Metadata · awaiting AI review", metadata_ai: "AI assessment based on metadata",
  sampled: "AI assessment with samples", detail: "All-file inspection ledger"};
const sampleNames = {excerpt: "Text / archive excerpt", metadata: "Metadata only", image: "Image sample", error: "Unreadable"};
const eventNames = {excluded: "Excluded", symlink: "Symbolic link", mount: "Different filesystem",
  unreadable: "Unreadable", extraction: "Read error", llm: "AI error", error: "Error", special: "Special file"};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(text, cls, action) {
  const node = el("button", cls, text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}
function formatBytes(n) {
  if (!n) return "0 B";
  const i = Math.min(4, Math.floor(Math.log(n) / Math.log(1024)));
  return `${new Intl.NumberFormat("en-GB", {maximumFractionDigits: i ? 1 : 0}).format(n / 1024 ** i)} ${["B", "KiB", "MiB", "GiB", "TiB"][i]}`;
}
const num = n => new Intl.NumberFormat("en-GB").format(n || 0);
const date = value => new Date(value).toLocaleDateString("en-GB", {day: "2-digit", month: "short", year: "numeric"});
let toastTimer;
function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = error ? "error" : "";
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 9000 : 4500);
}
async function api(path, data) {
  const response = await fetch(path, {method: data === undefined ? "GET" : "POST",
    headers: {"Content-Type": "application/json", "X-Drivestigate-Token": token},
    ...(data === undefined ? {} : {body: JSON.stringify(data)})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed.");
  return result;
}
async function guarded(action) {
  try { return await action(); } catch (error) { toast(error.message, true); return null; }
}
function switchView(view) {
  state.view = view;
  for (const key of ["overview", "new", "report"]) $(key + "-view").hidden = key !== view;
  $("nav-overview").classList.toggle("active", view === "overview");
  $("nav-new").classList.toggle("active", view === "new");
  $("breadcrumb").textContent = `Workspace / ${view === "report" ? "Investigation" : view === "new" ? "New investigation" : "Overview"}`;
  if (view !== "report") history.replaceState(null, "", "/");
}
async function loadScans() {
  const data = await api("/api/scans");
  if (state.scansLoaded && state.sidebarSelection === state.selected && JSON.stringify(data.scans) === JSON.stringify(state.scans)) {
    if (window.DriveViews) await window.DriveViews.refreshQueue();
    return;
  }
  state.scans = data.scans;
  state.scansLoaded = true; state.sidebarSelection = state.selected;
  $("version").textContent = data.version;
  $("scan-count").textContent = data.scans.length;
  $("overview-count").textContent = `${data.scans.length} saved`;
  const list = $("scan-list"), cards = $("overview-scans");
  list.replaceChildren(); cards.replaceChildren();
  for (const scan of data.scans) {
    const nav = button(scan.name, "scan-item" + (scan.id === state.selected ? " active" : ""), () => guarded(() => showReport(scan.id)));
    nav.append(el("small", "", `${statusNames[scan.status] || scan.status} · ${date(scan.created)}`));
    list.append(nav);
    const card = button("", "investigation-card", () => guarded(() => showReport(scan.id)));
    const title = el("div"); title.append(el("h3", "", scan.name), el("p", "path", scan.root));
    const meta = el("div", "card-meta"); meta.append(el("span", "state-label", statusNames[scan.status] || scan.status), el("small", "", date(scan.created)));
    card.append(title, meta); cards.append(card);
  }
  if (!data.scans.length) {
    const empty = el("div", "empty-state");
    empty.append(el("h3", "", "Your first discovery is waiting."), el("p", "", "Connect a drive or try Drivestigate with synthetic demo data."),
      button("New investigation ↗", "text-button", newScan)); cards.append(empty);
  }
  if (window.DriveViews) await window.DriveViews.refreshQueue();
}
async function checkConnection() {
  try {
    const data = await api("/api/models");
    $("connection").classList.add("online");
    $("connection-label").textContent = `Ollama local · ${data.models.length} models`;
  } catch {
    $("connection").classList.remove("online");
    $("connection-label").textContent = "Ollama unavailable";
  }
}
function syncNewSettings() {
  if (!state.settings) return;
  $("new-model").textContent = state.settings.model;
  $("new-depth").textContent = depthNames[state.settings.depth];
  $("focus-preview").textContent = state.settings.search_focus ? "Search focus: " + state.settings.search_focus : "No specific search focus · general investigation";
}
async function newScan() {
  switchView("new"); syncNewSettings();
  const data = await guarded(() => api("/api/volumes"));
  $("volumes").replaceChildren();
  if (data) for (const volume of data.volumes) {
    $("volumes").append(button(`${volume.name} · ${formatBytes(volume.bytes)}`, "volume-button", () => {
      $("scan-root").value = volume.path; $("scan-name").value = volume.name;
    }));
  }
}
async function showReport(id) {
  state.selected = id; state.offset = 0; state.query = ""; state.priority = "";
  state.scan = null;
  $("finding-search").value = ""; $("priority-filter").value = "";
  switchView("report");
  if (window.DriveViews) window.DriveViews.reset(id);
  history.replaceState(null, "", `/?scan=${encodeURIComponent(id)}`);
  await refreshReport(); await loadScans();
}
async function refreshReport() {
  const id = state.selected;
  if (!id) return;
  const data = await api(`/api/scans/${id}`);
  if (id !== state.selected) return;
  if (state.scan?.id === id && data.scan.updated === state.scan.updated && data.connected === state.connected && data.report_state === state.reportState && !["running", "pausing", "stopping"].includes(data.scan.status)) return;
  state.scan = data.scan; state.connected = data.connected; state.reportState = data.report_state;
  const scan = data.scan, stats = data.stats;
  $("report-title").textContent = scan.name; $("report-root").textContent = scan.root;
  $("scan-status-text").textContent = scan.message || "Ready";
  $("scan-state").textContent = statusNames[scan.status] || scan.status;
  const running = ["running", "pausing", "stopping"].includes(scan.status);
  const queued = ["queued", "waiting"].includes(scan.status);
  $("scan-status").classList.toggle("running", running);
  $("pause-scan").hidden = !running && !queued; $("stop-scan").hidden = !running && !queued;
  $("pause-scan").disabled = !queued && scan.status !== "running"; $("stop-scan").disabled = !queued && scan.status !== "running";
  $("resume-scan").hidden = running || queued;
  $("resume-scan").textContent = scan.status === "completed" ? scan.settings.investigation_mode === "detail" ? "Rebuild Obsidian vault" : "Investigate remaining folders" : "Resume";
  $("resume-scan").disabled = !data.connected;
  $("disconnected").hidden = data.connected;
  const metrics = $("metrics"); metrics.replaceChildren();
  for (const [title, value, note] of [["Files inventoried", num(stats.files), `${num(stats.directories)} folders discovered`],
    ["Cataloged data", formatBytes(stats.bytes), "Logical file sizes"],
    ["Folders assessed by AI", num(stats.analyzed_folders), `of ${num(stats.findings)} relevant folders`],
    ["Text / archive samples", num(stats.content_samples), `${num(stats.samples)} samples, including metadata`]]) {
    const metric = el("div", "metric"); metric.append(el("small", "", title), el("strong", "", value), el("p", "", note)); metrics.append(metric);
  }
  $("coverage-text").textContent = `${num(stats.inventoried_dirs)} / ${num(stats.directories)} discovered folders inventoried · ${num(stats.analyzed_folders)} / ${num(stats.findings)} relevant folders assessed by AI · ${num(scan.llm_calls)} investigation AI calls · ${formatBytes(scan.read_bytes)} read budget used`;
  $("coverage-fill").style.width = (stats.findings ? 100 * stats.analyzed_folders / stats.findings : 0) + "%";
  $("technical-summary").textContent = `${num(stats.technical_files)} technical files (${formatBytes(stats.technical_bytes)}) cataloged and grouped. Potential user files inside application and system folders remain visible. Image samples are separate from the content excerpt count.`;
  const types = $("type-breakdown"); types.replaceChildren();
  for (const type of stats.types) { const row = el("div", "type-row"); row.append(el("span", "", type.category), el("span", "", `${num(type.count)} files · ${formatBytes(type.bytes)}`)); types.append(row); }
  $("scan-settings").textContent = JSON.stringify(scan.settings, null, 2);
  for (const format of ["html", "csv", "json"]) $("export-" + format).href = `/api/scans/${id}/report.${format}`;
  if (window.DriveReport) await window.DriveReport.update(data);
  if (window.DriveViews) await window.DriveViews.update(data);
  await loadFindings();
  const events = await api(`/api/scans/${id}/events`);
  const target = $("scan-events"); target.replaceChildren();
  if (!events.events.length) target.append(el("p", "hint", "No exclusions or errors recorded."));
  else {
    target.append(el("p", "hint", "Latest 250 events. All events are included in the HTML and JSON exports."));
    for (const event of events.events) target.append(el("div", "event", `${eventNames[event.kind] || event.kind} · ${event.rel}: ${event.message}`));
  }
}
function absolutePath(relative) { return state.scan.root.replace(/\/$/, "") + (relative === "." ? "" : "/" + relative); }
async function copyPath(relative) { await guarded(async () => { await navigator.clipboard.writeText(absolutePath(relative)); toast("Path copied."); }); }
async function reveal(kind, id) { await guarded(async () => { await api(`/api/scans/${state.selected}/reveal`, {kind, id}); }); }
function renderFinding(finding) {
  const article = el("article", "finding"); article.id = `finding-${finding.id}`;
  const top = el("div", "finding-top"), title = el("div");
  const link = button(finding.rel === "." ? "Root folder" : finding.rel.split("/").pop(), "finding-title", () => reveal("finding", finding.id));
  link.title = "Open folder in Finder"; link.disabled = !state.connected;
  title.append(link, el("div", "finding-path", absolutePath(finding.rel)));
  top.append(title, el("span", `priority p${finding.priority}`, ["", "Take a closer look", "Worth reviewing", "Lower priority"][finding.priority]));
  article.append(top, el("p", "finding-summary", finding.summary), el("p", "finding-reason", finding.reason));
  if (finding.next_action && finding.analyzed) article.append(el("p", "finding-reason", "Next step: " + finding.next_action));
  const footer = el("div", "finding-footer"), actions = el("div", "finding-buttons");
  footer.append(el("span", "evidence-tag", `${methodNames[finding.method]} · ${num(finding.file_count)} files · ${formatBytes(finding.bytes)}`));
  const open = button("Open in Finder ↗", "text-button", () => reveal("finding", finding.id)); open.disabled = !state.connected;
  const deeper = button("Investigate further", "text-button", () => guarded(async () => {
    if (state.scan.settings.investigation_mode === "detail") return window.DriveViews.exploreFolder(finding.rel);
    await api(`/api/scans/${state.selected}/deepen`, {finding_id: finding.id}); toast("Deeper investigation started."); await refreshReport();
  })); deeper.disabled = !state.connected || ["running", "pausing", "stopping"].includes(state.scan.status);
  if (state.scan.settings.investigation_mode === "detail") { deeper.textContent = "Explore file evidence"; deeper.disabled = false; }
  actions.append(open, button("Copy path", "text-button", () => copyPath(finding.rel)), deeper); footer.append(actions); article.append(footer);
  if (finding.samples.length) {
    const details = el("details", "samples"); details.append(el("summary", "", `${finding.samples.length} samples & evidence`));
    for (const sample of finding.samples) {
      const entry = el("div", "sample"), row = el("div", "sample-top");
      const fileLink = button(sample.rel, "sample-path", () => reveal("file", sample.id)); fileLink.disabled = !state.connected;
      fileLink.title = "Reveal file in Finder";
      row.append(fileLink, el("small", "", sampleNames[sample.status] || sample.status));
      entry.append(row, el("pre", "", sample.text || "No text content retained.")); details.append(entry);
    }
    article.append(details);
  }
  return article;
}
async function loadFindings() {
  const id = state.selected;
  const params = new URLSearchParams({offset: state.offset, limit: 40, q: state.query});
  if (state.priority) params.set("priority", state.priority);
  const data = await api(`/api/scans/${id}/findings?${params}`);
  if (id !== state.selected) return;
  $("finding-count").textContent = num(data.total);
  const container = $("findings");
  // Do not replace expanded evidence while a scan is running; preserve reader state across polling.
  const expanded = new Set([...container.querySelectorAll("details[open]")].map(n => n.closest("article").id));
  container.replaceChildren();
  for (const finding of data.items) {
    const item = renderFinding(finding);
    if (expanded.has(item.id) && item.querySelector("details")) item.querySelector("details").open = true;
    container.append(item);
  }
  if (!data.items.length) container.append(el("div", "empty-state", state.scan.phase === "inventory"
    ? "Building the inventory. Relevant folders will appear here once it is complete."
    : "No matching folders. Check filters, technical files, and skipped paths."));
  const pagination = $("pagination"); pagination.replaceChildren();
  if (data.total > 40) {
    const prev = button("← Previous", "secondary", () => { state.offset = Math.max(0, state.offset - 40); guarded(loadFindings); });
    prev.disabled = state.offset === 0;
    const next = button("Next →", "secondary", () => { state.offset += 40; guarded(loadFindings); }); next.disabled = state.offset + 40 >= data.total;
    pagination.append(prev, el("span", "", `${state.offset + 1}–${Math.min(state.offset + 40, data.total)} of ${num(data.total)}`), next);
  }
}
const fields = {"ollama_url": "ollama-url", model: "model-select", system_prompt: "system-prompt", search_focus: "search-focus",
  depth: "scan-depth", max_folders: "max-folders", samples_per_folder: "samples-count", max_llm_calls: "max-calls",
  time_budget_minutes: "time-budget", max_read_mb: "read-budget", request_timeout_seconds: "request-timeout",
  hide_technical: "hide-technical", analyze_images: "analyze-images", detail_use_llm: "detail-use-llm",
  vault_include_excerpts: "vault-excerpts"};
function settingsFromForm() {
  const settings = structuredClone(state.settings);
  for (const [key, id] of Object.entries(fields)) {
    const field = $(id);
    settings[key] = field.type === "checkbox" ? field.checked : field.type === "number" ? Number(field.value) : field.value;
  }
  settings.exclude_patterns = $("exclude-patterns").value.split("\n").map(s => s.trim()).filter(Boolean);
  settings.profiles = state.profiles;
  return settings;
}
function renderProfiles() {
  $("profile-select").replaceChildren(new Option("Load search profile …", ""));
  state.profiles.forEach((profile, i) => $("profile-select").add(new Option(profile.name, i)));
}
async function refreshModels() {
  $("model-test-result").textContent = "Loading models …";
  try {
    // Model discovery respects the address currently entered without saving other settings.
    const settings = settingsFromForm();
    const result = await api("/api/models", {settings});
    const current = $("model-select").value || state.settings.model;
    $("model-select").replaceChildren();
    for (const model of result.models) $("model-select").add(new Option(`${model.name} · ${formatBytes(model.size)}`, model.name));
    if (![...$("model-select").options].some(o => o.value === current)) $("model-select").add(new Option(current, current));
    $("model-select").value = current;
    $("model-test-result").textContent = `${result.models.length} models found.`;
  } catch (error) { $("model-test-result").textContent = error.message; }
}
async function openSettings() {
  for (const [key, id] of Object.entries(fields)) {
    const field = $(id);
    if (key === "model") { field.replaceChildren(new Option(state.settings.model, state.settings.model)); }
    else if (field.type === "checkbox") field.checked = state.settings[key];
    else field.value = state.settings[key];
  }
  $("exclude-patterns").value = state.settings.exclude_patterns.join("\n");
  state.profiles = structuredClone(state.settings.profiles); renderProfiles(); $("profile-name").value = "";
  $("settings-status").textContent = "Changes apply to new investigations.";
  $("settings-dialog").showModal(); await refreshModels();
}

$("nav-overview").onclick = () => { switchView("overview"); guarded(loadScans); };
for (const id of ["nav-new", "start-new", "hero-new"]) $(id).onclick = () => guarded(newScan);
for (const id of ["open-settings", "mobile-settings", "new-settings", "connection"]) $(id).onclick = () => guarded(openSettings);
$("close-settings").onclick = () => $("settings-dialog").close();
$("pick-folder").onclick = () => guarded(async () => {
  $("pick-folder").disabled = true;
  try { const result = await api("/api/pick-folder", {}); if (result.path) $("scan-root").value = result.path; }
  finally { $("pick-folder").disabled = false; }
});
$("demo").onclick = () => guarded(async () => {
  const data = await api("/api/demo", {}); $("scan-root").value = data.path; $("scan-name").value = "Demo · Forgotten treasures";
  toast("Synthetic dataset created. Start the investigation to try your local model.");
});
$("new-form").onsubmit = event => {
  event.preventDefault(); guarded(async () => {
    $("submit-scan").disabled = true;
    try { const data = await api("/api/scans", {root: $("scan-root").value, name: $("scan-name").value,
      mode: document.querySelector('input[name="investigation-mode"]:checked').value}); await showReport(data.id); }
    finally { $("submit-scan").disabled = false; }
  });
};
for (const [id, action] of [["pause-scan", "pause"], ["stop-scan", "stop"], ["resume-scan", "resume"]]) {
  $(id).onclick = () => guarded(async () => { await api(`/api/scans/${state.selected}/${action}`, {}); await refreshReport(); await loadScans(); });
}
let searchTimer;
$("finding-search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => {
  state.query = $("finding-search").value; state.offset = 0; guarded(loadFindings);
}, 250); };
$("priority-filter").onchange = () => { state.priority = $("priority-filter").value; state.offset = 0; guarded(loadFindings); };
$("refresh-models").onclick = refreshModels;
$("test-model").onclick = () => guarded(async () => {
  $("test-model").disabled = true; $("model-test-result").textContent = "Testing connection …";
  try { const data = await api("/api/model-test", {settings: settingsFromForm()}); $("model-test-result").textContent = `Connected · ${data.model} · ${data.capabilities.join(", ")}`; }
  catch (error) { $("model-test-result").textContent = error.message; }
  finally { $("test-model").disabled = false; }
});
$("reset-prompt").onclick = () => { $("system-prompt").value = state.defaults.system_prompt; };
$("scan-depth").onchange = () => { const preset = state.depths[$("scan-depth").value]; for (const [key, value] of Object.entries(preset)) $(fields[key]).value = value; };
$("save-profile").onclick = () => {
  const name = $("profile-name").value.trim();
  if (!name) { $("profile-name").focus(); return toast("Please enter a profile name.", true); }
  const profile = {name, search_focus: $("search-focus").value, system_prompt: $("system-prompt").value};
  const index = state.profiles.findIndex(p => p.name === name);
  if (index >= 0) state.profiles[index] = profile; else state.profiles.push(profile);
  renderProfiles(); $("settings-status").textContent = "Profile prepared. Save settings to keep it.";
};
$("profile-select").onchange = () => {
  if ($("profile-select").value === "") return;
  const profile = state.profiles[Number($("profile-select").value)];
  $("search-focus").value = profile.search_focus; $("system-prompt").value = profile.system_prompt; $("profile-name").value = profile.name;
};
$("delete-profile").onclick = () => {
  if ($("profile-select").value === "") return;
  state.profiles.splice(Number($("profile-select").value), 1); renderProfiles();
  $("settings-status").textContent = "Profile removed. Save settings to apply.";
};
$("settings-form").onsubmit = event => {
  event.preventDefault(); guarded(async () => {
    $("save-settings").disabled = true;
    try { const data = await api("/api/settings", settingsFromForm()); state.settings = data.settings;
      syncNewSettings(); $("settings-dialog").close(); toast("Settings saved. Existing investigations retain their configuration."); await checkConnection(); }
    finally { $("save-settings").disabled = false; }
  });
};
async function init() {
  const data = await api("/api/settings"); state.settings = data.settings; state.defaults = data.defaults; state.depths = data.depths;
  await loadScans(); checkConnection();
  const params = new URLSearchParams(location.search);
  if (params.has("scan")) {
    await showReport(params.get("scan"));
    if (params.has("folder")) { const folder = await api(`/api/scans/${state.selected}/directory/${Number(params.get("folder"))}`); window.DriveViews.exploreFolder(folder.rel); }
    if (params.has("finding")) {
      $("individual-findings").open = true;
      const finding = await api(`/api/scans/${state.selected}/finding/${Number(params.get("finding"))}`);
      let card = $(`finding-${finding.id}`);
      if (!card) { card = renderFinding(finding); $("findings").prepend(card); }
      card.classList.add("highlight"); card.scrollIntoView({behavior: "smooth", block: "center"});
    }
  }
  setInterval(async () => {
    if (document.hidden || state.polling || $("settings-dialog").open) return;
    state.polling = true;
    try {
      if (state.view === "report") await refreshReport();
      await loadScans();
    } catch (error) { $("scan-status-text").textContent = "Connection to the application lost. Reload the page after restarting."; }
    finally { state.polling = false; }
  }, 4000);
}
guarded(init);
