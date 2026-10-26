"use strict";
window.FolderSummaries = (() => {
  const cache = new Map(), pending = new Map();
  let scanId = null, revision = "", target = null, showTimer, hideTimer, sequence = 0, selectedId = null, dialogData = null;
  const tip = $("folder-summary-tooltip"), dialog = $("folder-summary-dialog");
  function reset(id, updated) {
    const changed = id !== scanId || revision !== updated;
    if (id !== scanId) {
      hide(); selectedId = null; dialogData = null; if (dialog.open) dialog.close();
    }
    if (id !== scanId || revision !== updated) cache.clear();
    scanId = id; revision = updated;
    if (changed && selectedId !== null) selected({id: selectedId});
  }
  async function get(id) {
    const current = scanId, version = revision, key = `${current}:${version}:${id}`;
    if (cache.has(key)) return cache.get(key);
    if (!pending.has(key)) {
      const request = api(`/api/scans/${current}/directory/${id}/summary`).then(data => {
        if (scanId === current && revision === version) cache.set(key, data);
        return data;
      }).finally(() => pending.delete(key));
      pending.set(key, request);
    }
    return pending.get(key);
  }
  function description(element, add) {
    const ids = (element.getAttribute("aria-describedby") || "").split(/\s+/).filter(id => id && id !== tip.id);
    if (add) ids.push(tip.id);
    if (ids.length) element.setAttribute("aria-describedby", ids.join(" "));
    else element.removeAttribute("aria-describedby");
  }
  function hide() {
    clearTimeout(showTimer); clearTimeout(hideTimer); sequence++;
    if (target) description(target, false);
    target = null; tip.hidden = true;
  }
  function position() {
    if (!target?.isConnected) return hide();
    const box = target.getBoundingClientRect();
    if (box.bottom < 0 || box.top > innerHeight) return hide();
    const width = Math.min(390, innerWidth - 24);
    tip.style.width = `${width}px`;
    const height = tip.getBoundingClientRect().height;
    tip.style.left = `${Math.max(12, Math.min(box.left, innerWidth - width - 12))}px`;
    const below = box.bottom + 8;
    tip.style.top = `${Math.max(12, Math.min(below + height <= innerHeight - 12 ? below : box.top - height - 8, innerHeight - height - 12))}px`;
  }
  function show(element, id, name) {
    clearTimeout(hideTimer); clearTimeout(showTimer);
    if (target && target !== element) description(target, false);
    target = element; const ticket = ++sequence, current = scanId;
    showTimer = setTimeout(async () => {
      if (target !== element || !element.isConnected) return;
      tip.replaceChildren(el("strong", "folder-tip-name", name), el("p", "", "Loading saved folder summary …"));
      tip.hidden = false; description(element, true); position();
      try {
        const data = await get(id);
        if (ticket !== sequence || current !== scanId || target !== element || !element.isConnected) return;
        tip.replaceChildren(el("strong", "folder-tip-name", data.rel === "." ? state.scan.name : data.rel),
          el("span", "folder-summary-basis", data.basis),
          el("p", "folder-tip-text", data.summary.length > 750 ? data.summary.slice(0, 747) + "…" : data.summary),
          el("small", "folder-tip-coverage", data.coverage), el("small", "folder-tip-help", "Open the folder or use ⓘ to read the full summary. Esc closes this tooltip."));
        position();
      } catch {
        if (ticket === sequence && target === element) {
          tip.replaceChildren(el("p", "", "Could not load the saved summary. Select the folder to try again.")); position();
        }
      }
    }, 180);
  }
  function leave(element) {
    clearTimeout(showTimer);
    hideTimer = setTimeout(() => { if (target === element && document.activeElement !== element && !tip.matches(":hover")) hide(); }, 120);
  }
  function bind(element, row) {
    element.dataset.directoryId = row.id;
    element.removeAttribute("title");
    element.addEventListener("pointerenter", event => { if (event.pointerType !== "touch") show(element, row.id, row.rel); });
    element.addEventListener("pointerleave", () => leave(element));
    element.addEventListener("focus", () => show(element, row.id, row.rel));
    element.addEventListener("blur", () => { if (target === element) hide(); });
    element.addEventListener("click", hide);
  }
  function info(row) {
    const control = button("ⓘ", "folder-info-button", () => guarded(() => open(row.id)));
    control.setAttribute("aria-label", `Read folder summary: ${row.rel === "." ? state.scan.name : row.rel}`);
    return control;
  }
  function fill(data, prefix) {
    $(prefix + "-basis").textContent = data.basis;
    $(prefix + "-text").textContent = prefix === "selected-summary" && data.summary.length > 500 ? data.summary.slice(0, 497) + "…" : data.summary;
    $(prefix + "-coverage").textContent = data.coverage;
  }
  async function selected(row) {
    if (!row) return;
    const changing = selectedId !== row.id;
    selectedId = row.id;
    const current = scanId, version = revision;
    $("selected-folder-summary").hidden = false;
    if (changing) {
      $("selected-summary-basis").textContent = "Saved catalog evidence";
      $("selected-summary-text").textContent = "Loading folder summary …";
      $("selected-summary-coverage").textContent = "";
    }
    try {
      const data = await get(row.id);
      if (current === scanId && version === revision && selectedId === row.id) fill(data, "selected-summary");
    } catch {
      if (current === scanId && selectedId === row.id) $("selected-summary-text").textContent = "Summary unavailable. Select the folder again to retry.";
    }
  }
  async function open(id) {
    hide(); const current = scanId;
    dialogData = null; $("directory-summary-title").textContent = "Folder summary";
    $("directory-summary-text").textContent = "Loading saved summary …";
    $("directory-summary-basis").textContent = ""; $("directory-summary-coverage").textContent = "";
    $("directory-summary-path").textContent = "";
    $("copy-directory-summary").disabled = true; $("reveal-directory-summary").disabled = true;
    if (!dialog.open) dialog.showModal();
    try {
      const data = await get(id);
      if (current !== scanId || !dialog.open) return;
      dialogData = data; $("directory-summary-title").textContent = data.rel === "." ? state.scan.name : data.rel;
      $("directory-summary-path").textContent = absolutePath(data.rel); fill(data, "directory-summary");
      $("copy-directory-summary").disabled = false; $("reveal-directory-summary").disabled = !state.connected;
    } catch (error) {
      if (current === scanId && dialog.open) $("directory-summary-text").textContent = error.message;
    }
  }
  tip.addEventListener("pointerenter", () => clearTimeout(hideTimer));
  tip.addEventListener("pointerleave", hide);
  document.addEventListener("keydown", event => { if (event.key === "Escape") hide(); });
  document.addEventListener("scroll", event => {
    if (tip.contains(event.target)) return;
    if (target && document.activeElement === target) { if (!tip.hidden) position(); }
    else hide();
  }, true);
  window.addEventListener("resize", hide);
  $("close-directory-summary").onclick = () => dialog.close();
  $("selected-summary-full").onclick = () => { if (selectedId) guarded(() => open(selectedId)); };
  $("copy-directory-summary").onclick = () => guarded(async () => {
    if (!dialogData) return;
    await navigator.clipboard.writeText(`${absolutePath(dialogData.rel)}\n\n${dialogData.summary}\n\n${dialogData.basis}\n${dialogData.coverage}`);
    toast("Folder summary copied.");
  });
  $("reveal-directory-summary").onclick = () => { if (dialogData) reveal("directory", dialogData.id); };
  return {reset, bind, info, selected, open};
})();
