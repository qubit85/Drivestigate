"""Structure decisions use synthetic layouts, never personal drive content."""

from pathlib import Path

import pytest
from test_engine import FakeOllama, wait
from test_investigations import DetailModel

from drivestigate.config import DEFAULTS as NEW_DEFAULTS
from drivestigate.engine import Engine
from drivestigate.sizetree import size_tree
from drivestigate.store import Store
from drivestigate.structure import role
from drivestigate.summary import catalog_report, render_report

# Legacy-engine regression fixtures; progressive behavior has dedicated tests.
DEFAULTS = {**NEW_DEFAULTS, "investigation_mode": "overview", "search_focus": ""}


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "source"
    files = {
        "Windows/System32/kernel.dll": "Synthetic program bytes",
        "Windows/Web/Wallpaper/default.jpg": "Bundled wallpaper",
        "Program Files/OldWriter/bin/writer.exe": "Synthetic executable",
        "Program Files/OldWriter/resources/logo.png": "Bundled icon",
        "Program Files/OldWriter/docs/manual.pdf": "Bundled manual",
        "Program Files/OldWriter/User Data/notes.txt": "Personal journal",
        "Program Files/OldGame/saves/slot.sav": "Saved game",
        "Windows.old/Users/Alice/Desktop/tax-record.txt": "Personal tax note",
        "Windows.old/Users/Alice/Documents/thesis.txt": "Personal thesis",
        "Users/Alice/AppData/Roaming/Editor/settings.ini": "Personal editor settings",
        "Users/Alice/AppData/Local/Editor/Cache/generated.js": "Generated cached code",
        "Projects/Sensor/node_modules/vendor/index.js": "Bundled dependency",
        "Projects/Sensor/main.py": "Personal prototype",
        "Mystery/notes.txt": "Unknown personal note",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def test_overview_prioritizes_user_locations_and_groups_installation_assets(tmp_path, layout):
    class Model(FakeOllama):
        calls = []

    store, engine = Store(tmp_path / "catalog/db"), None
    engine = Engine(store, Model)
    sid = engine.create(layout, "Structure", {**DEFAULTS, "samples_per_folder": 1})
    wait(engine)
    assert store.get_scan(sid)["status"] == "completed", store.get_scan(sid)["message"]
    sampled = [s["rel"] for call in Model.calls for s in call["samples"]]
    assert sampled[0].startswith(
        (
            "Windows.old/Users/",
            "Program Files/OldGame/saves/",
            "Program Files/OldWriter/User Data/",
            "Projects/Sensor/",
        )
    )
    assert not any(
        "resources/logo" in p
        or "docs/manual" in p
        or "kernel.dll" in p
        or "node_modules" in p
        or "/Cache/" in p
        for p in sampled
    )
    assert any("User Data/notes" in p for p in sampled)
    assert any("Desktop/tax" in p for p in sampled)
    assert any("Roaming/Editor/settings" in p for p in sampled)
    plan = store.structure(sid)
    assert {"OldWriter", "OldGame", "Windows"} <= {g["label"] for g in plan["groups"]}
    app = next(g for g in plan["groups"] if g["label"] == "OldWriter")
    assert app["technical_files"] == 3 and app["user_files"] == 1
    report = render_report(catalog_report(store, sid), "http://127.0.0.1:8741")
    assert "Operating systems and application footprints" in report and "OldWriter" in report
    assert "Personal data · look here first" in report


def test_detail_default_does_not_read_or_ai_assess_grouped_files(tmp_path, layout, monkeypatch):
    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, DetailModel)
    original = engine.sample
    read = []

    def sample(scan, file, remaining):
        assert not file["technical"]
        read.append(file["rel"])
        return original(scan, file, remaining)

    monkeypatch.setattr(engine, "sample", sample)
    sid = engine.create(layout, "Personal first", {**DEFAULTS, "investigation_mode": "detail"})
    wait(engine)
    assert store.get_scan(sid)["status"] == "completed", store.get_scan(sid)["message"]
    stats = store.stats(sid)
    assert stats["detail"]["grouped"] == stats["technical_files"] == 7
    assert stats["detail"]["hashed"] == len(read) == stats["files"] - 7
    with store.connect() as db:
        assert not db.execute(
            "SELECT x.file_id FROM file_details x JOIN files f ON f.id=x.file_id WHERE f.scan_id=? AND f.technical=1 AND (x.sha256!='' OR x.excerpt!='' OR x.state='reviewed')",
            (sid,),
        ).fetchall()
    assert len(list((Path(store.get_scan(sid)["vault_path"]) / "Files").glob("*.md"))) == stats["files"]


def test_technical_only_scan_needs_no_model_or_content_reader(tmp_path):
    root = tmp_path / "source/Program Files/OldApp/resources"
    root.mkdir(parents=True)
    (root / "logo.png").write_text("Bundled image")

    class NoModel:
        def __init__(self, settings):
            raise AssertionError("Technical-only metadata grouping must not construct a model")

    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, NoModel)
    sid = engine.create(root.parent, "Only application", {**DEFAULTS, "investigation_mode": "detail"})
    wait(engine)
    assert store.get_scan(sid)["status"] == "completed", store.get_scan(sid)["message"]
    assert store.get_scan(sid)["read_bytes"] == store.get_scan(sid)["llm_calls"] == 0
    assert store.stats(sid)["detail"]["grouped"] == 1


def test_metadata_structure_refresh_works_offline_and_preserves_state(tmp_path, layout):
    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, FakeOllama)
    sid = engine.create(layout, "Refresh", DEFAULTS)
    wait(engine)
    before = store.get_scan(sid)
    layout.rename(tmp_path / "offline")
    engine.start(sid, structure_only=True)
    wait(engine)
    after = store.get_scan(sid)
    assert after["status"] == before["status"] and after["phase"] == before["phase"]
    assert after["read_bytes"] == before["read_bytes"] and after["llm_calls"] == before["llm_calls"]
    assert store.structure(sid)["version"] == 1
    assert store.report(sid)["data"]["structure"]["groups"]


def test_user_exceptions_and_ambiguous_windows_folder():
    assert role("Program Files/App/assets/logo.png", "logo.png")["technical"]
    assert not role("Program Files/App/User Data/journal.txt", "journal.txt")["technical"]
    assert not role("Program Files/App/saves/game.sav", "game.sav")["technical"]
    assert not role("Windows.old/Users/Alice/Documents/draft.txt", "draft.txt")["technical"]
    assert not role("Project/windows/resources/logo.png", "logo.png")["technical"]
    assert not role("Windows/System32/family.jpg", "family.jpg")["technical"]
    assert role("ProgramData/Editor/settings.ini", "settings.ini")["role"] == "application_data"


def test_size_tree_includes_files_folder_totals_paging_and_literal_paths(tmp_path):
    root = tmp_path / "source"
    (root / "folder%_ [x]/nested").mkdir(parents=True)
    (root / "folder%_ [x]/nested/large.txt").write_bytes(b"x" * 1234)
    for n in range(205):
        (root / f"file-{n:03}.bin").write_bytes(b"x" * n)
    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, FakeOllama)
    sid = engine.create(root, "Tree", {**DEFAULTS, "max_folders": 1})
    wait(engine)
    first = size_tree(store, sid)
    second = size_tree(store, sid, offset=200)
    assert first["total"] == 206 and len(first["items"]) == 200 and len(second["items"]) == 6
    entries = first["items"] + second["items"]
    assert len({(r["kind"], r["id"]) for r in entries}) == 206
    assert sum(r["bytes"] for r in entries) == first["root"]["tree_bytes"] == sum(range(205)) + 1234
    assert [r["bytes"] for r in entries] == sorted((r["bytes"] for r in entries), reverse=True)
    assert size_tree(store, sid, "folder%_ [x]")["root"]["tree_bytes"] == 1234
    assert size_tree(store, sid, sort="name")["items"][0]["name"] == "file-000.bin"
    with pytest.raises(ValueError):
        size_tree(store, sid, "../../etc")
    with pytest.raises(ValueError):
        size_tree(store, sid, sort="invalid")
