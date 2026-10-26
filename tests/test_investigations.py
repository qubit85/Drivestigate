"""Queue, coverage and graph checks use synthetic sources only."""

import hashlib
import json
import re
import sqlite3
import threading
import time

import pytest
from test_engine import FakeOllama, wait

from drivestigate.config import DEFAULTS
from drivestigate.engine import Engine
from drivestigate.knowledge import build_vault, file_note, graph_data, vault_path
from drivestigate.store import SCHEMA, Store


class DetailModel(FakeOllama):
    def analyze_file(self, evidence, timeout=None):
        return {
            "summary": "An archived project file.",
            "reason": "Based on the supplied excerpt or metadata.",
            "priority": 1,
            "topics": ["Project atlas", "Archive [[unsafe|label]]"],
            "file_ids": [s["id"] for s in evidence["samples"]],
        }


@pytest.fixture
def detail(tmp_path):
    root = tmp_path / "source"
    (root / "Windows/System32").mkdir(parents=True)
    (root / "notes").mkdir()
    (root / "notes/a [#|].txt").write_text("Project atlas\nRead this [[untrusted]] note.")
    (root / "duplicate.txt").write_bytes((root / "notes/a [#|].txt").read_bytes())
    (root / "Windows/System32/program.bin").write_bytes(b"\x00\xff\x10" * 100)
    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, DetailModel)
    settings = {**DEFAULTS, "investigation_mode": "detail", "max_llm_calls": 1, "max_folders": 1}
    return store, engine, root, settings


def wait_all(engine):
    deadline = time.monotonic() + 20
    while engine.active_id and time.monotonic() < deadline:
        engine.thread.join(0.1)
    assert engine.active_id is None


def test_queue_order_survives_restart_and_rejects_stale_orders(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "note.txt").write_text("A note")
    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, FakeOllama)
    engine.hold_queue(True)
    ids = [engine.create(root, name, DEFAULTS) for name in ("First", "Second", "Third")]
    engine.move(ids[2], "up")
    assert [j["id"] for j in store.queue()["jobs"]] == [ids[0], ids[2], ids[1]]
    engine.reorder(ids[::-1])
    with pytest.raises(ValueError):
        engine.reorder([ids[0], ids[0], ids[1]])
    with pytest.raises(ValueError):
        engine.reorder(ids[:1])
    engine.close()
    recovered = Store(store.path)
    assert recovered.queue()["paused"] is True
    assert [j["id"] for j in recovered.queue()["jobs"]] == ids[::-1]
    next_engine = Engine(recovered, FakeOllama)
    next_engine.interrupt(ids[1])
    next_engine.hold_queue(False)
    wait_all(next_engine)
    assert recovered.get_scan(ids[1])["status"] == "paused"
    assert all(recovered.get_scan(s)["status"] == "completed" for s in (ids[0], ids[2]))


def test_disconnected_job_waits_and_does_not_block_queue(tmp_path):
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()
        (root / "note.txt").write_text("A note")
    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, FakeOllama)
    engine.hold_queue(True)
    ids = [engine.create(root, "Drive", DEFAULTS) for root in roots]
    roots[0].rename(tmp_path / "detached")
    engine.hold_queue(False)
    wait_all(engine)
    assert store.get_scan(ids[0])["status"] == "waiting"
    assert store.get_scan(ids[1])["status"] == "completed"
    (tmp_path / "detached").rename(roots[0])
    engine.kick()
    wait_all(engine)
    assert store.get_scan(ids[0])["status"] == "completed"


def test_add_to_queue_during_running_investigation(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Blocking(FakeOllama):
        def analyze(self, evidence, timeout=None):
            entered.set()
            release.wait(10)
            return super().analyze(evidence, timeout)

    root = tmp_path / "source"
    root.mkdir()
    (root / "note.txt").write_text("A note")
    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, Blocking)
    first = engine.create(root, "Running", DEFAULTS)
    assert entered.wait(10)
    second = engine.create(root, "Queued", DEFAULTS)
    assert store.get_scan(second)["status"] == "queued"
    with pytest.raises(ValueError):
        engine.reorder([first, second])
    release.set()
    wait_all(engine)
    assert all(store.get_scan(s)["status"] == "completed" for s in (first, second))


def test_detail_reads_every_file_and_exports_valid_separate_vault(detail):
    store, engine, root, settings = detail
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    sid = engine.create(root, "Detail", settings)
    wait(engine)
    scan, stats = store.get_scan(sid), store.stats(sid)
    assert scan["status"] == "completed", scan["message"]
    assert stats["files"] == stats["detail"]["hashed"] == stats["detail"]["processed"] == 3
    assert (
        stats["detail"]["ai_reviewed"] == scan["llm_calls"] == 3
    )  # overview budget does not truncate detail
    assert stats["technical_files"] == 1 and stats["detail"]["duplicate_groups"] == 1
    with store.connect() as db:
        files = db.execute("SELECT id,rel,name FROM files WHERE scan_id=?", (sid,)).fetchall()
    for f in files:
        record = store.detail_file(sid, f["id"])
        assert record["sha256"] == hashlib.sha256((root / f["rel"]).read_bytes()).hexdigest()
        assert record["topics"] == ["Project atlas", "Archive [[unsafe|label]]"]
    after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    assert before == after
    vault = vault_path(store, sid)
    assert not vault.is_relative_to(root)
    assert len(list((vault / "Files").glob("*.md"))) == 3
    assert len(list((vault / "Duplicates").glob("*.md"))) == 1
    for note in vault.rglob("*.md"):
        body = note.read_text()
        assert "## Evidence excerpt" not in body
        for target in re.findall(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", body):
            assert (vault / (target + ".md")).is_file(), (note, target)
    assert not list(vault.rglob("*.bin"))
    assert "untrusted" not in (vault / (file_note(files[0]["id"], files[0]["name"]) + ".md")).read_text()
    (vault / "User edits.md").write_text("Keep this")
    regenerated = build_vault(engine, scan)
    assert regenerated != vault and (vault / "User edits.md").read_text() == "Keep this"
    graph = graph_data(store, sid)
    assert {"folder", "file", "topic", "type", "duplicate"} <= {n["kind"] for n in graph["nodes"]}
    assert {"ai", "filesystem", "fingerprint", "extension"} == {e["provenance"] for e in graph["edges"]}
    assert graph_data(store, sid, query="%does-not-exist")["matching_files"] == 0
    assert graph_data(store, sid, limit=1)["truncated"]
    assert graph_data(store, sid, folder="notes")["matching_files"] == 1


def test_detail_can_run_without_ai_and_keeps_unreadable_files_visible(detail, monkeypatch):
    store, engine, root, settings = detail
    original = engine.sample

    def broken(scan, file, limit):
        if file["name"].endswith(".bin"):
            return {
                "id": file["id"],
                "rel": file["rel"],
                "status": "error",
                "text": "Unsupported damaged data",
            }
        return original(scan, file, limit)

    monkeypatch.setattr(engine, "sample", broken)
    sid = engine.create(root, "No AI", {**settings, "detail_use_llm": False})
    wait(engine)
    stats = store.stats(sid)
    assert store.get_scan(sid)["status"] == "completed"
    assert stats["detail"]["processed"] == 3 and stats["detail"]["errors"] == 1
    assert stats["detail"]["ai_reviewed"] == store.get_scan(sid)["llm_calls"] == 0
    assert not any(n["kind"] == "topic" for n in graph_data(store, sid)["nodes"])


def test_detail_ai_failure_pauses_then_retries_without_rehash(detail, monkeypatch):
    store, engine, root, settings = detail

    class Failing(DetailModel):
        def analyze_file(self, evidence, timeout=None):
            raise ValueError("Model unavailable")

    engine.client_factory = Failing
    sid = engine.create(root, "Retry", settings)
    wait(engine)
    assert store.get_scan(sid)["status"] == "paused"
    assert store.stats(sid)["detail"]["hashed"] == 1
    engine.client_factory = DetailModel
    engine.start(sid)
    wait(engine)
    assert store.get_scan(sid)["status"] == "completed"
    assert store.stats(sid)["detail"]["hashed"] == 3


def test_tree_pages_include_parent_without_duplicates_and_cover_all_children(detail):
    store, engine, root, settings = detail
    for i in range(155):
        (root / f"folder-{i:03}").mkdir()
    sid = engine.create(root, "Tree", {**settings, "detail_use_llm": False})
    wait(engine)
    one, two = store.tree(sid), store.tree(sid, offset=150)
    assert one["total"] == 157
    assert one["items"][0]["rel"] == two["items"][0]["rel"] == "."
    assert len(one["items"]) == 151 and len(two["items"]) == 8
    assert not ({r["id"] for r in one["items"][1:]} & {r["id"] for r in two["items"][1:]})
    assert one["items"][0]["tree_files"] == 3
    assert store.tree(sid, parent="notes")["items"][0]["processed_files"] == 1


def test_v1_migration_preserves_settings_and_inventory(tmp_path):
    path = tmp_path / "v1.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA.split("CREATE TABLE IF NOT EXISTS scheduler")[0])
        db.execute("PRAGMA user_version=1")
        db.execute(
            "INSERT INTO config VALUES(1,?)", (json.dumps({**DEFAULTS, "search_focus": "Find backups"}),)
        )
        db.execute(
            "INSERT INTO scans(id,name,root,device,identity,created,updated,status,phase,settings) "
            "VALUES('legacy','Legacy','/tmp/source',1,'1','today','today','completed','analysis',?)",
            (json.dumps(DEFAULTS),),
        )
        db.executemany(
            "INSERT INTO directories(scan_id,rel,done) VALUES('legacy',?,1)", [(".",), ("a",), ("a/b",)]
        )
    store = Store(path)
    assert store.get_scan("legacy")["status"] == "completed"
    assert store.settings()["search_focus"] == "Find backups"
    assert store.tree("legacy", parent="a")["items"][1]["rel"] == "a/b"
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4


def test_eta_waits_for_sample_then_reports_phase_range(detail):
    store, engine, root, settings = detail
    engine.hold_queue(True)
    sid = engine.create(root, "ETA", settings)
    store.update(sid, status="running", phase="inventory", progress={"completed": 2, "total": 0})
    assert engine.progress(store.get_scan(sid))["percent"] is None
    store.update(sid, phase="detail", progress={"completed": 2, "total": 10})
    engine.live[sid] = {"durations": [2], "baseline": 0}
    assert engine.progress(store.get_scan(sid))["eta_low"] is None
    engine.live[sid]["durations"].append(4)
    p = engine.progress(store.get_scan(sid))
    assert p["percent"] == 20 and p["eta_low"] == 17 and p["eta_high"] == 36
    assert "excludes later phases" in p["eta_basis"]


def test_detail_pause_saves_completed_file_and_resumes_remaining(detail):
    store, engine, root, settings = detail
    entered, release = threading.Event(), threading.Event()

    class Blocking(DetailModel):
        def analyze_file(self, evidence, timeout=None):
            entered.set()
            release.wait(10)
            return super().analyze_file(evidence, timeout)

    engine.client_factory = Blocking
    sid = engine.create(root, "Pause detail", settings)
    assert entered.wait(10)
    engine.interrupt(sid)
    release.set()
    wait(engine)
    assert store.get_scan(sid)["status"] == "paused"
    assert store.stats(sid)["detail"]["processed"] == 1
    engine.client_factory = DetailModel
    engine.start(sid)
    wait(engine)
    assert store.get_scan(sid)["status"] == "completed"
    assert store.get_scan(sid)["llm_calls"] == 3


def test_detail_exports_include_file_coverage_and_known_links(detail):
    from drivestigate.reports import csv_report, html_report, json_report

    store, engine, root, settings = detail
    sid = engine.create(root, "Exports", {**settings, "detail_use_llm": False})
    wait(engine)
    data = json.loads("".join(json_report(store, sid)))
    assert len(data["file_details"]) == 3
    assert all(len(row["sha256"]) == 64 for row in data["file_details"])
    csv = "".join(csv_report(store, sid))
    assert "Content coverage" in csv and "SHA-256" in csv
    html = "".join(html_report(store, sid, engine.origin))
    assert "All-file coverage" in html and "&amp;file=" in html
    assert "bounded supported content or metadata" in html
