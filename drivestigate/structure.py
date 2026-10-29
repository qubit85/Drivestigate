"""Recognize directory roles from catalog metadata, without reading source content."""

import json
import re
from pathlib import PurePosixPath

VERSION = 1
PROGRAM_ROOTS = {"program files", "program files (x86)", "programme", "programme (x86)"}
PROFILE_ROOTS = {"users", "documents and settings", "dokumente und einstellungen", "home"}
PERSONAL = {
    "desktop",
    "documents",
    "my documents",
    "dokumente",
    "eigene dateien",
    "downloads",
    "pictures",
    "my pictures",
    "bilder",
    "eigene bilder",
    "photos",
    "music",
    "musik",
    "videos",
    "my videos",
    "eigene videos",
    "saved games",
    "savegames",
    "saves",
    "userdata",
    "user data",
    "profiles",
    "projects",
    "projekte",
    "onedrive",
    "dropbox",
}
STRONG_PERSONAL = {
    "saved games",
    "savegames",
    "saves",
    "userdata",
    "user data",
    "profiles",
    "projects",
    "projekte",
}
DEPENDENCIES = {"node_modules", ".git", ".venv", "venv", "__pycache__", "site-packages"}
CACHES = {"cache", "caches", ".cache", "gpucache", "code cache", "temp", "temporary internet files"}
ASSETS = {
    "assets",
    "resources",
    "templates",
    "template",
    "samples",
    "examples",
    "docs",
    "documentation",
    "help",
    "manuals",
    "fonts",
    "locales",
}
SYSTEM_PARTS = {"system32", "syswow64", "winsxs", "driverstore", "$windows.~bt"}
PRIVATE_FILES = re.compile(
    r"wallet|vertrag|contract|rechnung|invoice|steuer|tax|thesis|abschluss|family|familie|holiday|urlaub|passwort|password",
    re.I,
)
USER_EXTS = {".pst", ".ost", ".kdbx", ".wallet", ".sav", ".sqlite", ".sqlite3", ".db", ".mdb", ".accdb"}
EDITABLE = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".rtf", ".pages", ".numbers"}
BINARY = {".dll", ".exe", ".sys", ".msi", ".cab", ".dylib", ".so", ".pyc", ".class", ".o"}
LABELS = {
    "system": "Operating system files",
    "application": "Application files",
    "dependencies": "Dependencies",
    "cache": "Cache / temporary files",
    "personal": "Personal data location",
    "profile": "User profile",
    "application_data": "Application settings / user data",
    "unknown": "Unclassified",
}


def role(relative, name="", root="", windows=()):
    prefix = PurePosixPath(root).parts if root else ()
    relative_parts = () if relative == "." else PurePosixPath(relative).parts
    parts = (*prefix, *relative_parts)
    folders = parts[:-1] if name else parts
    lower = [p.casefold() for p in folders]
    ext = PurePosixPath(name).suffix.lower()

    def location(index):
        return "/".join(folders[len(prefix) : index + 1]) or "."

    def result(kind, index=None, label="", reason=""):
        return {
            "role": kind,
            "group_rel": location(index) if index is not None else "",
            "label": label or (folders[index] if index is not None else ""),
            "reason": reason,
            "technical": kind in {"system", "application", "dependencies", "cache"},
        }

    applications = [i for i, p in enumerate(lower) if p in PROGRAM_ROOTS or p.endswith(".app")]
    app = applications[-1] if applications else None
    app_index = (
        min(app + (0 if lower[app].endswith(".app") else 1), len(folders) - 1) if app is not None else None
    )
    system = next((i for i, p in enumerate(lower) if p in SYSTEM_PARTS), None)
    for candidate in windows:
        candidate_parts = (*prefix, *(() if candidate == "." else PurePosixPath(candidate).parts))
        if tuple(p.casefold() for p in folders[: len(candidate_parts)]) == tuple(
            p.casefold() for p in candidate_parts
        ):
            system = len(candidate_parts) - 1
    base = (
        result(
            "application",
            app_index,
            reason="Program installation directory layout; presence does not establish a working installation.",
        )
        if app is not None
        else result("system", system, reason="Recognized operating-system directory layout.")
        if system is not None
        else result("unknown")
    )
    sensitive = bool(PRIVATE_FILES.search(name)) or ext in USER_EXTS and not set(lower) & CACHES
    if sensitive:
        return {
            **base,
            "role": "personal",
            "technical": False,
            "reason": "Filename or file type may contain user-created or private data; verify its contents.",
        }
    dependency = next((i for i, p in enumerate(lower) if p in DEPENDENCIES), None)
    cache = next((i for i, p in enumerate(lower) if p in CACHES), None)
    if dependency is not None or cache is not None:
        index = dependency if dependency is not None else cache
        return result(
            "dependencies" if dependency is not None else "cache",
            index,
            reason="Generated dependency or temporary directory; content is grouped, not reviewed individually.",
        )
    personal = next((i for i, p in enumerate(lower) if p in PERSONAL or p.startswith("onedrive - ")), None)
    if personal is not None and (app is None and system is None or lower[personal] in STRONG_PERSONAL):
        return {
            **result(
                "personal",
                personal,
                reason="Recognized personal-data folder name; contents still require review.",
            ),
            "group_rel": base["group_rel"] or location(personal),
        }
    if app is not None or system is not None:
        assets = bool(set(lower) & ASSETS)
        if (
            name
            and (ext in EDITABLE or ext == ".pdf")
            and not assets
            and not re.search(r"manual|licen[cs]e|readme|guide", name, re.I)
        ):
            return {
                **base,
                "role": "personal",
                "technical": False,
                "reason": "Potential user document inside a technical tree; retained for content review.",
            }
        return base
    appdata = next(
        (
            i
            for i, p in enumerate(lower)
            if p in {"appdata", "application data", "anwendungsdaten", "programdata"}
        ),
        None,
    )
    if appdata is not None:
        return result(
            "application_data",
            appdata,
            reason="Application data can include browser profiles, mail, databases and user settings.",
        )
    profile = next((i for i, p in enumerate(lower) if p in PROFILE_ROOTS and i + 1 < len(folders)), None)
    if profile is not None:
        return result(
            "profile",
            profile + 1,
            reason="Recognized user-profile layout; inspect personal folders before technical material.",
        )
    return {**base, "technical": ext in BINARY or name.casefold() in {".ds_store", "thumbs.db"}}


def priority(info, category, old_score):
    if info["technical"]:
        return 5
    return max(old_score, {"personal": 120, "application_data": 105, "profile": 90}.get(info["role"], 0))


def prepare_structure(store, scan, checkpoint=lambda: None):
    sid, root = scan["id"], scan["root"]
    with store.connect() as db:
        directories = [
            dict(r)
            for r in db.execute(
                "SELECT id,rel,parent_rel,tree_files,tree_bytes FROM directories WHERE scan_id=?", (sid,)
            )
        ]
    children = {}
    for directory in directories:
        children.setdefault(directory["parent_rel"], set()).add(
            PurePosixPath(directory["rel"]).name.casefold()
        )
    windows = [
        d["rel"]
        for d in directories
        if (
            PurePosixPath(d["rel"]).name.casefold() == "windows"
            or d["rel"] == "."
            and PurePosixPath(root).name.casefold() == "windows"
        )
        and children.get(d["rel"], set()) & {"system32", "winsxs", "syswow64"}
    ]
    contexts, groups = {}, {}
    for directory in directories:
        info = role(directory["rel"], root=root, windows=windows)
        contexts[directory["rel"]] = info
        if info["group_rel"] and info["role"] in {"system", "application", "dependencies", "cache"}:
            groups.setdefault(
                info["group_rel"],
                {
                    "kind": info["role"],
                    "label": info["label"],
                    "rel": info["group_rel"],
                    "basis": info["reason"],
                },
            )
    last = 0
    while True:
        checkpoint()
        with store.connect() as db:
            rows = db.execute(
                "SELECT id,rel,name,category,score FROM files WHERE scan_id=? AND id>? ORDER BY id LIMIT 1000",
                (sid, last),
            ).fetchall()
        if not rows:
            break
        updates = []
        for row in rows:
            info = role(row["rel"], row["name"], root, windows)
            updates.append(
                (
                    info["role"],
                    info["group_rel"],
                    int(info["technical"]),
                    priority(info, row["category"], row["score"]),
                    row["id"],
                )
            )
        with store.connect() as db:
            db.executemany("UPDATE files SET role=?,group_rel=?,technical=?,score=? WHERE id=?", updates)
        last = rows[-1]["id"]
    with store.connect() as db:
        db.executemany(
            "UPDATE directories SET role=?,role_reason=?,group_rel=? WHERE id=?",
            [
                (
                    contexts[d["rel"]]["role"],
                    contexts[d["rel"]]["reason"],
                    contexts[d["rel"]]["group_rel"],
                    d["id"],
                )
                for d in directories
            ],
        )
        totals = {
            r["group_rel"]: dict(r)
            for r in db.execute(
                "SELECT group_rel,COUNT(*) files,COALESCE(SUM(size),0) bytes,SUM(technical) technical_files,SUM(technical=0) user_files FROM files WHERE scan_id=? GROUP BY group_rel",
                (sid,),
            )
        }
        ids = {d["rel"]: d["id"] for d in directories}
        installations = [
            {**g, **totals[g["rel"]], "id": ids.get(g["rel"]), "kind": "directory", "role": g["kind"]}
            for g in groups.values()
            if g["rel"] in totals
        ]
        locations = []
        for d in directories:
            info, parent = contexts[d["rel"]], contexts.get(d["parent_rel"], {})
            if info["role"] in {"personal", "profile", "application_data"} and (
                info["role"] != parent.get("role") or info["group_rel"] != parent.get("group_rel")
            ):
                locations.append(
                    {
                        "id": d["id"],
                        "rel": d["rel"],
                        "role": info["role"],
                        "kind": "directory",
                        "files": d["tree_files"],
                        "bytes": d["tree_bytes"],
                        "basis": info["reason"],
                    }
                )
        payload = {
            "version": VERSION,
            "groups": sorted(installations, key=lambda g: (-g["bytes"], g["rel"])),
            "user_locations": sorted(
                locations, key=lambda d: (d["role"] != "personal", -d["bytes"], d["rel"])
            ),
            "basis": "Directory layout and file metadata only. Installation footprints do not establish that software was runnable; folder roles are discovery hints, not content assessments.",
        }
        db.execute(
            "INSERT INTO scan_structure(scan_id,payload) VALUES(?,?) ON CONFLICT(scan_id) DO UPDATE SET payload=excluded.payload",
            (sid, json.dumps(payload)),
        )
        if scan["settings"]["hide_technical"]:
            db.execute(
                "DELETE FROM findings WHERE scan_id=? AND analyzed=0 AND NOT EXISTS (SELECT 1 FROM files f WHERE f.dir_id=findings.dir_id AND f.technical=0)",
                (sid,),
            )
    return payload
