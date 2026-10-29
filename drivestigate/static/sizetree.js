"use strict";
window.SizeTree = (() => {
  let scanId = null, revision = "", generation = 0, sort = "size", expanded = new Set(["."]), cache = new Map();
  const labels = {explore:"Exploring",sample:"Sampling",deepen:"Deeper review",defer:"Deferred",summarize:"Summarized",system:"Operating system",application:"Application",personal:"Personal data",profile:"User profile",application_data:"Application data",dependencies:"Dependencies",cache:"Cache / temporary"};
  async function load(path, append = false) {
    const id = scanId, ticket = generation, previous = cache.get(path);
    const offset = append ? previous?.items.length || 0 : 0;
    const data = await api(`/api/scans/${id}/size-tree?${new URLSearchParams({parent:path,offset,sort})}`);
    if (id !== scanId || ticket !== generation) return;
    cache.set(path, {...data, items:append ? [...previous.items,...data.items] : data.items});
  }
  function reset(id) { scanId = id; revision = ""; generation++; expanded = new Set(["."]); cache.clear(); $("size-tree-rows").replaceChildren(); }
  async function update(scan) {
    if (scan.id !== scanId) reset(scan.id);
    if (scan.updated === revision && cache.has(".")) return;
    revision = scan.updated;
    await refresh();
  }
  async function refresh() {
    const ticket = ++generation;
    // Fetch only expanded branches. Additional fan-out remains accessible via paging.
    await Promise.all([...expanded].slice(0,32).map(async path => {
      const pages = Math.max(1, Math.ceil((cache.get(path)?.items.length || 0) / 200));
      await load(path);
      for (let page = 1; page < pages && ticket === generation; page++) await load(path,true);
    }));
    if (ticket === generation) render();
  }
  async function toggle(row) {
    if (expanded.has(row.rel)) {
      for (const path of [...expanded]) if (row.rel === "." || path === row.rel || path.startsWith(row.rel + "/")) expanded.delete(path);
    } else {
      expanded.add(row.rel);
      if (!cache.has(row.rel)) await load(row.rel);
    }
    render();
  }
  function render() {
    const root = cache.get("."); if (!root) return;
    const rows = $("size-tree-rows"); rows.replaceChildren();
    $("size-tree-note").textContent = root.provisional ? "Partial catalog: ≥ means at least this much discovered data. Unlisted branches have unknown sizes." : "Fully listed folder sizes include all cataloged descendant files. Entries are loaded as you expand; sizes come from the saved catalog and also work offline.";
    const total = root.root.tree_bytes || 0;
    const append = (row, depth) => {
      const folder = row.kind === "directory", line = el("tr", "size-tree-row");
      const nameCell = el("td", "size-tree-name"); nameCell.style.paddingLeft = `${12 + Math.min(depth,40) * 18}px`;
      const name = button("", "size-tree-name-button", () => guarded(() => folder ? toggle(row) : window.DriveViews.openFile(row.id)));
      name.append(el("span", "size-tree-icon", folder ? expanded.has(row.rel) ? "▾" : "▸" : "·"), el("span", "", row.rel === "." ? state.scan.name : row.name));
      name.setAttribute("aria-label", folder ? `${expanded.has(row.rel) ? "Collapse" : "Expand"} folder: ${row.rel}` : `File evidence: ${row.rel}`);
      if (folder) { name.setAttribute("aria-expanded", expanded.has(row.rel)); window.FolderSummaries.bind(name,row); }
      nameCell.append(name); if (folder) nameCell.append(window.FolderSummaries.info(row));
      const sizeCell = el("td", "size-tree-size");
      sizeCell.append(el("span", "", folder && !row.tree_complete ? row.bytes ? "≥ " + formatBytes(row.bytes) : "Unknown" : formatBytes(row.bytes)));
      const bar = el("div", "size-tree-bar"), fill = el("i"); fill.style.width = `${total ? Math.min(100,100 * row.bytes / total) : 0}%`; bar.append(fill); sizeCell.append(bar);
      line.append(nameCell,sizeCell,el("td", "size-tree-count", folder ? num(row.files) : "1"),el("td", "size-tree-role", labels[row.role] || (folder ? "Folder" : "File")));
      rows.append(line);
      if (folder && expanded.has(row.rel)) {
        const data = cache.get(row.rel);
        if (!data) return;
        data.items.forEach(child => append(child, depth + 1));
        if (data.items.length < data.total) {
          const more = el("tr"), cell = el("td"); cell.colSpan = 4;
          cell.append(button(`Load more in ${row.rel} (${num(data.items.length)} / ${num(data.total)})`, "text-button", () => guarded(async () => { await load(row.rel,true); render(); })));
          more.append(cell); rows.append(more);
        }
      }
    };
    append({id:root.root.id,rel:".",name:state.scan.name,kind:"directory",bytes:root.root.tree_bytes,files:root.root.tree_files,role:root.root.role,tree_complete:root.root.tree_complete},0);
  }
  $("size-tree-sort").onchange = () => guarded(async () => { sort = $("size-tree-sort").value; cache.clear(); await refresh(); });
  $("size-tree-collapse").onclick = () => { expanded = new Set(["."]); render(); };
  return {reset,update};
})();
