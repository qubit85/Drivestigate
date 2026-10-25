"""Streaming, escaped exports. AI prose is always rendered as plain text."""

import csv
import html
import io
import json


def all_findings(store, scan_id):
    offset = 0
    while True:
        page = store.findings(scan_id, offset=offset, limit=250)
        yield from page["items"]
        offset += len(page["items"])
        if offset >= page["total"]:
            break


def safe_cell(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value


def csv_report(store, scan_id):
    if store.get_scan(scan_id)["settings"].get("investigation_mode") == "detail":
        yield from detail_csv(store, scan_id)
        return
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    yield "\ufeff"
    writer.writerow(["Priority", "Path", "Summary", "Reason", "Evidence", "Samples", "Next action"])
    yield buffer.getvalue()
    for finding in all_findings(store, scan_id):
        buffer.seek(0)
        buffer.truncate()
        writer.writerow(
            [safe_cell(finding[k]) for k in ("priority", "rel", "summary", "reason", "method")]
            + [len(finding["samples"]), safe_cell(finding["next_action"])]
        )
        yield buffer.getvalue()


def json_report(store, scan_id):
    yield '{"scan":' + json.dumps(store.get_scan(scan_id), ensure_ascii=True)
    yield ',"coverage":' + json.dumps(store.stats(scan_id), ensure_ascii=True)
    yield ',"events":['
    with store.connect() as db:
        first = True
        for row in db.execute("SELECT * FROM events WHERE scan_id=? ORDER BY id", (scan_id,)):
            yield ("" if first else ",") + json.dumps(dict(row), ensure_ascii=True)
            first = False
    yield '],"findings":['
    first = True
    for finding in all_findings(store, scan_id):
        yield ("" if first else ",") + json.dumps(finding, ensure_ascii=True)
        first = False
    yield '],"file_details":['
    if store.get_scan(scan_id)["settings"].get("investigation_mode") == "detail":
        first = True
        with store.connect() as db:
            for row in db.execute(
                "SELECT f.rel,f.name,f.size,x.* FROM files f JOIN file_details x ON x.file_id=f.id WHERE f.scan_id=? ORDER BY f.id",
                (scan_id,),
            ):
                record = dict(row)
                record["topics"] = json.loads(record["topics"])
                yield ("" if first else ",") + json.dumps(record, ensure_ascii=True)
                first = False
    yield "]}"


def html_report(store, scan_id, origin):
    scan, stats = store.get_scan(scan_id), store.stats(scan_id)

    def esc(value):
        return html.escape(str(value), quote=True)

    yield f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Drivestigate · {esc(scan["name"])}</title>
<style>body{{font:16px/1.6 system-ui;max-width:1000px;margin:48px auto;padding:0 24px;color:#21362d;background:#f6f7f2}}
article,section{{background:white;border:1px solid #dde3d9;border-radius:16px;padding:24px;margin:16px 0}}
code{{overflow-wrap:anywhere}}h1{{font-size:36px}}h2{{font-size:22px}}small{{color:#59685f}}a{{color:#215d43}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere}}@media print{{body{{background:white}}article{{break-inside:avoid}}}}</style>
<h1>Drivestigate</h1><p>Investigate your drives. Rediscover what matters.</p>
<section><h2>{esc(scan["name"])}</h2><code>{esc(scan["root"])}</code><p>Status: {esc(scan["status"])} ·
{stats["files"]} files · {stats["bytes"]} Bytes · {stats["technical_files"]} technical files grouped</p>
<p>{stats["analyzed_folders"]} of {stats["findings"]} folders with completed assessments;
{stats["samples"]} samples, including {stats["content_samples"]} text/archive excerpts.
Inventory: {stats["inventoried_dirs"]} of {stats["directories"]} discovered folders.</p>
<p>Sampling is not an exhaustive content review. Technical classification is not permission to delete.
Read errors, exclusions, unsupported formats, and disconnected drives limit coverage.</p>
<p>Finding links open the finding in the running local application, where Finder actions are available.</p></section>"""
    if scan["settings"].get("investigation_mode") == "detail":
        detail = stats["detail"]
        yield f"<section><h2>All-file coverage</h2><p>{detail['processed']} files processed; {detail['hashed']} full-byte fingerprints; {detail['ai_reviewed']} AI assessments; {detail['errors']} read issues.</p><p>Semantic review uses bounded supported content or metadata. Fingerprints cover bytes, not meaning.</p></section>"
        with store.connect() as db:
            for row in db.execute(
                "SELECT f.id,f.rel,x.* FROM files f JOIN file_details x ON x.file_id=f.id WHERE f.scan_id=? ORDER BY f.id",
                (scan_id,),
            ):
                target = f"{origin}/?scan={scan_id}&file={row['file_id']}"
                yield f"<article><h2><a href='{esc(target)}'>{esc(row['rel'])}</a></h2><p>{esc(row['summary'])}</p><p>{esc(row['reason'])}</p><small>{esc(row['state'])} · Content: {esc(row['content_status'])}</small><p>SHA-256: <code>{esc(row['sha256'])}</code></p><p>{esc(row['error'])}</p></article>"
    for finding in all_findings(store, scan_id):
        link = f"{origin}/?scan={scan_id}&finding={finding['id']}"
        yield (
            f"<article><small>Priority {finding['priority']} · {esc(finding['method'])}</small>"
            f"<h2><a href='{esc(link)}'>{esc(finding['rel'])}</a></h2>"
            f"<p>{esc(finding['summary'])}</p><p>{esc(finding['reason'])}</p>"
            f"<p>{esc(finding['next_action'])}</p><details><summary>Samples ({len(finding['samples'])})</summary>"
        )
        for sample in finding["samples"]:
            yield (
                f"<p><code>{esc(sample['rel'])}</code> · {esc(sample['status'])}</p>"
                f"<pre>{esc(sample.get('text', ''))}</pre>"
            )
        yield "</details></article>"
    yield "<section><h2>Investigation settings</h2><pre>" + esc(
        json.dumps(scan["settings"], ensure_ascii=False, indent=2)
    )
    yield "</pre></section><section><h2>Skipped paths & errors</h2>"
    with store.connect() as db:
        for event in db.execute(
            "SELECT kind,rel,message FROM events WHERE scan_id=? ORDER BY id", (scan_id,)
        ):
            yield f"<p><b>{esc(event['kind'])}</b> · <code>{esc(event['rel'])}</code>: {esc(event['message'])}</p>"
    yield "</section></html>"


def detail_csv(store, scan_id):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    yield "\ufeff"
    writer.writerow(
        [
            "Path",
            "Size bytes",
            "State",
            "Content coverage",
            "SHA-256",
            "Priority",
            "Summary",
            "Reason",
            "Topics",
            "Error",
        ]
    )
    yield buffer.getvalue()
    with store.connect() as db:
        for row in db.execute(
            "SELECT f.rel,f.size,x.* FROM files f JOIN file_details x ON x.file_id=f.id WHERE f.scan_id=? ORDER BY f.id",
            (scan_id,),
        ):
            buffer.seek(0)
            buffer.truncate()
            writer.writerow(
                [
                    safe_cell(row[k])
                    for k in (
                        "rel",
                        "size",
                        "state",
                        "content_status",
                        "sha256",
                        "priority",
                        "summary",
                        "reason",
                        "topics",
                        "error",
                    )
                ]
            )
            yield buffer.getvalue()
