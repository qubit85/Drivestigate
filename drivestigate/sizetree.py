"""Paged mixed file/folder tree with cataloged subtree sizes."""


def size_tree(store, scan_id, parent=".", offset=0, limit=200, sort="size"):
    store.get_scan(scan_id)
    if sort not in {"size", "name"}:
        raise ValueError("Sort must be size or name.")
    limit, offset = max(1, min(500, int(limit))), max(0, int(offset))
    with store.connect() as db:
        row = db.execute("SELECT * FROM directories WHERE scan_id=? AND rel=?", (scan_id, parent)).fetchone()
        if not row:
            raise ValueError("Folder not found in this investigation.")
        root = dict(row)
        total = db.execute(
            "SELECT (SELECT COUNT(*) FROM directories WHERE scan_id=? AND parent_rel=?) + (SELECT COUNT(*) FROM files WHERE dir_id=?)",
            (scan_id, parent, root["id"]),
        ).fetchone()[0]
        ordering = (
            "bytes DESC,name COLLATE NOCASE,kind,id" if sort == "size" else "name COLLATE NOCASE,kind,id"
        )
        rows = db.execute(
            "SELECT * FROM (SELECT d.id,d.rel,'directory' kind,d.rel name,d.tree_bytes bytes,d.tree_files files,COALESCE(NULLIF(d.role,''),d.decision) role,d.tree_complete,"
            "(SELECT COUNT(*) FROM directories c WHERE c.scan_id=d.scan_id AND c.parent_rel=d.rel) + "
            "(SELECT COUNT(*) FROM files f WHERE f.dir_id=d.id) children FROM directories d WHERE d.scan_id=? AND d.parent_rel=? "
            "UNION ALL SELECT f.id,f.rel,'file' kind,f.name,f.size bytes,1 files,f.role,1 tree_complete,0 children FROM files f WHERE f.dir_id=?) "
            f"ORDER BY {ordering} LIMIT ? OFFSET ?",
            (scan_id, parent, root["id"], limit, offset),
        ).fetchall()
    items = [dict(r) for r in rows]
    for item in items:
        item["name"] = item["rel"].rsplit("/", 1)[-1]
    return {
        "parent": parent,
        "root": root,
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "provisional": not bool(root["tree_complete"]),
    }
