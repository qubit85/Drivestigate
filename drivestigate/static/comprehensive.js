"use strict";
window.DriveReport = (() => {
  let key = "", selected = null;
  const requested = new Set();
  async function update(data) {
    const scan = data.scan, detail = scan.settings.investigation_mode === "detail", progressive = scan.settings.investigation_mode === "progressive";
    $("current-mode").textContent = progressive ? "Progressive investigation" : detail ? scan.settings.detail_include_technical ? "All files · including technical" : "Detailed review · personal data first" : "Sampled files";
    $("current-mode-description").textContent = progressive ? "Discovery, selective reading and local AI run together. Each round adds evidence and updates this report." : detail
      ? scan.settings.detail_include_technical ? "All inventoried files receive fingerprints and individual bounded assessments, including technical files." : "Personal files receive fingerprints and bounded assessments. Recognized technical files are grouped from metadata without individual content reads."
      : "Every file is inventoried; selected files are sampled for content assessment.";
    $("alternate-mode").textContent = "New progressive investigation …";
    $("alternate-mode").hidden = progressive;
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
      live: "Live report · updated from new evidence while discovery and analysis continue.",
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
    const scan = state.scan;
    await newScan();
    $("scan-root").value = scan.root; $("scan-name").value = `${scan.name} · Progressive`.slice(0,100);
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
