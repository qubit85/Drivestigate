"""A bounded, evidence-linked synthesis over the complete saved catalog."""

import html
from pathlib import Path

from .store import now


def catalog_report(store, scan_id):
    scan, stats = store.get_scan(scan_id), store.stats(scan_id)
    detail = scan["settings"].get("investigation_mode") == "detail"
    with store.connect() as db:
        areas = [
            dict(r)
            for r in db.execute(
                "SELECT CASE WHEN instr(f.rel,'/')=0 THEN '.' ELSE substr(f.rel,1,instr(f.rel,'/')-1) END rel,"
                "COUNT(*) files,SUM(f.size) bytes,SUM(f.technical) technical_files FROM files f WHERE scan_id=? "
                "GROUP BY 1 ORDER BY bytes DESC,rel",
                (scan_id,),
            )
        ]
        directories = {
            r["rel"]: r["id"]
            for r in db.execute(
                "SELECT id,rel FROM directories WHERE scan_id=? AND (parent_rel='.' OR rel='.')", (scan_id,)
            )
        }
        years = [
            dict(r)
            for r in db.execute(
                "SELECT COALESCE(strftime('%Y',mtime,'unixepoch'),'Unknown') year,COUNT(*) files,SUM(size) bytes "
                "FROM files WHERE scan_id=? GROUP BY 1 ORDER BY 1",
                (scan_id,),
            )
        ]
        if detail:
            candidates = [
                dict(r)
                for r in db.execute(
                    "SELECT f.id,'file' kind,f.rel,f.size bytes,1 files,x.priority,x.summary,x.reason "
                    "FROM files f JOIN file_details x ON x.file_id=f.id WHERE f.scan_id=? AND f.technical=0 "
                    "AND x.state='reviewed' AND x.priority<=2 ORDER BY x.priority,f.score DESC,f.size DESC LIMIT 120",
                    (scan_id,),
                )
            ]
            priorities = [
                dict(r)
                for r in db.execute(
                    "SELECT x.priority,COUNT(*) count FROM file_details x JOIN files f ON f.id=x.file_id "
                    "WHERE f.scan_id=? AND f.technical=0 AND x.state='reviewed' GROUP BY 1",
                    (scan_id,),
                )
            ]
        else:
            candidates = [
                dict(r)
                for r in db.execute(
                    "SELECT a.id,'finding' kind,d.rel,COUNT(f.id) files,COALESCE(SUM(f.size),0) bytes,"
                    "a.priority,a.summary,a.reason FROM findings a JOIN directories d ON d.id=a.dir_id "
                    "LEFT JOIN files f ON f.dir_id=d.id WHERE a.scan_id=? AND a.analyzed=1 AND a.priority<=2 GROUP BY a.id "
                    "ORDER BY a.priority,bytes DESC LIMIT 120",
                    (scan_id,),
                )
            ]
            priorities = [
                dict(r)
                for r in db.execute(
                    "SELECT priority,COUNT(*) count FROM findings WHERE scan_id=? AND analyzed=1 GROUP BY 1",
                    (scan_id,),
                )
            ]
    # Diversify a short reading list instead of reproducing hundreds of cards.
    leads, branches = [], {}
    for candidate in candidates:
        branch = candidate["rel"].split("/")[0]
        if branches.get(branch, 0) >= 2:
            continue
        branches[branch] = branches.get(branch, 0) + 1
        leads.append(candidate)
        if len(leads) == 10:
            break
    if len(leads) < 10:
        known = {(r["kind"], r["id"]) for r in leads}
        leads.extend(r for r in candidates if (r["kind"], r["id"]) not in known)
        leads = leads[:10]
    storage = [{**r, "id": directories.get(r["rel"]), "kind": "directory"} for r in areas[:12]]
    if len(areas) > 12:
        storage.append(
            {
                "rel": "Other top-level folders",
                "id": None,
                "kind": "directory",
                "files": sum(a["files"] for a in areas[12:]),
                "bytes": sum(a["bytes"] for a in areas[12:]),
            }
        )
    kinds = ", ".join(t["category"].lower() for t in stats["types"][:4]) or "no inventoried files yet"
    reviewed = stats["detail"]["ai_reviewed"] if detail else stats["analyzed_folders"]
    unit = "files" if detail else "folders"
    summary = (
        f"The catalog contains {stats['files']:,} files in {stats['directories']:,} discovered folders. "
        f"The largest content categories by size are {kinds}. "
        f"{stats['technical_files']:,} files are grouped as likely operating-system, application or cache material. "
        f"Local AI has assessed {reviewed:,} {unit}; the remaining inventory is not evidence of reviewed content."
    )
    if leads:
        summary += "\n\nReviewed evidence includes: " + " ".join(
            f"{r['rel']}: {r['summary'][:320]}" for r in leads[:3]
        )
    caveat = (
        "All-file processing attempts every inventoried regular file. Full-byte fingerprints do not establish "
        "complete semantic understanding; AI sees supported bounded content or metadata."
        if detail
        else "Content conclusions come from selected samples. An inventoried folder has not necessarily been assessed by AI."
    )
    next_steps = [
        "Open the selected evidence paths and verify promising material against the original files.",
        "Review coverage gaps and skipped paths before treating the investigation as complete.",
    ]
    if not detail and stats["analyzed_folders"] < stats["findings"]:
        next_steps.insert(
            0,
            "Resume the sampled investigation to assess the remaining relevant folders, or start an all-file investigation.",
        )
    return {
        "version": 1,
        "scan_id": scan_id,
        "name": scan["name"],
        "root": scan["root"],
        "mode": "detail" if detail else "overview",
        "generated": now(),
        "scan_status": scan["status"],
        "stats": stats,
        "storage": storage,
        "timeline": years,
        "priorities": priorities,
        "leads": leads,
        "summary": summary,
        "basis": "Compiled from catalog totals and saved assessments.",
        "caveat": caveat,
        "next_steps": next_steps,
        "synthesis": "catalog",
        "ai_error": "",
    }


def generate_report(engine, scan_id):
    from .engine import Interrupted

    store, scan = engine.store, engine.store.get_scan(scan_id)
    report = catalog_report(store, scan_id)
    store.save_report(scan_id, report, state="building")
    if engine.control.is_set():
        raise Interrupted()
    settings = scan["settings"]
    use_ai = report["mode"] != "detail" or settings.get("detail_use_llm", True)
    client = engine.client_factory(settings) if use_ai else None
    if client and hasattr(client, "summarize_report") and report["leads"]:
        evidence = {
            "scope": "Synthesis of the saved investigation, not new source-file inspection. All totals cover the catalog; examples are bounded.",
            "review_unit": "individual files"
            if report["mode"] == "detail"
            else "folders, not individual files",
            "coverage": {
                "inventory_complete": report["stats"]["directories"] == report["stats"]["inventoried_dirs"],
                "content_review": "all inventoried files attempted, bounded supported content only"
                if report["mode"] == "detail"
                else "selected folder samples only",
                "unreviewed_folders_remain": report["stats"]["analyzed_folders"]
                < report["stats"]["findings"],
            },
            "largest_categories_in_order": [r["category"] for r in report["stats"]["types"][:8]],
            "areas": [{"folder": a["rel"][:120], "content_verified": False} for a in report["storage"]],
            "samples": [
                {"id": i + 1, "rel": r["rel"][:240], "text": r["summary"][:600]}
                for i, r in enumerate(report["leads"][:8])
            ],
        }
        try:
            client.test()
            result = client.summarize_report(evidence, timeout=min(60, settings["request_timeout_seconds"]))
            report.update(
                summary=result["summary"],
                basis=result["reason"],
                next_steps=result["next_steps"],
                synthesis="local_ai",
            )
        except (ValueError, OSError) as exc:
            report["ai_error"] = str(exc)[:500]
            store.event(
                scan_id, "report", ".", "AI synthesis unavailable; catalog report preserved. " + str(exc)
            )
    if engine.control.is_set():
        raise Interrupted()
    store.save_report(scan_id, report)
    return report


def report_payload(store, scan_id, origin):
    saved = store.report(scan_id)
    data = saved["data"] if saved and saved["data"] else catalog_report(store, scan_id)
    return {
        "state": saved["state"] if saved else "preview",
        "generated": saved["generated"] if saved else "",
        "html": render_report(data, origin),
        "synthesis": data["synthesis"],
    }


def size(value):
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{number:,.1f} {unit}" if unit != "B" else f"{number:,.0f} B"
        number /= 1024


def render_report(data, origin, standalone=False):
    def esc(value):
        return html.escape(str(value), quote=True)

    stats, sid = data["stats"], data["scan_id"]

    def path_link(item, label=None):
        label = label or item["rel"]
        if item.get("id") is None:
            return esc(label)
        query = "file" if item["kind"] == "file" else "finding" if item["kind"] == "finding" else "folder"
        url = f"{origin}/?scan={sid}&{query}={item['id']}"
        return (
            f'<a href="{esc(url)}" data-evidence-kind="{item["kind"]}" data-evidence-id="{item["id"]}" '
            f'title="Open evidence for {esc(item["rel"])}">{esc(label)}</a>'
        )

    def bars(rows, key, label_key, links=False):
        maximum = max((r[key] for r in rows), default=1) or 1
        return "".join(
            f'<div class="report-bar-row"><div>{path_link(r) if links else esc(r[label_key])}</div>'
            f'<div class="report-bar-track"><i style="width:{100 * r[key] / maximum:.3f}%"></i></div>'
            f"<span>{size(r[key]) if key == 'bytes' else format(r[key], ',')}</span></div>"
            for r in rows
        )

    reviewed = stats["detail"]["processed"] if data["mode"] == "detail" else stats["analyzed_folders"]
    total = stats["files"] if data["mode"] == "detail" else stats["findings"]
    coverage = min(100, reviewed / total * 100) if total else 0
    parts = [
        f'<article class="report-document"><header class="report-doc-header"><p class="report-kicker">CONSOLIDATED INVESTIGATION REPORT</p>'
        f'<h2>{esc(data["name"])}</h2><p class="path">{esc(data["root"])}</p><p>{"All files" if data["mode"] == "detail" else "Sampled files"} · '
        f"Catalog snapshot · {esc(data['generated'][:19].replace('T', ' '))} UTC</p></header>",
        f'<div class="report-stat-strip"><div><strong>{stats["files"]:,}</strong><span>files inventoried</span></div>'
        f"<div><strong>{size(stats['bytes'])}</strong><span>logical data size</span></div><div><strong>{stats['directories']:,}</strong><span>folders discovered</span></div>"
        f"<div><strong>{coverage:.0f}%</strong><span>{'files processed' if data['mode'] == 'detail' else 'relevant folders assessed'}</span></div></div>",
        '<section class="report-section"><h3>What is on this drive?</h3>',
        *[f'<p class="report-narrative">{esc(p)}</p>' for p in data["summary"].split("\n\n") if p.strip()],
        f'<p class="report-provenance">{"Local AI synthesis" if data["synthesis"] == "local_ai" else "Catalog synthesis"} · {esc(data["basis"])}'
        + (
            f"<br>Local AI synthesis unavailable: {esc(data['ai_error'])}. Catalog summary retained."
            if data.get("ai_error")
            else ""
        )
        + "</p></section>",
        '<div class="report-chart-grid"><section class="report-section"><h3>Content by file type</h3><p>Logical file sizes across the complete inventory.</p>',
        bars(stats["types"], "bytes", "category"),
        '</section><section class="report-section"><h3>Where the space is</h3><p>Top-level areas, including technical material.</p>',
        bars(data["storage"], "bytes", "rel", True),
        "</section></div>",
        f'<section class="report-section"><h3>Coverage and confidence</h3><div class="report-coverage-track"><i style="width:{coverage:.3f}%"></i></div>'
        f"<p>{reviewed:,} / {total:,} {'files processed' if data['mode'] == 'detail' else 'relevant folders assessed'}. "
        f"{stats['inventoried_dirs']:,} / {stats['directories']:,} discovered folders inventoried.</p><p>{esc(data['caveat'])}</p>"
        f"<p>{stats['technical_files']:,} likely technical files ({size(stats['technical_bytes'])}) are grouped as clutter; this is a heuristic.</p>",
    ]
    if data["mode"] == "detail":
        d = stats["detail"]
        parts.append(
            f"<p>{d['hashed']:,} full-byte fingerprints · {d['ai_reviewed']:,} AI assessments · {d['content_read']:,} readable content samples · {d['errors']:,} read issues · {d['duplicate_groups']:,} duplicate groups.</p>"
        )
    parts.extend(
        [
            '</section><section class="report-section"><h3>Where to look first</h3><p>A short, diversified reading list from saved assessments. All individual findings remain available below the report.</p>',
            '<div class="report-leads">',
        ]
    )
    for item in data["leads"]:
        parts.append(
            f'<div class="report-lead"><div><span class="report-priority">P{item["priority"]}</span> {path_link(item)}</div>'
            f"<p>{esc(item['summary'])}</p><small>{item['files']:,} files · {size(item['bytes'])}</small></div>"
        )
    if not data["leads"]:
        parts.append(
            "<p>No content assessments are available yet. The charts show inventory metadata only.</p>"
        )
    parts.extend(
        [
            '</div></section><div class="report-chart-grid"><section class="report-section"><h3>File timeline</h3><p>Last-modified years from filesystem metadata; copied files may retain old dates.</p>',
            bars(data["timeline"], "files", "year"),
            '</section><section class="report-section"><h3>Recommended next steps</h3><ol>',
            *[f"<li>{esc(step)}</li>" for step in data["next_steps"]],
            "</ol><h3>Skipped paths and issues</h3>",
        ]
    )
    events = stats["events"]
    parts.append(
        "<p>"
        + (
            " · ".join(f"{esc(kind)}: {count:,}" for kind, count in events.items())
            if events
            else "No skipped paths or issues recorded."
        )
        + "</p>"
    )
    parts.append(
        "<p>Detailed paths and errors are available in the investigation details and full JSON/HTML evidence exports.</p></section></div>"
        '<footer class="report-doc-footer">Generated by Drivestigate from saved catalog evidence. Model judgments may be inaccurate. '
        "Classification and matching fingerprints are not permission to delete files.</footer></article>"
    )
    body = "".join(parts)
    if not standalone:
        return body
    css = (Path(__file__).parent / "static/report.css").read_text()
    return f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Drivestigate report · {esc(data["name"])}</title><style>{css}</style><body class="standalone-report">{body}</body></html>'
