"""SQLite catalog with transactional inventory checkpoints."""

import json
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .config import DEFAULTS, validate_settings


def now():
    return datetime.now(UTC).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scans (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL, device INTEGER NOT NULL,
 identity TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
 status TEXT NOT NULL, phase TEXT NOT NULL, settings TEXT NOT NULL,
 message TEXT NOT NULL DEFAULT '', llm_calls INTEGER NOT NULL DEFAULT 0,
 read_bytes INTEGER NOT NULL DEFAULT 0, elapsed REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS directories (
 id INTEGER PRIMARY KEY, scan_id TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
 rel TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
 UNIQUE(scan_id,rel)
);
CREATE TABLE IF NOT EXISTS files (
 id INTEGER PRIMARY KEY, scan_id TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
 dir_id INTEGER NOT NULL REFERENCES directories(id) ON DELETE CASCADE,
 rel TEXT NOT NULL, name TEXT NOT NULL, ext TEXT NOT NULL,
 size INTEGER NOT NULL, mtime REAL NOT NULL, category TEXT NOT NULL,
 technical INTEGER NOT NULL, score INTEGER NOT NULL,
 UNIQUE(scan_id,rel)
);
CREATE INDEX IF NOT EXISTS files_dir ON files(dir_id);
CREATE INDEX IF NOT EXISTS files_scan ON files(scan_id,technical);
CREATE TABLE IF NOT EXISTS findings (
 id INTEGER PRIMARY KEY, scan_id TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
 dir_id INTEGER NOT NULL UNIQUE REFERENCES directories(id) ON DELETE CASCADE,
 priority INTEGER NOT NULL, summary TEXT NOT NULL, reason TEXT NOT NULL,
 method TEXT NOT NULL, samples TEXT NOT NULL DEFAULT '[]', references_json TEXT NOT NULL DEFAULT '[]',
 next_action TEXT NOT NULL DEFAULT '', analyzed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS findings_scan ON findings(scan_id,priority);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, scan_id TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
 created TEXT NOT NULL, kind TEXT NOT NULL, rel TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduler (id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO scheduler VALUES(1,0);
CREATE TABLE IF NOT EXISTS file_details (
 file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
 state TEXT NOT NULL DEFAULT 'pending', sha256 TEXT NOT NULL DEFAULT '',
 content_status TEXT NOT NULL DEFAULT 'pending', summary TEXT NOT NULL DEFAULT '',
 reason TEXT NOT NULL DEFAULT '', topics TEXT NOT NULL DEFAULT '[]',
 excerpt TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 2,
 error TEXT NOT NULL DEFAULT '', duration REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS detail_hash ON file_details(sha256);
CREATE TABLE IF NOT EXISTS scan_reports (
 scan_id TEXT PRIMARY KEY REFERENCES scans(id) ON DELETE CASCADE,
 payload TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'pending',
 generated TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS investigation_tasks (
 id INTEGER PRIMARY KEY, scan_id TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, target INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 priority INTEGER NOT NULL DEFAULT 50, attempts INTEGER NOT NULL DEFAULT 0,
 UNIQUE(scan_id,kind,target)
);
CREATE INDEX IF NOT EXISTS investigation_pending ON investigation_tasks(scan_id,state,priority);
CREATE TABLE IF NOT EXISTS investigation_evidence (
 file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
 scan_id TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investigation_memory (
 scan_id TEXT PRIMARY KEY REFERENCES scans(id) ON DELETE CASCADE, payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS scan_structure (
 scan_id TEXT PRIMARY KEY REFERENCES scans(id) ON DELETE CASCADE,
 payload TEXT NOT NULL DEFAULT '{}'
);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 6:
                raise RuntimeError("This catalog was created by a newer version of Drivestigate.")
            db.executescript(SCHEMA)
            scan_columns = {r[1] for r in db.execute("PRAGMA table_info(scans)")}
            for name, definition in {
                "queue_order": "INTEGER",
                "queued_finding": "INTEGER",
                "progress": "TEXT NOT NULL DEFAULT '{}'",
                "vault_path": "TEXT NOT NULL DEFAULT ''",
                "queue_task": "TEXT NOT NULL DEFAULT 'investigate'",
                "delete_requested": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in scan_columns:
                    db.execute(f"ALTER TABLE scans ADD COLUMN {name} {definition}")
            dir_columns = {r[1] for r in db.execute("PRAGMA table_info(directories)")}
            if "parent_rel" not in dir_columns:
                db.execute("ALTER TABLE directories ADD COLUMN parent_rel TEXT NOT NULL DEFAULT ''")
                rows = db.execute("SELECT id,rel FROM directories").fetchall()
                db.executemany(
                    "UPDATE directories SET parent_rel=? WHERE id=?",
                    [(str(PurePosixPath(r[1]).parent) if r[1] != "." else "", r[0]) for r in rows],
                )
            needs_rollup = version < 6 or "tree_files" not in dir_columns
            for name in ("tree_files", "tree_bytes"):
                if name not in dir_columns:
                    db.execute(f"ALTER TABLE directories ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            for name in ("role", "role_reason", "group_rel"):
                if name not in dir_columns:
                    db.execute(f"ALTER TABLE directories ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            for name, definition in {
                "revision": "INTEGER NOT NULL DEFAULT 0",
                "assessed_revision": "INTEGER NOT NULL DEFAULT -1",
                "planned_revision": "INTEGER NOT NULL DEFAULT -1",
                "decision": "TEXT NOT NULL DEFAULT ''",
                "uncertainty": "TEXT NOT NULL DEFAULT ''",
                "listing_error": "INTEGER NOT NULL DEFAULT 0",
                "tree_complete": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in dir_columns:
                    db.execute(f"ALTER TABLE directories ADD COLUMN {name} {definition}")
            file_columns = {r[1] for r in db.execute("PRAGMA table_info(files)")}
            for name in ("role", "group_rel"):
                if name not in file_columns:
                    db.execute(f"ALTER TABLE files ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS dirs_parent ON directories(scan_id,parent_rel)")
            if version < 6:
                config = db.execute("SELECT value FROM config WHERE id=1").fetchone()
                if config:
                    settings = json.loads(config[0])
                    settings["investigation_mode"] = "progressive"
                    if (
                        settings.get("depth"),
                        settings.get("max_folders"),
                        settings.get("samples_per_folder"),
                        settings.get("max_llm_calls"),
                        settings.get("time_budget_minutes"),
                    ) == ("balanced", 40, 6, 60, 60):
                        settings.update(
                            {
                                k: DEFAULTS[k]
                                for k in (
                                    "depth",
                                    "max_folders",
                                    "samples_per_folder",
                                    "max_llm_calls",
                                    "time_budget_minutes",
                                )
                            }
                        )
                    if not settings.get("search_focus"):
                        settings["search_focus"] = DEFAULTS["search_focus"]
                    db.execute("UPDATE config SET value=? WHERE id=1", (json.dumps(settings),))
                for existing in db.execute("SELECT id,settings FROM scans").fetchall():
                    settings = json.loads(existing["settings"])
                    if (
                        settings.get("investigation_mode") == "detail"
                        and "detail_include_technical" not in settings
                    ):
                        settings["detail_include_technical"] = True
                        db.execute(
                            "UPDATE scans SET settings=? WHERE id=?", (json.dumps(settings), existing["id"])
                        )
            db.execute("PRAGMA user_version=6")
            # No worker survives a process restart. Finish confirmed deletions that
            # were waiting for a read or inference operation when the app closed.
            db.execute("DELETE FROM scans WHERE delete_requested=1")
            db.execute("UPDATE investigation_tasks SET state='pending' WHERE state='running'")
            db.execute("UPDATE scan_reports SET state='pending' WHERE state='building'")
            db.execute(
                "UPDATE scans SET status='paused',message='Resume after restarting.' "
                "WHERE status IN ('running','pausing','stopping')"
            )
        if needs_rollup:
            for scan in self.scans():
                if scan["phase"] != "inventory":
                    self.rollup(scan["id"])
        Path(path).chmod(0o600)

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def settings(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM config WHERE id=1").fetchone()
        return validate_settings(json.loads(row[0])) if row else validate_settings(DEFAULTS)

    def save_settings(self, settings):
        settings = validate_settings(settings)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO config VALUES(1,?)", (json.dumps(settings),))
        return settings

    @contextmanager
    def snapshot(self):
        """Exports see one coherent catalog even while analysis updates priorities."""
        with tempfile.NamedTemporaryFile(
            prefix="export-", suffix=".sqlite3", dir=Path(self.path).parent
        ) as temp:
            with self.connect() as source:
                target = sqlite3.connect(temp.name)
                try:
                    source.backup(target)
                finally:
                    target.close()
            clone = object.__new__(Store)
            clone.path = temp.name
            clone.lock = threading.RLock()
            yield clone

    def get_scan(self, scan_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if not row:
            raise ValueError("Investigation not found.")
        result = dict(row)
        result["settings"] = json.loads(result["settings"])
        result["progress"] = json.loads(result["progress"])
        if result["delete_requested"]:
            result["status"] = "deleting"
        return result

    def delete_scan(self, scan_id):
        """Remove one catalog and its dependent rows, never filesystem paths."""
        with self.connect() as db:
            if not db.execute("DELETE FROM scans WHERE id=?", (scan_id,)).rowcount:
                raise ValueError("Investigation not found.")

    def update(self, scan_id, **values):
        allowed = {
            "status",
            "phase",
            "message",
            "llm_calls",
            "read_bytes",
            "elapsed",
            "device",
            "queue_order",
            "queued_finding",
            "progress",
            "vault_path",
            "queue_task",
            "delete_requested",
        }
        if set(values) - allowed:
            raise ValueError("Unknown scan field")
        values["updated"] = now()
        if isinstance(values.get("progress"), dict):
            values["progress"] = json.dumps(values["progress"])
        with self.connect() as db:
            db.execute(
                f"UPDATE scans SET {','.join(k + '=?' for k in values)} WHERE id=?",
                (*values.values(), scan_id),
            )

    def event(self, scan_id, kind, rel, message):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(scan_id,created,kind,rel,message) VALUES(?,?,?,?,?)",
                (scan_id, now(), kind, rel, str(message)[:1000]),
            )

    def report(self, scan_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM scan_reports WHERE scan_id=?", (scan_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["data"] = json.loads(result.pop("payload"))
        return result

    def save_report(self, scan_id, data, state="ready", error=""):
        with self.connect() as db:
            db.execute(
                "INSERT INTO scan_reports(scan_id,payload,state,generated,error) VALUES(?,?,?,?,?) "
                "ON CONFLICT(scan_id) DO UPDATE SET payload=excluded.payload,state=excluded.state,"
                "generated=excluded.generated,error=excluded.error",
                (scan_id, json.dumps(data), state, now(), error[:500]),
            )

    def scans(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,name,root,created,updated,CASE WHEN delete_requested THEN 'deleting' ELSE status END status,phase,message,queue_order,"
                    "COALESCE(json_extract(settings,'$.investigation_mode'),'overview') mode "
                    "FROM scans ORDER BY created DESC"
                )
            ]

    def structure(self, scan_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM scan_structure WHERE scan_id=?", (scan_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def stats(self, scan_id):
        with self.connect() as db:
            row = dict(
                db.execute(
                    "SELECT COUNT(*) files,COALESCE(SUM(size),0) bytes,"
                    "COALESCE(SUM(technical),0) technical_files,"
                    "COALESCE(SUM(CASE WHEN technical THEN size ELSE 0 END),0) technical_bytes "
                    "FROM files WHERE scan_id=?",
                    (scan_id,),
                ).fetchone()
            )
            row.update(
                dict(
                    db.execute(
                        "SELECT COUNT(*) directories,COALESCE(SUM(done),0) inventoried_dirs "
                        "FROM directories WHERE scan_id=?",
                        (scan_id,),
                    ).fetchone()
                )
            )
            row.update(
                dict(
                    db.execute(
                        "SELECT COUNT(*) findings,COALESCE(SUM(analyzed),0) analyzed_folders "
                        "FROM findings WHERE scan_id=?",
                        (scan_id,),
                    ).fetchone()
                )
            )
            row["samples"] = sum(
                len(json.loads(r[0]))
                for r in db.execute("SELECT samples FROM findings WHERE scan_id=?", (scan_id,))
            )
            row["content_samples"] = sum(
                sum(s.get("status") == "excerpt" for s in json.loads(r[0]))
                for r in db.execute("SELECT samples FROM findings WHERE scan_id=?", (scan_id,))
            )
            if self.get_scan(scan_id)["settings"].get("investigation_mode") == "progressive":
                counts = db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(json_extract(payload,'$.status')='excerpt'),0) FROM investigation_evidence WHERE scan_id=?",
                    (scan_id,),
                ).fetchone()
                row["samples"], row["content_samples"] = counts
            row["events"] = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT kind,COUNT(*) FROM events WHERE scan_id=? GROUP BY kind", (scan_id,)
                )
            }
            row["types"] = [
                dict(r)
                for r in db.execute(
                    "SELECT category,COUNT(*) count,COALESCE(SUM(size),0) bytes FROM files "
                    "WHERE scan_id=? GROUP BY category ORDER BY bytes DESC",
                    (scan_id,),
                )
            ]
            row["detail"] = dict(
                db.execute(
                    "SELECT COUNT(*) attempted,COALESCE(SUM(x.sha256!=''),0) hashed,"
                    "COALESCE(SUM(x.state IN ('reviewed','metadata','error')),0) processed,"
                    "COALESCE(SUM(x.state='reviewed'),0) ai_reviewed,"
                    "COALESCE(SUM(x.content_status IN ('excerpt','image')),0) content_read,"
                    "COALESCE(SUM(x.content_status='grouped'),0) grouped,"
                    "COALESCE(SUM(x.state='error' OR x.error!=''),0) errors FROM file_details x JOIN files f ON f.id=x.file_id "
                    "WHERE f.scan_id=?",
                    (scan_id,),
                ).fetchone()
            )
            row["detail"]["duplicate_groups"] = db.execute(
                "SELECT COUNT(*) FROM (SELECT x.sha256 FROM file_details x JOIN files f ON f.id=x.file_id "
                "WHERE f.scan_id=? AND x.sha256!='' GROUP BY x.sha256 HAVING COUNT(*)>1)",
                (scan_id,),
            ).fetchone()[0]
        return row

    def queue(self):
        with self.connect() as db:
            paused = bool(db.execute("SELECT paused FROM scheduler WHERE id=1").fetchone()[0])
            jobs = [
                dict(r)
                for r in db.execute(
                    "SELECT id,name,root,CASE WHEN delete_requested THEN 'deleting' ELSE status END status,phase,queue_order,queued_finding,message,"
                    "COALESCE(json_extract(settings,'$.investigation_mode'),'overview') mode "
                    "FROM scans WHERE delete_requested=1 OR status IN ('queued','waiting','running','pausing','stopping') "
                    "ORDER BY CASE WHEN delete_requested=1 OR status IN ('running','pausing','stopping') THEN 0 ELSE 1 END,queue_order,created"
                )
            ]
        return {"paused": paused, "jobs": jobs}

    def rollup(self, scan_id):
        with self.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT d.id,d.rel,d.parent_rel,d.done,d.listing_error,COUNT(f.id) files,COALESCE(SUM(f.size),0) bytes "
                    "FROM directories d LEFT JOIN files f ON f.dir_id=d.id WHERE d.scan_id=? GROUP BY d.id",
                    (scan_id,),
                )
            ]
            for row in rows:
                row["complete"] = bool(row["done"] and not row["listing_error"])
            mapping = {r["rel"]: r for r in rows}
            for row in sorted(rows, key=lambda r: r["rel"].count("/") + (r["rel"] != "."), reverse=True):
                parent = mapping.get(row["parent_rel"])
                if parent:
                    parent["files"] += row["files"]
                    parent["bytes"] += row["bytes"]
                    parent["complete"] = parent["complete"] and row["complete"]
            db.executemany(
                "UPDATE directories SET tree_files=?,tree_bytes=?,tree_complete=? WHERE id=?",
                [(r["files"], r["bytes"], int(r["complete"]), r["id"]) for r in rows],
            )

    def tree(self, scan_id, parent=".", offset=0, limit=150):
        with self.connect() as db:
            base = db.execute(
                "SELECT id FROM directories WHERE scan_id=? AND rel=?", (scan_id, parent)
            ).fetchone()
            if not base:
                raise ValueError("Folder not found in this investigation.")
            total = db.execute(
                "SELECT COUNT(*) FROM directories WHERE scan_id=? AND parent_rel=?", (scan_id, parent)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT d.*,a.id finding_id,a.analyzed,COUNT(f.id) own_files,COALESCE(SUM(f.size),0) own_bytes,"
                "COALESCE(SUM(f.technical),0) technical_files,"
                "COALESCE(SUM(x.state IN ('reviewed','metadata','error')),0) processed_files,"
                "COALESCE(SUM(x.content_status='grouped'),0) grouped_files,"
                "COALESCE(SUM(x.state='error' OR x.error!=''),0) + "
                "(SELECT COUNT(*) FROM events e WHERE e.scan_id=d.scan_id AND e.rel=d.rel AND e.kind='unreadable') errors,"
                "(SELECT COUNT(*) FROM directories c WHERE c.scan_id=d.scan_id AND c.parent_rel=d.rel) children "
                "FROM directories d LEFT JOIN files f ON f.dir_id=d.id LEFT JOIN file_details x ON x.file_id=f.id "
                "LEFT JOIN findings a ON a.dir_id=d.id WHERE d.scan_id=? AND (d.id=? OR d.id IN "
                "(SELECT id FROM directories WHERE scan_id=? AND parent_rel=? "
                "ORDER BY rel COLLATE NOCASE LIMIT ? OFFSET ?)) "
                "GROUP BY d.id ORDER BY (d.rel=?) DESC,d.rel COLLATE NOCASE",
                (scan_id, base[0], scan_id, parent, limit, offset, parent),
            ).fetchall()
            files = [
                dict(r)
                for r in db.execute(
                    "SELECT f.id,f.name,f.rel,f.size,x.state,x.content_status FROM files f "
                    "LEFT JOIN file_details x ON x.file_id=f.id WHERE f.dir_id=? ORDER BY f.size DESC,f.name LIMIT 80",
                    (base[0],),
                )
            ]
        return {"parent": parent, "total": total, "items": [dict(r) for r in rows], "files": files}

    def detail_file(self, scan_id, file_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT f.*,x.state,x.sha256,x.content_status,x.summary,x.reason,x.topics,x.excerpt,"
                "x.error,x.priority FROM files f LEFT JOIN file_details x ON x.file_id=f.id "
                "WHERE f.scan_id=? AND f.id=?",
                (scan_id, file_id),
            ).fetchone()
        if not row:
            raise ValueError("File not found in this investigation.")
        result = dict(row)
        result["topics"] = json.loads(result["topics"] or "[]")
        if not result.get("state"):
            with self.connect() as db:
                cached = db.execute(
                    "SELECT payload FROM investigation_evidence WHERE scan_id=? AND file_id=?",
                    (scan_id, file_id),
                ).fetchone()
            if cached:
                evidence = json.loads(cached[0])
                result.update(
                    state="sampled",
                    content_status=evidence["status"],
                    excerpt=evidence.get("text", ""),
                    summary="A bounded sample is available below.",
                    reason="This is saved extraction evidence, not a whole-file AI assessment.",
                )
        return result

    def findings(self, scan_id, offset=0, limit=100, query="", priority=None):
        where, args = "f.scan_id=?", [scan_id]
        if query:
            where += " AND (d.rel LIKE ? ESCAPE '\\' OR f.summary LIKE ? ESCAPE '\\')"
            pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.extend([pattern, pattern])
        if priority:
            where += " AND f.priority=?"
            args.append(int(priority))
        with self.connect() as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM findings f JOIN directories d ON d.id=f.dir_id WHERE {where}", args
            ).fetchone()[0]
            rows = db.execute(
                "SELECT f.*,d.rel,(SELECT COUNT(*) FROM files x WHERE x.dir_id=d.id) file_count,"
                "(SELECT COALESCE(SUM(size),0) FROM files x WHERE x.dir_id=d.id) bytes "
                f"FROM findings f JOIN directories d ON d.id=f.dir_id WHERE {where} "
                "ORDER BY f.priority,f.analyzed DESC,d.rel COLLATE NOCASE LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["samples"] = json.loads(item["samples"])
            item["references"] = json.loads(item.pop("references_json"))
            items.append(item)
        return {"total": total, "items": items}

    def events(self, scan_id, limit=100):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM events WHERE scan_id=? ORDER BY id DESC LIMIT ?", (scan_id, limit)
                )
            ]
