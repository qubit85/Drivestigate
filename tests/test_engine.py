import io
import itertools
import json
import os
import threading
import zipfile
from types import SimpleNamespace

import pytest

from drivestigate.config import DEFAULTS as NEW_DEFAULTS
from drivestigate.config import validate_settings
from drivestigate.demo import create_demo
from drivestigate.engine import Engine, classify
from drivestigate.extract import extract
from drivestigate.llm import Ollama
from drivestigate.reports import csv_report, html_report, json_report
from drivestigate.source import source_file
from drivestigate.store import Store

# Legacy-engine regression fixtures; progressive behavior has dedicated tests.
DEFAULTS = {**NEW_DEFAULTS, "investigation_mode": "overview", "search_focus": ""}


class FakeOllama:
    calls = []

    def __init__(self, settings):
        self.settings = settings

    def test(self):
        return {"ok": True, "capabilities": ["vision"]}

    def analyze(self, evidence, timeout=None):
        self.calls.append(evidence)
        return {
            "summary": "An interesting personal archive.",
            "reason": "Samples contain personal notes.",
            "priority": 1,
            "needs_more_samples": len(evidence["samples"]) < 2,
            "next_action": "Review the selected files.",
            "file_ids": [s["id"] for s in evidence["samples"]],
        }


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "catalog" / "catalog.sqlite3")
    root = create_demo(tmp_path / "drive")
    return store, Engine(store, FakeOllama), root


def wait(engine):
    engine.thread.join(20)
    assert not engine.thread.is_alive(), "Engine did not finish"


def test_full_scan_is_readonly_and_has_real_evidence(setup):
    store, engine, root = setup
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    sid = engine.create(root, "Demo", DEFAULTS)
    wait(engine)
    scan = store.get_scan(sid)
    assert scan["status"] == "completed", scan["message"]
    stats = store.stats(sid)
    assert stats["files"] == 13
    assert stats["technical_files"] == 3
    assert stats["events"]["excluded"] == 1
    assert stats["content_samples"] > 0
    assert stats["analyzed_folders"] == stats["findings"]
    findings = store.findings(sid)["items"]
    assert any("User Data" in f["rel"] for f in findings)
    assert all(f["method"] == "sampled" for f in findings)
    assert any(
        "project-backup.zip" in s["rel"] and "Archive listing" in s["text"]
        for f in findings
        for s in f["samples"]
    )
    after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    assert before == after
    assert engine.path_for(sid, "finding", next(f["id"] for f in findings if f["rel"] == ".")) == str(root)
    with pytest.raises(ValueError):
        engine.path_for(sid, "file", -1)


def test_budget_leaves_unreviewed_folders_visible_and_resume_works(setup):
    store, engine, root = setup
    settings = {**DEFAULTS, "max_folders": 1, "max_llm_calls": 1}
    sid = engine.create(root, "Budget", settings)
    wait(engine)
    stats = store.stats(sid)
    assert stats["findings"] > stats["analyzed_folders"] == 1
    assert store.get_scan(sid)["llm_calls"] == 1
    # Saved settings remain independent from global settings.
    store.save_settings({**DEFAULTS, "search_focus": "Changed later"})
    engine.start(sid)
    wait(engine)
    assert store.stats(sid)["analyzed_folders"] == 2
    assert store.get_scan(sid)["settings"]["search_focus"] == ""


def test_pause_after_current_call_and_resume(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Blocking(FakeOllama):
        def analyze(self, evidence, timeout=None):
            entered.set()
            release.wait(10)
            return super().analyze(evidence, timeout)

    store = Store(tmp_path / "catalog" / "db")
    root = create_demo(tmp_path / "drive")
    engine = Engine(store, Blocking)
    sid = engine.create(root, "Pause", DEFAULTS)
    assert entered.wait(15)
    engine.interrupt(sid)
    release.set()
    wait(engine)
    assert store.get_scan(sid)["status"] == "paused"
    assert store.stats(sid)["analyzed_folders"] >= 1
    engine.start(sid)
    wait(engine)
    assert store.get_scan(sid)["status"] == "completed"


def test_symlinks_are_not_followed_and_replacements_are_rejected(setup, tmp_path):
    store, engine, root = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not for scanning")
    (root / "link").symlink_to(outside, target_is_directory=True)
    sid = engine.create(root, "Links", DEFAULTS)
    wait(engine)
    assert store.stats(sid)["events"]["symlink"] == 1
    assert not any("secret" in str(f) for f in store.findings(sid)["items"])
    family = root / "Documents/Family"
    family.rename(root / "Documents/Family-original")
    family.symlink_to(outside, target_is_directory=True)
    fid = next(f["id"] for f in store.findings(sid)["items"] if f["rel"] == "Documents/Family")
    with pytest.raises(ValueError):
        engine.path_for(sid, "finding", fid)
    with pytest.raises(OSError):
        with source_file(root, "Documents/Family/secret.txt", os.stat(root).st_dev):
            pass
    with pytest.raises(ValueError):
        with source_file(root, "../outside/secret.txt", os.stat(root).st_dev):
            pass


def test_disconnected_drive_cannot_resume(setup):
    store, engine, root = setup
    sid = engine.create(root, "Disconnect", DEFAULTS)
    wait(engine)
    root.rename(root.with_name("detached"))
    root.mkdir()
    assert not engine.connected(store.get_scan(sid))
    with pytest.raises(ValueError):
        engine.start(sid)


def test_crash_recovery_preserves_catalog(tmp_path):
    store = Store(tmp_path / "catalog/db")
    root = create_demo(tmp_path / "drive")
    engine = Engine(store, FakeOllama)
    sid = engine.create(root, "Crash", DEFAULTS)
    wait(engine)
    store.update(sid, status="running")
    recovered = Store(tmp_path / "catalog/db")
    assert recovered.get_scan(sid)["status"] == "paused"
    assert recovered.stats(sid)["files"] == store.stats(sid)["files"]


def test_exports_escape_content_and_protect_csv(setup):
    store, engine, root = setup
    sid = engine.create(root, "<script>alert(1)</script>", DEFAULTS)
    wait(engine)
    with store.connect() as db:
        db.execute("UPDATE findings SET summary=? WHERE scan_id=?", ('=HYPERLINK("evil")<script>', sid))
    report = "".join(html_report(store, sid, "http://127.0.0.1:8741"))
    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    assert f"/?scan={sid}&amp;finding=" in report
    csv = "".join(csv_report(store, sid))
    assert "'=HYPERLINK" in csv
    exported = json.loads("".join(json_report(store, sid)))
    assert len(exported["findings"]) == store.stats(sid)["findings"]
    assert exported["scan"]["settings"]["model"] == DEFAULTS["model"]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test",
        "http://127.0.0.1.evil.test",
        "http://localhost@evil.test",
        "http://127.0.0.1:11434/private",
        "file:///tmp/a",
        "http://[::1]:99999",
    ],
)
def test_settings_disallow_remote_or_malformed_endpoints(url):
    with pytest.raises(ValueError):
        validate_settings({**DEFAULTS, "ollama_url": url})


def test_prompt_focus_and_filter_persist(setup):
    store, _, _ = setup
    settings = store.save_settings(
        {
            **DEFAULTS,
            "system_prompt": "Find personal work",
            "search_focus": "old photos",
            "profiles": [{"name": "Photos", "system_prompt": "Find photos", "search_focus": "2012"}],
        }
    )
    assert store.settings() == settings
    assert classify("Program Files/App/data/personal-backup.txt", "personal-backup.txt")[1] == 0
    assert classify("Windows/System32/kernel.dll", "kernel.dll")[1] == 1
    assert classify("Windows.old/Users/A/Documents/notes.txt", "notes.txt")[1] == 0
    assert classify("Program Files/App/User Data/settings.ini", "settings.ini")[1] == 0
    assert classify("Windows.old/Users/A/project/node_modules/lib/index.js", "index.js")[1] == 1


def test_invalid_llm_references_are_rejected():
    client = Ollama(DEFAULTS)
    client.request = lambda *a, **kw: {
        "message": {
            "content": json.dumps(
                {
                    "summary": "x",
                    "reason": "x",
                    "priority": 1,
                    "needs_more_samples": False,
                    "next_action": "x",
                    "file_ids": [999],
                }
            )
        }
    }
    with pytest.raises(ValueError, match="file reference"):
        client.analyze({"samples": [{"id": 1}]})


def test_office_zip_bombs_and_untrusted_xml_are_not_extracted():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "A" * 500000)
    data = buf.getvalue()
    result = extract(io.BytesIO(data), ".docx", len(data))
    assert result["status"] == "metadata"


def test_pdf_extraction_when_available():
    pypdf = pytest.importorskip("pypdf")
    buf = io.BytesIO()
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(buf)
    result = extract(io.BytesIO(buf.getvalue()), ".pdf", len(buf.getvalue()))
    assert result["status"] == "metadata"
    assert "No extractable text" in result["text"]


def test_catalog_cannot_be_scanned(setup):
    store, engine, root = setup
    with pytest.raises(ValueError, match="data directory"):
        engine.create(root.parent, "Bad", DEFAULTS)


def test_export_snapshot_is_stable_while_live_catalog_changes(setup):
    store, engine, root = setup
    sid = engine.create(root, "Snapshot", DEFAULTS)
    wait(engine)
    before = store.get_scan(sid)["message"]
    with store.snapshot() as frozen:
        store.update(sid, message="Changed after snapshot")
        assert frozen.get_scan(sid)["message"] == before
        assert store.get_scan(sid)["message"] != before


def test_invalid_utf8_filenames_are_logged_instead_of_aborting(setup, monkeypatch):
    store, engine, root = setup
    original_scandir = os.scandir
    injected = False

    class Entries:
        def __init__(self, path):
            self.entries = original_scandir(path)

        def __enter__(self):
            nonlocal injected
            source = self.entries.__enter__()
            if not injected:
                injected = True
                return itertools.chain(source, [SimpleNamespace(name="bad-\udcff.txt")])
            return source

        def __exit__(self, *args):
            return self.entries.__exit__(*args)

    monkeypatch.setattr(os, "scandir", Entries)
    sid = engine.create(root, "Filename", DEFAULTS)
    wait(engine)
    assert store.get_scan(sid)["status"] == "completed"
    assert any("UTF-8" in e["message"] for e in store.events(sid))
