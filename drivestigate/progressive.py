"""Concurrent discovery and AI planning, with durable bounded read-only work."""

import json
import re
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from .discovery import DirectoryCursor
from .engine import Interrupted, classify
from .planner import directory_context, validate_plan
from .store import now


def redact(text):
    """Suppress common credential forms before caching excerpts or model prose."""
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
        "[private key redacted]",
        text,
        flags=re.S,
    )
    text = re.sub(
        r"(?im)^.*\b(?:password|passwd|passwort|secret|api[_ -]?key|access[_ -]?token|seed phrase|recovery phrase|mnemonic)\b\s*[:=].*$",
        "[credential value redacted]",
        text,
    )
    return re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b",
        "[token redacted]",
        text,
    )


class Investigation:
    def __init__(self, engine, scan, finding_id=None):
        self.engine, self.store, self.scan = engine, engine.store, scan
        self.sid, self.settings = scan["id"], scan["settings"]
        self.started = time.monotonic()
        self.deadline = self.started + self.settings["time_budget_minutes"] * 60
        self.call_limit = scan["llm_calls"] + self.settings["max_llm_calls"]
        self.read_limit = scan["read_bytes"] + self.settings["max_read_mb"] * 1024**2
        self.stop = threading.Event()
        self.changed = threading.Event()
        self.reader_done = threading.Event()
        self.ai_done = threading.Event()
        self.lock = threading.RLock()
        self.open_dirs = {}
        self.error = ""
        self.ai_error = ""
        self.reading = "Starting discovery"
        self.thinking = "Waiting for the first evidence"
        self.turn = 0
        self.last_publish = 0
        self.last_ai_time = 0
        self.ai_seconds = []
        self.feedback = ""
        self.finding_id = finding_id
        with self.store.connect() as db:
            row = db.execute(
                "SELECT payload FROM investigation_memory WHERE scan_id=?", (self.sid,)
            ).fetchone()
        self.memory = (
            json.loads(row[0]) if row else {"overview": "", "questions": [], "references": [], "changes": []}
        )

    def enqueue(self, kind, target, priority=50, reopen=False, db=None):
        with nullcontext(db) if db is not None else self.store.connect() as db:
            db.execute(
                "INSERT INTO investigation_tasks(scan_id,kind,target,priority) VALUES(?,?,?,?) "
                "ON CONFLICT(scan_id,kind,target) DO UPDATE SET priority=MAX(priority,excluded.priority)",
                (self.sid, kind, target, priority),
            )
            if reopen:
                db.execute(
                    "UPDATE investigation_tasks SET state='pending' WHERE scan_id=? AND kind=? AND target=? AND state IN ('deferred','error')",
                    (self.sid, kind, target),
                )
        self.changed.set()

    def alive(self):
        return (
            not self.stop.is_set() and not self.engine.control.is_set() and time.monotonic() < self.deadline
        )

    def initialize(self):
        self.store.update(
            self.sid,
            phase="progressive",
            message="Discovering storage while local AI reviews incoming evidence.",
        )
        with self.store.connect() as db:
            db.execute(
                "UPDATE investigation_tasks SET state='pending',attempts=0 WHERE scan_id=? AND (state='running' OR (kind='sample' AND state='deferred'))",
                (self.sid,),
            )
            db.execute(
                "UPDATE investigation_tasks SET state='pending',attempts=0 WHERE scan_id=? AND state='error'",
                (self.sid,),
            )
            # Retry model errors on resume, while preserving completed evidence and decisions.
            db.execute(
                "UPDATE directories SET assessed_revision=-1 WHERE scan_id=? AND decision='ai_error'",
                (self.sid,),
            )
            dirs = db.execute("SELECT id,rel,done FROM directories WHERE scan_id=?", (self.sid,)).fetchall()
            if self.finding_id:
                target = db.execute(
                    "SELECT dir_id FROM findings WHERE id=? AND scan_id=?", (self.finding_id, self.sid)
                ).fetchone()
                if target:
                    db.execute(
                        "UPDATE directories SET assessed_revision=-1,planned_revision=-1,decision='deepen' WHERE id=?",
                        (target[0],),
                    )
                    for row in db.execute(
                        "SELECT id FROM directories WHERE scan_id=? AND (id=? OR parent_rel=(SELECT rel FROM directories WHERE id=?))",
                        (self.sid, target[0], target[0]),
                    ):
                        self.enqueue("list", row[0], 100, reopen=True, db=db)
        for directory in dirs:
            if not directory["done"]:
                self.enqueue("list", directory["id"], 60 if directory["rel"] == "." else 50)
        self.publish(force=True)

    def choose_task(self):
        self.turn += 1
        with self.store.connect() as db:
            # The oldest discovered branch gets regular turns, including AI-deferred branches.
            explore = self.turn % 10 in (0, 1)
            verification = self.turn % 10 == 2
            states = "('pending','deferred')" if explore or verification else "('pending')"
            rows = db.execute(
                f"SELECT * FROM investigation_tasks WHERE scan_id=? AND state IN {states} "
                "AND attempts<4 ORDER BY "
                + ("(state='deferred') DESC,id" if verification else "id" if explore else "priority DESC,id")
                + " LIMIT 64",
                (self.sid,),
            ).fetchall()
            for row in rows:
                if row["kind"] == "list" and len(self.open_dirs) >= 6 and row["target"] not in self.open_dirs:
                    continue
                db.execute("UPDATE investigation_tasks SET state='running' WHERE id=?", (row["id"],))
                return dict(row)
        return None

    def close_directory(self, did):
        item = self.open_dirs.pop(did, None)
        if item:
            item.close()

    def list_chunk(self, task):
        with self.store.connect() as db:
            directory = dict(
                db.execute(
                    "SELECT * FROM directories WHERE id=? AND scan_id=?", (task["target"], self.sid)
                ).fetchone()
            )
        did, rel = directory["id"], directory["rel"]
        if directory["done"]:
            return "done"
        self.reading = "Listing " + rel
        if did not in self.open_dirs:
            self.open_dirs[did] = DirectoryCursor(
                self.scan["root"], rel, self.scan["device"], self.settings["exclude_patterns"]
            )
        batch = self.open_dirs[did].next(
            self.alive, timeout=min(8, max(0.1, self.deadline - time.monotonic()))
        )
        complete, events = batch["complete"], batch["events"]
        new_files, new_dirs = [], []
        for entry in batch["entries"]:
            if entry["directory"]:
                new_dirs.append(entry["rel"])
            else:
                category, technical, score = classify(
                    entry["rel"], entry["name"], self.settings["search_focus"]
                )
                new_files.append(
                    (
                        self.sid,
                        did,
                        entry["rel"],
                        entry["name"],
                        Path(entry["name"]).suffix.lower(),
                        entry["size"],
                        entry["mtime"],
                        category,
                        technical,
                        score,
                    )
                )
        with self.store.connect() as db:
            before = db.total_changes
            db.executemany(
                "INSERT OR IGNORE INTO files(scan_id,dir_id,rel,name,ext,size,mtime,category,technical,score) VALUES(?,?,?,?,?,?,?,?,?,?)",
                new_files,
            )
            db.executemany(
                "INSERT OR IGNORE INTO directories(scan_id,rel,parent_rel) VALUES(?,?,?)",
                [(self.sid, c, rel) for c in new_dirs],
            )
            added = db.total_changes - before
            if added or complete:
                db.execute(
                    "UPDATE directories SET revision=revision+1,done=? WHERE id=?", (int(complete), did)
                )
            if events:
                db.execute("UPDATE directories SET listing_error=1 WHERE id=?", (did,))
            for child in new_dirs:
                child_id = db.execute(
                    "SELECT id FROM directories WHERE scan_id=? AND rel=?", (self.sid, child)
                ).fetchone()[0]
                self.enqueue("list", child_id, 50, db=db)
        for kind, path, message in events:
            self.store.event(self.sid, kind, path, message)
        if complete:
            self.close_directory(did)
        return "done" if complete else "pending"

    def sample_file(self, task):
        with self.store.connect() as db:
            if db.execute(
                "SELECT 1 FROM investigation_evidence WHERE file_id=?", (task["target"],)
            ).fetchone():
                return "done"
            row = db.execute(
                "SELECT * FROM files WHERE id=? AND scan_id=?", (task["target"], self.sid)
            ).fetchone()
        if not row:
            return "error"
        file = dict(row)
        self.reading = "Sampling " + file["rel"]
        remaining = self.read_limit - self.store.get_scan(self.sid)["read_bytes"]
        if remaining <= 0:
            return "deferred"
        sample = self.engine.sample(self.scan, file, remaining)
        if sample.get("text") == "Read budget exhausted.":
            return "deferred"
        if sample.get("status") == "error":
            self.store.event(self.sid, "read_issue", file["rel"], sample.get("text", "Unreadable sample"))
        consumed = sample.pop("bytes_read", 0)
        sample["text"] = redact(sample.get("text", ""))[:6000]
        # Images are retained only in the local evidence cache for a bounded vision request.
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO investigation_evidence VALUES(?,?,?)",
                (file["id"], self.sid, json.dumps(sample)),
            )
            db.execute("UPDATE scans SET read_bytes=read_bytes+? WHERE id=?", (consumed, self.sid))
            db.execute("UPDATE directories SET revision=revision+1 WHERE id=?", (file["dir_id"],))
        return "done"

    def reader(self):
        try:
            while self.alive():
                if self.engine.control.is_set():
                    break
                # Bounded lead over inference: do not race through the whole drive while AI is warming up.
                with self.store.connect() as db:
                    backlog = db.execute(
                        "SELECT COUNT(*) FROM directories WHERE scan_id=? AND revision>planned_revision AND revision>0",
                        (self.sid,),
                    ).fetchone()[0]
                if backlog > 32 and not self.ai_done.is_set():
                    self.changed.wait(0.15)
                    self.changed.clear()
                    continue
                task = self.choose_task()
                if not task:
                    if self.ai_done.is_set():
                        break
                    self.reading = "Waiting for AI-directed work"
                    self.changed.wait(0.15)
                    self.changed.clear()
                    continue
                try:
                    outcome = self.list_chunk(task) if task["kind"] == "list" else self.sample_file(task)
                except (OSError, ValueError) as exc:
                    if not self.alive():
                        break
                    outcome = "error"
                    self.store.event(self.sid, "read_issue", ".", str(exc))
                    if task["kind"] == "list":
                        self.close_directory(task["target"])
                        with self.store.connect() as db:
                            db.execute(
                                "UPDATE directories SET listing_error=1,revision=revision+1 WHERE id=?",
                                (task["target"],),
                            )
                with self.store.connect() as db:
                    # A concurrent model deferral wins over the tail of a directory chunk.
                    db.execute(
                        "UPDATE investigation_tasks SET state=CASE WHEN state='deferred' AND ?='pending' THEN state ELSE ? END,attempts=attempts+?,priority=CASE WHEN kind='list' AND ?='pending' THEN MAX(5,priority-5) ELSE priority END WHERE id=?",
                        (outcome, outcome, int(outcome in ("error", "deferred")), outcome, task["id"]),
                    )
                self.changed.set()
        except Interrupted:
            pass
        except Exception as exc:
            self.error = str(exc)
            self.stop.set()
        finally:
            for did in list(self.open_dirs):
                self.close_directory(did)
            self.reading = "Reader idle"
            self.reader_done.set()
            self.changed.set()

    def evidence(self):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT d.*,a.summary previous_summary FROM directories d LEFT JOIN findings a ON a.dir_id=d.id "
                "WHERE d.scan_id=? AND d.revision>d.planned_revision AND d.revision>0 "
                "ORDER BY EXISTS(SELECT 1 FROM investigation_evidence e JOIN files f ON f.id=e.file_id WHERE f.dir_id=d.id) DESC,(d.planned_revision<0) DESC,d.id LIMIT 4",
                (self.sid,),
            ).fetchall()
            directories = []
            for row in rows:
                row = dict(row)
                files = [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,name,category,size,score FROM files WHERE dir_id=? ORDER BY score DESC,id LIMIT 8",
                        (row["id"],),
                    )
                ]
                seen = {f["id"] for f in files}
                # A reproducible exploration sample complements high-scoring names.
                for r in db.execute(
                    "SELECT id,name,category,size,score FROM files WHERE dir_id=? ORDER BY ((id*1103515245+12345)%2147483647) LIMIT 3",
                    (row["id"],),
                ):
                    if r["id"] not in seen:
                        files.append(dict(r))
                samples = []
                for r in db.execute(
                    "SELECT e.payload FROM investigation_evidence e JOIN files f ON f.id=e.file_id WHERE f.dir_id=? ORDER BY e.file_id DESC LIMIT 6",
                    (row["id"],),
                ):
                    item = json.loads(r[0])
                    samples.append(
                        {k: v for k, v in item.items() if k in ("id", "rel", "status", "text", "image")}
                    )
                    samples[-1]["text"] = samples[-1].get("text", "")[:1000]
                directories.append(
                    {
                        "id": row["id"],
                        "path": row["rel"],
                        "revision": row["revision"],
                        "listing_complete": bool(row["done"]),
                        "listing_issues": bool(row["listing_error"]),
                        "previous_summary": (row["previous_summary"] or "")[:500],
                        "files": files,
                        "children": [
                            dict(c)
                            for c in db.execute(
                                "SELECT id,rel path,decision FROM directories WHERE scan_id=? AND parent_rel=? LIMIT 12",
                                (self.sid, row["rel"]),
                            )
                        ],
                        "samples": samples,
                    }
                )
            previous = [
                dict(r)
                for r in db.execute(
                    "SELECT d.id,d.rel path,d.done listing_complete,a.summary,d.uncertainty FROM directories d JOIN findings a ON a.dir_id=d.id WHERE d.scan_id=? AND a.analyzed=1 ORDER BY a.priority,d.id LIMIT 10",
                    (self.sid,),
                )
            ]
        with self.lock:
            memory = {k: self.memory.get(k) for k in ("overview", "questions", "search_results")}
        return {
            "scope": "Partial discovered catalog. No exhaustive inventory or content claim is possible.",
            "decision_directory_ids": sorted(
                directory_context({"directories": directories, "previous_findings": previous})
            ),
            "validation_feedback": self.feedback,
            "directories": directories,
            "previous_findings": previous,
            "working_memory": memory,
        }

    def sample_candidates(self, did, requested):
        with self.store.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT f.* FROM files f LEFT JOIN investigation_evidence e ON e.file_id=f.id WHERE f.dir_id=? AND e.file_id IS NULL ORDER BY f.score DESC,f.id LIMIT 100",
                    (did,),
                )
            ]
        if not rows:
            with self.store.connect() as db:
                parent = db.execute(
                    "SELECT rel FROM directories WHERE id=? AND scan_id=?", (did, self.sid)
                ).fetchone()
                if parent:
                    rows = [
                        dict(r)
                        for r in db.execute(
                            "SELECT f.* FROM files f LEFT JOIN investigation_evidence e ON e.file_id=f.id WHERE f.scan_id=? AND e.file_id IS NULL AND (?='.' OR instr(f.rel,?)=1) ORDER BY f.score DESC,f.id LIMIT 100",
                            (self.sid, parent[0], parent[0] + "/"),
                        )
                    ]
        selected = []
        if requested:
            with self.store.connect() as db:
                selected = [
                    dict(r)
                    for r in db.execute(
                        "SELECT f.* FROM files f LEFT JOIN investigation_evidence e ON e.file_id=f.id WHERE f.dir_id=? AND e.file_id IS NULL AND f.id IN ("
                        + ",".join("?" for _ in requested)
                        + ")",
                        (did, *requested),
                    )
                ]
        # Cover multiple types and an off-priority example, not simply the first N files.
        for r in rows:
            if len(selected) >= self.settings["samples_per_folder"]:
                break
            if r not in selected and (not selected or r["category"] not in {f["category"] for f in selected}):
                selected.append(r)
        if rows and rows[-1] not in selected and len(selected) < self.settings["samples_per_folder"]:
            selected.append(rows[-1])
        return selected[: self.settings["samples_per_folder"]]

    def apply(self, result, evidence):
        validate_plan(result, evidence)
        supplied = directory_context(evidence)
        with self.store.connect() as db:
            for directory in evidence["directories"]:
                db.execute(
                    "UPDATE directories SET planned_revision=MAX(planned_revision,?) WHERE id=?",
                    (directory["revision"], directory["id"]),
                )
        changes = []
        for decision in result["decisions"]:
            did = decision["directory_id"]
            before = supplied[did]
            action = decision["action"]
            summary, reason, uncertainty = (redact(decision[k]) for k in ("summary", "reason", "uncertainty"))
            with self.store.connect() as db:
                old = db.execute("SELECT summary FROM findings WHERE dir_id=?", (did,)).fetchone()
                samples = [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT e.payload FROM investigation_evidence e JOIN files f ON f.id=e.file_id WHERE f.dir_id=? ORDER BY file_id LIMIT 30",
                        (did,),
                    )
                ]
                for sample in samples:
                    sample.pop("image", None)
                refs = [s["id"] for s in before["samples"]]
                db.execute(
                    "INSERT INTO findings(scan_id,dir_id,priority,summary,reason,method,samples,references_json,next_action,analyzed) VALUES(?,?,?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(dir_id) DO UPDATE SET priority=excluded.priority,summary=excluded.summary,reason=excluded.reason,method=excluded.method,samples=excluded.samples,references_json=excluded.references_json,next_action=excluded.next_action,analyzed=1",
                    (
                        self.sid,
                        did,
                        decision["priority"],
                        summary,
                        reason,
                        "sampled" if refs else "metadata_ai",
                        json.dumps(samples),
                        json.dumps(refs),
                        action + ": " + uncertainty,
                    ),
                )
                db.execute(
                    "UPDATE directories SET assessed_revision=MAX(assessed_revision,?),decision=?,uncertainty=? WHERE id=?",
                    (before["revision"], action, uncertainty, did),
                )
                children = [
                    r[0]
                    for r in db.execute(
                        "SELECT id FROM directories WHERE scan_id=? AND parent_rel=?",
                        (self.sid, before["path"]),
                    )
                ]
                if action in ("defer", "summarize"):
                    for target in [did, *children]:
                        db.execute(
                            "UPDATE investigation_tasks SET state='deferred',priority=10 WHERE scan_id=? AND kind='list' AND target=? AND state IN ('pending','running','deferred') AND attempts<4",
                            (self.sid, target),
                        )
                if not old or old[0] != summary:
                    changes.append(
                        {"directory_id": did, "path": before["path"], "summary": summary, "at": now()}
                    )
            if action in ("explore", "deepen"):
                for target in [did, *children]:
                    self.enqueue("list", target, 90 if action == "deepen" else 65, reopen=True)
            if action in ("sample", "deepen"):
                for file in self.sample_candidates(did, decision["file_ids"]):
                    self.enqueue("sample", file["id"], 100)
        searches = []
        with self.store.connect() as db:
            for query in result["searches"]:
                if not query.strip():
                    continue
                matches = [
                    dict(r)
                    for r in db.execute(
                        "SELECT f.id,f.dir_id,f.rel FROM files f LEFT JOIN investigation_evidence e ON e.file_id=f.id WHERE f.scan_id=? AND (instr(lower(f.rel),lower(?))>0 OR instr(lower(e.payload),lower(?))>0) LIMIT 20",
                        (self.sid, query, query),
                    )
                ]
                searches.append({"query": query, "matches": matches, "scope": "Discovered catalog only"})
                for match in matches:
                    self.enqueue("sample", match["id"], 95, db=db)
        with self.lock:
            self.memory.update(
                overview=redact(result["overview"]),
                references=result["overview_directory_ids"],
                questions=[redact(q) for q in result["questions"]],
                search_results=searches,
                changes=(self.memory.get("changes", []) + changes)[-30:],
                updated=now(),
            )
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO investigation_memory VALUES(?,?) ON CONFLICT(scan_id) DO UPDATE SET payload=excluded.payload",
                    (self.sid, json.dumps(self.memory)),
                )
        self.changed.set()

    def analyst(self):
        failures = 0
        try:
            client = self.engine.client_factory(self.settings)
            self.thinking = "Checking the local model"
            capabilities = client.test().get("capabilities", [])
            if self.settings["analyze_images"] and "vision" not in capabilities:
                self.ai_error = "Selected model has no vision capability; image content is not interpreted."
            while self.alive() and self.store.get_scan(self.sid)["llm_calls"] < self.call_limit:
                evidence = self.evidence()
                if not evidence["directories"]:
                    with self.store.connect() as db:
                        active = db.execute(
                            "SELECT COUNT(*) FROM investigation_tasks WHERE scan_id=? AND state IN ('pending','running','deferred') AND attempts<4",
                            (self.sid,),
                        ).fetchone()[0]
                    if not active:
                        break
                    self.changed.wait(0.15)
                    self.changed.clear()
                    continue
                if "vision" not in capabilities:
                    for directory in evidence["directories"]:
                        for sample in directory["samples"]:
                            sample.pop("image", None)
                self.thinking = "Analyzing " + ", ".join(d["path"] for d in evidence["directories"])
                self.last_ai_time = time.monotonic()
                with self.store.connect() as db:
                    db.execute("UPDATE scans SET llm_calls=llm_calls+1 WHERE id=?", (self.sid,))
                try:
                    result = client.plan(
                        evidence,
                        timeout=max(
                            0.1,
                            min(
                                60, self.settings["request_timeout_seconds"], self.deadline - time.monotonic()
                            ),
                        ),
                    )
                    self.apply(result, evidence)
                    failures = 0
                    self.feedback = ""
                    self.ai_error = ""
                except (ValueError, OSError) as exc:
                    if time.monotonic() >= self.deadline or self.engine.control.is_set():
                        break
                    failures += 1
                    self.ai_error = str(exc)
                    self.feedback = (
                        str(exc)
                        + " Use only decision_directory_ids as decision targets. Never invent IDs or sample file IDs."
                    )
                    self.store.event(self.sid, "ai_issue", ".", str(exc))
                    if failures >= 2:
                        break
                    self.stop.wait(0.3)
                self.ai_seconds.append(time.monotonic() - self.last_ai_time)
                self.last_ai_time = 0
        except Exception as exc:
            self.ai_error = str(exc)
            self.store.event(self.sid, "ai_issue", ".", str(exc))
        finally:
            self.thinking = (
                "AI unavailable · saved evidence retained" if self.ai_error else "AI round finished"
            )
            self.ai_done.set()
            self.changed.set()

    def publish(self, force=False):
        if not force and time.monotonic() - self.last_publish < 2:
            return
        self.last_publish = time.monotonic()
        self.store.rollup(self.sid)
        with self.store.connect() as db:
            tasks = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT state,COUNT(*) FROM investigation_tasks WHERE scan_id=? GROUP BY state",
                    (self.sid,),
                )
            }
            listed, found, reviewed = db.execute(
                "SELECT COALESCE(SUM(done),0),COUNT(*),COALESCE(SUM(assessed_revision>=0),0) FROM directories WHERE scan_id=?",
                (self.sid,),
            ).fetchone()
        with self.lock:
            memory = json.loads(json.dumps(self.memory))
        progress = {
            "stage": "progressive",
            "current": self.reading,
            "reader": self.reading,
            "analyst": self.thinking,
            "completed": listed,
            "discovered": found,
            "reviewed": reviewed,
            "total": 0,
            "percent": None,
            "tasks": tasks,
            "remaining_seconds": max(0, self.deadline - time.monotonic()),
            "elapsed_seconds": self.scan["elapsed"] + time.monotonic() - self.started,
            "ai_request_seconds": time.monotonic() - self.last_ai_time if self.last_ai_time else 0,
            "ai_error": self.ai_error,
            "eta_basis": "Time budget for this round; total coverage is unknown.",
        }
        self.store.update(self.sid, progress=progress, message=self.reading + " · " + self.thinking)
        from .summary import catalog_report

        report = catalog_report(self.store, self.sid)
        report["investigation"] = {**memory, "tasks": tasks, "ai_error": self.ai_error}
        if memory.get("overview"):
            report.update(
                summary=memory["overview"],
                basis="Evolving interpretation of referenced evidence. Coverage is partial.",
                synthesis="local_ai",
            )
        self.store.save_report(self.sid, report, state="live" if self.alive() else "ready")
        self.last_publish = time.monotonic()

    def run(self):
        self.initialize()
        reader = threading.Thread(target=self.reader, name="drive-reader", daemon=True)
        analyst = threading.Thread(target=self.analyst, name="drive-analyst", daemon=True)
        reader.start()
        analyst.start()
        try:
            while self.alive():
                self.publish()
                if self.ai_done.is_set():
                    # A finite AI round bounds discovery too. Save queued work for the next round.
                    break
                self.stop.wait(0.15)
        finally:
            self.stop.set()
            self.changed.set()
            while reader.is_alive() or analyst.is_alive():
                reader.join(0.1)
                analyst.join(0.1)
                self.publish()
            with self.store.connect() as db:
                db.execute(
                    "UPDATE investigation_tasks SET state='pending' WHERE scan_id=? AND state='running'",
                    (self.sid,),
                )
            self.publish(force=True)
        if self.engine.control.is_set():
            raise Interrupted()
        if self.error:
            raise OSError(self.error)
        self.store.update(
            self.sid,
            status="completed",
            message="Investigation round saved. Continue deepening to explore remaining or deferred areas."
            + (" AI needs attention: " + self.ai_error if self.ai_error else ""),
        )
        self.publish(force=True)


def investigate(engine, scan, finding_id=None):
    Investigation(engine, scan, finding_id).run()
