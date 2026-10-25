"""Resumable inventory, stratified sampling and budgeted adaptive investigation."""

import fnmatch
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

from .config import validate_settings
from .extract import IMAGES, TEXT
from .llm import Ollama
from .source import source_directory
from .store import now

DOCUMENTS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".rtf",
    ".pages",
    ".numbers",
    ".key",
    ".epub",
}
CODE = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".rs",
    ".go",
    ".ipynb",
    ".swift",
    ".kt",
    ".sql",
    ".html",
    ".css",
    ".sh",
}
ARCHIVES = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".dmg", ".iso", ".vhd", ".vhdx"}
TECH_PARTS = {
    "node_modules",
    ".git",
    "__pycache__",
    "site-packages",
    ".cache",
    "caches",
    "winsxs",
    "system32",
    "syswow64",
    "$windows.~bt",
    "windows.old",
    "program files",
    "program files (x86)",
}
TECH_EXTS = {".dll", ".exe", ".sys", ".msi", ".cab", ".dylib", ".so", ".pyc", ".class", ".o"}
USER_PARTS = {
    "users",
    "documents",
    "desktop",
    "downloads",
    "pictures",
    "photos",
    "music",
    "videos",
    "saved games",
    "userdata",
    "user data",
    "savegames",
    "saves",
    "profiles",
}
HARD_TECH_PARTS = {
    "node_modules",
    ".git",
    "__pycache__",
    "site-packages",
    ".cache",
    "caches",
    "system32",
    "syswow64",
    "winsxs",
}
SIGNALS = re.compile(
    r"wallet|backup|sicherung|vertrag|contract|rechnung|invoice|tax|steuer|password|"
    r"passwort|recovery|seed|abschluss|thesis|project|projekt|family|familie",
    re.I,
)


def classify(relative, name, focus=""):
    ext = Path(name).suffix.lower()
    parts = {p.lower() for p in PurePosixPath(relative).parts[:-1]}
    category = (
        "Images"
        if ext in IMAGES
        else "Documents"
        if ext in DOCUMENTS
        else "Source code"
        if ext in CODE
        else "Text & data"
        if ext in TEXT
        else "Archives & disk images"
        if ext in ARCHIVES
        else "Audio & Video"
        if ext in {".mp3", ".wav", ".flac", ".mp4", ".mov", ".avi", ".mkv"}
        else "Other"
    )
    # Keep potential user material visible even inside an application/system tree.
    personal = (
        ext in DOCUMENTS | IMAGES | ARCHIVES
        or bool(SIGNALS.search(name))
        or (bool(parts & USER_PARTS) and not parts & HARD_TECH_PARTS and ext not in TECH_EXTS)
    )
    is_tech = bool(parts & TECH_PARTS) or ext in TECH_EXTS or name in {".DS_Store", "Thumbs.db"}
    if personal:
        is_tech = False
    score = (
        5
        if is_tech
        else {
            "Documents": 65,
            "Source code": 55,
            "Images": 45,
            "Archives & disk images": 50,
            "Text & data": 40,
        }.get(category, 20)
    )
    if SIGNALS.search(relative):
        score += 30
    words = re.findall(r"[\w-]{4,}", focus.lower())
    if any(word in relative.lower() for word in words):
        score += 40
    return category, int(is_tech), score


def identity(root):
    info = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Please select an existing folder or drive.")
    return info.st_dev, f"{info.st_ino}:{getattr(info, 'st_birthtime', 0)}"


def volume_uuid(root):
    if sys.platform != "darwin":
        return ""
    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", str(root)], capture_output=True, timeout=5, check=False
        )
        if result.returncode:
            filesystem = subprocess.run(
                ["/bin/df", "-P", str(root)], capture_output=True, timeout=5, check=False
            )
            device = filesystem.stdout.decode().splitlines()[-1].split()[0]
            if not device.startswith("/dev/"):
                return ""
            result = subprocess.run(
                ["/usr/sbin/diskutil", "info", "-plist", device], capture_output=True, timeout=5, check=False
            )
        data = plistlib.loads(result.stdout)
        return data.get("VolumeUUID", data.get("APFSVolumeUUID", ""))
    except (OSError, ValueError, IndexError, plistlib.InvalidFileException, subprocess.TimeoutExpired):
        return ""


class Interrupted(Exception):
    pass


class Engine:
    def __init__(self, store, client_factory=Ollama):
        self.store = store
        self.client_factory = client_factory
        self.thread = None
        self.control = threading.Event()
        self.lock = threading.RLock()
        self.active_id = None
        self.intent = "paused"
        self.closing = False
        self.scheduler_thread = None
        self.live = {}
        self.origin = "http://127.0.0.1:8741"

    def start_scheduler(self):
        if self.scheduler_thread:
            return

        def schedule():
            while not self.closing:
                self.kick()
                for _ in range(20):
                    if self.closing:
                        return
                    time.sleep(0.25)

        self.scheduler_thread = threading.Thread(target=schedule, daemon=True)
        self.scheduler_thread.start()

    def close(self):
        self.closing = True
        with self.lock:
            if self.active_id:
                self.intent = "paused"
                self.control.set()
        if self.thread:
            self.thread.join(timeout=3)

    def kick(self):
        with self.lock:
            if self.closing or self.active_id:
                return
            with self.store.connect() as db:
                if db.execute("SELECT paused FROM scheduler WHERE id=1").fetchone()[0]:
                    return
                jobs = [
                    r[0]
                    for r in db.execute(
                        "SELECT id FROM scans WHERE status IN ('queued','waiting') "
                        "ORDER BY queue_order,created"
                    )
                ]
            for scan_id in jobs:
                scan = self.store.get_scan(scan_id)
                report_only = scan["queue_task"].startswith("report:")
                if not report_only and not self.connected(scan):
                    if scan["status"] != "waiting":
                        self.store.update(
                            scan_id, status="waiting", message="Waiting for the original drive."
                        )
                    continue
                self.control.clear()
                self.active_id = scan_id
                self.live[scan_id] = {
                    "started": time.monotonic(),
                    "phase_started": time.monotonic(),
                    "durations": [],
                    "baseline": 0,
                }
                self.store.update(
                    scan_id,
                    device=scan["device"] if report_only else identity(scan["root"])[0],
                    status="running",
                    message="Starting investigation …",
                )
                self.thread = threading.Thread(
                    target=self.run, args=(scan_id, scan["queued_finding"]), daemon=True
                )
                self.thread.start()
                return

    def hold_queue(self, paused):
        with self.lock, self.store.connect() as db:
            db.execute("UPDATE scheduler SET paused=? WHERE id=1", (int(paused),))
        self.kick()

    def reorder(self, ids):
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise ValueError("Queue order must contain investigation IDs.")
        with self.lock, self.store.connect() as db:
            expected = {r[0] for r in db.execute("SELECT id FROM scans WHERE status IN ('queued','waiting')")}
            if len(ids) != len(expected) or set(ids) != expected:
                raise ValueError("The queue changed. Refresh it and try again.")
            db.executemany("UPDATE scans SET queue_order=? WHERE id=?", enumerate(ids, start=1))

    def move(self, scan_id, direction):
        with self.lock:
            ids = [j["id"] for j in self.store.queue()["jobs"] if j["status"] in ("queued", "waiting")]
            if scan_id not in ids or direction not in ("up", "down"):
                raise ValueError("Only queued investigations can be reordered.")
            index = ids.index(scan_id)
            target = index + (-1 if direction == "up" else 1)
            if 0 <= target < len(ids):
                ids[index], ids[target] = ids[target], ids[index]
                self.reorder(ids)

    def connected(self, scan):
        try:
            device, signature = identity(scan["root"])
            expected, _, expected_uuid = scan["identity"].partition("|")
            if signature != expected:
                return False
            if device != scan["device"] and expected_uuid:
                return volume_uuid(scan["root"]) == expected_uuid
            return True
        except OSError:
            return False
        except ValueError:
            return False

    def create(self, root, name, settings):
        with self.lock:
            root = Path(root).expanduser().resolve(strict=True)
            if not root.is_dir():
                raise ValueError("The source must be a folder.")
            # Writing the catalog into its own scan would create an unbounded moving target.
            db = Path(self.store.path).resolve()
            if db.is_relative_to(root) or root.is_relative_to(db.parent):
                raise ValueError("The source must not contain the Drivestigate data directory.")
            settings = validate_settings(settings)
            dev, signature = identity(root)
            signature += "|" + volume_uuid(root)
            scan_id = uuid.uuid4().hex
            with self.store.connect() as conn:
                conn.execute(
                    "INSERT INTO scans(id,name,root,device,identity,created,updated,status,phase,settings) "
                    "VALUES(?,?,?,?,?,?,?,'paused','inventory',?)",
                    (
                        scan_id,
                        name.strip()[:100] or root.name or str(root),
                        str(root),
                        dev,
                        signature,
                        now(),
                        now(),
                        json.dumps(settings),
                    ),
                )
                conn.execute("INSERT INTO directories(scan_id,rel) VALUES(?,'.')", (scan_id,))
            self.start(scan_id)
            return scan_id

    def ensure_idle(self):
        if self.active_id:
            raise ValueError("An investigation is already running. Pause it first.")

    def start(self, scan_id, finding_id=None, report_only=False):
        with self.lock:
            scan = self.store.get_scan(scan_id)
            if self.active_id == scan_id:
                raise ValueError("This investigation is already running.")
            if report_only and scan["status"] in ("queued", "waiting", "running", "pausing", "stopping"):
                raise ValueError(
                    "The comprehensive report will be generated after this investigation finishes."
                )
            if not report_only and not self.connected(scan):
                raise ValueError("The original drive is disconnected or its identity has changed.")
            if finding_id is not None:
                with self.store.connect() as db:
                    row = db.execute(
                        "SELECT id FROM findings WHERE id=? AND scan_id=?", (finding_id, scan_id)
                    ).fetchone()
                if not row:
                    raise ValueError("Finding not found.")
            with self.store.connect() as db:
                order = db.execute(
                    "SELECT COALESCE(MAX(queue_order),0)+1 FROM scans "
                    "WHERE status IN ('queued','waiting','running','pausing','stopping')"
                ).fetchone()[0]
            self.store.update(
                scan_id,
                status="queued",
                queue_order=scan["queue_order"] if scan["status"] in ("queued", "waiting") else order,
                queued_finding=finding_id,
                queue_task=f"report:{scan['status']}" if report_only else "investigate",
                message="Queued for comprehensive report." if report_only else "Queued for investigation.",
            )
            if not report_only:
                with self.store.connect() as db:
                    db.execute("UPDATE scan_reports SET state='stale' WHERE scan_id=?", (scan_id,))
            self.kick()

    def interrupt(self, scan_id, stop=False):
        with self.lock:
            scan = self.store.get_scan(scan_id)
            if scan["status"] in ("queued", "waiting"):
                self.store.update(
                    scan_id,
                    status="stopped" if stop else "paused",
                    queue_order=None,
                    message="Removed from the queue; progress preserved.",
                )
                return
            if self.active_id != scan_id or not self.thread or not self.thread.is_alive():
                raise ValueError("This investigation is not running.")
            self.intent = "stopped" if stop else "paused"
            self.control.set()
            self.store.update(
                scan_id,
                status="stopping" if stop else "pausing",
                message="Stopping after the current read operation or AI request …",
            )

    def checkpoint(self, scan):
        if self.control.is_set():
            raise Interrupted()
        if not self.connected(scan):
            raise OSError("Drive disconnected or replaced. Reconnect it to resume.")
        if identity(scan["root"])[0] != scan["device"]:
            raise OSError("Drive reconnected. Please resume the investigation.")

    def run(self, scan_id, finding_id=None):
        started = time.monotonic()
        scan = self.store.get_scan(scan_id)
        try:
            if scan["queue_task"].startswith("report:"):
                from .summary import generate_report

                generate_report(self, scan_id)
                self.store.update(
                    scan_id, status=scan["queue_task"].split(":")[1], message="Comprehensive report ready."
                )
                return
            if scan["phase"] == "inventory":
                self.inventory(scan)
                self.checkpoint(scan)
                self.store.rollup(scan_id)
                self.seed_findings(scan)
                self.store.update(scan_id, phase="analysis")
            if scan["settings"].get("investigation_mode") == "detail":
                from .detail import investigate_files

                investigate_files(self, self.store.get_scan(scan_id))
            else:
                self.investigate(self.store.get_scan(scan_id), finding_id, time.monotonic())
            if self.store.get_scan(scan_id)["status"] == "completed":
                from .summary import generate_report

                message = self.store.get_scan(scan_id)["message"]
                self.store.update(scan_id, status="running", message="Creating the comprehensive report …")
                generate_report(self, scan_id)
                self.store.update(
                    scan_id, status="completed", message=message + " Comprehensive report ready."
                )
        except Interrupted:
            self.store.update(scan_id, status=self.intent, message="Investigation paused; progress saved.")
        except Exception as exc:
            self.store.event(scan_id, "error", ".", str(exc))
            self.store.update(scan_id, status="paused", message=str(exc)[:500])
        finally:
            current = self.store.get_scan(scan_id)
            self.store.update(scan_id, elapsed=current["elapsed"] + time.monotonic() - started)
            with self.store.connect() as db:
                db.execute(
                    "UPDATE scan_reports SET state='pending' WHERE scan_id=? AND state='building'", (scan_id,)
                )
            with self.lock:
                self.active_id = None
                self.kick()

    def progress_update(self, scan_id, **values):
        scan = self.store.get_scan(scan_id)
        progress = scan["progress"]
        progress.update(values)
        self.store.update(scan_id, progress=progress)

    def unit_finished(self, scan_id, duration):
        live = self.live.get(scan_id)
        if live is not None:
            live["durations"] = [*live["durations"][-19:], max(0.001, duration)]

    def progress(self, scan):
        progress = dict(scan["progress"])
        live = self.live.get(scan["id"], {})
        duration = time.monotonic() - live.get("phase_started", time.monotonic())
        completed, total = progress.get("completed", 0), progress.get("total", 0)
        progress["percent"] = min(100, 100 * completed / total) if total else None
        progress["eta_low"] = progress["eta_high"] = None
        progress["elapsed_seconds"] = scan["elapsed"] + (
            time.monotonic() - live["started"]
            if scan["status"] in ("running", "pausing", "stopping") and "started" in live
            else 0
        )
        durations = live.get("durations", [])
        remaining = max(0, total - completed)
        if len(durations) >= 2 and remaining and scan["status"] == "running" and scan["phase"] != "inventory":
            average = sum(durations) / len(durations)
            progress.update(
                eta_low=round(average * remaining * 0.7),
                eta_high=round(average * remaining * 1.5),
                eta_basis=f"Recent {len(durations)} completed units; estimate excludes later phases.",
            )
        progress["units_per_second"] = (completed - live.get("baseline", 0)) / max(1, duration)
        if scan["phase"] == "inventory":
            progress.update(percent=None, eta_basis="Discovering folders; total size is not known yet.")
        return progress

    def inventory(self, scan):
        scan_id = scan["id"]
        patterns = scan["settings"]["exclude_patterns"]
        while True:
            self.checkpoint(scan)
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT * FROM directories WHERE scan_id=? AND done=0 ORDER BY id LIMIT 1", (scan_id,)
                ).fetchone()
            if not row:
                break
            rel, dir_id = row["rel"], row["id"]
            self.store.update(scan_id, message=f"Inventory: {rel}")
            with self.store.connect() as db:
                counts = db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(done),0) FROM directories WHERE scan_id=?", (scan_id,)
                ).fetchone()
            self.progress_update(
                scan_id,
                completed=counts[1],
                total=0,
                discovered=counts[0],
                current=rel,
                unit="folders",
                stage="inventory",
            )
            with self.store.connect() as db:
                db.execute("DELETE FROM files WHERE dir_id=?", (dir_id,))
            batch = []

            def flush(batch=batch):
                if batch:
                    with self.store.connect() as db:
                        db.executemany(
                            "INSERT OR IGNORE INTO files(scan_id,dir_id,rel,name,ext,size,mtime,"
                            "category,technical,score) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            batch,
                        )
                    batch.clear()

            try:
                with source_directory(scan["root"], rel, scan["device"]) as folder:
                    with os.scandir(folder) as entries:
                        for entry in entries:
                            self.checkpoint(scan)
                            child = (PurePosixPath(rel) / entry.name).as_posix()
                            try:
                                child.encode("utf-8")
                            except UnicodeEncodeError:
                                self.store.event(
                                    scan_id,
                                    "unreadable",
                                    child.encode("utf-8", errors="backslashreplace").decode(),
                                    "Filename is not valid UTF-8; not examined.",
                                )
                                continue
                            if any(
                                fnmatch.fnmatchcase(entry.name.casefold(), p.casefold())
                                or fnmatch.fnmatchcase(child.casefold(), p.casefold())
                                for p in patterns
                            ):
                                self.store.event(scan_id, "excluded", child, "Skipped by an exclusion rule.")
                                continue
                            try:
                                info = entry.stat(follow_symlinks=False)
                                if stat.S_ISLNK(info.st_mode):
                                    self.store.event(scan_id, "symlink", child, "Symbolic link not followed.")
                                elif info.st_dev != scan["device"]:
                                    self.store.event(
                                        scan_id, "mount", child, "Different filesystem not traversed."
                                    )
                                elif stat.S_ISDIR(info.st_mode):
                                    with self.store.connect() as db:
                                        db.execute(
                                            "INSERT OR IGNORE INTO directories(scan_id,rel,parent_rel) VALUES(?,?,?)",
                                            (scan_id, child, rel),
                                        )
                                elif stat.S_ISREG(info.st_mode):
                                    cat, tech, score = classify(
                                        child, entry.name, scan["settings"]["search_focus"]
                                    )
                                    batch.append(
                                        (
                                            scan_id,
                                            dir_id,
                                            child,
                                            entry.name,
                                            Path(entry.name).suffix.lower(),
                                            info.st_size,
                                            info.st_mtime,
                                            cat,
                                            tech,
                                            score,
                                        )
                                    )
                                else:
                                    self.store.event(scan_id, "special", child, "Special file not read.")
                            except OSError as exc:
                                self.store.event(scan_id, "unreadable", child, str(exc))
                            if len(batch) >= 250:
                                flush()
                flush()
            except Interrupted:
                flush()
                raise
            except OSError as exc:
                if not self.connected(scan):
                    raise
                flush()
                self.store.event(scan_id, "unreadable", rel, str(exc))
            with self.store.connect() as db:
                db.execute("UPDATE directories SET done=1 WHERE id=?", (dir_id,))

    def seed_findings(self, scan):
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO findings(scan_id,dir_id,priority,summary,reason,method,next_action) "
                "SELECT scan_id,dir_id,CASE WHEN MAX(score)>=85 THEN 1 ELSE 2 END,"
                "'Not yet reviewed by AI', 'Classified using filenames and metadata.',"
                "'metadata','Review samples' FROM files WHERE scan_id=? "
                "GROUP BY dir_id HAVING SUM(CASE WHEN technical=0 THEN 1 ELSE 0 END)>0 "
                "OR ?=0",
                (scan["id"], int(scan["settings"]["hide_technical"])),
            )

    def candidates(self, finding_id, count, seen):
        with self.store.connect() as db:
            info = db.execute("SELECT dir_id FROM findings WHERE id=?", (finding_id,)).fetchone()
            # SQL windows represent each extension, age range and size range without loading the directory.
            rows = db.execute(
                "SELECT * FROM (SELECT *,ROW_NUMBER() OVER (PARTITION BY ext ORDER BY score DESC,"
                "mtime DESC,id) rank FROM files WHERE dir_id=? AND technical=0) "
                "WHERE rank<=8 ORDER BY score DESC,rank,id LIMIT 400",
                (info[0],),
            ).fetchall()
            extra = db.execute(
                "SELECT * FROM files WHERE dir_id=? AND technical=0 ORDER BY mtime LIMIT 4", (info[0],)
            ).fetchall()
            large = db.execute(
                "SELECT * FROM files WHERE dir_id=? AND technical=0 ORDER BY size DESC LIMIT 4", (info[0],)
            ).fetchall()
            # A deterministic pseudo-random slice prevents relying entirely on semantic filenames.
            randoms = db.execute(
                "SELECT * FROM files WHERE dir_id=? AND technical=0 "
                "ORDER BY ((id * 1103515245 + 12345) & 2147483647) LIMIT 4",
                (info[0],),
            ).fetchall()
        pool = {r["id"]: dict(r) for r in [*rows, *extra, *large, *randoms] if r["id"] not in seen}
        if not pool:
            return []
        ordered = sorted(pool.values(), key=lambda f: (-f["score"], f["id"]))
        selected = [ordered.pop(0)]
        for group in (extra, large, randoms):
            match = next(
                (
                    pool[r["id"]]
                    for r in group
                    if r["id"] in pool and r["id"] not in {s["id"] for s in selected}
                ),
                None,
            )
            if match and len(selected) < count:
                selected.append(match)
        while len(selected) < count:
            remaining = [r for r in ordered if r["id"] not in {s["id"] for s in selected}]
            if not remaining:
                break
            used_types = {r["ext"] for r in selected}
            pick = next((r for r in remaining if r["ext"] not in used_types), remaining[0])
            selected.append(pick)
        return selected

    def sample(self, scan, file, remaining):
        sample = {"id": file["id"], "rel": file["rel"], "size": file["size"], "mtime": file["mtime"]}
        reservation = min(file["size"], 2 * 1024 * 1024)
        if reservation > remaining:
            return {**sample, "status": "metadata", "text": "Read budget exhausted.", "bytes_read": 0}
        args = {
            "root": scan["root"],
            "rel": file["rel"],
            "device": scan["device"],
            "size": file["size"],
            "mtime": file["mtime"],
            "image": scan["settings"]["analyze_images"],
        }
        try:
            command = (
                [sys.executable, "--extract-worker"]
                if getattr(sys, "frozen", False)
                else [sys.executable, "-m", "drivestigate.extract"]
            )
            process = subprocess.run(
                command,
                input=json.dumps(args),
                capture_output=True,
                text=True,
                timeout=12,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
            if process.returncode:
                raise ValueError("Reader stopped (time/memory limit or damaged file).")
            result = json.loads(process.stdout)
            return {**sample, **result}
        except (ValueError, subprocess.TimeoutExpired) as exc:
            return {**sample, "status": "error", "text": str(exc)[:300], "bytes_read": reservation}

    def investigate(self, scan, finding_id, started):
        settings, scan_id = scan["settings"], scan["id"]
        client = self.client_factory(settings)
        model_info = client.test()
        if settings["analyze_images"]:
            capabilities = model_info.get("capabilities", [])
            if "vision" not in capabilities:
                raise ValueError("Image analysis is enabled, but the model does not report vision support.")
        with self.store.connect() as db:
            if finding_id:
                pending = [finding_id]
            else:
                pending = [
                    r[0]
                    for r in db.execute(
                        "SELECT f.id FROM findings f JOIN files x ON x.dir_id=f.dir_id "
                        "WHERE f.scan_id=? AND f.analyzed=0 GROUP BY f.id "
                        "ORDER BY MAX(x.score) DESC,f.id LIMIT ?",
                        (scan_id, settings["max_folders"]),
                    )
                ]
        calls, read = scan["llm_calls"], scan["read_bytes"]
        call_limit = calls + settings["max_llm_calls"]
        read_limit = read + settings["max_read_mb"] * 1024**2
        deadline = started + settings["time_budget_minutes"] * 60
        errors = 0
        limited = False
        self.live.setdefault(scan_id, {}).update(phase_started=time.monotonic(), durations=[], baseline=0)
        self.progress_update(
            scan_id, completed=0, total=len(pending), unit="folders", stage="analysis", current=""
        )
        processed = 0
        for fid in pending:
            unit_started = time.monotonic()
            self.checkpoint(scan)
            if calls >= call_limit or read >= read_limit or time.monotonic() >= deadline:
                limited = True
                break
            with self.store.connect() as db:
                row = dict(
                    db.execute(
                        "SELECT f.*,d.rel FROM findings f JOIN directories d ON d.id=f.dir_id WHERE f.id=?",
                        (fid,),
                    ).fetchone()
                )
                types = [
                    dict(r)
                    for r in db.execute(
                        "SELECT category,COUNT(*) count,MIN(mtime) oldest,MAX(mtime) newest,SUM(size) bytes "
                        "FROM files WHERE dir_id=? GROUP BY category",
                        (row["dir_id"],),
                    )
                ]
            samples = json.loads(row["samples"])
            self.store.update(scan_id, message=f"Investigating: {row['rel']}")
            self.progress_update(scan_id, current=row["rel"])
            for round_number in range(2):
                self.checkpoint(scan)
                if calls >= call_limit or time.monotonic() >= deadline:
                    limited = True
                    break
                candidates = self.candidates(fid, settings["samples_per_folder"], {s["id"] for s in samples})
                if not candidates and samples and round_number > 0:
                    break
                images = {}
                for file in candidates:
                    self.checkpoint(scan)
                    if time.monotonic() >= deadline:
                        limited = True
                        break
                    sample = self.sample(scan, file, read_limit - read)
                    read += sample.pop("bytes_read", 0)
                    if "image" in sample:
                        images[sample["id"]] = sample.pop("image")
                    if sample["status"] == "error":
                        self.store.event(scan_id, "extraction", file["rel"], sample["text"])
                    samples.append(sample)
                    with self.store.connect() as db:
                        db.execute("UPDATE findings SET samples=? WHERE id=?", (json.dumps(samples), fid))
                    self.store.update(scan_id, read_bytes=read)
                self.checkpoint(scan)
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    limited = True
                    break
                evidence = {
                    "folder": row["rel"],
                    "inventory": types,
                    "sample_note": "Only selected excerpts/metadata; no exhaustive content review.",
                    "samples": [
                        {
                            **s,
                            **({"image": images[s["id"]]} if s["id"] in images else {}),
                            "text": s.get("text", "")[:2200],
                        }
                        for s in samples[-50:]
                    ],
                }
                calls += 1
                self.store.update(scan_id, llm_calls=calls)
                try:
                    result = client.analyze(
                        evidence, timeout=min(settings["request_timeout_seconds"], remaining_time)
                    )
                    errors = 0
                    if result["needs_more_samples"]:
                        result["next_action"] += (
                            " Further sampling recommended; each run permits at most two rounds."
                        )
                    method = (
                        "sampled"
                        if any(s["status"] in ("excerpt", "image") for s in samples)
                        else "metadata_ai"
                    )
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE findings SET priority=?,summary=?,reason=?,method=?,references_json=?,"
                            "next_action=?,analyzed=1 WHERE id=?",
                            (
                                result["priority"],
                                result["summary"],
                                result["reason"],
                                method,
                                json.dumps(result["file_ids"]),
                                result["next_action"],
                                fid,
                            ),
                        )
                except ValueError as exc:
                    errors += 1
                    self.store.event(scan_id, "llm", row["rel"], str(exc))
                    if errors >= 3:
                        raise ValueError(
                            "Three AI requests failed. Investigation paused; check Ollama and the selected model."
                        ) from exc
                    break
                if not result["needs_more_samples"]:
                    break
            processed += 1
            self.unit_finished(scan_id, time.monotonic() - unit_started)
            self.progress_update(scan_id, completed=processed)
        self.checkpoint(scan)
        with self.store.connect() as db:
            remaining_folders = db.execute(
                "SELECT COUNT(*) FROM findings WHERE scan_id=? AND analyzed=0", (scan_id,)
            ).fetchone()[0]
        message = (
            f"Inventory complete. {remaining_folders} folders still await AI review. "
            "Resume to investigate additional folders."
            if remaining_folders or limited
            else "Investigation complete. Review coverage and skipped paths in the report."
        )
        self.store.update(scan_id, status="completed", message=message)

    def path_for(self, scan_id, kind, item_id):
        scan = self.store.get_scan(scan_id)
        if not self.connected(scan):
            raise ValueError("Drive disconnected.")
        with self.store.connect() as db:
            if kind == "finding":
                row = db.execute(
                    "SELECT d.rel FROM findings f JOIN directories d ON d.id=f.dir_id "
                    "WHERE f.scan_id=? AND f.id=?",
                    (scan_id, item_id),
                ).fetchone()
            elif kind == "file":
                row = db.execute(
                    "SELECT rel FROM files WHERE scan_id=? AND id=?", (scan_id, item_id)
                ).fetchone()
            elif kind == "directory":
                row = db.execute(
                    "SELECT rel FROM directories WHERE scan_id=? AND id=?", (scan_id, item_id)
                ).fetchone()
            else:
                raise ValueError("Invalid catalog entry.")
        if not row:
            raise ValueError("Path not found in the catalog.")
        path = Path(scan["root"]) / row[0]
        # Finder resolves parent symlinks; reject replacements since the inventory.
        if path.resolve(strict=True) != Path(os.path.abspath(path)) or not path.resolve().is_relative_to(
            scan["root"]
        ):
            raise ValueError("Path has changed or contains symbolic links.")
        return str(path)
