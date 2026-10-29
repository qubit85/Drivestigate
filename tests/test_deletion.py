"""Deleting a catalog must never modify a source or an exported vault."""

import threading
import time

import pytest
from test_engine import FakeOllama, wait

from drivestigate.config import DEFAULTS as NEW_DEFAULTS
from drivestigate.demo import create_demo
from drivestigate.engine import Engine
from drivestigate.store import Store

# Legacy-engine regression fixtures; progressive behavior has dedicated tests.
DEFAULTS = {**NEW_DEFAULTS, "investigation_mode": "overview", "search_focus": ""}


def assert_absent(store, sid):
    with pytest.raises(ValueError, match="not found"):
        store.get_scan(sid)
    assert sid not in [s["id"] for s in store.scans()]
    assert sid not in [s["id"] for s in store.queue()["jobs"]]
    with store.connect() as db:
        for table in ("directories", "files", "findings", "events", "scan_reports"):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE scan_id=?", (sid,)).fetchone()[0] == 0
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_completed_deletion_cascades_and_preserves_source_vault_and_other_scan(tmp_path):
    source = create_demo(tmp_path / "source")
    before = {
        p.relative_to(source): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in source.rglob("*")
        if p.is_file()
    }
    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, FakeOllama)
    settings = {**DEFAULTS, "investigation_mode": "detail", "detail_use_llm": False}
    sid = engine.create(source, "Delete me", settings)
    wait(engine)
    vault = store.get_scan(sid)["vault_path"]
    from pathlib import Path

    edited_note = Path(vault) / "My own note.md"
    edited_note.write_text("Keep my edits.")
    keep = engine.create(source, "Keep me", settings)
    wait(engine)
    keep_stats = store.stats(keep)
    assert store.report(sid) and store.stats(sid)["detail"]["processed"] > 0
    assert engine.delete(sid) == {"deleted": True, "pending": False}
    assert_absent(store, sid)
    assert store.stats(keep) == keep_stats and store.report(keep)
    with store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM file_details").fetchone()[0] == keep_stats["detail"]["processed"]
        )
    assert edited_note.read_text() == "Keep my edits."
    assert before == {
        p.relative_to(source): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in source.rglob("*")
        if p.is_file()
    }
    assert sid not in engine.live
    with pytest.raises(ValueError):
        engine.delete(sid)


def test_queued_and_offline_investigations_can_be_deleted(tmp_path):
    source = create_demo(tmp_path / "source")
    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, FakeOllama)
    engine.hold_queue(True)
    ids = [engine.create(source, name, DEFAULTS) for name in ("A", "B", "C")]
    source.rename(tmp_path / "offline")
    engine.delete(ids[1])
    assert_absent(store, ids[1])
    assert [j["id"] for j in store.queue()["jobs"]] == [ids[0], ids[2]]
    engine.reorder([ids[2], ids[0]])
    engine.hold_queue(False)
    assert all(j["status"] == "waiting" for j in store.queue()["jobs"])
    engine.delete(ids[0])
    assert_absent(store, ids[0])
    assert [j["id"] for j in store.queue()["jobs"]] == [ids[2]]


@pytest.mark.parametrize("fail_request", [False, True])
def test_active_delete_waits_for_worker_then_continues_queue(tmp_path, fail_request):
    entered, release = threading.Event(), threading.Event()

    class BlockingModel(FakeOllama):
        blocked = False

        def analyze(self, evidence, timeout=None):
            if not BlockingModel.blocked:
                BlockingModel.blocked = True
                entered.set()
                assert release.wait(10)
                if fail_request:
                    raise RuntimeError("Synthetic request failed.")
            return super().analyze(evidence, timeout)

    source = create_demo(tmp_path / "source")
    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, BlockingModel)
    sid = engine.create(source, "Active", {**DEFAULTS, "max_folders": 1})
    try:
        assert entered.wait(10)
        worker = engine.thread
        keep = engine.create(source, "Next", {**DEFAULTS, "max_folders": 1})
        assert engine.delete(sid) == {"deleted": False, "pending": True}
        assert engine.active_id == sid and worker.is_alive()
        assert store.get_scan(sid)["status"] == "deleting"
        assert store.scans()[-1]["status"] == "deleting"
        assert store.queue()["jobs"][0]["status"] == "deleting"
        for action in (engine.start, engine.interrupt):
            with pytest.raises(ValueError, match="being deleted"):
                action(sid)
        assert engine.delete(sid)["pending"]  # repeated requests remain safe
    finally:
        release.set()
    worker.join(10)
    deadline = time.monotonic() + 10
    while engine.active_id and time.monotonic() < deadline:
        engine.thread.join(0.1)
    assert engine.active_id is None
    assert_absent(store, sid)
    assert store.get_scan(keep)["status"] == "completed"


def test_confirmed_deletion_finishes_after_restart(tmp_path):
    source = create_demo(tmp_path / "source")
    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, FakeOllama)
    engine.hold_queue(True)
    sid = engine.create(source, "Confirmed", DEFAULTS)
    keep = engine.create(source, "Unconfirmed", DEFAULTS)
    store.update(sid, delete_requested=1, status="stopping")
    recovered = Store(store.path)
    assert_absent(recovered, sid)
    assert recovered.get_scan(keep)["status"] == "queued"
    assert recovered.queue()["paused"]
