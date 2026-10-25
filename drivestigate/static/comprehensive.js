"use strict";
window.DriveReport = (() => {
  let key = "", selected = null;
  const requested = new Set();
  async function update(data) {
    const scan = data.scan, detail = scan.settings.investigation_mode === "detail";
    $("current-mode").textContent = detail ? "All files" : "Sampled files";
    $("current-mode-description").textContent = detail
      ? "Every inventoried file is processed. Supported bounded content or metadata is assessed individually."
      : "Every file is inventoried; selected files are sampled for content assessment.";
    $("alternate-mode").textContent = detail ? "Start sampled investigation …" : "Start all-file investigation …";
    selected = scan.id;
    const next = `${scan.id}:${scan.updated}:${data.report_state}`;
    if (key === next) return;
    const report = await api(`/api/scans/${scan.id}/summary`);
    if (scan.id !== state.selected) return;
    key = next;
    // The application renders this escaped fragment on the server. Source/model text is never HTML.
    $("comprehensive-content").innerHTML = report.html;
    const active = ["running", "pausing", "stopping", "queued", "waiting"].includes(scan.status);
    $("generate-summary").disabled = active;
    $("generate-summary").textContent = report.state === "ready" ? "Regenerate report" : "Generate report";
    $("download-summary").href = `/api/scans/${scan.id}/comprehensive.html`;
    const captions = {
      ready: `Generated report · ${report.synthesis === "local_ai" ? "local AI synthesis" : "catalog synthesis"}. Open the standalone report to save or print it.`,
      building: "Generating the final report from saved findings …",
      stale: "Previous report snapshot. A fresh report is generated when this investigation finishes.",
      pending: "Report generation was interrupted. Generate it again to finish the synthesis.",
      preview: "Current catalog snapshot. A final report is generated automatically after the investigation."
    };
    $("comprehensive-status").textContent = captions[report.state] || captions.preview;
    if (scan.status === "completed" && ["preview", "stale"].includes(report.state) && !requested.has(scan.id)) {
      requested.add(scan.id);
      await api(`/api/scans/${scan.id}/summary`, {});
      key = "";
    }
  }
  $("alternate-mode").onclick = () => guarded(async () => {
    const scan = state.scan, mode = scan.settings.investigation_mode === "detail" ? "overview" : "detail";
    await newScan();
    $("scan-root").value = scan.root; $("scan-name").value = `${scan.name} · ${mode === "detail" ? "All files" : "Sampled files"}`.slice(0,100);
    const option = document.querySelector(`input[name="investigation-mode"][value="${mode}"]`);
    option.checked = true; option.dispatchEvent(new Event("change"));
    document.querySelector(".mode-choice").scrollIntoView({block:"center",behavior:"smooth"});
  });
  $("generate-summary").onclick = () => guarded(async () => {
    await api(`/api/scans/${selected}/summary`, {}); key = "";
    await refreshReport(); await loadScans();
  });
  $("comprehensive-content").addEventListener("click", event => {
    const link = event.target.closest("a[data-evidence-id]"); if (!link) return;
    event.preventDefault();
    const id = Number(link.dataset.evidenceId), kind = link.dataset.evidenceKind;
    guarded(async () => {
      if (state.connected) return reveal(kind, id);
      if (kind === "file") return window.DriveViews.openFile(id);
      if (kind === "directory") {
        const row = await api(`/api/scans/${selected}/directory/${id}`); return window.DriveViews.exploreFolder(row.rel);
      }
      $("individual-findings").open = true;
      const finding = await api(`/api/scans/${selected}/finding/${id}`);
      let card = $(`finding-${id}`);
      if (!card) {card = renderFinding(finding); $("findings").prepend(card);}
      card.scrollIntoView({block:"center",behavior:"smooth"});
    });
  });
  return {update};
})();
