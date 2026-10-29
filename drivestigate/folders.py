"""On-demand folder descriptions from saved evidence; no source reads or inference."""

from pathlib import PurePosixPath


def scope(alias, relative, include_self=False):
    if relative == ".":
        return "1=1", []
    prefix = relative + "/"
    # BINARY prefix range uses the catalog's (scan_id, rel) index and treats SQL
    # wildcard characters and case-distinct paths literally. '/' sorts before '0'.
    descendants = f"({alias}.rel>=? AND {alias}.rel<?)"
    if include_self:
        return f"({alias}.rel=? OR {descendants})", [relative, prefix, relative + "0"]
    return descendants, [prefix, relative + "0"]


def folder_summary(store, scan_id, directory_id):
    scan = store.get_scan(scan_id)
    with store.connect() as db:
        row = db.execute(
            "SELECT * FROM directories WHERE scan_id=? AND id=?", (scan_id, directory_id)
        ).fetchone()
        if not row:
            raise ValueError("Folder not found in this investigation.")
        folder = dict(row)
        file_scope, file_args = scope("f", folder["rel"])
        dir_scope, dir_args = scope("d", folder["rel"], include_self=True)
        counts = dict(
            db.execute(
                "SELECT COUNT(*) files,COALESCE(SUM(f.size),0) bytes,COALESCE(SUM(f.technical),0) technical_files,"
                "COALESCE(SUM(x.state IN ('reviewed','metadata','error')),0) processed,"
                "COALESCE(SUM(x.state='reviewed'),0) ai_files,"
                "COALESCE(SUM(x.content_status IN ('excerpt','image')),0) content_samples,"
                "COALESCE(SUM(x.content_status='grouped'),0) grouped,"
                "COALESCE(SUM(x.error!='' OR x.state='error'),0) read_issues "
                "FROM files f LEFT JOIN file_details x ON x.file_id=f.id WHERE f.scan_id=? AND " + file_scope,
                (scan_id, *file_args),
            ).fetchone()
        )
        directories = dict(
            db.execute(
                "SELECT COUNT(*) folders,COALESCE(SUM(done=0),0) pending FROM directories d WHERE scan_id=? AND "
                + dir_scope,
                (scan_id, *dir_args),
            ).fetchone()
        )
        types = [
            dict(r)
            for r in db.execute(
                "SELECT category,COUNT(*) count FROM files f WHERE f.scan_id=? AND "
                + file_scope
                + " GROUP BY category ORDER BY count DESC,category LIMIT 4",
                (scan_id, *file_args),
            )
        ]
        assessed = dict(
            db.execute(
                "SELECT COUNT(*) relevant,COALESCE(SUM(a.analyzed),0) assessed FROM findings a "
                "JOIN directories d ON d.id=a.dir_id WHERE a.scan_id=? AND " + dir_scope,
                (scan_id, *dir_args),
            ).fetchone()
        )
        event_scope, event_args = scope("e", folder["rel"], include_self=True)
        path_events = dict(
            db.execute(
                "SELECT COALESCE(SUM(kind IN ('unreadable','error')),0) issues,"
                "COALESCE(SUM(kind IN ('excluded','symlink','mount','special')),0) skipped "
                "FROM events e WHERE scan_id=? AND " + event_scope,
                (scan_id, *event_args),
            ).fetchone()
        )
        own = db.execute(
            "SELECT * FROM findings WHERE scan_id=? AND dir_id=? AND analyzed=1", (scan_id, directory_id)
        ).fetchone()
        children = [
            r["rel"]
            for r in db.execute(
                "SELECT rel FROM directories WHERE scan_id=? AND parent_rel=? ORDER BY rel COLLATE NOCASE LIMIT 4",
                (scan_id, folder["rel"]),
            )
        ]
        examples = [
            r["rel"]
            for r in db.execute(
                "SELECT f.rel FROM files f WHERE f.scan_id=? AND "
                + file_scope
                + " ORDER BY (f.dir_id=?) DESC,f.technical,f.score DESC,f.id LIMIT 3",
                (scan_id, *file_args, directory_id),
            )
        ]
        evidence = []
        if own and own["method"] != "detail":
            evidence.append(
                {
                    "path": folder["rel"],
                    "text": own["summary"],
                    "basis": "Sampled content" if own["method"] == "sampled" else "AI metadata assessment",
                }
            )
        if scan["settings"].get("investigation_mode") == "detail":
            for r in db.execute(
                "SELECT f.rel,x.summary,x.content_status FROM files f JOIN file_details x ON x.file_id=f.id "
                "WHERE f.scan_id=? AND x.state='reviewed' AND "
                + file_scope
                + " ORDER BY (f.dir_id=?) DESC,f.technical,x.priority,f.id LIMIT 2",
                (scan_id, *file_args, directory_id),
            ):
                evidence.append(
                    {
                        "path": r["rel"],
                        "text": r["summary"],
                        "basis": "Per-file content assessment"
                        if r["content_status"] in ("excerpt", "image")
                        else "Per-file metadata assessment",
                    }
                )
        if not evidence:
            for r in db.execute(
                "SELECT d.rel,a.summary,a.method FROM findings a JOIN directories d ON d.id=a.dir_id "
                "WHERE a.scan_id=? AND a.analyzed=1 AND a.method!='detail' AND "
                + dir_scope
                + " ORDER BY a.priority,d.rel LIMIT 2",
                (scan_id, *dir_args),
            ):
                evidence.append(
                    {
                        "path": r["rel"],
                        "text": r["summary"],
                        "basis": "Sampled subfolder content"
                        if r["method"] == "sampled"
                        else "Subfolder metadata assessment",
                    }
                )
    subfolders = max(0, directories["folders"] - 1)
    if not counts["files"] and not subfolders:
        inventory = (
            "This folder is empty in the saved inventory."
            if folder["done"]
            else "This folder has been discovered; its inventory is not complete yet."
        )
        if folder["done"] and (path_events["issues"] or path_events["skipped"]):
            inventory = "No files were inventoried here. Skipped or unreadable paths prevent assuming this folder is empty."
    else:
        file_word = "file" if counts["files"] == 1 else "files"
        folder_word = "subfolder" if subfolders == 1 else "subfolders"
        inventory = (
            f"The saved inventory contains {counts['files']:,} {file_word} across this folder and its {subfolders:,} {folder_word}."
            if subfolders
            else f"The saved inventory contains {counts['files']:,} {file_word} directly in this folder."
        )
        if types:
            inventory += (
                " Most common file types: "
                + ", ".join(f"{t['category'].lower()} ({t['count']:,})" for t in types)
                + "."
            )
    if own and own["method"] != "detail":
        summary = own["summary"].strip() + "\n\n" + inventory
        basis = evidence[0]["basis"]
    elif evidence:
        lead = (
            "Saved per-file assessments:"
            if scan["settings"].get("investigation_mode") == "detail"
            else "Selected reviewed subfolders:"
        )
        summary = (
            lead
            + "\n"
            + "\n".join(f"{e['path']} ({e['basis']}): {e['text'][:700]}" for e in evidence)
            + "\n\n"
            + inventory
        )
        basis = (
            "Per-file assessments"
            if scan["settings"].get("investigation_mode") == "detail"
            else "Selected subfolder assessments"
        )
    else:
        summary, basis = inventory, "Filenames and metadata only"
        if examples:
            summary += "\n\nExample paths from the inventory: " + "; ".join(examples) + "."
        if children:
            summary += "\nSubfolders include: " + ", ".join(PurePosixPath(c).name for c in children) + "."
    if folder.get("role") in {"application", "system", "dependencies", "cache"}:
        from .structure import LABELS

        summary = (
            f"{LABELS[folder['role']]} recognized from directory layout. {folder['role_reason']}\n\n"
            + summary
        )
        if not evidence:
            basis = "Recognized structure · metadata only"
    if scan["settings"].get("investigation_mode") == "detail":
        coverage = f"{counts['processed']:,} / {counts['files']:,} files processed across this subtree; {counts['ai_files']:,} AI assessments, {counts['content_samples']:,} readable content samples. Supported content is bounded; fingerprints do not prove semantic understanding."
        coverage += f" {counts['grouped']:,} technical files were grouped without individual content reads."
    else:
        coverage = f"{assessed['assessed']:,} / {assessed['relevant']:,} relevant folders assessed across this subtree."
        if own:
            coverage += " The direct-folder assessment does not cover every file or all subfolders."
        elif evidence:
            coverage += " Subfolder examples are selected evidence, not a full review of this parent folder."
        else:
            coverage += " Contents have not been established from reviewed evidence; file types and example paths are metadata."
    if scan["settings"].get("investigation_mode") == "progressive":
        coverage += (
            " Decision: "
            + (folder.get("decision") or "awaiting evidence")
            + ". "
            + folder.get("uncertainty", "")
        )
    if directories["pending"]:
        coverage += f" Inventory is still incomplete in {directories['pending']:,} discovered folders."
    if counts["read_issues"]:
        coverage += f" {counts['read_issues']:,} file read issues are recorded."
    if path_events["issues"] or path_events["skipped"]:
        coverage += f" {path_events['issues']:,} path issues and {path_events['skipped']:,} skipped entries are recorded."
    return {
        "id": directory_id,
        "scan_id": scan_id,
        "rel": folder["rel"],
        "summary": summary,
        "basis": basis,
        "coverage": coverage,
        "inventory": {**counts, "subfolders": subfolders},
        "evidence": evidence,
    }
