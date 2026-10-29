import json

import pytest
from test_engine import FakeOllama, wait

from drivestigate.config import DEFAULTS as NEW_DEFAULTS
from drivestigate.demo import create_demo
from drivestigate.engine import Engine
from drivestigate.llm import Ollama
from drivestigate.store import Store
from drivestigate.summary import catalog_report, render_report

# Legacy-engine regression fixtures; progressive behavior has dedicated tests.
DEFAULTS = {**NEW_DEFAULTS, "investigation_mode": "overview", "search_focus": ""}


class ReportModel(FakeOllama):
    def summarize_report(self, evidence, timeout=None):
        assert all(s["id"] for s in evidence["samples"])
        return {
            "summary": "The reviewed archive includes personal project notes.\n\nUnreviewed content remains uncertain.",
            "reason": "Based on saved folder assessments and complete inventory counts.",
            "next_steps": ["Review the supplied evidence paths."],
        }


def setup_report(tmp_path, model=ReportModel, **settings):
    store = Store(tmp_path / "catalog/db")
    root = create_demo(tmp_path / "source")
    engine = Engine(store, model)
    sid = engine.create(root, "<script>Drive</script>", {**DEFAULTS, **settings})
    wait(engine)
    assert store.get_scan(sid)["status"] == "completed"
    return store, engine, root, sid


def test_finished_investigation_saves_a_single_visual_report(tmp_path):
    store, _, _, sid = setup_report(tmp_path)
    report = store.report(sid)
    assert report["state"] == "ready" and report["data"]["synthesis"] == "local_ai"
    data = report["data"]
    assert data["stats"]["files"] == 13
    assert sum(r["files"] for r in data["storage"]) == 13
    assert sum(r["files"] for r in data["timeline"]) == 13
    assert len(data["leads"]) <= 10
    output = render_report(data, "http://127.0.0.1:8741", standalone=True)
    assert "Content by file type" in output and "Where the space is" in output
    assert "Coverage and confidence" in output and "File timeline" in output
    assert "<script>" not in output and "&lt;script&gt;" in output
    assert "data-evidence-id=" in output
    assert "<style>" in output and "stylesheet" not in output  # portable export


def test_summary_only_works_offline_without_rescanning_and_preserves_phase(tmp_path, monkeypatch):
    store, engine, root, sid = setup_report(tmp_path)
    store.update(sid, status="paused")
    before = store.get_scan(sid)
    root.rename(tmp_path / "offline")
    monkeypatch.setattr(engine, "inventory", lambda *_: pytest.fail("Inventory must not run for a report"))
    monkeypatch.setattr(
        engine, "investigate", lambda *_: pytest.fail("Source content must not be read again")
    )
    engine.start(sid, report_only=True)
    wait(engine)
    after = store.get_scan(sid)
    assert after["status"] == "paused" and after["phase"] == before["phase"]
    assert after["read_bytes"] == before["read_bytes"] and after["llm_calls"] == before["llm_calls"]
    assert store.report(sid)["state"] == "ready"


def test_report_failure_keeps_catalog_summary_and_does_not_pause_scan(tmp_path):
    class Broken(ReportModel):
        def summarize_report(self, *args, **kwargs):
            raise ValueError("Model unavailable")

    store, _, _, sid = setup_report(tmp_path, model=Broken)
    report = store.report(sid)
    assert report["state"] == "ready" and report["data"]["synthesis"] == "catalog"
    assert "Model unavailable" in report["data"]["ai_error"]
    assert "13 files" in report["data"]["summary"]


def test_report_request_cannot_replace_an_active_or_queued_job(tmp_path):
    store = Store(tmp_path / "catalog/db")
    root = create_demo(tmp_path / "source")
    engine = Engine(store, ReportModel)
    engine.hold_queue(True)
    sid = engine.create(root, "Queued", DEFAULTS)
    with pytest.raises(ValueError):
        engine.start(sid, report_only=True)
    assert store.get_scan(sid)["queue_task"] == "investigate"


def test_structural_detail_report_does_not_call_model(tmp_path):
    class NoModel:
        def __init__(self, *_):
            pytest.fail("AI was disabled")

    store, _, _, sid = setup_report(
        tmp_path, model=NoModel, investigation_mode="detail", detail_use_llm=False
    )
    assert store.report(sid)["data"]["synthesis"] == "catalog"
    assert store.report(sid)["data"]["stats"]["detail"]["processed"] == 13


def test_report_preview_and_many_areas_are_bounded_and_accounted_for(tmp_path):
    store, engine, root, sid = setup_report(tmp_path, max_folders=1)
    for i in range(20):
        folder = root / f"area-{i}"
        folder.mkdir()
        (folder / "note.txt").write_text("A note")
    new = engine.create(root, "Many areas", {**DEFAULTS, "max_folders": 1})
    wait(engine)
    data = catalog_report(store, new)
    assert len(data["storage"]) == 13
    assert data["storage"][-1]["rel"] == "Other top-level folders"
    assert sum(r["files"] for r in data["storage"]) == 33
    assert sum(r["bytes"] for r in data["storage"]) == data["stats"]["bytes"]
    assert data["stats"]["analyzed_folders"] < data["stats"]["findings"]


def test_report_model_rejects_unknown_references_and_bad_next_steps():
    result = {
        "summary": "Archive summary",
        "reason": "Known findings",
        "priority": 2,
        "file_ids": [99],
        "next_steps": ["Review the source."],
    }
    client = Ollama(DEFAULTS)
    client.request = lambda *a, **kw: {"message": {"content": json.dumps(result)}}
    with pytest.raises(ValueError):
        client.summarize_report({"samples": [{"id": 1}]})
    result["file_ids"] = [1]
    result["next_steps"] = "Invalid"
    with pytest.raises(ValueError):
        client.summarize_report({"samples": [{"id": 1}]})


def test_low_priority_clutter_is_not_in_the_short_reading_list(tmp_path):
    store, _, _, sid = setup_report(tmp_path)
    with store.connect() as db:
        db.execute("UPDATE findings SET priority=3 WHERE scan_id=?", (sid,))
    report = catalog_report(store, sid)
    assert not report["leads"]
    assert report["stats"]["files"] == 13  # aggregate coverage still includes clutter


@pytest.mark.parametrize(
    "claim,valid",
    [
        ("It contains 999 files.", False),
        ("The notes describe a project from 2019.", True),
        ("Review the Photos 2018 area and example 1.", True),
    ],
)
def test_report_numerical_claims_must_come_from_assessed_evidence(claim, valid):
    result = {
        "summary": claim,
        "reason": "Based on supplied notes.",
        "priority": 2,
        "file_ids": [1],
        "next_steps": ["Review the notes."],
    }
    client = Ollama(DEFAULTS)
    client.request = lambda *a, **kw: {"message": {"content": json.dumps(result)}}
    evidence = {
        "samples": [{"id": 1, "rel": "notes.txt", "text": "A project from 2019."}],
        "areas": [{"folder": "Photos 2018"}],
    }
    if valid:
        assert client.summarize_report(evidence)["summary"] == claim
    else:
        with pytest.raises(ValueError, match="numerical"):
            client.summarize_report(evidence)
