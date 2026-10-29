import json
import threading
import time

import pytest

from drivestigate.config import DEFAULTS
from drivestigate.engine import Engine
from drivestigate.planner import validate_plan
from drivestigate.progressive import Investigation, redact
from drivestigate.sizetree import size_tree
from drivestigate.store import Store
from drivestigate.summary import render_report


class Planner:
    def __init__(self, settings):
        pass

    def test(self):
        return {"capabilities": []}

    def plan(self, evidence, timeout=None):
        return {
            "overview": "Discovered folders contain document and text examples; unlisted areas remain unknown.",
            "overview_directory_ids": [d["id"] for d in evidence["directories"]],
            "questions": ["What remains inside unlisted branches?"],
            "searches": [],
            "decisions": [
                {
                    "directory_id": d["id"],
                    "action": "sample" if d["files"] and not d["samples"] else "explore",
                    "summary": "Selected text evidence."
                    if d["samples"]
                    else "Names suggest text files; contents unverified.",
                    "reason": "Explore unfamiliar storage.",
                    "uncertainty": "Only discovered entries are represented.",
                    "priority": 1,
                    "file_ids": [],
                }
                for d in evidence["directories"]
            ],
        }


def wait(engine):
    engine.thread.join(15)
    assert not engine.thread.is_alive(), engine.store.get_scan(engine.active_id)["message"]


def setup(tmp_path, cls=Planner, settings=None):
    root = tmp_path / "drive"
    root.mkdir()
    for branch in ("odd", "cache", "mixed"):
        (root / branch).mkdir()
        for i in range(4):
            (root / branch / f"note-{i}.txt").write_text(
                "Personal financial notes.\npassword=supersecret123\n"
            )
    store = Store(tmp_path / "catalog" / "db")
    engine = Engine(store, cls)
    sid = engine.create(root, "Synthetic progressive", {**DEFAULTS, "max_llm_calls": 12, **(settings or {})})
    return root, store, engine, sid


def test_progressive_samples_readonly_and_live_report(tmp_path):
    root, store, engine, sid = setup(tmp_path)
    wait(engine)
    scan = store.get_scan(sid)
    assert scan["status"] == "completed", scan["message"]
    assert scan["phase"] == "progressive"
    assert store.stats(sid)["files"] == 12
    assert store.stats(sid)["content_samples"] > 0
    assert all("supersecret123" in p.read_text() for p in root.rglob("*.txt"))
    report = store.report(sid)
    assert report["state"] == "ready"
    assert report["data"]["investigation"]["questions"]
    assert "Working understanding" in render_report(report["data"], "http://localhost:8741")
    with store.connect() as db:
        evidence = " ".join(r[0] for r in db.execute("SELECT payload FROM investigation_evidence"))
        assert "supersecret123" not in evidence
        assert "credential value redacted" in evidence
    tree = size_tree(store, sid)
    assert tree["root"]["tree_complete"] == 1
    assert tree["root"]["tree_bytes"] == sum(p.stat().st_size for p in root.rglob("*.txt"))


def test_analysis_overlaps_discovery_and_publishes_before_completion(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = Investigation.list_chunk

    def slow(self, task):
        time.sleep(0.025)
        return original(self, task)

    monkeypatch.setattr(Investigation, "list_chunk", slow)

    class Blocking(Planner):
        def plan(self, evidence, timeout=None):
            entered.set()
            assert release.wait(8)
            return super().plan(evidence, timeout)

    root = tmp_path / "many"
    root.mkdir()
    for i in range(40):
        (root / f"folder-{i}").mkdir()
        (root / f"folder-{i}" / "notes.txt").write_text("notes")
    store = Store(tmp_path / "catalog" / "db")
    engine = Engine(store, Blocking)
    sid = engine.create(root, "Overlap", {**DEFAULTS, "max_llm_calls": 2})
    try:
        assert entered.wait(5)
        early = store.stats(sid)
        assert early["inventoried_dirs"] < early["directories"]
        time.sleep(0.3)
        assert store.stats(sid)["inventoried_dirs"] > early["inventoried_dirs"]
        assert store.report(sid)["state"] == "live"
    finally:
        release.set()
    wait(engine)
    assert store.get_scan(sid)["llm_calls"] == 2


def test_pause_resume_keeps_evidence_and_pending_work(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Blocking(Planner):
        def plan(self, evidence, timeout=None):
            entered.set()
            release.wait(5)
            return super().plan(evidence, timeout)

    _, store, engine, sid = setup(tmp_path, Blocking)
    assert entered.wait(5)
    engine.interrupt(sid)
    release.set()
    wait(engine)
    assert store.get_scan(sid)["status"] == "paused"
    engine.start(sid)
    wait(engine)
    with store.connect() as db:
        first = db.execute("SELECT COUNT(*) FROM investigation_evidence").fetchone()[0]
    assert first > 0
    reads = store.get_scan(sid)["read_bytes"]
    engine.start(sid)
    wait(engine)
    assert store.get_scan(sid)["read_bytes"] == reads


def test_ai_failure_retains_catalog_and_retryable_work(tmp_path):
    class Broken(Planner):
        def plan(self, evidence, timeout=None):
            raise ValueError("Model unavailable")

    _, store, engine, sid = setup(tmp_path, Broken)
    wait(engine)
    assert store.stats(sid)["files"] > 0
    assert "Model unavailable" in store.report(sid)["data"]["investigation"]["ai_error"]
    engine.client_factory = Planner
    engine.start(sid)
    wait(engine)
    assert store.stats(sid)["analyzed_folders"] > 0


def test_plan_tool_validation_rejects_foreign_ids_and_bad_actions():
    evidence = {"directories": [{"id": 1, "files": [{"id": 10}], "samples": []}]}
    result = Planner({}).plan(evidence)
    validate_plan(result, evidence)
    for field, value in (("directory_id", 2), ("file_ids", [999]), ("action", "execute"), ("priority", True)):
        invalid = json.loads(json.dumps(result))
        invalid["decisions"][0][field] = value
        with pytest.raises(ValueError):
            validate_plan(invalid, evidence)


def test_redaction_and_default_mode():
    assert DEFAULTS["investigation_mode"] == "progressive"
    assert DEFAULTS["time_budget_minutes"] == 5
    assert "private key redacted" in redact(
        "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    )


def test_refocus_reuses_cached_content_and_rejects_active_change(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Blocking(Planner):
        def plan(self, evidence, timeout=None):
            entered.set()
            release.wait(5)
            return super().plan(evidence, timeout)

    _, store, engine, sid = setup(tmp_path, Blocking)
    assert entered.wait(5)
    with pytest.raises(ValueError, match="Pause"):
        engine.refocus(sid, {"search_focus": "Creative projects"})
    release.set()
    wait(engine)
    read = store.get_scan(sid)["read_bytes"]
    engine.refocus(sid, {"search_focus": "Creative projects"})
    assert store.get_scan(sid)["settings"]["search_focus"] == "Creative projects"
    assert store.stats(sid)["analyzed_folders"] == 0
    engine.start(sid)
    wait(engine)
    assert store.get_scan(sid)["read_bytes"] == read
    assert store.stats(sid)["analyzed_folders"] > 0


def test_large_root_yields_to_child_and_source_access_is_serial(tmp_path):
    root = tmp_path / "drive"
    root.mkdir()
    (root / "unknown").mkdir()
    (root / "unknown" / "important.txt").write_text("Synthetic notes")
    for i in range(500):
        (root / f"file-{i}.txt").write_text("sample")
    store = Store(tmp_path / "catalog" / "db")
    engine = Engine(store, Planner)
    engine.hold_queue(True)
    sid = engine.create(root, "Broad first", DEFAULTS)
    investigation = Investigation(engine, store.get_scan(sid))
    investigation.initialize()
    seen_child = False
    try:
        for _ in range(8):
            task = investigation.choose_task()
            if not task:
                continue
            outcome = investigation.list_chunk(task)
            with store.connect() as db:
                row = db.execute("SELECT rel FROM directories WHERE id=?", (task["target"],)).fetchone()
                if row[0] == "unknown":
                    seen_child = True
                    assert (
                        db.execute(
                            "SELECT done FROM directories WHERE scan_id=? AND rel='.'", (sid,)
                        ).fetchone()[0]
                        == 0
                    )
                    break
                db.execute(
                    "UPDATE investigation_tasks SET state=?,priority=MAX(5,priority-5) WHERE id=?",
                    (outcome, task["id"]),
                )
        assert seen_child
        assert size_tree(store, sid)["provisional"]
    finally:
        for did in list(investigation.open_dirs):
            investigation.close_directory(did)


def test_deleted_progressive_catalog_cascades_and_source_survives(tmp_path):
    root, store, engine, sid = setup(tmp_path)
    wait(engine)
    assert store.report(sid)
    engine.delete(sid)
    with store.connect() as db:
        for table in ("investigation_tasks", "investigation_evidence", "investigation_memory"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert len(list(root.rglob("*.txt"))) == 12


def test_restart_preserves_pending_tools_and_completed_evidence(tmp_path):
    _, store, engine, sid = setup(tmp_path, settings={"max_llm_calls": 1})
    wait(engine)
    with store.connect() as db:
        db.execute("UPDATE investigation_tasks SET state='running' WHERE state='pending'")
    reopened = Store(store.path)
    with reopened.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM investigation_tasks WHERE state='running'").fetchone()[0] == 0
    replacement = Engine(reopened, Planner)
    replacement.start(sid)
    wait(replacement)
    assert reopened.get_scan(sid)["llm_calls"] == 2


def test_child_tool_targets_are_scoped_to_supplied_names():
    evidence = {
        "directories": [
            {
                "id": 1,
                "path": ".",
                "revision": 1,
                "files": [],
                "samples": [],
                "children": [{"id": 2, "path": "unfamiliar"}],
            }
        ]
    }
    result = Planner({}).plan(evidence)
    result["decisions"][0]["directory_id"] = 2
    result["decisions"][0]["action"] = "explore"
    validate_plan(result, evidence)
    result["decisions"][0]["file_ids"] = [42]
    with pytest.raises(ValueError, match="unknown files"):
        validate_plan(result, evidence)


def test_planner_json_fallback_and_image_budget():
    from drivestigate.llm import Ollama, OllamaError

    evidence = {
        "directories": [
            {
                "id": 1,
                "files": [],
                "samples": [{"id": i, "image": "synthetic-thumbnail", "text": "metadata"} for i in range(7)],
            }
        ]
    }
    result = Planner({}).plan(evidence)
    result["decisions"][0]["action"] = "explore"
    calls = []
    client = Ollama(DEFAULTS)

    def request(path, payload, timeout=None):
        calls.append(json.loads(json.dumps(payload)))
        if "format" in payload:
            raise OllamaError(501, "structured output unsupported")
        return {"message": {"content": json.dumps(result)}}

    client.request = request
    client.plan(evidence, timeout=10)
    assert len(calls) == 2
    assert len(calls[-1]["messages"][1]["images"]) == 4
    sent = json.loads(calls[-1]["messages"][1]["content"])
    assert sum("image_index" in s for s in sent["directories"][0]["samples"]) == 4
    assert "image" in evidence["directories"][0]["samples"][0]


def test_default_upgrade_preserves_scan_settings_and_custom_budgets(tmp_path):
    store = Store(tmp_path / "catalog" / "db")
    old = {
        **DEFAULTS,
        "investigation_mode": "overview",
        "depth": "balanced",
        "max_folders": 40,
        "samples_per_folder": 6,
        "max_llm_calls": 60,
        "time_budget_minutes": 60,
        "search_focus": "",
    }
    store.save_settings(old)
    with store.connect() as db:
        db.execute("PRAGMA user_version=4")
    upgraded = Store(store.path)
    assert upgraded.settings()["time_budget_minutes"] == 5
    assert upgraded.settings()["investigation_mode"] == "progressive"
    upgraded.save_settings({**old, "time_budget_minutes": 7})
    with upgraded.connect() as db:
        db.execute("PRAGMA user_version=4")
    assert Store(store.path).settings()["time_budget_minutes"] == 7


def test_directory_worker_cancellation_and_symlink_boundary(tmp_path):
    from drivestigate.discovery import DirectoryCursor

    root = tmp_path / "drive"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("must not be listed")
    (root / "link").symlink_to(outside, target_is_directory=True)
    cursor = DirectoryCursor(str(root), ".", root.stat().st_dev, [])
    try:
        batch = cursor.next()
        assert batch["complete"]
        assert not batch["entries"]
        assert batch["events"][0][0] == "symlink"
    finally:
        cursor.close()
    assert cursor.process.poll() is not None
    cursor = DirectoryCursor(str(root), ".", root.stat().st_dev, [])
    try:
        with pytest.raises(InterruptedError):
            cursor.next(alive=lambda: False)
    finally:
        cursor.close()
    assert cursor.process.poll() is not None


def test_directory_worker_timeout_is_bounded(tmp_path, monkeypatch):
    import subprocess
    import sys

    import drivestigate.discovery as discovery

    original = subprocess.Popen

    def stalled(*args, **kwargs):
        return original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(discovery.subprocess, "Popen", stalled)
    cursor = discovery.DirectoryCursor(str(tmp_path), ".", tmp_path.stat().st_dev, [])
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            cursor.next(timeout=0.15)
    finally:
        cursor.close()
    assert time.monotonic() - started < 2
    assert cursor.process.poll() is not None
