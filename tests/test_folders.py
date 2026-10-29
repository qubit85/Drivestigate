import json

import pytest
from test_engine import FakeOllama, wait

from drivestigate.config import DEFAULTS as NEW_DEFAULTS
from drivestigate.engine import Engine
from drivestigate.folders import folder_summary
from drivestigate.store import Store

# Legacy-engine regression fixtures; progressive behavior has dedicated tests.
DEFAULTS = {**NEW_DEFAULTS, "investigation_mode": "overview", "search_focus": ""}


@pytest.fixture
def folders(tmp_path):
    source = tmp_path / "source"
    for name in (
        "Documents/Notes",
        "Empty",
        "Secret",
        "docs%_[]",
        "docs%_[]/Child",
        "docs%_[]0",
    ):
        (source / name).mkdir(parents=True)
    (source / "Documents/notes.txt").write_text("Project notes")
    (source / "Documents/Notes/readme.md").write_text("Old project archive")
    (source / "docs%_[]/photo.jpg").write_bytes(b"not-an-image")
    (source / "docs%_[]/Child/document.txt").write_text("Keep this")
    (source / "docs%_[]0/outside.txt").write_text("Outside the requested folder")
    store = Store(tmp_path / "catalog/db")
    engine = Engine(store, FakeOllama)
    sid = engine.create(source, "Folders", {**DEFAULTS, "max_folders": 1, "max_llm_calls": 1})
    wait(engine)
    with store.connect() as db:
        db.execute("UPDATE findings SET analyzed=0 WHERE scan_id=?", (sid,))
        # Model a case-sensitive removable volume even when the test host's own
        # filesystem folds case (the default on macOS).
        lower_id = db.execute(
            "INSERT INTO directories(scan_id,rel,parent_rel,done) VALUES(?,'documents','.',1)", (sid,)
        ).lastrowid
        db.execute(
            "INSERT INTO files(scan_id,dir_id,rel,name,ext,size,mtime,category,technical,score) "
            "VALUES(?,?,'documents/other.txt','other.txt','.txt',20,0,'Text & data',0,40)",
            (sid, lower_id),
        )
        ids = {
            r["rel"]: r["id"] for r in db.execute("SELECT id,rel FROM directories WHERE scan_id=?", (sid,))
        }
    return store, engine, source, sid, ids


def test_every_folder_has_a_plain_metadata_summary_without_new_source_reads(folders):
    store, _, source, sid, ids = folders
    before = store.get_scan(sid)
    source.rename(source.with_name("offline"))
    for rel, directory_id in ids.items():
        data = folder_summary(store, sid, directory_id)
        assert data["rel"] == rel and data["summary"] and data["coverage"]
        assert data["basis"] == "Filenames and metadata only"
    assert folder_summary(store, sid, ids["Documents"])["inventory"]["files"] == 2
    root = folder_summary(store, sid, ids["."])
    assert root["inventory"]["files"] == 6
    assert "content" in root["coverage"].lower()
    after = store.get_scan(sid)
    assert before["read_bytes"] == after["read_bytes"] and before["llm_calls"] == after["llm_calls"]


def test_reviewed_direct_folder_reuses_assessment_and_labels_scope(folders):
    store, _, _, sid, ids = folders
    with store.connect() as db:
        db.execute(
            "UPDATE findings SET analyzed=1,method='sampled',summary=? WHERE dir_id=?",
            ("Project planning notes and an archive of source documentation.", ids["Documents"]),
        )
    data = folder_summary(store, sid, ids["Documents"])
    assert data["summary"].startswith("Project planning notes")
    assert data["basis"] == "Sampled content"
    assert "does not cover every file or all subfolders" in data["coverage"]
    parent = folder_summary(store, sid, ids["."])
    assert parent["basis"] == "Selected subfolder assessments"
    assert "Documents (Sampled subfolder content)" in parent["summary"]
    assert "not a full review of this parent folder" in parent["coverage"]


def test_metadata_only_ai_assessments_are_not_presented_as_content_reviews(folders):
    store, _, _, sid, ids = folders
    with store.connect() as db:
        db.execute(
            "UPDATE findings SET analyzed=1,method='metadata_ai',summary='Text documents by filename.' WHERE dir_id=?",
            (ids["Documents"],),
        )
    assert folder_summary(store, sid, ids["Documents"])["basis"] == "AI metadata assessment"
    assert "Subfolder metadata assessment" in folder_summary(store, sid, ids["."])["summary"]


def test_folder_scoping_is_literal_and_case_sensitive(folders):
    store, _, _, sid, ids = folders
    data = folder_summary(store, sid, ids["docs%_[]"])
    assert data["inventory"]["files"] == 2
    assert data["inventory"]["subfolders"] == 1
    assert "outside.txt" not in data["summary"]
    assert folder_summary(store, sid, ids["documents"])["inventory"]["files"] == 1


def test_unreadable_or_excluded_folder_is_not_claimed_to_be_empty(folders):
    store, _, _, sid, ids = folders
    assert "empty in the saved inventory" in folder_summary(store, sid, ids["Empty"])["summary"]
    store.event(sid, "unreadable", "Secret", "Permission denied")
    secret = folder_summary(store, sid, ids["Secret"])
    assert "prevent assuming this folder is empty" in secret["summary"]
    assert "1 path issues" in secret["coverage"]
    store.event(sid, "excluded", "Empty/.Trashes", "Excluded")
    assert "prevent assuming this folder is empty" in folder_summary(store, sid, ids["Empty"])["summary"]


def test_detail_folder_summary_uses_per_file_assessments_with_content_coverage(folders):
    store, _, _, sid, ids = folders
    with store.connect() as db:
        settings = {**store.get_scan(sid)["settings"], "investigation_mode": "detail"}
        db.execute("UPDATE scans SET settings=? WHERE id=?", (json.dumps(settings), sid))
        fid = db.execute("SELECT id FROM files WHERE dir_id=?", (ids["Documents"],)).fetchone()[0]
        db.execute(
            "INSERT INTO file_details(file_id,state,summary,content_status) VALUES(?,'reviewed','Project notes about a photo organizer.','excerpt')",
            (fid,),
        )
    data = folder_summary(store, sid, ids["Documents"])
    assert "photo organizer" in data["summary"]
    assert data["basis"] == "Per-file assessments"
    assert "(Per-file content assessment)" in data["summary"]
    assert "1 AI assessments" in data["coverage"]
    assert "1 readable content samples" in data["coverage"]


def test_directory_ids_are_scoped_to_the_investigation(folders):
    store, _, _, sid, ids = folders
    with pytest.raises(ValueError):
        folder_summary(store, sid, -1)
    with pytest.raises(ValueError):
        folder_summary(store, "unknown", ids["."])
