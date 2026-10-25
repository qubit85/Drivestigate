"""SQLite catalog with transactional inventory checkpoints."""

import json
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

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
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("This catalog was created by a newer version of Drivestigate.")
            db.executescript(SCHEMA)
            db.execute(
                "UPDATE scans SET status='paused',message='Resume after restarting.' "
                "WHERE status IN ('running','pausing','stopping')"
            )
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
        return result

    def update(self, scan_id, **values):
        allowed = {"status", "phase", "message", "llm_calls", "read_bytes", "elapsed", "device"}
        if set(values) - allowed:
            raise ValueError("Unknown scan field")
        values["updated"] = now()
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

    def scans(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,name,root,created,updated,status,phase,message FROM scans ORDER BY created DESC"
                )
            ]

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
        return row

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
