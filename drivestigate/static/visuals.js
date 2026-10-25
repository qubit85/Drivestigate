"use strict";

window.DriveViews = (() => {
  const view = {id: null, tab: "folder", folder: ".", expanded: new Set(["."]), cache: new Map(),
    queue: null, dragging: null, graph: null, positions: new Map(), selected: null, hover: null,
    zoom: 1, panX: 0, panY: 0, pointer: null, graphKey: "", graphLoaded: 0, nodePage: 0};
  const colors = {file: "#5b9877", folder: "#527e9d", topic: "#c49b4c", type: "#9c88b3", duplicate: "#c97468"};
  const phaseNames = {inventory: "Discovering folders", analysis: "Adaptive investigation", detail: "All-file inspection",
    vault: "Building Obsidian vault", complete: "Investigation complete"};
  const seconds = n => n < 60 ? `${Math.max(1, Math.round(n))} sec` : n < 3600 ? `${Math.round(n / 60)} min` : `${(n / 3600).toFixed(1)} hr`;
  const isDetail = () => state.scan?.settings.investigation_mode === "detail";

  async function refreshQueue() {
    if (view.dragging) return;
    const queue = await api("/api/queue"); view.queue = queue;
    const key = JSON.stringify(queue);
    if (key === view.queueKey) return;
    view.queueKey = key;
    $("queue-count").textContent = queue.jobs.length;
    $("hold-queue").textContent = queue.paused ? "Resume queue" : "Hold queue";
    $("queue-caption").textContent = queue.paused
      ? "Queue on hold. The current investigation can finish; queued jobs wait until you resume."
      : "Jobs run one at a time. Drag queued jobs or use their arrow buttons to reorder them. Disconnected drives wait without blocking other jobs.";
    const container = $("queue-list"); container.replaceChildren();
    if (!queue.jobs.length) return container.append(el("div", "empty-state", "Nothing queued. Add your next drive or folder."));
    const queued = queue.jobs.filter(j => ["queued", "waiting"].includes(j.status));
    queue.jobs.forEach(job => {
      const canMove = ["queued", "waiting"].includes(job.status), index = queued.findIndex(j => j.id === job.id);
      const card = el("div", "queue-card" + (canMove ? "" : " queue-active")); card.dataset.id = job.id;
      card.draggable = canMove;
      const handle = el("span", "queue-handle", canMove ? "⠿" : "▶"); handle.setAttribute("aria-hidden", "true");
      const label = button(job.name, "queue-name", () => guarded(() => showReport(job.id)));
      label.append(el("small", "", `${job.mode === "detail" ? "All-file detail" : "Adaptive overview"} · ${statusNames[job.status]}`));
      const actions = el("div", "queue-actions");
      if (canMove) {
        const up = button("↑", "icon-button", () => moveJob(job.id, "up")); up.title = "Move investigation up";
        up.setAttribute("aria-label", `Move ${job.name} up`); up.disabled = index === 0;
        const down = button("↓", "icon-button", () => moveJob(job.id, "down")); down.title = "Move investigation down";
        down.setAttribute("aria-label", `Move ${job.name} down`); down.disabled = index === queued.length - 1;
        actions.append(up, down, button("Remove", "text-button", () => guarded(async () => {
          await api(`/api/scans/${job.id}/pause`, {}); await loadScans();
        })));
        card.addEventListener("dragstart", event => { view.dragging = job.id; event.dataTransfer.setData("text/plain", job.id); card.classList.add("dragging"); });
        card.addEventListener("dragend", () => { view.dragging = null; card.classList.remove("dragging"); });
        card.addEventListener("dragover", event => { event.preventDefault(); card.classList.add("drop-target"); });
        card.addEventListener("dragleave", () => card.classList.remove("drop-target"));
        card.addEventListener("drop", event => {
          event.preventDefault(); card.classList.remove("drop-target");
          const source = event.dataTransfer.getData("text/plain"); view.dragging = null;
          const ids = queued.map(j => j.id); const from = ids.indexOf(source), to = ids.indexOf(job.id);
          if (from < 0 || to < 0 || from === to) return;
          ids.splice(from, 1); ids.splice(to, 0, source);
          guarded(async () => { await api("/api/queue/reorder", {ids}); await refreshQueue(); });
        });
      } else actions.append(el("span", "state-label", statusNames[job.status]));
      card.append(handle, label, actions); container.append(card);
    });
  }
  function moveJob(id, direction) { guarded(async () => { await api("/api/queue/move", {id, direction}); await refreshQueue(); }); }

  function reset(id) {
    view.id = id; view.folder = "."; view.cache.clear(); view.expanded = new Set(["."]);
    view.graph = null; view.positions.clear(); view.graphKey = ""; view.graphLoaded = 0; view.selected = null;
    view.treeKey = ""; $("graph-search").value = "";
  }
  function progress(data) {
    const p = data.progress || {}, scan = data.scan;
    const phases = isDetail() ? ["inventory", "detail", "vault", "complete"] : ["inventory", "analysis"];
    const current = phases.indexOf(scan.phase), target = $("phase-steps"); target.replaceChildren();
    phases.forEach((phase, i) => target.append(el("span", "phase-step" + (i === current ? " current" : i < current ? " done" : ""),
      `${i < current ? "✓" : i + 1}  ${phaseNames[phase]}`)));
    $("phase-label").textContent = phaseNames[scan.phase] || "Preparing investigation";
    $("phase-current").textContent = p.current || scan.message;
    const total = p.total || 0, completed = p.completed || 0;
    const indeterminate = p.percent === null || p.percent === undefined;
    $("phase-progress").classList.toggle("indeterminate", indeterminate && scan.status === "running");
    $("phase-fill").style.width = indeterminate ? "25%" : `${p.percent}%`;
    if (indeterminate) $("phase-progress").removeAttribute("aria-valuenow");
    else $("phase-progress").setAttribute("aria-valuenow", Math.round(p.percent));
    $("phase-counts").textContent = scan.phase === "inventory"
      ? `${num(completed)} folders inventoried · ${num(p.discovered)} discovered · total still growing`
      : `${num(completed)} / ${num(total)} ${p.unit || "units"} processed${p.percent !== null && p.percent !== undefined ? ` · ${Math.round(p.percent)}%` : ""}`;
    let rate = `${seconds(p.elapsed_seconds || 0)} elapsed`;
    if (scan.status === "running" && p.units_per_second > 0) rate += ` · ${p.units_per_second < 0.1 ? (p.units_per_second * 60).toFixed(1) + "/min" : p.units_per_second.toFixed(1) + "/sec"}`;
    if (p.current_size && scan.phase === "detail") rate += ` · current file ${formatBytes(p.current_bytes || 0)} / ${formatBytes(p.current_size)}`;
    $("phase-rate").textContent = rate;
    $("eta-value").textContent = p.eta_low !== null && p.eta_low !== undefined ? `≈ ${seconds(p.eta_low)}–${seconds(p.eta_high)}`
      : scan.status === "completed" ? "Run finished" : ["queued", "waiting"].includes(scan.status) ? statusNames[scan.status]
      : scan.status !== "running" ? "Paused" : "Calculating …";
    $("eta-caption").textContent = p.eta_basis || "Time remaining in this phase; available after two completed units.";
    if (isDetail()) {
      const d = data.stats.detail;
      $("coverage-text").textContent = `${num(d.processed)} / ${num(data.stats.files)} files processed · ${num(d.hashed)} full-byte fingerprints · ${num(d.ai_reviewed)} AI assessments · ${num(d.content_read)} readable content samples · ${num(d.errors)} read issues · ${num(d.duplicate_groups)} identical-byte groups`;
      $("coverage-fill").style.width = data.stats.files ? `${100 * d.processed / data.stats.files}%` : "0%";
      const metric = (index, title, value, caption) => {
        const item = $("metrics").children[index];
        item.querySelector("small").textContent = title; item.querySelector("strong").textContent = num(value); item.querySelector("p").textContent = caption;
      };
      metric(2, "Files processed", d.processed, `${num(d.ai_reviewed)} AI assessments · ${num(d.hashed)} fingerprints`);
      metric(3, "Readable content samples", d.content_read, `${num(d.errors)} read issues · bounded supported content`);
    }
    $("download-vault").hidden = !scan.vault_path; $("open-vault").hidden = !scan.vault_path;
    $("download-vault").href = `/api/scans/${scan.id}/obsidian.zip`;
  }
  async function update(data) {
    if (view.id !== data.scan.id) reset(data.scan.id);
    progress(data);
    if (view.tab === "folder") await refreshTree();
    else if (Date.now() - view.graphLoaded > 7000 || !view.graph) await loadGraph();
  }
  function folderState(row) {
    if (row.errors) return "error";
    if (row.own_files && row.processed_files === row.own_files) return "processed";
    if (row.analyzed) return "sampled";
    if (row.processed_files) return "partial";
    if (row.own_files && row.technical_files === row.own_files) return "technical";
    return row.done ? "inventoried" : "discovered";
  }
  async function fetchTree(parent, more = false) {
    const id = view.id, previous = view.cache.get(parent);
    const wanted = more ? (previous?.items.length || 1) - 1 + 150 : Math.max(150, (previous?.items.length || 1) - 1);
    let merged;
    for (let offset = 0; offset < wanted; offset += 150) {
      const result = await api(`/api/scans/${id}/tree?${new URLSearchParams({parent, offset})}`);
      if (id !== view.id) return;
      if (!merged) merged = result;
      else merged.items.push(...result.items.filter(r => r.rel !== parent));
      if (offset + 150 >= result.total) break;
    }
    if (id === view.id) view.cache.set(parent, merged);
  }
  async function refreshTree() {
    const paths = new Set([".", view.folder, ...view.expanded]);
    await Promise.all([...paths].slice(0, 32).map(fetchTree));
    const key = JSON.stringify([view.folder, [...view.expanded], [...view.cache]]);
    if (key === view.treeKey) return;
    view.treeKey = key;
    renderTree(); renderMap();
  }
  function renderTree() {
    const tree = $("folder-tree"); tree.replaceChildren();
    const rootData = view.cache.get("."); if (!rootData) return;
    const root = rootData.items.find(r => r.rel === "."); if (!root) return;
    function append(row, depth) {
      const item = el("div", "tree-item"), line = el("div", "tree-line" + (view.folder === row.rel ? " selected" : ""));
      line.style.paddingLeft = `${8 + depth * 15}px`;
      item.setAttribute("role", "treeitem"); item.setAttribute("aria-level", depth + 1);
      if (row.children) item.setAttribute("aria-expanded", view.expanded.has(row.rel));
      const expand = button(row.children ? view.expanded.has(row.rel) ? "⌄" : "›" : "·", "tree-expand", () => guarded(async () => {
        if (!row.children) return;
        if (view.expanded.has(row.rel)) view.expanded.delete(row.rel);
        else { view.expanded.add(row.rel); await fetchTree(row.rel); }
        renderTree();
      })); expand.setAttribute("aria-label", `${view.expanded.has(row.rel) ? "Collapse" : "Expand"} ${row.rel}`);
      const dot = el("span", `folder-dot ${folderState(row)}`);
      const choose = button(row.rel === "." ? state.scan.name : row.rel.split("/").pop(), "tree-name", () => guarded(async () => {
        view.folder = row.rel; if (!view.cache.has(row.rel)) await fetchTree(row.rel); renderTree(); renderMap();
      })); choose.title = `${row.rel} · ${folderState(row)} · ${row.processed_files}/${row.own_files} own files processed`;
      line.append(expand, dot, choose, el("small", "", row.tree_files ? num(row.tree_files) : row.own_files ? num(row.own_files) : "")); item.append(line); tree.append(item);
      if (view.expanded.has(row.rel)) {
        const cached = view.cache.get(row.rel);
        for (const child of cached?.items || []) if (child.rel !== row.rel) append(child, depth + 1);
        if (cached && cached.total > cached.items.length - 1) item.append(button(`Load more folders (${cached.items.length - 1}/${cached.total})`, "text-button tree-more", () => guarded(async () => {
          await fetchTree(row.rel, true); renderTree(); renderMap();
        })));
      }
    }
    append(root, 0);
  }
  function rectangles(items, x, y, width, height, result) {
    if (!items.length) return result;
    if (items.length === 1) { result.push({...items[0], x, y, width, height}); return result; }
    const weight = items.reduce((s, i) => s + i.weight, 0); let split = 1, left = items[0].weight;
    while (split < items.length - 1 && left + items[split].weight / 2 < weight / 2) left += items[split++].weight;
    const ratio = left / weight;
    if (width >= height) {
      rectangles(items.slice(0, split), x, y, width * ratio, height, result);
      rectangles(items.slice(split), x + width * ratio, y, width * (1 - ratio), height, result);
    } else {
      rectangles(items.slice(0, split), x, y, width, height * ratio, result);
      rectangles(items.slice(split), x, y + height * ratio, width, height * (1 - ratio), result);
    }
    return result;
  }
  function renderMap() {
    const data = view.cache.get(view.folder); if (!data) return;
    $("folder-breadcrumbs").textContent = view.folder === "." ? state.scan.name : view.folder;
    $("map-up").disabled = view.folder === ".";
    const children = data.items.filter(r => r.rel !== view.folder);
    const own = data.items.find(r => r.rel === view.folder);
    const tiles = (children.length ? children.map(r => ({id: r.id, label: r.rel.split("/").pop(), rel: r.rel,
      bytes: r.tree_bytes || r.own_bytes, status: folderState(r), kind: "folder", row: r}))
      : data.files.map(f => ({id: f.id, label: f.name, rel: f.rel, bytes: f.size,
        status: f.state === "error" ? "error" : ["reviewed", "metadata"].includes(f.state) ? "processed" : "inventoried", kind: "file"})))
      .sort((a, b) => b.bytes - a.bytes).slice(0, 24);
    const map = $("folder-treemap"); map.replaceChildren();
    const layout = rectangles(tiles.map(t => ({...t, weight: Math.max(1, t.bytes)})), 0, 0, 100, 100, []);
    for (const tile of layout) {
      const node = button("", `map-tile ${tile.status}`, () => guarded(async () => {
        if (tile.kind === "file") return openFile(tile.id);
        view.folder = tile.rel; view.expanded.add(tile.rel); await fetchTree(tile.rel); renderTree(); renderMap();
      }));
      Object.assign(node.style, {left: `calc(${tile.x}% + 2px)`, top: `calc(${tile.y}% + 2px)`, width: `calc(${tile.width}% - 4px)`, height: `calc(${tile.height}% - 4px)`});
      node.title = `${tile.rel} · ${formatBytes(tile.bytes)} · ${tile.status}`;
      node.setAttribute("aria-label", node.title);
      if (tile.width > 12 && tile.height > 12) node.append(el("strong", "", tile.label), el("small", "", formatBytes(tile.bytes)), el("span", "", tile.status));
      map.append(node);
    }
    if (!tiles.length) map.append(el("div", "map-empty", state.scan.phase === "inventory" ? "Discovering this folder …" : "This folder has no inventoried regular files."));
    $("map-caption").textContent = `Area follows logical file size · up to 24 largest immediate entries · ${own ? num(own.own_files) : data.files.length} own files. State colors describe own-file review, not an exhaustive semantic guarantee.`;
    const files = $("folder-file-list"); files.replaceChildren();
    if (data.files.length) files.append(el("h3", "", "Files in this folder"));
    for (const file of data.files) {
      const row = button("", "folder-file", () => guarded(() => openFile(file.id)));
      row.append(el("span", "", file.name), el("small", "", `${formatBytes(file.size)} · ${file.state || "inventoried"}`)); files.append(row);
    }
    if (own && own.own_files > 80) files.append(el("p", "hint", "Showing the 80 largest files here. Use the graph search to inspect other files."));
  }
  async function openFile(id) {
    const data = await api(`/api/scans/${state.selected}/file/${id}`);
    $("file-title").textContent = data.name; const target = $("file-evidence"); target.replaceChildren();
    target.append(el("p", "path", absolutePath(data.rel)), el("span", "state-label", `${data.state || "Inventoried"} · ${data.content_status || "No content inspection yet"}`),
      el("h3", "", "Assessment"), el("p", "", data.summary || "This file has not received a detail assessment yet."), el("p", "hint", data.reason || ""));
    if (data.error) target.append(el("p", "notice", data.error));
    target.append(el("h3", "", "Full-byte SHA-256"), el("code", "fingerprint", data.sha256 || "No completed fingerprint."));
    if (data.topics.length) target.append(el("h3", "", "AI-suggested topics"), el("p", "", data.topics.join(" · ")));
    if (data.excerpt) target.append(el("h3", "", "Evidence excerpt / reader status"), el("pre", "", data.excerpt));
    const actions = el("div", "file-actions"); const revealButton = button("Reveal in Finder ↗", "primary", () => reveal("file", data.id)); revealButton.disabled = !state.connected;
    actions.append(revealButton, button("Copy path", "secondary", () => copyPath(data.rel))); target.append(actions);
    if (!$("file-dialog").open) $("file-dialog").showModal();
  }

  function chooseTab(tab) {
    view.tab = tab;
    $("folder-panel").hidden = tab !== "folder"; $("graph-panel").hidden = tab !== "graph";
    $("folder-tab").setAttribute("aria-selected", tab === "folder"); $("graph-tab").setAttribute("aria-selected", tab === "graph");
    guarded(tab === "folder" ? refreshTree : loadGraph);
  }
  async function loadGraph() {
    if (!view.id) return;
    const id = view.id; const params = new URLSearchParams({q: $("graph-search").value, limit: $("graph-limit").value});
    const data = await api(`/api/scans/${id}/graph?${params}`); if (id !== view.id) return;
    view.graphLoaded = Date.now(); view.graph = data; view.nodePage = 0;
    $("graph-mode-notice").hidden = isDetail();
    $("graph-summary").textContent = `${num(data.nodes.length)} nodes · ${num(data.edges.length)} connections`;
    $("graph-coverage").textContent = `${num(data.shown_files)} of ${num(data.matching_files)} matching files shown (${num(data.total_files)} inventoried). ${data.truncated ? "Display limited; search to narrow the view. The exported vault includes all files. " : ""}${data.note}`;
    const graphKey = data.nodes.map(n => n.id).join(",");
    if (graphKey !== view.graphKey) { layoutGraph(data); view.graphKey = graphKey; fitGraph(); }
    renderNodeList(); drawGraph();
    if (view.selected) renderInspector(view.selected);
  }
  function layoutGraph(data) {
    const folders = data.nodes.filter(n => n.kind === "folder"), files = data.nodes.filter(n => n.kind === "file");
    const groups = new Map();
    folders.forEach((node, i) => {
      const angle = (i / Math.max(1, folders.length)) * Math.PI * 2 - Math.PI / 2;
      groups.set(node.id, {x: Math.cos(angle) * 230, y: Math.sin(angle) * 190, count: 0});
      if (!view.positions.has(node.id)) view.positions.set(node.id, groups.get(node.id));
    });
    const parents = new Map(data.edges.filter(e => e.kind === "contains").map(e => [e.target, e.source]));
    files.forEach((node, index) => {
      const group = groups.get(parents.get(node.id)) || {x: 0, y: 0, count: index};
      const j = group.count++, angle = j * 2.39996 + 0.7, radius = 65 + 25 * Math.sqrt(j);
      if (!view.positions.has(node.id)) view.positions.set(node.id, {x: group.x + Math.cos(angle) * radius, y: group.y + Math.sin(angle) * radius});
    });
    for (const kind of ["topic", "type", "duplicate"]) {
      const nodes = data.nodes.filter(n => n.kind === kind);
      nodes.forEach((node, i) => {
        const x = kind === "topic" ? -430 - Math.floor(i / 15) * 95 : kind === "type" ? 430 : (i - nodes.length / 2) * 90;
        const y = kind === "duplicate" ? 350 : (i % 15 - Math.min(nodes.length, 15) / 2) * 43;
        if (!view.positions.has(node.id)) view.positions.set(node.id, {x, y});
      });
    }
  }
  const canvas = $("knowledge-canvas"), ctx = canvas.getContext("2d");
  function fitGraph() {
    if (!view.graph?.nodes.length) return;
    const points = view.graph.nodes.map(n => view.positions.get(n.id)).filter(Boolean);
    const xs = points.map(p => p.x), ys = points.map(p => p.y);
    const minX = Math.min(...xs) - 60, maxX = Math.max(...xs) + 90, minY = Math.min(...ys) - 50, maxY = Math.max(...ys) + 50;
    view.zoom = Math.min(1.8, Math.max(0.08, Math.min(canvas.clientWidth / (maxX - minX), canvas.clientHeight / (maxY - minY)) * 0.9));
    view.panX = canvas.clientWidth / 2 - (minX + maxX) / 2 * view.zoom;
    view.panY = canvas.clientHeight / 2 - (minY + maxY) / 2 * view.zoom;
    drawGraph();
  }
  function drawGraph() {
    if (!view.graph || $("graph-panel").hidden) return;
    const ratio = window.devicePixelRatio || 1, width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    if (canvas.width !== width * ratio || canvas.height !== height * ratio) { canvas.width = width * ratio; canvas.height = height * ratio; }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#f8faf5"; ctx.fillRect(0, 0, width, height);
    ctx.save(); ctx.translate(view.panX, view.panY); ctx.scale(view.zoom, view.zoom);
    const focus = view.hover || view.selected, neighbors = new Set([focus]);
    for (const edge of view.graph.edges) {
      if (edge.source === focus) neighbors.add(edge.target); if (edge.target === focus) neighbors.add(edge.source);
    }
    for (const edge of view.graph.edges) {
      const a = view.positions.get(edge.source), b = view.positions.get(edge.target); if (!a || !b) continue;
      const active = !focus || edge.source === focus || edge.target === focus;
      ctx.globalAlpha = active ? 0.55 : 0.07;
      ctx.strokeStyle = edge.provenance === "ai" ? "#c49b4c" : edge.provenance === "fingerprint" ? "#c97468" : "#9aac9d";
      ctx.lineWidth = (active && focus ? 1.6 : 0.8) / view.zoom; ctx.setLineDash(edge.provenance === "ai" ? [4, 4] : []);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
    ctx.setLineDash([]);
    for (const node of view.graph.nodes) {
      const p = view.positions.get(node.id); if (!p) continue;
      const active = !focus || neighbors.has(node.id), radius = (node.kind === "file" ? 5.5 : 7) / Math.max(0.65, view.zoom);
      ctx.globalAlpha = active ? 1 : 0.16; ctx.fillStyle = node.state === "error" ? "#b76754" : node.kind === "file" && !["reviewed", "metadata"].includes(node.state) ? "#aeb9a8" : colors[node.kind];
      ctx.beginPath();
      if (node.kind === "topic") { ctx.moveTo(p.x, p.y - radius); ctx.lineTo(p.x + radius, p.y); ctx.lineTo(p.x, p.y + radius); ctx.lineTo(p.x - radius, p.y); ctx.closePath(); }
      else if (node.kind === "folder") ctx.rect(p.x - radius, p.y - radius * 0.7, radius * 2, radius * 1.4);
      else ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
      ctx.fill();
      if (node.id === focus) { ctx.strokeStyle = "#264f3e"; ctx.lineWidth = 2 / view.zoom; ctx.stroke(); }
      if (node.kind !== "file" || view.graph.shown_files <= 30 || node.id === focus || (view.zoom > 1.2 && active)) {
        ctx.fillStyle = "#4c604f"; ctx.font = `${11 / Math.max(0.65, view.zoom)}px system-ui`;
        ctx.fillText(node.label.length > 27 ? node.label.slice(0, 25) + "…" : node.label, p.x + radius + 4, p.y + 3);
      }
    }
    ctx.restore(); ctx.globalAlpha = 1;
  }
  function hit(event) {
    const rect = canvas.getBoundingClientRect(), x = (event.clientX - rect.left - view.panX) / view.zoom, y = (event.clientY - rect.top - view.panY) / view.zoom;
    const nodes = [...(view.graph?.nodes || [])].reverse();
    return nodes.find(n => { const p = view.positions.get(n.id); return p && Math.hypot(p.x - x, p.y - y) < 12 / Math.max(0.6, view.zoom); });
  }
  canvas.addEventListener("pointerdown", event => {
    const node = hit(event); view.pointer = {x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY, node: node?.id};
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", event => {
    if (view.pointer) {
      const dx = event.clientX - view.pointer.x, dy = event.clientY - view.pointer.y;
      if (view.pointer.node) { const p = view.positions.get(view.pointer.node); p.x += dx / view.zoom; p.y += dy / view.zoom; }
      else { view.panX += dx; view.panY += dy; }
      view.pointer.x = event.clientX; view.pointer.y = event.clientY; drawGraph();
    } else {
      const node = hit(event); if (view.hover !== node?.id) { view.hover = node?.id; canvas.title = node?.label || "Drag to pan · Scroll to zoom"; drawGraph(); }
    }
  });
  canvas.addEventListener("pointerup", event => {
    if (view.pointer?.node && Math.hypot(event.clientX - view.pointer.startX, event.clientY - view.pointer.startY) < 5) {
      view.selected = view.pointer.node; renderInspector(view.selected);
    }
    view.pointer = null; drawGraph();
  });
  canvas.addEventListener("pointercancel", () => { view.pointer = null; });
  canvas.addEventListener("pointerleave", () => { view.hover = null; drawGraph(); });
  canvas.addEventListener("wheel", event => {
    event.preventDefault(); const rect = canvas.getBoundingClientRect(), x = event.clientX - rect.left, y = event.clientY - rect.top;
    const next = Math.max(0.08, Math.min(4, view.zoom * (event.deltaY < 0 ? 1.15 : 0.87)));
    view.panX = x - (x - view.panX) * next / view.zoom; view.panY = y - (y - view.panY) * next / view.zoom; view.zoom = next; drawGraph();
  }, {passive: false});
  canvas.addEventListener("keydown", event => {
    if (event.key === "Home") fitGraph();
    else if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
      event.preventDefault(); view.panX += event.key === "ArrowLeft" ? 30 : event.key === "ArrowRight" ? -30 : 0;
      view.panY += event.key === "ArrowUp" ? 30 : event.key === "ArrowDown" ? -30 : 0;
    } else if (["+", "="].includes(event.key)) view.zoom *= 1.1;
    else if (event.key === "-") view.zoom /= 1.1;
    drawGraph();
  });
  new ResizeObserver(drawGraph).observe(canvas);
  function renderInspector(id) {
    const node = view.graph?.nodes.find(n => n.id === id); if (!node) return;
    const panel = $("graph-inspector"); panel.replaceChildren(el("span", "state-label", node.kind === "topic" ? "AI suggestion" : node.kind), el("h3", "", node.label));
    if (node.rel) panel.append(el("p", "path", node.rel));
    if (node.summary) panel.append(el("p", "", node.summary));
    if (node.kind === "file") {
      panel.append(el("p", "hint", `${formatBytes(node.size)} · ${node.state} · content: ${node.content_status}`),
        button("View file evidence ↗", "primary", () => guarded(() => openFile(node.file_id))));
    } else if (node.kind === "folder") panel.append(button("Explore this folder ↗", "secondary", () => guarded(async () => { view.folder = node.rel; chooseTab("folder"); })));
    else panel.append(button("Focus related files", "secondary", () => { $("graph-search").value = node.sha256 || node.label; guarded(loadGraph); }));
    panel.append(el("h3", "", "Connections"));
    let shown = 0;
    for (const edge of view.graph.edges) {
      if (edge.source !== id && edge.target !== id) continue;
      const other = view.graph.nodes.find(n => n.id === (edge.source === id ? edge.target : edge.source)); if (!other) continue;
      const linkButton = button(other.label, "neighbor-link", () => { view.selected = other.id; renderInspector(other.id); drawGraph(); });
      linkButton.append(el("small", "", `${edge.kind} · ${edge.provenance}`)); panel.append(linkButton);
      if (++shown >= 30) { panel.append(el("p", "hint", "First 30 displayed connections. Narrow the graph search to see more.")); break; }
    }
  }
  function renderNodeList() {
    const target = $("graph-node-list"); target.replaceChildren();
    const nodes = view.graph.nodes, start = view.nodePage * 100;
    for (const node of nodes.slice(start, start + 100)) target.append(button(`${node.kind}: ${node.label}`, "neighbor-link", () => { view.selected = node.id; renderInspector(node.id); drawGraph(); }));
    if (nodes.length > 100) {
      const controls = el("div", "pagination"), previous = button("Previous", "secondary", () => { view.nodePage--; renderNodeList(); }); previous.disabled = !view.nodePage;
      const next = button("Next", "secondary", () => { view.nodePage++; renderNodeList(); }); next.disabled = start + 100 >= nodes.length;
      controls.append(previous, el("span", "", `${start + 1}–${Math.min(nodes.length, start + 100)} of ${nodes.length}`), next); target.append(controls);
    }
  }
  $("hold-queue").onclick = () => guarded(async () => { await api("/api/queue/hold", {paused: !view.queue?.paused}); await refreshQueue(); });
  $("folder-tab").onclick = () => chooseTab("folder"); $("graph-tab").onclick = () => chooseTab("graph");
  $("map-up").onclick = () => guarded(async () => { view.folder = view.folder.includes("/") ? view.folder.slice(0, view.folder.lastIndexOf("/")) : "."; await fetchTree(view.folder); renderTree(); renderMap(); });
  $("graph-fit").onclick = fitGraph; $("graph-refresh").onclick = () => guarded(loadGraph);
  $("graph-limit").onchange = () => guarded(loadGraph);
  let searchTimer; $("graph-search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => guarded(loadGraph), 300); };
  $("close-file").onclick = () => $("file-dialog").close();
  $("open-vault").onclick = () => guarded(() => api(`/api/scans/${view.id}/open-vault`, {}));
  document.querySelectorAll('input[name="investigation-mode"]').forEach(input => input.addEventListener("change", () => { $("detail-mode-notice").hidden = input.value !== "detail"; }));
  const params = new URLSearchParams(location.search);
  if (params.has("file")) {
    const check = setInterval(() => { if (state.scan) { clearInterval(check); guarded(() => openFile(Number(params.get("file")))); } }, 200);
    setTimeout(() => clearInterval(check), 10000);
  }
  function exploreFolder(rel) { view.folder = rel; view.expanded.add(rel); chooseTab("folder"); $("folder-panel").scrollIntoView({block: "start", behavior: "smooth"}); }
  return {update, reset, refreshQueue, exploreFolder};
})();
