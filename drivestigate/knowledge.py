"""Evidence-linked local graph and a separate, non-destructive Obsidian vault export."""

import hashlib
import json
import re
import tempfile
import uuid
from pathlib import Path, PurePosixPath


def key(value):
    return hashlib.sha256(value.strip().casefold().encode()).hexdigest()[:20]


def text(value):
    return re.sub(r"([\\`*_[\]<>#|!])", r"\\\1", str(value)).replace("\r", " ").replace("\n", " ")


def link(note, label):
    clean = re.sub(r"[\[\]|#^\r\n]", " ", str(label))
    return f"[[{note}|{clean}]]"


def file_note(file_id, name):
    # Human-readable graph labels with stable IDs and portable, non-interpreted paths.
    clean = re.sub(r"[^\w .()&-]", " ", name, flags=re.UNICODE).strip(" .")[:70] or "File"
    return f"Files/{clean}--{file_id}"


def folder_note(folder_id, relative):
    clean = (
        re.sub(r"[^\w .()&-]", " ", PurePosixPath(relative).name or "Drive", flags=re.UNICODE).strip(" .")[
            :70
        ]
        or "Drive"
    )
    return f"Folders/{clean}--{folder_id}"


def frontmatter(kind, **values):
    # Obsidian recognizes wikilinks in properties too. Keep source/model labels literal.
    values = {k: text(v) if k in ("title", "source_path") else v for k, v in values.items()}
    return (
        "---\n"
        + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in {"type": kind, **values}.items())
        + "\n---\n\n"
    )


def graph_data(store, scan_id, query="", limit=200, folder=None):
    query = query[:200]
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    where = "f.scan_id=? AND (f.rel LIKE ? ESCAPE '\\' OR x.summary LIKE ? ESCAPE '\\' OR x.topics LIKE ? ESCAPE '\\' OR x.sha256 LIKE ? ESCAPE '\\')"
    args = [scan_id, pattern, pattern, pattern, pattern]
    if folder is not None:
        if PurePosixPath(folder).is_absolute() or ".." in PurePosixPath(folder).parts:
            raise ValueError("Invalid folder filter.")
        where += " AND (d.rel=? OR d.rel LIKE ? ESCAPE '\\')"
        prefix = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.extend([folder, prefix + "/%"])
    nodes, edges, folders, duplicate_counts = {}, [], {}, {}
    with store.connect() as db:
        total = db.execute("SELECT COUNT(*) FROM files WHERE scan_id=?", (scan_id,)).fetchone()[0]
        matching = db.execute(
            "SELECT COUNT(*) FROM files f JOIN directories d ON d.id=f.dir_id "
            f"LEFT JOIN file_details x ON x.file_id=f.id WHERE {where}",
            args,
        ).fetchone()[0]
        rows = db.execute(
            "SELECT f.*,d.rel folder,x.state,x.sha256,x.topics,x.summary,x.content_status,"
            "COALESCE(x.priority,2) priority FROM files f JOIN directories d ON d.id=f.dir_id "
            f"LEFT JOIN file_details x ON x.file_id=f.id WHERE {where} "
            "ORDER BY COALESCE(x.priority,2),f.id LIMIT ?",
            (*args, limit),
        ).fetchall()

        def add_folder(relative):
            if relative in folders:
                return folders[relative]
            row = db.execute(
                "SELECT id,parent_rel FROM directories WHERE scan_id=? AND rel=?", (scan_id, relative)
            ).fetchone()
            if not row:
                return None
            node_id = f"d:{row['id']}"
            folders[relative] = node_id
            nodes[node_id] = {
                "id": node_id,
                "kind": "folder",
                "label": PurePosixPath(relative).name or "Drive",
                "rel": relative,
                "state": "structural",
            }
            if relative != ".":
                parent = add_folder(row["parent_rel"])
                if parent:
                    edges.append(
                        {"source": parent, "target": node_id, "kind": "contains", "provenance": "filesystem"}
                    )
            return node_id

        for row in rows:
            f = dict(row)
            node_id = f"f:{f['id']}"
            nodes[node_id] = {
                "id": node_id,
                "kind": "file",
                "file_id": f["id"],
                "label": f["name"],
                "rel": f["rel"],
                "state": f["state"] or "pending",
                "size": f["size"],
                "content_status": f["content_status"] or "pending",
                "summary": f["summary"] or "Not yet inspected.",
                "priority": f["priority"],
            }
            folder_id = add_folder(f["folder"])
            if folder_id:
                edges.append(
                    {"source": folder_id, "target": node_id, "kind": "contains", "provenance": "filesystem"}
                )
            type_id = "k:" + key(f["category"])
            nodes[type_id] = {"id": type_id, "kind": "type", "label": f["category"], "state": "structural"}
            edges.append(
                {"source": node_id, "target": type_id, "kind": "file type", "provenance": "extension"}
            )
            for topic in json.loads(f["topics"] or "[]"):
                tid = "t:" + key(topic)
                nodes[tid] = {"id": tid, "kind": "topic", "label": topic, "state": "suggested"}
                edges.append(
                    {"source": node_id, "target": tid, "kind": "suggested topic", "provenance": "ai"}
                )
            digest = f["sha256"]
            if digest:
                if digest not in duplicate_counts:
                    duplicate_counts[digest] = db.execute(
                        "SELECT COUNT(*) FROM file_details x JOIN files f "
                        "ON f.id=x.file_id WHERE f.scan_id=? AND x.sha256=?",
                        (scan_id, digest),
                    ).fetchone()[0]
                if duplicate_counts[digest] > 1:
                    hid = "h:" + digest
                    nodes[hid] = {
                        "id": hid,
                        "kind": "duplicate",
                        "label": f"Identical bytes · {duplicate_counts[digest]} files",
                        "state": "verified",
                        "sha256": digest,
                    }
                    edges.append(
                        {
                            "source": node_id,
                            "target": hid,
                            "kind": "same SHA-256",
                            "provenance": "fingerprint",
                        }
                    )
    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "total_files": total,
        "matching_files": matching,
        "shown_files": len(rows),
        "truncated": matching > len(rows),
        "note": "Folder and hash links are observed. Topic links are AI suggestions, not verified facts.",
    }


def build_vault(engine, scan):
    store, sid = engine.store, scan["id"]
    base = Path(store.path).resolve().parent / "vaults"
    base.mkdir(mode=0o700, exist_ok=True)
    with store.connect() as db:
        total = db.execute("SELECT COUNT(*) FROM files WHERE scan_id=?", (sid,)).fetchone()[0]
        folders = [
            dict(r) for r in db.execute("SELECT * FROM directories WHERE scan_id=? ORDER BY id", (sid,))
        ]
    engine.progress_update(
        sid, stage="vault", completed=0, total=total + len(folders), unit="notes", current=""
    )
    engine.live.setdefault(sid, {}).update(durations=[], baseline=0)
    topics, categories, hashes = {}, {}, {}
    with tempfile.TemporaryDirectory(prefix=".building-", dir=base) as temporary:
        root = Path(temporary)
        for folder in ("Files", "Folders", "Topics", "Types", "Duplicates", ".obsidian"):
            (root / folder).mkdir()
        (root / ".obsidian/app.json").write_text('{"alwaysUpdateLinks":true}')
        completed, last_id = 0, 0
        while True:
            with store.connect() as db:
                batch = [
                    dict(r)
                    for r in db.execute(
                        "SELECT f.*,x.state,x.sha256,x.summary,x.reason,x.topics,"
                        "x.excerpt,x.error,x.content_status FROM files f LEFT JOIN file_details x ON x.file_id=f.id "
                        "WHERE f.scan_id=? AND f.id>? ORDER BY f.id LIMIT 250",
                        (sid, last_id),
                    )
                ]
            if not batch:
                break
            for f in batch:
                engine.checkpoint(scan)
                last_id = f["id"]
                related = json.loads(f["topics"] or "[]")
                body = frontmatter(
                    "file",
                    title=f["name"],
                    source_path=f["rel"],
                    scan_id=sid,
                    coverage=f["content_status"] or "pending",
                    status=f["state"] or "pending",
                    sha256=f["sha256"] or "",
                    size_bytes=f["size"],
                )
                body += f"# {text(f['name'])}\n\n{link(folder_note(f['dir_id'], str(PurePosixPath(f['rel']).parent)), 'Containing folder')} · "
                type_key = key(f["category"])
                body += link("Types/Type-" + type_key, f["category"]) + "\n\n"
                body += "## Assessment\n\n" + text(f["summary"] or "Not yet inspected.") + "\n\n"
                body += text(f["reason"] or "") + "\n\n"
                body += "## Source and coverage\n\n" + text(f["rel"]) + "\n\n"
                body += f"Content coverage: {text(f['content_status'] or 'pending')}. A full-byte fingerprint is not a full semantic review.\n\n"
                body += f"[View in Drivestigate]({engine.origin}/?scan={sid}&file={f['id']})\n\n"
                if f["error"]:
                    body += "Read issue: " + text(f["error"]) + "\n\n"
                if related:
                    body += "## Suggested topics\n\nAI-derived relationships; verify against the source.\n\n"
                for topic in related:
                    tk = key(topic)
                    topics.setdefault(tk, {"label": topic, "files": []})["files"].append((f["id"], f["name"]))
                    body += "- " + link("Topics/Topic-" + tk, topic) + "\n"
                categories.setdefault(type_key, {"label": f["category"], "files": []})["files"].append(
                    (f["id"], f["name"])
                )
                if f["sha256"]:
                    hashes.setdefault(f["sha256"], []).append((f["id"], f["name"]))
                if scan["settings"]["vault_include_excerpts"] and f["excerpt"]:
                    longest = max([len(m.group()) for m in re.finditer(r"`+", f["excerpt"])] or [0])
                    fence = "`" * max(3, longest + 1)
                    body += f"\n## Evidence excerpt (untrusted source data)\n\n{fence}text\n{f['excerpt']}\n{fence}\n"
                (root / (file_note(f["id"], f["name"]) + ".md")).write_text(body, encoding="utf-8")
                completed += 1
                if completed % 25 == 0:
                    engine.progress_update(sid, completed=completed, current=f["rel"])
        by_rel = {f["rel"]: f["id"] for f in folders}
        for folder in folders:
            engine.checkpoint(scan)
            body = frontmatter("folder", title=folder["rel"], scan_id=sid) + f"# {text(folder['rel'])}\n\n"
            if folder["parent_rel"] in by_rel:
                body += (
                    link(folder_note(by_rel[folder["parent_rel"]], folder["parent_rel"]), "Parent folder")
                    + "\n\n"
                )
            with store.connect() as db:
                for child in db.execute(
                    "SELECT id,rel FROM directories WHERE scan_id=? AND parent_rel=?", (sid, folder["rel"])
                ):
                    body += "- " + link(folder_note(child["id"], child["rel"]), child["rel"]) + "\n"
                for child in db.execute("SELECT id,name FROM files WHERE dir_id=?", (folder["id"],)):
                    body += "- " + link(file_note(child["id"], child["name"]), child["name"]) + "\n"
            (root / (folder_note(folder["id"], folder["rel"]) + ".md")).write_text(body, encoding="utf-8")
            completed += 1
        for kind, prefix, groups in (("Topics", "Topic-", topics), ("Types", "Type-", categories)):
            for group_id, group in groups.items():
                engine.checkpoint(scan)
                body = frontmatter(
                    "topic" if kind == "Topics" else "file-type",
                    title=group["label"],
                    provenance="ai-suggested" if kind == "Topics" else "extension",
                )
                body += f"# {text(group['label'])}\n\n"
                if kind == "Topics":
                    body += "AI-suggested relationship. Shared topics do not prove that files belong together.\n\n"
                body += "\n".join("- " + link(file_note(fid, name), name) for fid, name in group["files"])
                (root / kind / f"{prefix}{group_id}.md").write_text(body, encoding="utf-8")
        for digest, members in hashes.items():
            if len(members) < 2:
                continue
            body = frontmatter("duplicate-group", sha256=digest) + "# Identical byte fingerprints\n\n"
            body += "Matching SHA-256 hashes observed in this investigation; no deletion recommendation.\n\n"
            for fid, name in members:
                note_file = root / (file_note(fid, name) + ".md")
                with note_file.open("a", encoding="utf-8") as stream:
                    stream.write("\n" + link("Duplicates/Hash-" + digest, "Identical-byte group") + "\n")
                body += "- " + link(file_note(fid, name), name) + "\n"
            (root / "Duplicates" / f"Hash-{digest}.md").write_text(body, encoding="utf-8")
        stats = store.stats(sid)
        index = frontmatter("investigation", title=scan["name"], scan_id=sid) + f"# {text(scan['name'])}\n\n"
        index += f"{total} file notes, {len(folders)} folder notes, {len(topics)} suggested topics.\n\n"
        index += link(folder_note(by_rel.get(".", ""), "."), "Explore the folder hierarchy") + "\n\n"
        index += (
            link("Coverage", "Coverage and limitations")
            + "\n\nOpen this folder as a vault in Obsidian and select Graph view.\n"
        )
        (root / "Investigation.md").write_text(index, encoding="utf-8")
        coverage = "# Coverage and limitations\n\n"
        coverage += "All inventoried regular files were attempted. Exclusions, symlinks, other filesystems and unreadable paths are not covered.\n\n"
        coverage += "Full SHA-256 reads cover bytes, not meaning. Semantic analysis uses bounded supported excerpts, image thumbnails or metadata.\n\n"
        coverage += "```json\n" + json.dumps(stats, indent=2) + "\n```\n\n## Skipped paths and issues\n\n"
        with store.connect() as db:
            for event in db.execute("SELECT kind,rel,message FROM events WHERE scan_id=?", (sid,)):
                coverage += f"- {text(event['kind'])}: {text(event['rel'])} — {text(event['message'])}\n"
        (root / "Coverage.md").write_text(coverage, encoding="utf-8")
        engine.checkpoint(scan)
        destination = base / f"{sid}-{uuid.uuid4().hex[:8]}"
        root.rename(destination)
        engine.progress_update(sid, completed=total + len(folders), current="Vault ready")
        return destination


def vault_path(store, scan_id):
    scan = store.get_scan(scan_id)
    path = Path(scan["vault_path"])
    base = Path(store.path).resolve().parent / "vaults"
    if (
        not scan["vault_path"]
        or not path.is_dir()
        or path.resolve() != path.absolute()
        or not path.is_relative_to(base)
    ):
        raise ValueError("No completed vault is available for this investigation yet.")
    return path
